"""OCR via the Windows built-in engine (Windows.Media.Ocr) through `winocr`.

No external binary required: the recognizer ships with Windows 11. We return
both the plain text and per-word boxes (in *image pixel* coordinates of the
image we were handed) so callers can scale them however they like.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OcrWord:
    text: str
    rect: tuple[float, float, float, float]  # x0, y0, x1, y1 in image pixels


@dataclass
class OcrResult:
    text: str
    words: list[OcrWord]


class OcrUnavailable(RuntimeError):
    pass


def is_available(lang: str = "en") -> bool:
    try:
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.globalization import Language
        return bool(OcrEngine.is_language_supported(Language(lang)))
    except Exception:
        return False


def enhance(pil_image):
    """Clean up a scanned/photographed page before OCR: deskew small angles,
    boost contrast (CLAHE) and remove speckle. Helps crooked / low-contrast /
    faded scans; harmless on clean renders."""
    try:
        import cv2
        import numpy as np
        from PIL import Image
        g = np.array(pil_image.convert("L"))
        # deskew: estimate angle from the text mask, rotate if within ±15°
        thr = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        coords = np.column_stack(np.where(thr > 0))
        if len(coords) > 200:
            angle = cv2.minAreaRect(coords)[-1]
            if angle < -45:
                angle = 90 + angle
            if 0.4 < abs(angle) <= 15:
                h, w = g.shape
                m = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
                g = cv2.warpAffine(g, m, (w, h), flags=cv2.INTER_CUBIC,
                                   borderMode=cv2.BORDER_REPLICATE)
        g = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(g)
        g = cv2.medianBlur(g, 3)
        return Image.fromarray(cv2.cvtColor(g, cv2.COLOR_GRAY2RGB))
    except Exception:
        return pil_image


def _raw(pil_image, lang: str) -> OcrResult:
    import winocr
    if not is_available(lang):
        raise OcrUnavailable(
            f"Windows OCR language '{lang}' is not installed. Add it with:\n"
            f'  Add-WindowsCapability -Online -Name "Language.OCR~~~{lang}-US~0.0.1.0"'
        )
    try:
        result = winocr.recognize_pil_sync(pil_image, lang)
    except Exception as e:
        raise OcrUnavailable(f"OCR failed: {e}")
    text = result.get("text", "") if isinstance(result, dict) else ""
    words: list[OcrWord] = []
    for line in (result.get("lines", []) if isinstance(result, dict) else []):
        for w in line.get("words", []):
            br = w.get("bounding_rect", {}) or {}
            x = float(br.get("x", 0)); y = float(br.get("y", 0))
            ww = float(br.get("width", 0)); hh = float(br.get("height", 0))
            words.append(OcrWord(text=w.get("text", ""), rect=(x, y, x + ww, y + hh)))
    return OcrResult(text=text, words=words)


def _orient(pil_image, lang: str):
    """Pick the rotation (0/90/180/270) that reads as the most text — handles
    sideways/upside-down phone photos and scans (no Tesseract OSD needed)."""
    best_img, best_score = pil_image, -1
    for deg in (0, 90, 180, 270):
        im = pil_image if deg == 0 else pil_image.rotate(-deg, expand=True)
        try:
            score = sum(c.isalnum() for c in _raw(im, lang).text)
        except Exception:
            score = 0
        if score > best_score:
            best_img, best_score = im, score
    return best_img


def recognize(pil_image, lang: str = "en", enhance_scan: bool = False,
              auto_rotate: bool = False) -> OcrResult:
    """Run OCR on a PIL image. Raises OcrUnavailable if the engine/lang is missing.
    enhance_scan=True deskews/cleans scans; auto_rotate=True also fixes 90/180/270
    orientation (4x cost — use for explicit/first-page OCR, not the background queue)."""
    try:
        import winocr  # noqa: F401
    except Exception as e:  # pragma: no cover
        raise OcrUnavailable(f"winocr not importable: {e}")
    if enhance_scan:
        pil_image = enhance(pil_image)
    if auto_rotate:
        pil_image = _orient(pil_image, lang)
    return _raw(pil_image, lang)
