"""Run the real detection pipeline on sample forms and emit annotated previews.

Boxes = green, lines = blue, checkboxes = red. Saves PNGs to previews/ and
prints per-page counts so we can judge real-world quality.
"""
import os
import fitz
from PIL import Image, ImageDraw
import pdfcore, formdetect

OUT = os.path.join(os.path.dirname(__file__), "previews")
os.makedirs(OUT, exist_ok=True)
ZOOM = 2.0
COLORS = {"multiline": (20, 170, 60), "text": (30, 110, 230), "checkbox": (225, 40, 40),
          "comb": (160, 40, 220)}

PDFS = [
    r"D:\Downloads\RTGS-form.pdf",
    r"D:\Downloads\090626ET30628028.pdf",
    r"D:\Downloads\110626ET31091253.pdf",
    r"D:\Downloads\common_deposit_account_opening_form_effective_from_01.11.2023.pdf",
    r"D:\Downloads\Consent_Form_Final_2607.pdf",
    r"D:\Downloads\da1.pdf",
    r"D:\Downloads\da2.pdf",
    r"D:\Downloads\da3.pdf",
    r"D:\Downloads\decease_claim_set.pdf",
    r"D:\Downloads\doc1641557155.pdf",
    r"D:\Downloads\Link_PPF_LoanAC.pdf",
]


def annotate(pil, fields):
    img = pil.convert("RGB")
    d = ImageDraw.Draw(img)
    for f in fields:
        x0, y0, x1, y1 = (v * ZOOM for v in f.rect)
        d.rectangle([x0, y0, x1, y1], outline=COLORS.get(f.kind, (0, 0, 0)), width=3)
    return img


for path in PDFS:
    name = os.path.splitext(os.path.basename(path))[0][:40]
    try:
        doc = pdfcore.PdfDocument(path)
    except Exception as e:
        print(f"{name}: OPEN FAILED {e}")
        continue
    if doc.locked:
        print(f"{name[:34]:34} | LOCKED (needs password) — skipped")
        doc.close()
        continue
    n = doc.page_count
    has_acro = doc.has_form
    pages_to_check = min(n, 2)
    line = f"{name[:34]:34} | pages={n:<3} acroform={str(has_acro):5} |"
    for p in range(pages_to_check):
        try:
            pil = doc.render_pil(p, ZOOM)
            fields = formdetect.detect_fields(pil, p, ZOOM)
        except Exception as e:
            line += f" p{p+1}: ERR {e};"
            continue
        c = {"multiline": 0, "text": 0, "checkbox": 0, "comb": 0}
        for f in fields:
            c[f.kind] = c.get(f.kind, 0) + 1
        line += f" p{p+1}: box={c['multiline']} cell={c['text']} comb={c['comb']} chk={c['checkbox']} ;"
        annotate(pil, fields).save(os.path.join(OUT, f"{name}_p{p+1}.png"))
    print(line)
    doc.close()

print("\nPreviews in:", OUT)
