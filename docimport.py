"""Open .doc/.docx by converting to PDF first.

Uses the Word installed on the machine (via COM). Falls back with a clear
error if Word or the COM bridge isn't available, so the caller can tell the
user to save as PDF manually.
"""
from __future__ import annotations

import os
import tempfile

WORD_FORMATS = (".doc", ".docx", ".rtf", ".odt")


def needs_conversion(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in WORD_FORMATS


def convert_to_pdf(path: str) -> str:
    """Convert a Word document to a temporary PDF and return its path."""
    try:
        import pythoncom
        import win32com.client as win32
    except Exception as e:  # pragma: no cover
        raise RuntimeError(f"Word automation unavailable: {e}")

    out = os.path.join(tempfile.mkdtemp(prefix="pdfstudio_doc_"),
                       os.path.splitext(os.path.basename(path))[0] + ".pdf")

    pythoncom.CoInitialize()
    word = None
    doc = None
    try:
        word = win32.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0
        doc = word.Documents.Open(os.path.abspath(path), ReadOnly=True)
        doc.SaveAs2(out, FileFormat=17)   # 17 = wdFormatPDF
    except Exception as e:
        raise RuntimeError(
            f"Could not convert '{os.path.basename(path)}' with Word ({e}). "
            f"Please open it in Word and save as PDF."
        )
    finally:
        try:
            if doc is not None:
                doc.Close(False)
        except Exception:
            pass
        try:
            if word is not None:
                word.Quit()
        except Exception:
            pass
        pythoncom.CoUninitialize()

    if not os.path.exists(out):
        raise RuntimeError("Word conversion produced no output PDF.")
    return out
