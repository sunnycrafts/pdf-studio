"""Editable document model: serialize/restore the fillable layer to/from JSON,
and embed it (plus a clean base PDF) inside a saved PDF so the document reopens
fully editable. Flattened/Signed exports omit the model -> non-editable.
"""
from __future__ import annotations

import base64
import json

from pdfcore import Field

MODEL_NAME = "pdfstudio_model.json"   # embedded JSON: the editable layer
BASE_NAME = "pdfstudio_base.pdf"      # embedded clean base for exact re-render
VERSION = 1


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode("ascii")


def _unb64(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def _field_to_dict(f: Field) -> dict:
    return {
        "name": f.name, "kind": f.kind, "rect": list(f.rect), "page": f.page,
        "value": f.value, "options": list(f.options), "source": f.source,
        "export": f.export, "cells": [list(c) for c in f.cells], "align": f.align,
        "font": f.font, "size": f.size, "bold": f.bold, "italic": f.italic,
        "underline": f.underline, "color": list(f.color) if f.color else None,
        "intent": f.intent,
    }


def _dict_to_field(d: dict, page: int) -> Field:
    col = d.get("color")
    return Field(
        name=d.get("name", ""), kind=d.get("kind", "text"),
        rect=tuple(d.get("rect", (0, 0, 0, 0))), page=d.get("page", page),
        value=d.get("value", ""), options=list(d.get("options", [])),
        source=d.get("source", "detected"), export=d.get("export", ""),
        cells=[tuple(c) for c in d.get("cells", [])], align=d.get("align", "left"),
        font=d.get("font"), size=d.get("size"), bold=d.get("bold", False),
        italic=d.get("italic", False), underline=d.get("underline", False),
        color=tuple(col) if col else None, intent=d.get("intent", ""),
    )


def _placement_to_dict(p: dict) -> dict:
    return {"rect": list(p["rect"]), "png": _b64(p["png"]), "rotation": p.get("rotation", 0)}


def _dict_to_placement(d: dict) -> dict:
    return {"rect": tuple(d["rect"]), "png": _unb64(d["png"]), "rotation": d.get("rotation", 0)}


def serialize(doc) -> bytes:
    """Build the JSON model from a PdfDocument's fields/signatures/images."""
    pages: dict[str, dict] = {}
    page_nums = (set(doc.fields) | set(doc.signatures) | set(getattr(doc, "images", {}))
                 | set(getattr(doc, "annotations", {})))
    for p in page_nums:
        pages[str(p)] = {
            "fields": [_field_to_dict(f) for f in doc.fields.get(p, [])],
            "signatures": [_placement_to_dict(s) for s in doc.signatures.get(p, [])],
            "images": [_placement_to_dict(im) for im in getattr(doc, "images", {}).get(p, [])],
            "annotations": [dict(a) for a in getattr(doc, "annotations", {}).get(p, [])],
        }
    ocr = {str(k): v for k, v in getattr(doc, "ocr_text", {}).items() if v}
    return json.dumps({"version": VERSION, "pages": pages, "ocr": ocr}).encode("utf-8")


def deserialize(doc, raw: bytes):
    """Populate a PdfDocument's fields/signatures/images from the JSON model."""
    data = json.loads(raw.decode("utf-8"))
    doc.fields, doc.signatures, doc.images, doc.annotations = {}, {}, {}, {}
    for k, pg in data.get("pages", {}).items():
        p = int(k)
        doc.fields[p] = [_dict_to_field(d, p) for d in pg.get("fields", [])]
        doc.signatures[p] = [_dict_to_placement(s) for s in pg.get("signatures", [])]
        doc.images[p] = [_dict_to_placement(im) for im in pg.get("images", [])]
        doc.annotations[p] = [dict(a) for a in pg.get("annotations", [])]
    doc.ocr_text = {int(k): v for k, v in data.get("ocr", {}).items()}


def snapshot(doc) -> dict:
    """Cheap in-memory snapshot of the editable layer (for undo/redo & autosave)."""
    return json.loads(serialize(doc).decode("utf-8"))


def restore(doc, snap: dict):
    deserialize(doc, json.dumps(snap).encode("utf-8"))


# -- embedding inside a fitz document --------------------------------------
def embed(work_doc, model_bytes: bytes, base_bytes: bytes | None = None):
    """Embed the JSON model, and the clean base PDF only when given. AcroForm-only
    edits don't need a base (values live in the widgets), keeping the file ~1x."""
    items = [(MODEL_NAME, model_bytes)]
    if base_bytes is not None:
        items.append((BASE_NAME, base_bytes))
    for name, data in items:
        try:
            if name in work_doc.embfile_names():
                work_doc.embfile_upd(name, data)
            else:
                work_doc.embfile_add(name, data)
        except Exception:
            work_doc.embfile_add(name, data)


def extract(opened_doc):
    """Return (model_bytes, base_bytes) if this is a PDF Studio document, else None."""
    try:
        names = opened_doc.embfile_names()
    except Exception:
        return None
    if MODEL_NAME not in names:
        return None
    model = bytes(opened_doc.embfile_get(MODEL_NAME))
    base = bytes(opened_doc.embfile_get(BASE_NAME)) if BASE_NAME in names else None
    return model, base
