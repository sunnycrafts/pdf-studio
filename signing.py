"""Cryptographic (digital) PDF signing via pyHanko.

Distinct from the visual signature image: this embeds a PAdES/PKCS#7 digital
signature from the user's certificate (.pfx / .p12), so the document is
tamper-evident and verifiable. The certificate password is supplied by the
user at runtime — the app never stores it.
"""
from __future__ import annotations


def available() -> bool:
    try:
        import pyhanko  # noqa: F401
        return True
    except Exception:
        return False


def sign(in_pdf: str, out_pdf: str, pfx_path: str, passphrase: str,
         reason: str = "", location: str = "", field_name: str = "Signature1"):
    """Digitally sign `in_pdf` with the PKCS#12 cert, writing `out_pdf`.
    Raises RuntimeError on a bad certificate / password."""
    from pyhanko.sign import signers
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

    try:
        signer = signers.SimpleSigner.load_pkcs12(
            pfx_path, passphrase=(passphrase or "").encode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Could not load certificate: {e}")
    if signer is None:
        raise RuntimeError("Could not load certificate — wrong password or invalid .pfx file.")

    meta = signers.PdfSignatureMetadata(
        field_name=field_name, reason=reason or None, location=location or None)
    with open(in_pdf, "rb") as inf, open(out_pdf, "wb") as outf:
        w = IncrementalPdfFileWriter(inf)
        signers.sign_pdf(w, meta, signer=signer, output=outf)
