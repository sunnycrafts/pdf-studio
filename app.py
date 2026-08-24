"""PDF Studio — OCR & form-filling PDF editor with a Windows 11 (Fluent) UI.

Entry point. Run:  python app.py
"""
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from qfluentwidgets import setTheme, Theme

import logconf


def _selftest_sign():
    """Hidden self-test: exercise the frozen EXE's real code paths (detect, fill,
    save, flatten export, sign). Writes a per-step report to
    %TEMP%\\pdfstudio_selftest.txt (windowed build has no console)."""
    import os, tempfile, datetime, traceback
    out = os.path.join(tempfile.gettempdir(), "pdfstudio_selftest.txt")
    lines = []

    def step(name, fn):
        try:
            fn(); lines.append(f"OK   {name}")
        except Exception as e:
            lines.append(f"FAIL {name}: {type(e).__name__}: {e}")
            lines.append(traceback.format_exc())

    import fitz, pdfcore, formdetect
    d = tempfile.mkdtemp()
    # a synthetic labelled form (label + a write-on line)
    src = os.path.join(d, "form.pdf")
    doc = fitz.open(); pg = doc.new_page(width=400, height=300)
    pg.insert_text((40, 100), "Full Name:", fontname="helv", fontsize=11)
    pg.draw_line((150, 100), (360, 100), width=0.8)
    doc.save(src); doc.close()
    state = {}

    def do_open():
        state["pd"] = pdfcore.PdfDocument(src)

    def do_detect_fill():
        pd = state["pd"]
        pd.fields[0] = formdetect.detect_fields(pd.render_pil(0, 2.0), 0, 2.0)
        import fontmatch
        for f in pd.fields[0]:
            if f.kind in ("text", "multiline"):
                m = fontmatch.analyze(pd.doc[0], f.rect)
                f.font = m.get("font"); f.size = m.get("size"); f.color = m.get("color")
                f.value = "Sunny Patel"

    def do_save():
        state["pd"].save_document(os.path.join(d, "saved.pdf"), "save")

    def do_flatten():
        state["pd"].save_document(os.path.join(d, "flat.pdf"), "flatten")
        assert os.path.getsize(os.path.join(d, "flat.pdf")) > 0

    def do_sign():
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        import signing
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        nm = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Selftest")])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(nm).issuer_name(nm)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365)).sign(key, hashes.SHA256()))
        pfx = os.path.join(d, "t.pfx")
        open(pfx, "wb").write(pkcs12.serialize_key_and_certificates(
            b"t", key, cert, None, serialization.BestAvailableEncryption(b"pw")))
        sgn = os.path.join(d, "signed.pdf"); signing.sign(os.path.join(d, "flat.pdf"), sgn, pfx, "pw")
        assert b"adbe.pkcs7" in open(sgn, "rb").read()

    step("open", do_open)
    step("detect+fill", do_detect_fill)
    step("save", do_save)
    step("export-flatten", do_flatten)
    step("digital-sign", do_sign)
    open(out, "w", encoding="utf-8").write("\n".join(lines))


def main():
    import sys as _sys
    if "--selftest-sign" in _sys.argv:
        _selftest_sign()
        return
    log_path = logconf.setup()
    log = logconf.get()
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
        app = QApplication(sys.argv)
        app.setApplicationName("PDF Studio")
        setTheme(Theme.AUTO)

        # Import after QApplication exists so Fluent styles initialise cleanly.
        from mainwindow import MainWindow
        window = MainWindow()
        window.show()
        log.info("UI shown; entering event loop")
        sys.exit(app.exec())
    except SystemExit:
        raise
    except Exception:
        log.exception("Fatal error during startup")
        raise


if __name__ == "__main__":
    main()
