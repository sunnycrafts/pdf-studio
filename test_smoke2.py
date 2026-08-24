"""Headless test for the new features: box detection, multiline + signature +
ink save, signature export, and GUI construction."""
import os, tempfile
import fitz
import pdfcore, formdetect, ocrengine

tmp = tempfile.mkdtemp(prefix="pdfstudio2_")

# --- build a form with a box, writing lines, and checkboxes ----------------
doc = fitz.open()
page = doc.new_page(width=595, height=842)
page.insert_text((72, 80), "Service Request", fontsize=18)
for lab, y in [("Name", 140), ("Account no", 185)]:
    page.insert_text((72, y - 4), lab + ":", fontsize=11)
    page.draw_line((180, y), (520, y), color=(0, 0, 0), width=0.8)
# a big comment box (should become a multiline field; its edges must NOT
# also be detected as separate writing lines)
page.draw_rect(fitz.Rect(72, 230, 520, 360), color=(0, 0, 0), width=1.2)
page.insert_text((72, 222), "Comments:", fontsize=11)
# checkbox
page.draw_rect(fitz.Rect(72, 390, 88, 406), color=(0, 0, 0), width=1.2)
page.insert_text((96, 403), "Urgent", fontsize=11)
flat = os.path.join(tmp, "form.pdf")
doc.save(flat); doc.close()

d = pdfcore.PdfDocument(flat)
fields = formdetect.detect_fields(d.render_pil(0, 2.0), 0, 2.0)
kinds = sorted(f.kind for f in fields)
print("detected kinds:", kinds)
boxes = [f for f in fields if f.kind == "multiline"]
lines = [f for f in fields if f.kind == "text"]
checks = [f for f in fields if f.kind == "checkbox"]
print(f"boxes(multiline)={len(boxes)} lines={len(lines)} checkboxes={len(checks)}")
assert len(boxes) >= 1, "expected a comment box (multiline)"
assert len(lines) == 2, f"expected exactly 2 writing lines, got {len(lines)} (box edges leaking?)"
assert len(checks) >= 1, "expected a checkbox"

# --- fill (incl multiline) + a signature, save with BLUE ink ---------------
for f in fields:
    if f.kind == "multiline":
        f.value = "Please expedite. Repeated outages this week."
    elif f.kind == "text":
        f.value = "Sunny Patel"
    elif f.kind == "checkbox":
        f.value = "on"

# fabricate a tiny signature PNG via QImage (offscreen Qt)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication
app = QApplication([])
from signature import SignatureCanvas
from PySide6.QtGui import QColor
from PySide6.QtCore import QPoint
canvas = SignatureCanvas(QColor.fromRgbF(0.84, 0.07, 0.07))
canvas._strokes = [[QPoint(20, 120), QPoint(60, 60), QPoint(120, 150), QPoint(200, 70)]]
sig_png = canvas.to_png()
assert sig_png and sig_png[:8] == b"\x89PNG\r\n\x1a\n", "signature PNG not produced"
print("signature png bytes:", len(sig_png))
d.signatures[0] = [{"rect": (350, 400, 520, 470), "png": sig_png}]

out = os.path.join(tmp, "filled.pdf")
d.save(out, fields, ink=(0.0, 0.32, 0.78))
d.close()

chk = fitz.open(out)
text = chk[0].get_text()
imgs = chk[0].get_images(full=True)
print("contains name:", "Sunny Patel" in text)
print("contains comment:", "expedite" in text)
print("contains X (checkbox):", "X" in text)
print("image count (signature):", len(imgs))
assert "Sunny Patel" in text and "expedite" in text and len(imgs) >= 1
chk.close()

# --- GUI construction with new actions -------------------------------------
from qfluentwidgets import setTheme, Theme
setTheme(Theme.AUTO)
from mainwindow import MainWindow, INK_COLORS
win = MainWindow()
win.show(); app.processEvents()
assert set(win.color_actions.keys()) == set(INK_COLORS.keys())
win.set_ink("Blue"); app.processEvents()
assert win.color_actions["Blue"].isChecked() and not win.color_actions["Black"].isChecked()
print("GUI OK; checked color:", win.ink_name)
print("\nALL OK ->", tmp)
