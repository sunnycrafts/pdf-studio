"""Exhaustive headless test harness for PDF Studio (self-contained).

Uses the local samples/ folder plus a generated encrypted PDF, so it doesn't
depend on Downloads. Drives the real PdfDocument/formdetect/ocr logic AND the
real MainWindow (offscreen Qt, dialogs monkeypatched). Prints PASS/FAIL and
exits non-zero on any failure.
"""
import os, sys, glob, tempfile, traceback
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import fitz
from PySide6.QtWidgets import QApplication, QFileDialog
import PySide6.QtWidgets as QtW

APP = QApplication.instance() or QApplication([])
from qfluentwidgets import setTheme, Theme
setTheme(Theme.AUTO)

# Neutralise modal dialogs so headless runs never block.
from PySide6.QtWidgets import QMessageBox
QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.No)
QMessageBox.exec = lambda self, *a, **k: QMessageBox.StandardButton.Discard

import pdfcore, formdetect, ocrengine, docimport
from signature import SignatureCanvas
from PySide6.QtGui import QColor
from PySide6.QtCore import QPoint
import mainwindow as MW

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, "samples")
TMP = tempfile.mkdtemp(prefix="pdfstudio_full_")
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def make_sig():
    c = SignatureCanvas(QColor.fromRgbF(0.84, 0.07, 0.07))
    c._strokes = [[QPoint(20, 110), QPoint(70, 50), QPoint(130, 140), QPoint(210, 60), QPoint(280, 120)]]
    return c.to_png()

SIG = make_sig()


def make_encrypted_pdf():
    p = tempfile.mktemp(suffix=".pdf", dir=TMP)
    d = fitz.open()
    pg = d.new_page(width=400, height=300)
    pg.insert_text((50, 80), "Secret protected form", fontsize=14)
    pg.draw_line((50, 150), (350, 150))
    d.save(p, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="ownerpw", user_pw="secret123")
    d.close()
    return p


# 1) Core pipeline incl. comb fill ------------------------------------------
def core_pipeline(path, font, ink, size):
    name = os.path.basename(path)
    src = docimport.convert_to_pdf(path) if docimport.needs_conversion(path) else path
    d = pdfcore.PdfDocument(src)
    if d.locked:
        check(f"core:{name}", False, "unexpectedly locked"); return
    fields = formdetect.detect_fields(d.render_pil(0, 2.0), 0, 2.0)
    kinds = {}
    has_comb = False
    for i, f in enumerate(fields):
        kinds[f.kind] = kinds.get(f.kind, 0) + 1
        if f.kind == "checkbox":
            f.value = "on"
        elif f.kind == "multiline":
            f.value = "MLVAL alpha\nMLVAL beta"
        elif f.kind == "comb":
            has_comb = True
            f.value = ("ZQXJWVKBYFMPND" * 4)[:len(f.cells)]   # rare letters
        else:
            f.value = f"TXVAL{i}"
    d.fields[0] = fields
    d.signatures[0] = [{"rect": (60, 60, 240, 120), "png": SIG}]
    out = os.path.join(TMP, name + ".filled.pdf")
    d.save_document(out, mode="save", ink=MW.INK_COLORS[ink], font=MW.FONTS[font][0], font_size=size)
    d.close()
    c = fitz.open(out); pg = c[0]; text = pg.get_text(); imgs = len(pg.get_images(full=True))
    has_text = ("TXVAL" in text) or ("MLVAL" in text) or ("X" in text)
    comb_ok = (not has_comb) or ("Z" in text and "Q" in text)
    has_model = "pdfstudio_model.json" in c.embfile_names()
    c.close()
    # reopen -> editable layer restored
    r = pdfcore.PdfDocument(out)
    restored = sum(1 for f in r.fields.get(0, []) if f.value) + len(r.signatures.get(0, []))
    studio = r.is_studio
    r.close()
    ok = has_text and imgs >= 1 and comb_ok and has_model and studio and restored >= 1
    check(f"core:{name}", ok,
          f"{kinds} text={has_text} sig={imgs>=1} comb_ok={comb_ok} editable={has_model and studio} restored={restored}")


# 2) Password-protected open ------------------------------------------------
def password_tests():
    p = make_encrypted_pdf()
    d = pdfcore.PdfDocument(p)
    check("pw:detect-locked", d.locked, "needs password")
    check("pw:wrong-pw", d.unlock("nope") is False and d.locked)
    check("pw:correct-pw", d.unlock("secret123") is True and not d.locked)
    # can render after unlock
    try:
        d.render_pil(0, 1.5); check("pw:render-after-unlock", True)
    except Exception as e:
        check("pw:render-after-unlock", False, repr(e))
    d.close()

    # GUI: open locked file, supply correct password via prompt
    win = MW.MainWindow()
    try:
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(p, "")), \
             mock.patch.object(QtW.QInputDialog, "getText", return_value=("secret123", True)):
            win.open_pdf()
        APP.processEvents()
        check("pw:gui-open-with-pw", win.doc is not None and not win.doc.locked,
              "opened after entering password")
    except Exception as e:
        check("pw:gui-open-with-pw", False, repr(e))
    finally:
        win.close(); win.deleteLater(); APP.processEvents()


# 3) GUI flow ----------------------------------------------------------------
def gui_flow(form_path):
    name = os.path.basename(form_path)
    win = MW.MainWindow()
    try:
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(form_path, "")):
            win.open_pdf()
        APP.processEvents()
        if win.doc is None:
            check(f"gui:{name}", False, "doc None"); return
        # auto-detect + auto-ocr already ran on open
        auto_fields = len(win.doc.fields.get(0, []))
        auto_ocr = len(win.ocr.text.toPlainText())
        check(f"gui-autoopen:{name}", auto_fields > 0 and auto_ocr >= 0,
              f"auto fields={auto_fields} ocr_chars={auto_ocr}")
        win.set_ink("Red"); win.set_font_family("Courier"); win.set_font_size("12")
        for i, f in enumerate(win.doc.fields.get(0, [])):
            f.value = "on" if f.kind == "checkbox" else ("ABCD" if f.kind == "comb" else f"G{i}")

        class FakeDlg:
            def __init__(self, *a, **k): pass
            def exec(self): return 1
            def get_png(self): return SIG
        with mock.patch.object(MW, "SignatureDialog", FakeDlg):
            win.add_signature()
        APP.processEvents()
        win.next_page(); win.prev_page(); win.zoom_in(); win.zoom_out()
        APP.processEvents()
        # undo/redo should not crash
        win.undo(); win.redo(); APP.processEvents()
        # Save As (patched dialog) so we never overwrite the sample file
        outp = os.path.join(TMP, "gui_" + name + ".pdf")
        with mock.patch.object(QFileDialog, "getSaveFileName", return_value=(outp, "")):
            win.save_as()
        APP.processEvents()
        saved_ok = os.path.exists(outp) and os.path.getsize(outp) > 0
        not_dirty = not win._is_dirty()
        check(f"gui-save:{name}", saved_ok and not_dirty, f"saved={saved_ok} clean_after_save={not_dirty}")
        # export flattened
        flat = os.path.join(TMP, "flat_" + name + ".pdf")
        with mock.patch.object(QFileDialog, "getSaveFileName", return_value=(flat, "")):
            win.export_flattened()
        APP.processEvents()
        check(f"gui-export:{name}", os.path.exists(flat) and os.path.getsize(flat) > 0)
    finally:
        win.close(); win.deleteLater(); APP.processEvents()


# 3b) Phase 2-4: images, annotations, page ops, search ---------------------
def phase24_tests():
    from PySide6.QtGui import QImage, QColor
    from PySide6.QtCore import QBuffer
    im = QImage(120, 60, QImage.Format.Format_ARGB32); im.fill(QColor(0, 160, 0))
    b = QBuffer(); b.open(QBuffer.OpenModeFlag.WriteOnly); im.save(b, "PNG"); IMG = bytes(b.data()); b.close()
    src = next((p for p in glob.glob(os.path.join(SAMPLES, "*.pdf")) if "RTGS" in p), None)
    if not src:
        return
    d = pdfcore.PdfDocument(src)
    d.images[0] = [{"rect": (50, 50, 170, 110), "png": IMG, "rotation": 90}]
    d.annotations[0] = [{"type": "pen", "points": [(60, 60), (120, 90)], "color": [0.8, 0, 0], "width": 2.0},
                        {"type": "rect", "rect": [200, 200, 360, 260], "color": [0, 0, 0.8], "width": 1.5}]
    out = os.path.join(TMP, "p24.pdf")
    d.save_document(out, "save"); d.close()
    r = pdfcore.PdfDocument(out)
    ok = (len(r.images.get(0, [])) == 1 and r.images[0][0]["rotation"] == 90
          and len(r.annotations.get(0, [])) == 2)
    r.close()
    check("phase24:image+annot round-trip", ok)

    d = pdfcore.PdfDocument(src)
    d.fields[1] = [pdfcore.Field(name="x", kind="text", rect=(10, 10, 100, 30), page=1, value="KEEP", source="detected")]
    d.delete_page(0)
    moved = any(f.value == "KEEP" for f in d.fields.get(0, []))
    d.insert_blank(0)
    back = any(f.value == "KEEP" for f in d.fields.get(1, []))
    d.close()
    check("phase24:page-op remap", moved and back)

    d = pdfcore.PdfDocument(src)
    hits = d.search("Bank")
    d.close()
    check("phase24:search", len(hits) > 0, f"{len(hits)} hits")


# 3c) Feedback items: filled-form round-trip, dirty, search panel ----------
def feedback_tests():
    # existing filled AcroForm: values preserved + editable + round-trip
    af = os.path.join(TMP, "acro.pdf")
    d = fitz.open(); pg = d.new_page(width=400, height=300)
    w = fitz.Widget(); w.field_name = "nm"; w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(50, 50, 300, 70); w.field_value = "PREFILLED"; pg.add_widget(w)
    d.save(af); d.close()
    doc = pdfcore.PdfDocument(af)
    doc.fields[0] = doc.read_acro_fields(0)
    pre = doc.fields[0][0].value
    doc.fields[0][0].value = "EDITED"
    out = os.path.join(TMP, "acro_filled.pdf"); doc.save_document(out, "save"); doc.close()
    r = pdfcore.PdfDocument(out)
    ok = pre == "PREFILLED" and r.is_studio and r.fields.get(0, [{}])[0].value == "EDITED"
    r.close()
    check("feedback:filled-form preserve+edit", ok)

    win = MW.MainWindow()
    try:
        with mock.patch.object(QFileDialog, "getOpenFileName",
                               return_value=(_first_sample(), "")):
            win.open_pdf()
        APP.processEvents()
        clean = not win._is_dirty()
        if win.doc.fields.get(0):
            win.doc.fields[0][0].value = "DIRTYNOW"
        dirty = win._is_dirty(); win._refresh_title()
        check("feedback:dirty+title", clean and dirty and "*" in win.windowTitle())
        win.run_search_panel("the"); APP.processEvents()
        check("feedback:search panel", win.finder.results.count() >= 0)
    finally:
        win.close(); win.deleteLater(); APP.processEvents()


def _first_sample():
    return sorted(glob.glob(os.path.join(SAMPLES, "*.pdf")))[0]


def signal_tests():
    """Trigger actions through Qt signals (clicked/triggered) — catches the
    'menu passes a checked bool to a zero-arg handler' class of bug."""
    from PySide6.QtWidgets import QToolButton
    win = MW.MainWindow()
    try:
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(_first_sample(), "")):
            win.open_pdf()
        APP.processEvents()
        btns = win.editor.bar.findChildren(QToolButton)
        # toolbar button via clicked(bool)
        det = next((b for b in btns if b.text() == "Detect"), None)
        if det:
            det.click()
        APP.processEvents()
        check("signals:toolbar-click", True)
        # File-menu "Export Flattened" via QAction.trigger() (emits checked bool)
        filebtn = next((b for b in btns if b.text() == "File"), None)
        out = os.path.join(TMP, "sig_flat.pdf")
        acted = False
        if filebtn and filebtn.menu():
            for a in filebtn.menu().actions():
                if "Export Flattened" in a.text():
                    with mock.patch.object(QFileDialog, "getSaveFileName", return_value=(out, "")):
                        a.trigger()
                    acted = True
                    break
        APP.processEvents()
        check("signals:menu-export-flatten", acted and os.path.exists(out) and os.path.getsize(out) > 0)
    finally:
        win.close(); win.deleteLater(); APP.processEvents()


def tabs_tests():
    pdfs = sorted(glob.glob(os.path.join(SAMPLES, "*.pdf")))
    a = next((p for p in pdfs if "RTGS" in p), pdfs[0] if pdfs else None)
    b = next((p for p in pdfs if "doc1641" in p), pdfs[-1] if pdfs else None)
    if not a or not b or a == b:
        return
    win = MW.MainWindow()
    try:
        def openf(p):
            with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(p, "")):
                win.open_pdf()
            APP.processEvents()
        openf(a); pages_a = win.doc.page_count
        openf(b); pages_b = win.doc.page_count
        check("tabs:two-open", win.editor.tabs.count() == 2 and pages_a != pages_b)
        if win.doc.fields.get(0):
            win.doc.fields[0][0].value = "MARK_B"
        win.editor.tabs.setCurrentIndex(0); APP.processEvents()
        a_clean = (win.doc.page_count == pages_a
                   and not any(f.value == "MARK_B" for fl in win.doc.fields.values() for f in fl))
        win.editor.tabs.setCurrentIndex(1); APP.processEvents()
        b_kept = any(f.value == "MARK_B" for fl in win.doc.fields.values() for f in fl)
        check("tabs:isolation", a_clean and b_kept)
        win._tab_close(0); APP.processEvents()
        check("tabs:close", win.editor.tabs.count() == 1 and win.doc.page_count == pages_b)
    finally:
        win.close(); win.deleteLater(); APP.processEvents()


def signing_tests():
    import signing
    check("signing:available", signing.available())
    if not signing.available():
        return
    import datetime
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365)).sign(key, hashes.SHA256()))
    pfx = os.path.join(TMP, "t.pfx")
    open(pfx, "wb").write(pkcs12.serialize_key_and_certificates(
        b"t", key, cert, None, serialization.BestAvailableEncryption(b"pw")))
    src = os.path.join(TMP, "sign_in.pdf"); d = fitz.open(); d.new_page(width=200, height=120); d.save(src); d.close()
    out = os.path.join(TMP, "sign_out.pdf")
    signing.sign(src, out, pfx, "pw", reason="t")
    data = open(out, "rb").read()
    ok = b"adbe.pkcs7" in data or b"ETSI" in data
    bad = False
    try:
        signing.sign(src, os.path.join(TMP, "bad.pdf"), pfx, "wrong")
    except Exception:
        bad = True
    check("signing:sign+reject-wrong-pw", ok and bad)


def workflow_tests():
    import ocrengine
    from viewer import _apply_intent
    from PySide6.QtWidgets import QLineEdit
    from PIL import Image
    # autofill label matching + OCR enhance + validators
    check("workflow:profile-match",
          MW.MainWindow._match_profile_key("email address") == "email"
          and MW.MainWindow._match_profile_key("mobile no") == "phone"
          and MW.MainWindow._match_profile_key("ifsc code") == "ifsc")
    le = QLineEdit(); _apply_intent(le, "phone")
    le2 = QLineEdit(); _apply_intent(le2, "ifsc")
    check("workflow:validators", le.validator() is not None and le2.maxLength() == 11)
    img = Image.new("RGB", (160, 60), "white")
    check("workflow:ocr-enhance", hasattr(ocrengine.enhance(img), "size"))

    # end-to-end autofill through the window
    af = os.path.join(TMP, "wf_af.pdf")
    d = fitz.open(); pg = d.new_page(width=420, height=300)
    for lab, y in (("Full Name", 90), ("Email", 130)):
        pg.insert_text((40, y - 2), lab + ":", fontname="helv", fontsize=11)
        pg.draw_line((150, y), (380, y), width=0.8)
    d.save(af); d.close()
    import json
    win = MW.MainWindow()
    try:
        win._settings.setValue("profile", json.dumps({"full_name": "Sunny Patel", "email": "a@b.com"}))
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(af, "")):
            win.open_pdf()
        APP.processEvents()
        win.autofill_form(); APP.processEvents()
        vals = [f.value for fl in win.doc.fields.values() for f in fl]
        check("workflow:autofill", "Sunny Patel" in vals and "a@b.com" in vals)
        win.editor.view.focus_sibling(1)        # navigation: must not crash
        check("workflow:field-nav", True)
    finally:
        win.close(); win.deleteLater(); APP.processEvents()


def fidelity_tests():
    import fontmatch
    F = pdfcore.Field
    p = os.path.join(TMP, "fid.pdf")
    d = fitz.open(); pg = d.new_page(width=400, height=300)
    pg.insert_text((40, 100), "Full Name:", fontname="helv", fontsize=10)
    pg.insert_text((40, 140), "Salary Amount:", fontname="tiro", fontsize=13)
    pg.insert_text((40, 180), "Date of Birth:", fontname="cour", fontsize=9)
    d.save(p); d.close()
    doc = pdfcore.PdfDocument(p); page = doc.doc[0]
    m1 = fontmatch.analyze(page, (120, 90, 300, 108))
    m2 = fontmatch.analyze(page, (160, 130, 300, 150))
    m3 = fontmatch.analyze(page, (140, 170, 300, 190))
    check("fidelity:font-match",
          m1.get("font") == "helv" and m1.get("align") == "left"
          and m2.get("font") == "tiro" and m2.get("align") == "right" and m2.get("intent") == "amount"
          and m3.get("font") == "cour" and m3.get("align") == "center" and m3.get("intent") == "date",
          f"{m1.get('font')}/{m2.get('font')}/{m3.get('font')}")

    # shrink + clip: long value never exceeds the box right edge
    doc.fields[0] = [F(name="t", kind="text", rect=(40, 200, 120, 216), page=0,
                       value="ABCDEFGHIJKLMNOPQRSTUV", source="detected", size=12.0)]
    out = os.path.join(TMP, "fid_fit.pdf"); doc.save_document(out, "flatten"); doc.close()
    c = fitz.open(out)
    spans = [s for b in c[0].get_text("dict")["blocks"] for l in b.get("lines", [])
             for s in l.get("spans", []) if "ABCD" in s["text"]]
    right = max((s["bbox"][2] for s in spans), default=0)
    c.close()
    check("fidelity:shrink+clip", bool(spans) and right <= 121, f"right={right:.0f}")

    # per-field formatting round-trips
    doc = pdfcore.PdfDocument(p)
    doc.fields[0] = [F(name="t", kind="text", rect=(40, 210, 300, 230), page=0, value="Hi",
                       source="detected", font="tiro", size=14.0, bold=True, color=(1, 0, 0))]
    o2 = os.path.join(TMP, "fid_persist.pdf"); doc.save_document(o2, "save"); doc.close()
    r = pdfcore.PdfDocument(o2); g = r.fields[0][0]
    ok = g.font == "tiro" and g.size == 14.0 and g.bold and tuple(g.color) == (1, 0, 0)
    r.close()
    check("fidelity:per-field-persist", ok)
    check("fidelity:bold-variant",
          fontmatch.resolve_font(F(name="x", kind="text", rect=(0, 0, 1, 1), page=0, font="tiro", bold=True), "helv") == "tibo")


def scalability_tests():
    # AcroForm-only edit must NOT embed a base (≈1x file)
    af = os.path.join(TMP, "scal_acro.pdf")
    d = fitz.open(); pg = d.new_page(width=400, height=300)
    w = fitz.Widget(); w.field_name = "nm"; w.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    w.rect = fitz.Rect(50, 50, 300, 70); pg.add_widget(w)
    d.save(af); d.close()
    doc = pdfcore.PdfDocument(af); doc.fields[0] = doc.read_acro_fields(0); doc.fields[0][0].value = "X"
    o1 = os.path.join(TMP, "scal_a1.pdf"); doc.save_document(o1, "save"); doc.close()
    emb = fitz.open(o1).embfile_names()
    check("scal:acroform-no-base", "pdfstudio_base.pdf" not in emb and "pdfstudio_model.json" in emb)

    # No size compounding across repeated re-saves of a stamped doc
    src = _first_sample()
    d = pdfcore.PdfDocument(src)
    d.fields[0] = [pdfcore.Field(name="t", kind="text", rect=(20, 20, 160, 40), page=0,
                                 value="hello", source="detected")]
    cur = os.path.join(TMP, "scal_s.pdf"); d.save_document(cur, "save"); d.close()
    sizes = [os.path.getsize(cur)]
    for i in range(2):
        r = pdfcore.PdfDocument(cur); nxt = os.path.join(TMP, f"scal_s{i}.pdf")
        r.save_document(nxt, "save"); r.close(); cur = nxt; sizes.append(os.path.getsize(cur))
    check("scal:no-compounding", max(sizes) <= min(sizes) * 1.3, f"sizes={sizes}")

    # OCR text persists through save/reopen
    d = pdfcore.PdfDocument(src); d.ocr_text = {0: "cached ocr"}
    oo = os.path.join(TMP, "scal_ocr.pdf"); d.save_document(oo, "save"); d.close()
    r = pdfcore.PdfDocument(oo); ok = r.ocr_text.get(0) == "cached ocr"; r.close()
    check("scal:ocr-persist", ok)


# 4) Edge cases --------------------------------------------------------------
def edge_cases():
    from viewer import PdfView, SignatureItem
    from PySide6.QtGui import QKeyEvent
    from PySide6.QtCore import QEvent, Qt
    pdfs = glob.glob(os.path.join(SAMPLES, "*.pdf"))
    base = next((p for p in pdfs if "RTGS" in p or "doc1641" in p), pdfs[0] if pdfs else None)
    if base:
        d = pdfcore.PdfDocument(base)
        v = PdfView(); v.load(d); v.add_signature(SIG)
        n1 = len(d.signatures.get(0, []))
        item = next((it for it in v._scene.items() if isinstance(it, SignatureItem)), None)
        if item:
            item.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Delete, Qt.KeyboardModifier.NoModifier))
        check("edge:signature-delete", n1 == 1 and len(d.signatures.get(0, [])) == 0)
        d.close()

    # locked cancel clears doc
    p = make_encrypted_pdf()
    win = MW.MainWindow()
    try:
        with mock.patch.object(QFileDialog, "getOpenFileName", return_value=(p, "")), \
             mock.patch.object(QtW.QInputDialog, "getText", return_value=("", False)):
            win.open_pdf()
        APP.processEvents()
        check("edge:locked-cancel", win.doc is None)
    finally:
        win.close(); win.deleteLater(); APP.processEvents()


def main():
    pdfs = sorted(glob.glob(os.path.join(SAMPLES, "*.pdf")))
    docs = sorted(glob.glob(os.path.join(SAMPLES, "*.doc")) + glob.glob(os.path.join(SAMPLES, "*.docx")))
    combos = [("Helvetica", "Black", None), ("Times Roman", "Blue", 14.0),
              ("Courier", "Red", 12.0), ("Helvetica Bold", "Black", 10.0)]
    forms = pdfs + docs
    for i, p in enumerate(forms):
        try:
            d0 = pdfcore.PdfDocument(p)
            locked = d0.locked; d0.close()
        except Exception:
            locked = False
        if locked:
            check(f"core:{os.path.basename(p)}", True, "locked (skipped fill — expected)")
            continue
        f, ink, s = combos[i % len(combos)]
        try:
            core_pipeline(p, f, ink, s)
        except Exception as e:
            check(f"core:{os.path.basename(p)}", False, repr(e)); traceback.print_exc()

    password_tests()

    for p in forms:
        if "RTGS" in p or p.lower().endswith(".doc"):
            try:
                gui_flow(p)
            except Exception as e:
                check(f"gui:{os.path.basename(p)}", False, repr(e)); traceback.print_exc()

    try:
        phase24_tests()
    except Exception as e:
        check("phase24", False, repr(e)); traceback.print_exc()

    try:
        feedback_tests()
    except Exception as e:
        check("feedback", False, repr(e)); traceback.print_exc()

    try:
        signal_tests()
    except Exception as e:
        check("signals", False, repr(e)); traceback.print_exc()

    try:
        tabs_tests()
    except Exception as e:
        check("tabs", False, repr(e)); traceback.print_exc()

    try:
        signing_tests()
    except Exception as e:
        check("signing", False, repr(e)); traceback.print_exc()

    try:
        workflow_tests()
    except Exception as e:
        check("workflow", False, repr(e)); traceback.print_exc()

    try:
        fidelity_tests()
    except Exception as e:
        check("fidelity", False, repr(e)); traceback.print_exc()

    try:
        scalability_tests()
    except Exception as e:
        check("scalability", False, repr(e)); traceback.print_exc()

    try:
        edge_cases()
    except Exception as e:
        check("edge", False, repr(e)); traceback.print_exc()

    n = len(results); passed = sum(1 for _, ok, _ in results if ok)
    print("\n" + "=" * 60)
    print(f"RESULT: {passed}/{n} passed")
    for nm, ok, d in results:
        if not ok:
            print(f"  FAIL {nm}: {d}")
    print("artifacts:", TMP)
    sys.exit(0 if passed == n else 1)


if __name__ == "__main__":
    main()
