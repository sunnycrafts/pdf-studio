"""Document fidelity: match a field's formatting to the form's printed text.

Given a page and a blank field's rectangle, look at the nearby printed text
(usually the field's label) and infer the font family, size, colour and a
sensible alignment/intent so typed values look native to the original form.
"""
from __future__ import annotations

# base-14 family + bold/italic -> PyMuPDF font code
_VARIANTS = {
    ("helv", False, False): "helv", ("helv", True, False): "hebo",
    ("helv", False, True): "heit", ("helv", True, True): "hebi",
    ("tiro", False, False): "tiro", ("tiro", True, False): "tibo",
    ("tiro", False, True): "tiit", ("tiro", True, True): "tibi",
    ("cour", False, False): "cour", ("cour", True, False): "cobo",
    ("cour", False, True): "coit", ("cour", True, True): "cobi",
}
_BOLD_KEYS = {"hebo", "hebi", "tibo", "tibi", "cobo", "cobi"}
_ITALIC_KEYS = {"heit", "hebi", "tiit", "tibi", "coit", "cobi"}
_GLOBAL_FAMILY = {
    "helv": "helv", "hebo": "helv", "heit": "helv", "hebi": "helv",
    "tiro": "tiro", "tibo": "tiro", "tiit": "tiro", "tibi": "tiro",
    "cour": "cour", "cobo": "cour", "coit": "cour", "cobi": "cour",
}
QT_FAMILY = {"helv": "Arial", "tiro": "Times New Roman", "cour": "Courier New"}


def variant(family: str, bold: bool, italic: bool) -> str:
    return _VARIANTS.get((family, bool(bold), bool(italic)), "helv")


def resolve_font(field, global_font: str) -> str:
    """PyMuPDF font code for a field, honouring per-field font + B/I, else global."""
    if field.font:
        return variant(field.font, field.bold, field.italic)
    fam = _GLOBAL_FAMILY.get(global_font, "helv")
    return variant(fam, field.bold or global_font in _BOLD_KEYS,
                   field.italic or global_font in _ITALIC_KEYS)


def qt_family(field, global_font: str):
    fam = field.font or _GLOBAL_FAMILY.get(global_font, "helv")
    return QT_FAMILY.get(fam, "Arial")


def _family_from_span(flags: int, fontname: str) -> str:
    name = (fontname or "").lower()
    if (flags & 8) or "courier" in name or "mono" in name:
        return "cour"
    if (flags & 4) or "times" in name or "serif" in name or "georgia" in name or "roman" in name:
        return "tiro"
    return "helv"


def _intent_align(label: str):
    l = label.lower()
    if "ifsc" in l:
        return "ifsc", "left"
    if "email" in l or "e-mail" in l:
        return "email", "left"
    if any(k in l for k in ("phone", "mobile", "contact", "tel", "cell")):
        return "phone", "left"
    if any(k in l for k in ("account no", "account number", "a/c no", "acct", "account #")):
        return "account", "left"
    if "pan" in l:
        return "pan", "left"
    if any(k in l for k in ("amount", "amt", "total", "balance", "price", "sum",
                            "deposit", "rupees", "₹", "rs.", "rs ", "inr", "usd", "$")):
        return "amount", "right"
    if any(k in l for k in ("date", "dob", "d.o.b", "dd/mm", "dd / mm", "valid", "expiry", "issued")):
        return "date", "center"
    return "", "left"


def label_text(page, rect) -> str:
    """The printed text on the field's row (its label), lower-cased — for autofill
    matching and field-type guessing."""
    x0, y0, x1, y1 = rect
    try:
        d = page.get_text("dict")
    except Exception:
        return ""
    parts = []
    for b in d.get("blocks", []):
        for ln in b.get("lines", []):
            for s in ln.get("spans", []):
                t = s.get("text", "").strip()
                if not t:
                    continue
                sx0, sy0, sx1, sy1 = s["bbox"]
                if y0 - 4 <= (sy0 + sy1) / 2 <= y1 + 4 and sx0 <= x1:
                    parts.append((sx0, t))
    parts.sort()
    return " ".join(t for _, t in parts).lower()


def analyze(page, rect) -> dict:
    """Return {font, size, color, align, intent} inferred from nearby printed text.
    Returns {} if no text is found near the field."""
    x0, y0, x1, y1 = rect
    cy = (y0 + y1) / 2.0
    try:
        d = page.get_text("dict")
    except Exception:
        return {}
    spans = [s for b in d.get("blocks", []) for l in b.get("lines", [])
             for s in l.get("spans", []) if s.get("text", "").strip()]
    if not spans:
        return {}

    # Spans roughly on the field's row.
    band = [s for s in spans if y0 - 4 <= (s["bbox"][1] + s["bbox"][3]) / 2 <= y1 + 4]
    left = [s for s in band if s["bbox"][2] <= x0 + 2]      # labels to the left
    if left:
        ref = max(left, key=lambda s: s["bbox"][2])
    elif band:
        ref = min(band, key=lambda s: abs((s["bbox"][1] + s["bbox"][3]) / 2 - cy))
    else:
        ref = min(spans, key=lambda s: (abs((s["bbox"][1] + s["bbox"][3]) / 2 - cy),
                                        abs(s["bbox"][0] - x0)))

    flags = ref.get("flags", 0)
    fam = _family_from_span(flags, ref.get("font", ""))
    size = round(float(ref.get("size", 10)) or 10, 1)
    ci = int(ref.get("color", 0))
    color = ((ci >> 16 & 255) / 255.0, (ci >> 8 & 255) / 255.0, (ci & 255) / 255.0)
    label = " ".join(s["text"] for s in band)
    intent, align = _intent_align(label)
    # Values are typed regular even when the label is bold/italic.
    return {"font": fam, "size": size, "color": color, "align": align, "intent": intent}
