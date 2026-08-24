"""Application logging + crash capture.

Writes a rotating log to %LOCALAPPDATA%\\PDF Studio\\logs\\pdfstudio.log and
installs hooks so uncaught Python exceptions, Qt warnings, and faulthandler
crash dumps all land in files the user can hand back for troubleshooting.
"""
from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

_LOGGER_NAME = "pdfstudio"
_log_path = None


def log_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "PDF Studio", "logs")
    os.makedirs(d, exist_ok=True)
    return d


def get() -> logging.Logger:
    return logging.getLogger(_LOGGER_NAME)


def log_file() -> str:
    return _log_path or os.path.join(log_dir(), "pdfstudio.log")


def setup() -> str:
    """Configure logging + crash hooks. Returns the log file path."""
    global _log_path
    d = log_dir()
    _log_path = os.path.join(d, "pdfstudio.log")

    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        h = RotatingFileHandler(_log_path, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logger.addHandler(h)

    # Native-crash dumps (segfaults) -> a separate file.
    try:
        import faulthandler
        fh = open(os.path.join(d, "crash.log"), "a", encoding="utf-8")
        faulthandler.enable(file=fh)
    except Exception:
        pass

    # Uncaught Python exceptions.
    def _hook(exc_type, exc, tb):
        logger.error("UNCAUGHT EXCEPTION", exc_info=(exc_type, exc, tb))
        sys.__excepthook__(exc_type, exc, tb)
    sys.excepthook = _hook

    # Qt warnings/criticals.
    try:
        from PySide6.QtCore import qInstallMessageHandler, QtMsgType

        def _qt(mode, ctx, msg):
            lvl = logging.DEBUG
            if mode in (QtMsgType.QtWarningMsg,):
                lvl = logging.WARNING
            elif mode in (QtMsgType.QtCriticalMsg, QtMsgType.QtFatalMsg):
                lvl = logging.ERROR
            logger.log(lvl, "Qt: %s", msg)
        qInstallMessageHandler(_qt)
    except Exception:
        pass

    logger.info("================ PDF Studio start (py=%s) ================", sys.version.split()[0])
    logger.info("log file: %s", _log_path)
    return _log_path
