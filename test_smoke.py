"""Headless smoke test: core PDF, detection, OCR, and GUI construction."""
import os, tempfile
import fitz

import pdfcore, formdetect, ocrengine

tmp = tempfile.mkdtemp(prefix="pdfstudio_")


def make_flat_form():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4
    page.insert_text((72, 90), "Registration Form", fontsize=18)
    labels = ["Full name", "Email address", "Phone number"]
    y = 140
    for lab in labels:
        page.insert_text((72, y - 4), lab + ":", fontsize=11)
        page.draw_line((180, y), (520, y), color=(0, 0, 0), width=0.8)  # writing line
        y += 50
    # two checkbox squares
    page.draw_rect(fitz.Rect(72, y, 86, y + 14), color=(0, 0, 0), width=1)
    page.insert_text((92, y + 11), "I agree to the terms", fontsize=11)
    page.draw_rect(fitz.Rect(72, y + 24, 86, y + 38), color=(0, 0, 0), width=1)
    page.insert_text((92, y + 35), "Subscribe to newsletter", fontsize=11)
    path = os.path.join(tmp, "flat.pdf")
    doc.save(path); doc.close()
    return path


def make_acro_form():
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((72, 90), "AcroForm Test", fontsize=18)
    w = fitz.Widget()
    w.field_name = "fullname"
    w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(150, 120, 400, 140)
    w.field_value = ""
    page.add_widget(w)
    cb = fitz.Widget()
    cb.field_name = "agree"
    cb.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
    cb.rect = fitz.Rect(150, 160, 166, 176)
    page.add_widget(cb)
    path = os.path.join(tmp, "acro.pdf")
    doc.save(path); doc.close()
    return path


print("== flat form ==")
flat = make_flat_form()
d = pdfcore.PdfDocument(flat)
print("has_form:", d.has_form, "pages:", d.page_count)
img = d.render_pil(0, 2.0)
detected = formdetect.detect_fields(img, 0, 2.0)
print("detected fields:", len(detected), [(f.kind, tuple(round(x) for x in f.rect)) for f in detected])
# fill detected and save
for f in detected:
    f.value = "X" if f.kind == "checkbox" else "Sample value"
out = os.path.join(tmp, "flat_filled.pdf")
d.save(out, detected)
print("saved:", out, "size", os.path.getsize(out))
d.close()

print("\n== acro form ==")
a = pdfcore.PdfDocument(make_acro_form())
print("has_form:", a.has_form)
fields = a.read_acro_fields(0)
print("acro fields:", [(f.name, f.kind) for f in fields])
for f in fields:
    f.value = "Jane Doe" if f.kind == "text" else "on"
outa = os.path.join(tmp, "acro_filled.pdf")
a.save(outa, fields)
# reopen to verify value written
chk = fitz.open(outa)
vals = [(w.field_name, w.field_value) for w in chk[0].widgets()]
print("after save:", vals)
chk.close(); a.close()

print("\n== ocr ==")
print("available:", ocrengine.is_available("en"))
try:
    res = ocrengine.recognize(pdfcore.PdfDocument(flat).render_pil(0, 2.0))
    print("ocr text (first 120):", repr(res.text[:120]))
    print("word count:", len(res.words))
except ocrengine.OcrUnavailable as e:
    print("OCR unavailable:", e)

print("\n== gui construction (offscreen) ==")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
from qfluentwidgets import setTheme, Theme
app = QApplication([])
setTheme(Theme.AUTO)
from mainwindow import MainWindow
win = MainWindow()
win.show()
app.processEvents()
print("MainWindow constructed OK; title:", win.windowTitle())
print("\nALL OK. temp dir:", tmp)
