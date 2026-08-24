"""PDF core: load, render, enumerate/fill form fields, stamp text, save.

Built on PyMuPDF (fitz). All rectangles exposed to the UI are in PDF *points*
using a top-left origin (the same convention PyMuPDF uses), so the only thing
the viewer has to do is multiply by the render zoom to get pixel coordinates.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import fitz  # PyMuPDF

import logconf
log = logconf.get()


# Field "kinds" we expose to the UI. Everything an AcroForm can hold is mapped
# onto one of these, plus "text" for regions we detect on flat/scanned forms.
KIND_TEXT = "text"
KIND_MULTILINE = "multiline"
KIND_CHECKBOX = "checkbox"
KIND_RADIO = "radio"
KIND_CHOICE = "choice"
KIND_COMB = "comb"      # a row of per-character cells (one letter/digit per cell)


@dataclass
class Field:
    """A fillable region on a page."""
    name: str
    kind: str
    rect: tuple[float, float, float, float]  # (x0, y0, x1, y1) in PDF points
    page: int
    value: str = ""
    options: list[str] = field(default_factory=list)
    # How the field came to exist: an embedded AcroForm widget, or a region we
    # detected on a flat form. Determines how we write the value back on save.
    source: str = "acro"  # "acro" | "detected"
    # Group name for radio buttons (the export value lives in `export`).
    export: str = ""
    # For comb fields: the individual character cells (x0,y0,x1,y1) in points.
    cells: list = field(default_factory=list)
    # Per-field formatting (None = inherit the global default). Set by font
    # matching against the form's own printed text ("Match Original Form").
    align: str = "left"                 # "left" | "center" | "right"
    font: Optional[str] = None          # base-14 family key: helv | tiro | cour
    size: Optional[float] = None        # point size; None = auto-fit
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: Optional[tuple] = None       # (r,g,b) 0-1; None = global ink
    intent: str = ""                    # form-intelligence: email|phone|date|amount


class PdfDocument:
    def __init__(self, path: str):
        self.path = path
        # Open from bytes (not the file handle) so we never lock the file -> we
        # can save back onto it, and a saved PDF-Studio doc can be reopened from
        # its embedded clean base.
        with open(path, "rb") as fh:
            self.doc = fitz.open(stream=fh.read(), filetype="pdf")
        # Many bank PDFs are encrypted only for permissions; an empty password
        # unlocks them. If a real password is required, flag it for the caller.
        self.locked = False
        if self.doc.needs_pass:
            if not self.doc.authenticate(""):
                self.locked = True
        # The editable layer (source of truth), keyed by page index.
        self.fields: dict[int, list[Field]] = {}        # Field objects
        self.signatures: dict[int, list[dict]] = {}      # {"rect","png","rotation"}
        self.images: dict[int, list[dict]] = {}          # {"rect","png","rotation"}
        # Markup annotations: {"type","color","width","rect"|"points","fill"}
        self.annotations: dict[int, list[dict]] = {}
        self.ocr_text: dict[int, str] = {}               # page -> recognised text (cached)
        self.is_studio = False                            # reopened editable doc?
        if not self.locked:
            self._load_studio()

    def unlock(self, password: str) -> bool:
        """Try a user-supplied password. Returns True if the document opens."""
        ok = bool(self.doc.authenticate(password))
        self.locked = not ok
        if ok:
            self._load_studio()
        return ok

    def _load_studio(self):
        """If this file carries an embedded PDF Studio model, restore the
        editable layer and use the embedded clean base as the background."""
        import docmodel
        res = docmodel.extract(self.doc)
        if not res:
            return
        model, base = res
        if base:
            try:
                self.doc.close()
            except Exception:
                pass
            self.doc = fitz.open(stream=base, filetype="pdf")
        try:
            docmodel.deserialize(self, model)
            self.is_studio = True
        except Exception:
            log.exception("failed to load embedded studio model")

    # -- basic info ---------------------------------------------------------
    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def has_form(self) -> bool:
        return bool(self.doc.is_form_pdf)

    def page_size(self, index: int) -> tuple[float, float]:
        r = self.doc[index].rect
        return (r.width, r.height)

    # -- rendering ----------------------------------------------------------
    def render(self, index: int, zoom: float = 2.0):
        """Render a page for display. Widget/annotation appearances are NOT baked
        in (annots=False) so our editable overlays are the single source of truth
        — otherwise a pre-filled AcroForm would show its value twice."""
        page = self.doc[index]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=False)
        return pix.samples, pix.width, pix.height, pix.stride

    def render_pil(self, index: int, zoom: float = 2.0):
        """Render a page to a PIL.Image (used by OCR / detection)."""
        from PIL import Image
        page = self.doc[index]
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

    # -- form fields --------------------------------------------------------
    def read_acro_fields(self, index: int) -> list[Field]:
        """Read embedded AcroForm widgets on a page into Field objects."""
        page = self.doc[index]
        out: list[Field] = []
        for w in page.widgets() or []:
            kind = _map_widget_kind(w)
            if kind is None:
                continue  # push buttons, signatures: nothing to type into
            r = w.rect
            options = list(w.choice_values or []) if hasattr(w, "choice_values") else []
            f = Field(
                name=w.field_name or f"field_{len(out)+1}",
                kind=kind,
                rect=(r.x0, r.y0, r.x1, r.y1),
                page=index,
                value=str(w.field_value) if w.field_value not in (None, "") else "",
                options=[str(o) for o in options] if options else [],
                source="acro",
            )
            out.append(f)
        return out

    # -- saving -------------------------------------------------------------
    def save_document(self, out_path: str, mode: str = "save",
                      ink: tuple[float, float, float] = (0, 0, 0),
                      font: str = "helv", font_size: float | None = None):
        """Render the editable layer onto a clean copy of the base and save.

        mode="save"    -> also embed the model + clean base so the file reopens
                          fully editable (non-destructive).
        mode="flatten" -> burn everything in, no embedded model (not editable).
        mode="sign"    -> like flatten; intended for submission.
        """
        import docmodel
        base_bytes = self.doc.tobytes()
        work = fitz.open(stream=base_bytes, filetype="pdf")
        try:
            try:
                self._render_onto(work, ink, font, font_size)
            except Exception:
                # Never let one bad object abort the whole export; save what
                # rendered and keep the traceback for diagnosis.
                log.exception("render_onto failed mid-way; saving remainder")
            if mode in ("flatten", "sign"):
                if self.has_form:
                    try:
                        work.bake()      # flatten AcroForm widgets into content
                    except Exception:
                        log.exception("bake failed")
            else:                        # editable save
                # The base is only needed to re-hide *baked* content on reopen
                # (stamped text / images / signatures / annotations). AcroForm-only
                # edits keep their values in the widgets -> no base -> ~1x file size.
                needs_base = (
                    any(f.source == "detected" and f.value.strip()
                        for fl in self.fields.values() for f in fl)
                    or any(self.images.values())
                    or any(self.signatures.values())
                    or any(self.annotations.values())
                )
                docmodel.embed(work, docmodel.serialize(self), base_bytes if needs_base else None)
            try:
                work.save(out_path, deflate=True, garbage=3)
            except Exception:
                # Some real-world PDFs choke on garbage collection / deflate;
                # fall back to a plain save so export never hard-fails.
                log.exception("save with gc failed, retrying plain save")
                work.save(out_path)
            log.info("saved (%s, base=%s) -> %s", mode,
                     locals().get("needs_base", False), out_path)
        finally:
            work.close()

    def _render_onto(self, work, ink, font, font_size):
        """Stamp fields, signatures and images of every page onto `work`."""
        for page_idx, flds in self.fields.items():
            page = work[page_idx]
            acro = {f.name: f for f in flds if f.source == "acro"}
            if acro:
                for w in page.widgets() or []:
                    f = acro.get(w.field_name)
                    if f is not None:
                        _apply_value(w, f, ink, font, font_size)
            for f in flds:
                if f.source != "detected" or not f.value.strip():
                    continue
                try:
                    _stamp_field(page, f, ink, font, font_size)
                except Exception:
                    log.exception("failed to stamp field %s (%s)", f.name, f.kind)
        for page_idx, placements in self.signatures.items():
            for pl in placements:
                _insert_image(work, page_idx, pl)
        for page_idx, placements in self.images.items():
            for pl in placements:
                _insert_image(work, page_idx, pl)
        for page_idx, anns in self.annotations.items():
            for a in anns:
                try:
                    _stamp_annotation(work[page_idx], a)
                except Exception:
                    log.exception("failed to stamp annotation on page %s", page_idx)

    # -- page operations ----------------------------------------------------
    def _remap(self, mapping: dict):
        """Rebuild the per-page model after a page reorder. `mapping` maps
        old page index -> new index (omit deleted pages)."""
        def remap(d):
            nd = {}
            for old, items in d.items():
                if old in mapping:
                    nd[mapping[old]] = items
            return nd
        self.fields = remap(self.fields)
        for new in self.fields:
            for f in self.fields[new]:
                f.page = new
        self.signatures = remap(self.signatures)
        self.images = remap(self.images)
        self.annotations = remap(self.annotations)

    def rotate_page(self, idx: int, deg: int = 90):
        page = self.doc[idx]
        page.set_rotation((page.rotation + deg) % 360)

    def delete_page(self, idx: int):
        n = self.page_count
        if n <= 1:
            return
        self.doc.delete_page(idx)
        self._remap({p: (p if p < idx else p - 1) for p in range(n) if p != idx})

    def insert_blank(self, idx: int):
        n = self.page_count
        ref = self.doc[min(idx, n - 1)].rect
        self.doc.insert_page(idx, width=ref.width, height=ref.height)
        self._remap({p: (p if p < idx else p + 1) for p in range(n)})

    def move_page(self, src: int, dst: int):
        n = self.page_count
        if src == dst or not (0 <= dst < n):
            return
        order = list(range(n))
        order.insert(dst, order.pop(src))
        self.doc.select(order)
        self._remap({old: new for new, old in enumerate(order)})

    def extract_pages(self, indices: list[int], out_path: str):
        sub = fitz.open()
        for i in indices:
            sub.insert_pdf(self.doc, from_page=i, to_page=i)
        sub.save(out_path); sub.close()

    def merge_pdf(self, other_path: str):
        with open(other_path, "rb") as fh:
            other = fitz.open(stream=fh.read(), filetype="pdf")
        self.doc.insert_pdf(other)
        other.close()

    # -- search -------------------------------------------------------------
    def search(self, query: str, ocr_fallback: bool = True) -> list[tuple]:
        """Return [(page_index, (x0,y0,x1,y1) in points), ...] for `query`."""
        if not query.strip():
            return []
        hits = []
        for pno in range(self.page_count):
            page = self.doc[pno]
            rects = list(page.search_for(query) or [])
            if not rects and ocr_fallback and not page.get_text().strip():
                try:
                    import ocrengine
                    res = ocrengine.recognize(self.render_pil(pno, 2.0), enhance_scan=True)
                    for w in res.words:
                        if query.lower() in w.text.lower():
                            x0, y0, x1, y1 = w.rect
                            rects.append(fitz.Rect(x0 / 2, y0 / 2, x1 / 2, y1 / 2))
                except Exception:
                    pass
            hits.extend((pno, (r.x0, r.y0, r.x1, r.y1)) for r in rects)
        return hits

    def close(self):
        try:
            self.doc.close()
        except Exception:
            pass


def _map_widget_kind(w) -> Optional[str]:
    s = (getattr(w, "field_type_string", "") or "").lower()
    if "text" in s:
        # Detect multiline text fields via field flags if available.
        try:
            if getattr(w, "text_format", 0) or getattr(w, "is_multiline", False):
                return KIND_MULTILINE
        except Exception:
            pass
        return KIND_TEXT
    if "checkbox" in s:
        return KIND_CHECKBOX
    if "radio" in s:
        return KIND_RADIO
    if "combobox" in s or "listbox" in s:
        return KIND_CHOICE
    return None  # pushbutton / signature / unknown


def _apply_value(w, f: Field, ink: tuple[float, float, float] = (0, 0, 0),
                 font: str = "helv", font_size: float | None = None):
    try:
        if f.kind == KIND_CHECKBOX:
            on = f.value.strip().lower() in ("1", "true", "yes", "on", "x", "checked")
            w.field_value = on
        else:
            w.field_value = f.value
            for attr, val in (("text_color", ink), ("text_font", font),
                              ("text_fontsize", font_size or 0)):  # 0 = auto-size
                try:
                    setattr(w, attr, val)
                except Exception:
                    pass
        w.update()
    except Exception:
        pass


_ALIGN = {"left": 0, "center": 1, "right": 2}      # fitz.TEXT_ALIGN_*


def _stamp_field(page, f: Field, ink, font, font_size):
    """Stamp a field honouring its own font/size/colour/style, shrinking text to
    fit the box (then wrapping for multiline, then clipping) — fields never grow."""
    import fontmatch
    x0, y0, x1, y1 = f.rect
    align = _ALIGN.get(getattr(f, "align", "left"), 0)
    if f.kind == KIND_CHECKBOX:
        side = min(x1 - x0, y1 - y0)
        page.insert_text((x0 + (x1 - x0) * 0.18, y1 - (y1 - y0) * 0.2),
                         "X", fontsize=side * 0.95, fontname="helv", color=ink)
        return

    fontname = fontmatch.resolve_font(f, font)
    color = f.color if f.color is not None else ink

    if f.kind == KIND_COMB:
        for ch, (cx0, cy0, cx1, cy1) in zip(f.value, f.cells):
            if not ch.strip():
                continue
            fs = f.size or font_size or min((cy1 - cy0) * 0.72, (cx1 - cx0) * 1.1)
            cw = fs * 0.5
            page.insert_text((cx0 + max(0, (cx1 - cx0 - cw) / 2), cy1 - (cy1 - cy0) * 0.22),
                             ch, fontsize=fs, fontname=fontname, color=color)
        return

    if f.kind == KIND_MULTILINE:
        rect = fitz.Rect(x0 + 1, y0 + 1, x1 - 1, y1 - 1)
        fs = f.size or font_size or 11
        for _ in range(8):                    # shrink until it fits the box, then clip
            leftover = page.insert_textbox(rect, f.value, fontsize=fs, fontname=fontname,
                                           color=color, align=align)
            if leftover >= 0 or fs <= 6:
                break
            fs -= 1
        return

    # single-line: shrink the font until the value fits the width (Stage 1)…
    fs = f.size or font_size or max(8.0, min(12.0, (y1 - y0) * 0.85))
    maxw = max(4.0, (x1 - x0) - 4)
    try:
        while fs > 6 and fitz.get_text_length(f.value, fontname=fontname, fontsize=fs) > maxw:
            fs -= 0.5
        tw = fitz.get_text_length(f.value, fontname=fontname, fontsize=fs)
    except Exception:
        tw = 0
    if tw and tw > maxw:
        # …still too long at min size -> clip inside the box (Stage 3), never overflow
        page.insert_textbox(fitz.Rect(x0 + 1, y0, x1 - 1, y1), f.value,
                            fontsize=fs, fontname=fontname, color=color, align=align)
        return
    x = x0 + 2
    if align == 1:
        x = x0 + max(2, ((x1 - x0) - tw) / 2)
    elif align == 2:
        x = x0 + max(2, (x1 - x0) - tw - 2)
    page.insert_text((x, y1 - 3), f.value, fontsize=fs, fontname=fontname, color=color)
    if f.underline and tw:
        page.draw_line((x, y1 - 1.5), (x + tw, y1 - 1.5), color=color, width=max(0.5, fs * 0.06))


def rotate_png(png: bytes, deg: int) -> bytes:
    if not deg:
        return png
    from io import BytesIO
    from PIL import Image
    im = Image.open(BytesIO(png)).convert("RGBA").rotate(-deg, expand=True)
    buf = BytesIO(); im.save(buf, "PNG"); return buf.getvalue()


def _insert_image(work, page_idx, pl):
    try:
        png = rotate_png(pl["png"], int(pl.get("rotation", 0)))
        work[page_idx].insert_image(fitz.Rect(*pl["rect"]), stream=png,
                                    keep_proportion=True, overlay=True)
    except Exception:
        log.exception("failed to stamp image on page %s", page_idx)


def _stamp_annotation(page, a: dict):
    """Render a markup annotation onto the page content."""
    color = tuple(a.get("color", (0.85, 0.1, 0.1)))
    width = float(a.get("width", 1.5))
    typ = a.get("type")
    if typ == "pen":
        pts = [tuple(p) for p in a.get("points", [])]
        if len(pts) >= 2:
            page.draw_polyline(pts, color=color, width=width)
    elif typ in ("rect", "ellipse", "highlight"):
        r = fitz.Rect(*a["rect"])
        if typ == "rect":
            page.draw_rect(r, color=color, width=width)
        elif typ == "ellipse":
            page.draw_oval(r, color=color, width=width)
        else:  # highlight: translucent yellow-ish fill
            page.draw_rect(r, color=None, fill=color, fill_opacity=0.35)
    elif typ in ("line", "arrow"):
        p0 = tuple(a["rect"][:2]); p1 = tuple(a["rect"][2:])
        page.draw_line(p0, p1, color=color, width=width)
        if typ == "arrow":
            _arrow_head(page, p0, p1, color, width)


def _arrow_head(page, p0, p1, color, width):
    import math
    ang = math.atan2(p1[1] - p0[1], p1[0] - p0[0])
    size = max(6.0, width * 4)
    for d in (math.pi - 0.5, math.pi + 0.5):
        x = p1[0] + size * math.cos(ang + d)
        y = p1[1] + size * math.sin(ang + d)
        page.draw_line(p1, (x, y), color=color, width=width)
