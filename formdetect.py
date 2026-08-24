"""Detect fillable regions on flat / scanned forms (no embedded AcroForm).

Cues:
  * Boxes / cells - bordered rectangles and the cells of a ruled table.
  * Comb fields   - a row of equal, tightly-spaced character cells (one box per
                    letter/digit, e.g. NAME or ACCOUNT NUMBER rows). Filled one
                    character per cell.
  * Check boxes   - isolated small near-square hollow outlines.
  * Writing lines - long thin horizontal strokes (ruled lines / underscores).

Everything is computed in image pixels and converted to PDF points by dividing
by the render zoom, so detected fields share the AcroForm coordinate space.
"""
from __future__ import annotations

import numpy as np

from pdfcore import (
    Field, KIND_TEXT, KIND_MULTILINE, KIND_CHECKBOX, KIND_COMB,
)


def detect_fields(pil_image, page_index: int, zoom: float) -> list[Field]:
    import cv2

    rgb = np.array(pil_image)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    binary = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 10
    )

    boxes = _detect_boxes(binary, w, h, zoom, page_index)

    squares = _detect_square_cells(binary)          # small square candidates (px)
    comb_runs, singles = _group_combs(squares)
    combs = [_comb_field(run, zoom, page_index) for run in comb_runs]
    checks = [_to_field(r, zoom, page_index, KIND_CHECKBOX) for r in singles]

    lines = _detect_lines(binary, w, h, zoom, page_index)
    lines = _filter_lines_on_boxes(lines, boxes + combs)

    fields = _dedupe(boxes + combs + checks + lines)
    for i, f in enumerate(fields, 1):
        f.name = f"{f.kind}_{i}"
    return fields


# --- bordered boxes & table cells -----------------------------------------
def _detect_boxes(binary, w, h, zoom, page_index) -> list[Field]:
    import cv2

    hk = max(25, w // 40)
    vk = max(18, h // 55)
    horiz = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (hk, 1)))
    vert = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, vk)))
    grid = cv2.dilate(cv2.bitwise_or(horiz, vert),
                      cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3)), iterations=1)

    contours, hierarchy = cv2.findContours(grid, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]

    page_area = float(w * h)
    out: list[Field] = []
    for i, c in enumerate(contours):
        if hierarchy[i][3] == -1:            # keep enclosed cells, not outer borders
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        if cw <= 48 and ch <= 48 and 0.7 <= cw / float(ch) <= 1.4:
            continue                          # small squares -> comb/checkbox path
        if cw < 45 or ch < 16:
            continue
        if cw > 0.97 * w or ch > 0.97 * h:
            continue
        if not (0.0005 * page_area < cw * ch < 0.6 * page_area):
            continue
        if cv2.contourArea(c) < 0.55 * cw * ch:
            continue
        kind = KIND_MULTILINE if (ch / zoom) >= 38 else KIND_TEXT
        out.append(_to_field((x + 3, y + 3, x + cw - 3, y + ch - 3), zoom, page_index, kind))
    return out


# --- small square cells (combs + checkboxes) ------------------------------
def _detect_square_cells(binary) -> list[tuple]:
    """Return pixel rects of small ~square cells (comb cells / checkboxes).

    These are the *interiors* of small boxes. In a comb the cells share borders,
    so the whole comb is one connected shape whose cells are interior holes; an
    isolated checkbox is also a border with an interior hole. So we collect small
    square holes (contours that have a parent), which catches both.
    """
    import cv2

    contours, hierarchy = cv2.findContours(binary, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if hierarchy is None:
        return []
    hierarchy = hierarchy[0]

    lo, hi = 9, 48
    out: list[tuple] = []
    for i, c in enumerate(contours):
        if hierarchy[i][3] == -1:                       # only holes (have a parent)
            continue
        x, y, cw, ch = cv2.boundingRect(c)
        if not (lo <= cw <= hi and lo <= ch <= hi):
            continue
        if not (0.55 <= cw / float(ch) <= 1.8):
            continue
        bbox = float(cw * ch)
        if cv2.contourArea(c) < 0.78 * bbox:            # square-ish hole (rejects round letters)
            continue
        peri = cv2.arcLength(c, True)
        approx = cv2.approxPolyDP(c, 0.06 * peri, True)
        if len(approx) != 4:
            continue
        out.append((x, y, x + cw, y + ch))
    return out


def _group_combs(squares: list[tuple]):
    """Split square cells into comb runs (>=4 equal, tightly-spaced cells in a
    row) and leftover singles. Returns (list_of_runs, list_of_single_rects)."""
    if not squares:
        return [], []
    cells = [dict(r=r, cx=(r[0] + r[2]) / 2, cy=(r[1] + r[3]) / 2,
                  w=r[2] - r[0], h=r[3] - r[1]) for r in squares]
    cells.sort(key=lambda c: (c["cy"], c["cx"]))

    # cluster into rows by vertical centre
    rows: list[list[dict]] = []
    for c in cells:
        placed = False
        for row in rows:
            if abs(c["cy"] - row[0]["cy"]) <= max(8, row[0]["h"] * 0.6):
                row.append(c)
                placed = True
                break
        if not placed:
            rows.append([c])

    runs: list[list[tuple]] = []
    singles: list[tuple] = []
    for row in rows:
        row.sort(key=lambda c: c["cx"])
        i = 0
        n = len(row)
        while i < n:
            run = [row[i]]
            j = i + 1
            while j < n:
                prev, cur = row[j - 1], row[j]
                same_w = abs(cur["w"] - prev["w"]) <= 0.35 * max(cur["w"], prev["w"])
                gap = cur["r"][0] - prev["r"][2]
                tight = -4 <= gap <= prev["w"] * 1.2
                if same_w and tight:
                    run.append(cur)
                    j += 1
                else:
                    break
            if len(run) >= 4:
                runs.append([c["r"] for c in run])
            else:
                for c in run:
                    if 0.8 <= c["w"] / float(c["h"]) <= 1.25:   # only square -> checkbox
                        singles.append(c["r"])
            i = j
    return runs, singles


def _comb_field(cells_px: list[tuple], zoom: float, page_index: int) -> Field:
    cells_pt = [tuple(v / zoom for v in c) for c in cells_px]
    x0 = min(c[0] for c in cells_pt); y0 = min(c[1] for c in cells_pt)
    x1 = max(c[2] for c in cells_pt); y1 = max(c[3] for c in cells_pt)
    f = Field(name="", kind=KIND_COMB, rect=(x0, y0, x1, y1),
              page=page_index, source="detected")
    f.cells = cells_pt
    return f


# --- writing lines ---------------------------------------------------------
def _detect_lines(binary, w, h, zoom, page_index) -> list[Field]:
    import cv2

    min_len = max(40, w // 25)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (min_len, 1))
    horiz = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    contours, _ = cv2.findContours(horiz, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    row_h = 22
    out: list[Field] = []
    for c in contours:
        x, y, cw, ch = cv2.boundingRect(c)
        if cw < min_len or ch > 8:
            continue
        top = max(0, y - row_h)
        out.append(_to_field((x + 1, top, x + cw - 1, y - 1), zoom, page_index, KIND_TEXT))
    return out


def _filter_lines_on_boxes(lines: list[Field], boxes: list[Field]) -> list[Field]:
    out: list[Field] = []
    for ln in lines:
        lx0, ly0, lx1, ly1 = ln.rect
        lcy = (ly0 + ly1) / 2.0
        lw = max(1.0, lx1 - lx0)
        drop = False
        for b in boxes:
            bx0, by0, bx1, by1 = b.rect
            if max(0.0, min(lx1, bx1) - max(lx0, bx0)) < 0.5 * lw:
                continue
            if abs(ly1 - by0) <= 6 or abs(ly1 - by1) <= 6 or (by0 - 4) <= lcy <= (by1 + 4):
                drop = True
                break
        if not drop:
            out.append(ln)
    return out


# --- helpers ---------------------------------------------------------------
def _to_field(rect_px, zoom, page_index, kind) -> Field:
    x0, y0, x1, y1 = rect_px
    return Field(name="", kind=kind, page=page_index, source="detected",
                 rect=(x0 / zoom, y0 / zoom, x1 / zoom, y1 / zoom))


def _dedupe(fields: list[Field], tol: float = 6.0) -> list[Field]:
    kept: list[Field] = []
    for f in fields:
        if any(_close(f.rect, k.rect, tol) for k in kept):
            continue
        kept.append(f)
    return kept


def _close(a, b, tol) -> bool:
    return all(abs(a[i] - b[i]) <= tol for i in range(4))
