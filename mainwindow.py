"""Main window: a Fluent (Windows 11) shell around the PDF viewer.

Left nav = Open / Editor / OCR Text / Save + theme toggle.
Editor = a Fluent command bar (page nav, zoom, detect fields, OCR, save) over
the page view. Detected/AcroForm fields become editable overlays; OCR text lands
on its own page.
"""
from __future__ import annotations

import os
import functools

import logconf
log = logconf.get()

from PySide6.QtCore import Qt, QRectF, QSize, QTimer, QSettings
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QFileDialog, QApplication, QToolButton, QFrame,
    QMessageBox,
)
from PySide6.QtGui import QGuiApplication, QColor, QIcon, QPixmap, QPainter, QShortcut, QKeySequence

from qfluentwidgets import (
    FluentWindow, NavigationItemPosition, FluentIcon as FIF, CommandBar, Action,
    BodyLabel, TextEdit, InfoBar, InfoBarPosition, setTheme, Theme, isDarkTheme,
    PrimaryPushButton, PushButton, ComboBox, RoundMenu,
)

import pdfcore
import ocrengine
import formdetect
import fontmatch
import docimport
import docmodel
import signing
from viewer import PdfView
from signature import SignatureDialog


# Available ink colours, as 0-1 RGB (what PyMuPDF expects).
INK_COLORS = {
    "Black": (0.0, 0.0, 0.0),
    "Blue": (0.0, 0.32, 0.78),
    "Red": (0.84, 0.07, 0.07),
}

# Common form-filling fonts: label -> (PyMuPDF base-14 name, Qt family, bold).
FONTS = {
    "Helvetica": ("helv", "Arial", False),
    "Helvetica Bold": ("hebo", "Arial", True),
    "Times Roman": ("tiro", "Times New Roman", False),
    "Courier": ("cour", "Courier New", False),
}

FONT_SIZES = ["Auto", "8", "9", "10", "11", "12", "14", "16", "18", "20", "24"]

# Autofill profile fields: (key, display label).
PROFILE_KEYS = [
    ("full_name", "Full name"), ("first_name", "First name"), ("last_name", "Last name"),
    ("email", "Email"), ("phone", "Phone"), ("address", "Address"),
    ("city", "City"), ("state", "State"), ("zip", "PIN / ZIP"),
    ("dob", "Date of birth"), ("pan", "PAN"), ("account", "Account number"), ("ifsc", "IFSC"),
]


def _dump_error(action: str, exc: BaseException) -> str:
    """Write a full traceback to a plain file, bypassing the logging framework
    (whose handler can silently fail). Returns a short error string for the UI."""
    import traceback
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "PDF Studio", "logs"); os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "last_error.txt"), "w", encoding="utf-8") as fh:
            import datetime
            fh.write(f"{datetime.datetime.now()}  action={action}\n\n{tb}")
    except Exception:
        pass
    return f"{type(exc).__name__}: {exc}"


def guarded(action: str):
    """Wrap a window action: log entry, catch+report errors (with the real
    message + a plain-file traceback), instead of crashing the app."""
    def deco(fn):
        @functools.wraps(fn)
        def wrap(self, *a, **k):
            log.info("action: %s", action)
            try:
                return fn(self, *a, **k)
            except Exception as exc:
                log.exception("error in action: %s", action)
                detail = _dump_error(action, exc)
                try:
                    QApplication.restoreOverrideCursor()
                except Exception:
                    pass
                try:
                    self.info(f"{action} failed", f"{detail}  (details: logs\\last_error.txt)", error=True)
                except Exception:
                    pass
        return wrap
    return deco


def _icon(*names):
    """Resilient FluentIcon lookup (icon set varies across versions)."""
    for n in names:
        ic = getattr(FIF, n, None)
        if ic is not None:
            return ic
    return FIF.DOCUMENT


def _swatch_icon(rgb) -> QIcon:
    """A rounded square filled with `rgb` (0-1) for the colour buttons."""
    pix = QPixmap(24, 24)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor.fromRgbF(*rgb))
    p.drawRoundedRect(QRectF(3, 3, 18, 18), 5, 5)
    p.end()
    return QIcon(pix)


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(int(c * 255) for c in rgb)


class EditorInterface(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.setObjectName("editorInterface")
        self.win = window

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 12)
        layout.setSpacing(8)

        from PySide6.QtWidgets import QTabBar
        self.tabs = QTabBar(self)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setExpanding(False)
        self.tabs.setDrawBase(False)
        self.tabs.setUsesScrollButtons(True)
        self.tabs.currentChanged.connect(window._tab_changed)
        self.tabs.tabCloseRequested.connect(window._tab_close)
        self.tabs.hide()                       # shown once a document is open
        layout.addWidget(self.tabs)

        self.page_label = BodyLabel("No document", self)
        self.bar = self._build_toolbar(window)
        from PySide6.QtWidgets import QScrollArea
        bar_scroll = QScrollArea(self)
        bar_scroll.setWidget(self.bar)
        bar_scroll.setWidgetResizable(True)
        bar_scroll.setFixedHeight(66)
        bar_scroll.setFrameShape(QFrame.Shape.NoFrame)
        bar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        bar_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout.addWidget(bar_scroll)

        # Controls row: page info + font family / size pickers.
        controls = QHBoxLayout()
        controls.setContentsMargins(2, 0, 2, 0)
        controls.setSpacing(8)
        controls.addWidget(self.page_label)
        controls.addStretch(1)
        controls.addWidget(BodyLabel("Font:", self))
        self.font_combo = ComboBox(self)
        self.font_combo.addItems(list(FONTS.keys()))
        self.font_combo.setCurrentText("Helvetica")
        self.font_combo.setFixedWidth(150)
        self.font_combo.currentTextChanged.connect(window.set_font_family)
        controls.addWidget(self.font_combo)
        controls.addWidget(BodyLabel("Size:", self))
        self.size_combo = ComboBox(self)
        self.size_combo.addItems(FONT_SIZES)
        self.size_combo.setCurrentText("Auto")
        self.size_combo.setFixedWidth(80)
        self.size_combo.currentTextChanged.connect(window.set_font_size)
        controls.addWidget(self.size_combo)
        layout.addLayout(controls)

        from PySide6.QtWidgets import QListWidget
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0); body.setSpacing(6)
        self.thumbs = QListWidget(self)
        self.thumbs.setFixedWidth(132)
        self.thumbs.setIconSize(QSize(104, 140))
        self.thumbs.setSpacing(3)
        self.thumbs.itemClicked.connect(window._thumb_clicked)
        body.addWidget(self.thumbs)
        self.view = PdfView(self)
        body.addWidget(self.view, 1)
        layout.addLayout(body, 1)

    def _build_toolbar(self, window) -> QWidget:
        bar = QWidget(self)
        bar.setStyleSheet(
            "QToolButton{border:none;border-radius:6px;padding:3px 7px;font-size:11px;}"
            "QToolButton:hover{background:rgba(128,128,128,0.18);}"
            "QToolButton:checked{background:rgba(0,120,215,0.28);}"
        )
        hl = QHBoxLayout(bar)
        hl.setContentsMargins(2, 2, 2, 2)
        hl.setSpacing(2)

        def drop(slot):
            # Qt's clicked/triggered signals pass a `checked` bool; our handlers
            # take none. Swallow any signal args so they aren't forwarded.
            return lambda *_, s=slot: s()

        def sep():
            line = QFrame(bar)
            line.setFrameShape(QFrame.Shape.VLine)
            line.setStyleSheet("color:rgba(128,128,128,0.35);")
            hl.addWidget(line)

        def tool(icon, text, slot, checkable=False, tip=""):
            b = QToolButton(bar)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setIcon(icon.icon() if hasattr(icon, "icon") else icon)
            b.setIconSize(QSize(20, 20))
            b.setText(text)
            b.setToolTip(tip or text)
            b.setCheckable(checkable)
            b.setFixedHeight(50)
            b.clicked.connect(drop(slot))
            hl.addWidget(b)
            return b

        def menu_button(text, ic, items):
            b = QToolButton(bar)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            b.setIcon(ic.icon() if hasattr(ic, "icon") else ic)
            b.setIconSize(QSize(20, 20)); b.setText(text); b.setFixedHeight(50)
            m = RoundMenu(parent=b)
            for it in items:
                if it == "sep":
                    m.addSeparator()
                else:
                    label, slot = it
                    a = Action(label, m); a.triggered.connect(drop(slot)); m.addAction(a)
            b.setMenu(m)
            b.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
            hl.addWidget(b)
            return b

        def mact(icon, text, slot):           # File-menu action that drops signal args
            a = Action(icon, text, menu); a.triggered.connect(drop(slot))
            return a

        # File menu (Open / Recent / Save / Save As / Save Copy / Export / Exit)
        file_btn = QToolButton(bar)
        file_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        file_btn.setIcon(_icon("MENU", "ALIGNMENT", "DOCUMENT").icon())
        file_btn.setText("File")
        file_btn.setIconSize(QSize(20, 20))
        file_btn.setFixedHeight(50)
        menu = RoundMenu(parent=file_btn)
        menu.addAction(mact(_icon("FOLDER", "FOLDER_ADD"), "Open…", window.open_pdf))
        self.recent_menu = RoundMenu("Open Recent", parent=file_btn)
        menu.addMenu(self.recent_menu)
        menu.addSeparator()
        menu.addAction(mact(_icon("SAVE"), "Save", window.save_pdf))
        menu.addAction(mact(_icon("SAVE_AS", "SAVE"), "Save As…", window.save_as))
        menu.addAction(mact(_icon("COPY", "SAVE"), "Save Copy…", window.save_copy))
        menu.addSeparator()
        menu.addAction(mact(_icon("DOCUMENT"), "Export Flattened PDF…", window.export_flattened))
        menu.addAction(mact(_icon("CERTIFICATE", "DOCUMENT"), "Export Signed PDF…", window.export_signed))
        menu.addAction(mact(_icon("CERTIFICATE", "FINGERPRINT", "DOCUMENT"), "Digitally Sign PDF…", window.digitally_sign))
        menu.addSeparator()
        menu.addAction(mact(_icon("CLOSE", "CANCEL"), "Exit", window.close))
        menu.aboutToShow.connect(lambda: window._populate_recent(self.recent_menu))
        file_btn.setMenu(menu)
        file_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        hl.addWidget(file_btn)
        sep()

        tool(_icon("FOLDER", "FOLDER_ADD"), "Open", window.open_pdf, tip="Open a PDF or Word file  (Ctrl+O)")
        sep()
        tool(_icon("LEFT_ARROW", "PAGE_LEFT", "CARE_LEFT_SOLID"), "Prev", window.prev_page, tip="Previous page  (Ctrl+PgUp)")
        tool(_icon("RIGHT_ARROW", "PAGE_RIGHT", "CARE_RIGHT_SOLID"), "Next", window.next_page, tip="Next page  (Ctrl+PgDn)")
        sep()
        tool(_icon("ZOOM_OUT", "REMOVE"), "Zoom −", window.zoom_out, tip="Zoom out  (Ctrl+−)")
        tool(_icon("ZOOM_IN", "ADD"), "Zoom +", window.zoom_in, tip="Zoom in  (Ctrl++)")
        sep()
        tool(_icon("EDIT", "VIEW", "BRUSH"), "Detect", window.detect_fields, tip="Detect fillable fields  (Ctrl+D)")
        tool(_icon("FONT", "FONT_SIZE", "EDIT"), "Text box", window.start_textbox, tip="Draw a text box  (Ctrl+T)")
        tool(_icon("SEARCH", "ZOOM"), "OCR", window.run_ocr_page, tip="Recognise text on this page  (Ctrl+R)")
        tool(_icon("DELETE", "CANCEL"), "Clear", window.clear_fields, tip="Clear fields on this page")
        sep()
        for name, rgb in INK_COLORS.items():
            b = tool(_swatch_icon(rgb), name, lambda _=False, n=name: window.set_ink(n),
                     checkable=True, tip=f"{name} ink")
            window.color_actions[name] = b
        tool(_icon("PENCIL_INK", "BRUSH", "EDIT"), "Signature", window.add_signature, tip="Add a signature  (Ctrl+D)")
        sep()
        menu_button("Insert", _icon("ADD", "PHOTO", "EDIT"), [
            ("Image…", window.insert_image),
            ("Text box", window.start_textbox),
            ("Signature…", window.add_signature),
        ])
        menu_button("Markup", _icon("HIGHTLIGHT", "BRUSH", "EDIT"), [
            ("Pen", lambda: window._begin_markup("pen")),
            ("Rectangle", lambda: window._begin_markup("rect")),
            ("Ellipse", lambda: window._begin_markup("ellipse")),
            ("Line", lambda: window._begin_markup("line")),
            ("Arrow", lambda: window._begin_markup("arrow")),
            ("Highlight", lambda: window._begin_markup("highlight")),
            "sep",
            ("Markup colour…", window.pick_annot_color),
        ])
        menu_button("Format", _icon("FONT", "EDIT"), [
            ("Bold  (Ctrl+B)", window.toggle_bold),
            ("Italic  (Ctrl+I)", window.toggle_italic),
            ("Underline  (Ctrl+U)", window.toggle_underline),
            "sep",
            ("Align left", lambda: window.set_align("left")),
            ("Align center", lambda: window.set_align("center")),
            ("Align right", lambda: window.set_align("right")),
            "sep",
            ("Copy formatting", window.copy_format),
            ("Paste formatting", window.paste_format),
            "sep",
            ("Match Original Form (toggle)", window.toggle_match_original),
        ])
        menu_button("Autofill", _icon("PEOPLE", "PEOPLE_FILLED", "EDIT"), [
            ("Autofill form from profile", window.autofill_form),
            ("Edit profile…", window.edit_profile),
        ])
        menu_button("Page", _icon("DOCUMENT", "COPY"), [
            ("Rotate 90°", window.rotate_page),
            ("Insert blank page", window.insert_blank_page),
            ("Move page up", window.move_page_up),
            ("Move page down", window.move_page_down),
            ("Delete page", window.delete_page),
            "sep",
            ("Extract this page…", window.extract_page),
            ("Merge / append PDF…", window.merge_pdf),
        ])
        menu_button("View", _icon("ZOOM", "VIEW"), [
            ("Fit width", window.fit_width),
            ("Fit page", window.fit_page),
            ("Actual size", window.actual_size),
            "sep",
            ("Single page", lambda: window.set_view_mode("single")),
            ("Continuous scroll", lambda: window.set_view_mode("continuous")),
            ("Two pages", lambda: window.set_view_mode("two")),
            "sep",
            ("Print…", window.print_doc),
        ])
        tool(_icon("SEARCH", "ZOOM"), "Find", window.find, tip="Find text  (Ctrl+F)")
        sep()
        tool(_icon("SAVE", "SAVE_AS"), "Save", window.save_pdf, tip="Save filled PDF  (Ctrl+S)")
        hl.addStretch(1)
        return bar


class OcrInterface(QWidget):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.setObjectName("ocrInterface")
        self.win = window
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 16)
        layout.setSpacing(10)

        row = QWidget(self)
        from PySide6.QtWidgets import QHBoxLayout
        hl = QHBoxLayout(row)
        hl.setContentsMargins(0, 0, 0, 0)
        self.btn_page = PrimaryPushButton("Recognize current page", row)
        self.btn_all = PushButton("Recognize all pages", row)
        self.btn_copy = PushButton("Copy text", row)
        self.btn_page.clicked.connect(window.run_ocr_page)
        self.btn_all.clicked.connect(window.run_ocr_all)
        self.btn_copy.clicked.connect(self.copy_text)
        hl.addWidget(self.btn_page)
        hl.addWidget(self.btn_all)
        hl.addWidget(self.btn_copy)
        hl.addStretch(1)
        layout.addWidget(row)

        self.text = TextEdit(self)
        self.text.setReadOnly(False)
        self.text.setPlaceholderText("Recognized text will appear here.")
        layout.addWidget(self.text, 1)

    def copy_text(self):
        QGuiApplication.clipboard().setText(self.text.toPlainText())
        self.win.info("Copied", "Recognized text copied to clipboard.")


class FindInterface(QWidget):
    """A search panel: query box + clickable results (native + OCR text)."""
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.setObjectName("findInterface")
        self.win = window
        from PySide6.QtWidgets import QListWidget
        from qfluentwidgets import LineEdit
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 16); layout.setSpacing(10)
        row = QHBoxLayout()
        self.box = LineEdit(self)
        self.box.setPlaceholderText("Search the document (native text + OCR)…")
        self.box.returnPressed.connect(lambda: window.run_search_panel(self.box.text()))
        btn = PrimaryPushButton("Search", self)
        btn.clicked.connect(lambda: window.run_search_panel(self.box.text()))
        row.addWidget(self.box, 1); row.addWidget(btn)
        layout.addLayout(row)
        self.count = BodyLabel("", self)
        layout.addWidget(self.count)
        self.results = QListWidget(self)
        self.results.itemActivated.connect(window._jump_result)
        self.results.itemClicked.connect(window._jump_result)
        layout.addWidget(self.results, 1)


class MainWindow(FluentWindow):
    def __init__(self):
        super().__init__()
        self.doc: pdfcore.PdfDocument | None = None
        self.source_path: str | None = None   # original file (may be a .doc)
        self.save_target: str | None = None   # .pdf that "Save" overwrites
        self.sessions: list = []               # open documents (tabs)
        self._active_session = -1
        self._switching = False
        self._detected: set[int] = set()       # pages already auto-scanned
        self.match_original = True              # "Match Original Form" (font fidelity)
        self._fmt_clip = None                  # format painter clipboard
        self._saved_snapshot = None            # model state at last save (dirty check)
        self._undo: list = []
        self._redo: list = []
        self._settings = QSettings("PDFStudio", "PDFStudio")
        self.ink_name = "Black"
        self.ink_rgb = INK_COLORS["Black"]
        self.color_actions: dict = {}      # populated by EditorInterface
        self.font_key = "helv"             # PyMuPDF base-14 name for stamping
        self.font_size = None              # None = auto-fit

        self.editor = EditorInterface(self)
        self.ocr = OcrInterface(self)
        self.finder = FindInterface(self)
        self.set_ink("Black")              # sync swatch check state + overlay colour
        self.set_font_family("Helvetica")  # sync overlay font
        self.set_font_size("Auto")

        self.addSubInterface(self.editor, _icon("EDIT", "VIEW"), "Editor")
        self.addSubInterface(self.finder, _icon("SEARCH", "ZOOM"), "Find")
        self.addSubInterface(self.ocr, _icon("DOCUMENT", "FONT"), "OCR Text")

        self.navigationInterface.addItem(
            "open", _icon("FOLDER", "FOLDER_ADD"), "Open PDF",
            onClick=self.open_pdf, selectable=False,
            position=NavigationItemPosition.TOP,
        )
        self.navigationInterface.addItem(
            "save", _icon("SAVE", "SAVE_AS"), "Save as…",
            onClick=self.save_pdf, selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            "logs", _icon("COMMAND_PROMPT", "DOCUMENT", "FOLDER"), "Logs",
            onClick=self._open_logs, selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )
        self.navigationInterface.addItem(
            "theme", _icon("CONSTRACT", "PALETTE", "BRUSH"), "Theme",
            onClick=self.toggle_theme, selectable=False,
            position=NavigationItemPosition.BOTTOM,
        )

        self.setWindowTitle("PDF Studio — OCR & Forms")
        self.resize(1240, 840)
        self._center()
        self.setAcceptDrops(True)                     # drag a PDF/Word file onto the window
        self.editor.view.on_file_dropped = self._open_path
        self.editor.view.on_field_created = self._update_label
        self.editor.view.on_page_changed = self._on_page_changed
        self._add_shortcuts()
        self._setup_autosave()
        self._title_timer = QTimer(self)               # keep title's "*" in sync
        self._title_timer.timeout.connect(self._refresh_title)
        self._title_timer.start(1500)
        self._refresh_title()
        QTimer.singleShot(300, self._check_recovery)   # offer recovery once UI is up
        try:
            self.setMicaEffectEnabled(True)  # Windows 11 Mica backdrop
        except Exception:
            pass

    # -- helpers ------------------------------------------------------------
    def _center(self):
        geo = QGuiApplication.primaryScreen().availableGeometry()
        self.move((geo.width() - self.width()) // 2, (geo.height() - self.height()) // 2)

    def info(self, title, content, error=False):
        fn = InfoBar.error if error else InfoBar.success
        fn(title=title, content=content, parent=self, position=InfoBarPosition.TOP, duration=3500)

    def _fields_for_page(self, index: int):
        if self.doc is None:
            return []
        if index not in self.doc.fields:
            self.doc.fields[index] = self.doc.read_acro_fields(index)
        return self.doc.fields[index]

    def _ensure_fields(self, index: int):
        """Return this page's fields, auto-detecting flat pages on first view."""
        fields = self._fields_for_page(index)
        # Flat page (no embedded AcroForm fields) not yet scanned -> detect once.
        if index not in self._detected and not fields:
            self._run_detect(index)
            fields = self.doc.fields[index]
        return fields

    def _run_detect(self, index: int) -> int:
        """Detect fillable regions on a page, keeping any AcroForm fields, and
        (Match Original) match each field's font/size/align to the form's text."""
        found = formdetect.detect_fields(self.doc.render_pil(index, 2.0), index, 2.0)
        if self.match_original:
            self._match_fonts(index, found)
        if index not in self.doc.fields:
            self.doc.fields[index] = self.doc.read_acro_fields(index)
        kept = [f for f in self.doc.fields[index] if f.source == "acro"]
        kept.extend(found)
        self.doc.fields[index] = kept
        self._detected.add(index)
        return len(found)

    def _match_fonts(self, index: int, fields):
        page = self.doc.doc[index]
        for f in fields:
            if f.kind == "checkbox":
                continue
            try:
                m = fontmatch.analyze(page, f.rect)
            except Exception:
                m = {}
            if not m:
                continue
            f.font = m.get("font", f.font)
            f.size = m.get("size", f.size)
            f.color = m.get("color", f.color)
            f.intent = m.get("intent", "")
            if f.kind != "comb":
                f.align = m.get("align", f.align)

    @guarded("Show page")
    def _show_page(self, index: int):
        if self.doc is None:
            return
        self.editor.view.goto(index)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            fields = self._ensure_fields(self.editor.view.page_index)
        finally:
            QApplication.restoreOverrideCursor()
        self.editor.view.set_page_fields(fields)
        self._update_label()

    def _update_label(self):
        if self.doc is None:
            self.editor.page_label.setText("No document")
            return
        idx = self.editor.view.page_index
        n = len(self.doc.fields.get(idx, []))
        suffix = f" · {n} field(s)" if n else ""
        self.editor.page_label.setText(
            f"Page {idx + 1} / {self.doc.page_count}  ·  {int(self.editor.view.zoom*100)}%{suffix}"
        )
        if 0 <= idx < self.editor.thumbs.count() and self.editor.thumbs.currentRow() != idx:
            self.editor.thumbs.setCurrentRow(idx)

    # -- document tabs (multi-document) ------------------------------------
    def _stash_active(self):
        """Save the live per-document state back into its session."""
        if not (0 <= self._active_session < len(self.sessions)) or self.doc is None:
            return
        s = self.sessions[self._active_session]
        s.update(doc=self.doc, source_path=self.source_path, save_target=self.save_target,
                 detected=self._detected, undo=self._undo, redo=self._redo,
                 saved_snapshot=self._saved_snapshot,
                 page_index=self.editor.view.page_index, zoom=self.editor.view.zoom,
                 view_mode=self.editor.view.view_mode)

    def _load_session(self, i: int):
        s = self.sessions[i]
        self._active_session = i
        self.doc = s["doc"]; self.source_path = s["source_path"]; self.save_target = s["save_target"]
        self._detected = s["detected"]; self._undo = s["undo"]; self._redo = s["redo"]
        self._saved_snapshot = s["saved_snapshot"]
        self.editor.view.view_mode = s.get("view_mode", "single")
        self.editor.view.zoom = s.get("zoom", 1.5)
        self.editor.view.load(self.doc)
        self._populate_thumbs()
        self._show_page(s.get("page_index", 0))
        self._refresh_title()

    def _tab_title(self, path, dirty=False):
        return (os.path.basename(path) if path else "Untitled.pdf") + (" *" if dirty else "")

    def _new_document_tab(self, doc, source_path, save_target):
        self._stash_active()
        s = dict(doc=doc, source_path=source_path, save_target=save_target,
                 detected=set(), undo=[], redo=[], saved_snapshot=None,
                 page_index=0, zoom=1.5, view_mode="single")
        self.sessions.append(s)
        self._switching = True
        self.editor.tabs.addTab(self._tab_title(source_path))
        self.editor.tabs.setCurrentIndex(len(self.sessions) - 1)
        self.editor.tabs.show()
        self._switching = False
        self._load_session(len(self.sessions) - 1)

    def _tab_changed(self, i: int):
        if self._switching or not (0 <= i < len(self.sessions)) or i == self._active_session:
            return
        self._stash_active()
        self._load_session(i)

    def _tab_close(self, i: int):
        if not (0 <= i < len(self.sessions)):
            return
        if i == self._active_session and self._is_dirty() and not self._maybe_discard():
            return
        try:
            self.sessions[i]["doc"].close()
        except Exception:
            pass
        del self.sessions[i]
        self._switching = True
        self.editor.tabs.removeTab(i)
        self._switching = False
        if not self.sessions:
            self.doc = None; self._active_session = -1
            self.editor.tabs.hide()
            self.editor.view.document = None; self.editor.view.render_page()
            self.editor.thumbs.clear(); self._update_label(); self._refresh_title()
            return
        self._active_session = -1               # force reload
        self._load_session(self.editor.tabs.currentIndex())

    def _refresh_tab_titles(self):
        if 0 <= self._active_session < self.editor.tabs.count():
            self.editor.tabs.setTabText(self._active_session,
                                        self._tab_title(self.save_target or self.source_path, self._is_dirty()))

    # -- actions ------------------------------------------------------------
    def open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open document", "",
            "Documents (*.pdf *.doc *.docx *.rtf *.odt);;PDF files (*.pdf);;Word (*.doc *.docx *.rtf *.odt)",
        )
        if path:
            self._open_path(path)

    @guarded("Open file")
    def _open_path(self, path: str):
        log.info("opening: %s", path)
        ext = os.path.splitext(path)[1].lower()
        if ext != ".pdf" and not docimport.needs_conversion(path):
            self.info("Unsupported file", f"Can't open {os.path.basename(path)}.", error=True)
            return
        open_path = path

        # Word documents: convert to PDF via the installed Word, then open that.
        if docimport.needs_conversion(path):
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                open_path = docimport.convert_to_pdf(path)
            except Exception as e:
                QApplication.restoreOverrideCursor()
                self.info("Could not open", str(e), error=True)
                return
            finally:
                QApplication.restoreOverrideCursor()

        try:
            doc = pdfcore.PdfDocument(open_path)
        except Exception as e:
            self.info("Could not open", str(e), error=True)
            return

        if doc.locked and not self._prompt_password(doc):
            self.info("Locked", "This PDF needs a password to open.", error=True)
            return

        if hasattr(self, "_ocr_timer"):
            self._ocr_timer.stop()
        # Open in a NEW tab, keeping any already-open documents.
        self._stash_active()
        s = dict(doc=doc, source_path=path,
                 save_target=path if ext == ".pdf" else None,   # .doc has no pdf target
                 detected=set(range(doc.page_count)) if doc.is_studio else set(),
                 undo=[], redo=[], saved_snapshot=None, page_index=0, zoom=1.5, view_mode="single")
        self.sessions.append(s)
        self._switching = True
        self.editor.tabs.addTab(self._tab_title(path))
        self.editor.tabs.setCurrentIndex(len(self.sessions) - 1)
        self.editor.tabs.show()
        self._switching = False
        self._load_session(len(self.sessions) - 1)   # loads view + auto-detects page 1

        self.switchTo(self.editor)
        self._auto_ocr(0)             # auto-recognise text on page 1 (instant)
        self._start_bg_ocr()          # OCR the rest in the background
        if ext == ".pdf":
            self._add_recent(path)
        self._mark_saved()            # baseline for dirty-tracking
        n = len(self.doc.fields.get(0, []))
        if self.doc.is_studio:
            kind = "editable PDF Studio document"
        else:
            kind = "AcroForm" if self.doc.has_form else "auto-detected"
        self.info("Opened",
                  f"{os.path.basename(path)} — {self.doc.page_count} page(s); "
                  f"{n} field(s) on page 1 ({kind}).")

    # -- drag & drop --------------------------------------------------------
    @staticmethod
    def _dropped_file(mime):
        """Return the first supported local file path from a drag's mime data."""
        if not mime.hasUrls():
            return None
        exts = (".pdf", ".doc", ".docx", ".rtf", ".odt")
        for url in mime.urls():
            if url.isLocalFile():
                p = url.toLocalFile()
                if os.path.splitext(p)[1].lower() in exts:
                    return p
        return None

    def dragEnterEvent(self, e):
        if self._dropped_file(e.mimeData()):
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if self._dropped_file(e.mimeData()):
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        path = self._dropped_file(e.mimeData())
        if path:
            e.acceptProposedAction()
            self._open_path(path)
        else:
            super().dropEvent(e)

    def _auto_ocr(self, index: int):
        """Recognise the page's text in the background-ish (brief wait) and fill
        the OCR panel. Stays on the editor; never blocks form filling."""
        if self.doc is None or self.doc.locked:
            return
        if index in self.doc.ocr_text:                 # restored from the model — no re-OCR
            self.ocr.text.setPlainText(self.doc.ocr_text[index])
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            scan = self._is_scanned(index)
            res = ocrengine.recognize(self.doc.render_pil(index, 2.0),
                                      enhance_scan=scan, auto_rotate=scan)
            self.ocr.text.setPlainText(res.text)
            self.doc.ocr_text[index] = res.text
        except ocrengine.OcrUnavailable:
            pass
        except Exception:
            pass
        finally:
            QApplication.restoreOverrideCursor()

    def _is_scanned(self, index: int) -> bool:
        """A page with no extractable text is a scan/photo -> enhance before OCR."""
        try:
            return not self.doc.doc[index].get_text().strip()
        except Exception:
            return False

    def _start_bg_ocr(self):
        """OCR remaining pages incrementally on a timer (winocr isn't thread-safe).
        One page per tick keeps the UI usable while building the text cache."""
        if not hasattr(self, "_ocr_timer"):
            self._ocr_timer = QTimer(self)
            self._ocr_timer.timeout.connect(self._ocr_tick)
        if self.doc is None:
            return
        self._ocr_pending = [p for p in range(self.doc.page_count) if p not in self.doc.ocr_text]
        if self._ocr_pending:
            self._ocr_timer.start(350)

    def _ocr_tick(self):
        if self.doc is None or not getattr(self, "_ocr_pending", None):
            self._ocr_timer.stop()
            return
        p = self._ocr_pending.pop(0)
        try:
            self.doc.ocr_text[p] = ocrengine.recognize(
                self.doc.render_pil(p, 2.0), enhance_scan=self._is_scanned(p)).text
        except Exception:
            self.doc.ocr_text[p] = ""
        if not self._ocr_pending:
            self._ocr_timer.stop()
            log.info("background OCR complete (%d pages cached)", len(self.doc.ocr_text))

    def _prompt_password(self, doc) -> bool:
        from PySide6.QtWidgets import QInputDialog, QLineEdit
        for _ in range(3):
            pw, ok = QInputDialog.getText(
                self, "Password required", "Enter PDF password:", QLineEdit.EchoMode.Password
            )
            if not ok:
                return False
            if doc.unlock(pw):
                return True
        return False

    def prev_page(self):
        if self.doc:
            self._show_page(self.editor.view.page_index - 1)

    def next_page(self):
        if self.doc:
            self._show_page(self.editor.view.page_index + 1)

    def zoom_in(self):
        if self.doc:
            self.editor.view.set_zoom(self.editor.view.zoom + 0.25)  # re-renders fields
            self._update_label()

    def zoom_out(self):
        if self.doc:
            self.editor.view.set_zoom(self.editor.view.zoom - 0.25)  # re-renders fields
            self._update_label()

    @guarded("Add text box")
    def start_textbox(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True)
            return
        self.push_undo()
        self.editor.view.begin_add_text()
        self.info("Add text box", "Drag a rectangle on the page, then type into it.")

    def _open_logs(self):
        try:
            os.startfile(logconf.log_dir())   # noqa: S606 (Windows: open folder)
        except Exception:
            self.info("Logs", logconf.log_dir())

    @guarded("Detect fields")
    def detect_fields(self):
        """Manual re-scan of the current page (detection also runs on open)."""
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True)
            return
        idx = self.editor.view.page_index
        self.push_undo()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            n = self._run_detect(idx)
        finally:
            QApplication.restoreOverrideCursor()
        self.editor.view.set_page_fields(self.doc.fields[idx])
        self._update_label()
        self.info("Detected", f"Found {n} fillable region(s) on this page.")

    def clear_fields(self):
        if self.doc is None:
            return
        idx = self.editor.view.page_index
        self.push_undo()
        kept = [f for f in self._fields_for_page(idx) if f.source == "acro"]
        for f in kept:
            f.value = ""
        self.doc.fields[idx] = kept
        self.editor.view.set_page_fields(kept)
        self._update_label()
        self.info("Cleared", "Detected fields removed and AcroForm values reset.")

    def run_ocr_page(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True)
            return
        idx = self.editor.view.page_index
        self._ocr_pages([idx], header=f"--- Page {idx + 1} ---\n")

    def run_ocr_all(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True)
            return
        self._ocr_pages(range(self.doc.page_count), header=None)

    def _ocr_pages(self, indices, header):
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        chunks = []
        try:
            for i in indices:
                img = self.doc.render_pil(i, 2.0)
                scan = self._is_scanned(i)
                res = ocrengine.recognize(img, enhance_scan=scan, auto_rotate=scan)
                if header is None:
                    chunks.append(f"--- Page {i + 1} ---\n{res.text}")
                else:
                    chunks.append(res.text)
        except ocrengine.OcrUnavailable as e:
            QApplication.restoreOverrideCursor()
            self.info("OCR unavailable", str(e), error=True)
            return
        finally:
            QApplication.restoreOverrideCursor()
        self.ocr.text.setPlainText("\n\n".join(chunks))
        self.switchTo(self.ocr)
        self.info("OCR complete", f"Recognized text from {len(chunks)} page(s).")

    def _write(self, path: str, mode: str):
        self.doc.save_document(path, mode=mode, ink=self.ink_rgb,
                               font=self.font_key, font_size=self.font_size)

    def _default_name(self, suffix=""):
        src = self.save_target or self.source_path or self.doc.path
        base = os.path.splitext(os.path.basename(src))[0]
        d = os.path.dirname(src) if (self.save_target or self.source_path) else os.path.expanduser("~")
        return os.path.join(d, f"{base}{suffix}.pdf")

    @guarded("Save")
    def save_pdf(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        if not self.save_target:
            self.save_as()
            return
        self._write(self.save_target, "save")
        self._mark_saved()
        self.info("Saved", os.path.basename(self.save_target))

    @guarded("Save As")
    def save_as(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        path, _ = QFileDialog.getSaveFileName(self, "Save As", self._default_name(), "PDF files (*.pdf)")
        if not path:
            return
        self._write(path, "save")
        self.save_target = path
        self._add_recent(path)
        self._mark_saved()
        self.info("Saved", os.path.basename(path))

    @guarded("Save a copy")
    def save_copy(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        path, _ = QFileDialog.getSaveFileName(self, "Save Copy", self._default_name(" copy"), "PDF files (*.pdf)")
        if path:
            self._write(path, "save")   # copy on disk; current document unchanged
            self.info("Copy saved", os.path.basename(path))

    @guarded("Export flattened PDF")
    def export_flattened(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        path, _ = QFileDialog.getSaveFileName(self, "Export Flattened PDF",
                                              self._default_name("_flattened"), "PDF files (*.pdf)")
        if path:
            self._write(path, "flatten")
            self.info("Exported", f"{os.path.basename(path)} (not editable)")

    @guarded("Export signed PDF")
    def export_signed(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        path, _ = QFileDialog.getSaveFileName(self, "Export Signed PDF",
                                              self._default_name("_signed"), "PDF files (*.pdf)")
        if path:
            self._write(path, "sign")
            self.info("Exported", f"{os.path.basename(path)} (for submission)")

    @guarded("Digitally sign")
    def digitally_sign(self):
        """Flatten, then embed a cryptographic (PKCS#7) signature from the user's
        certificate. The cert password is entered by the user, never stored."""
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        if not signing.available():
            self.info("Digital signing unavailable",
                      "pyHanko is not bundled in this build.", error=True); return
        pfx, _ = QFileDialog.getOpenFileName(self, "Choose your certificate", "",
                                             "Certificates (*.pfx *.p12)")
        if not pfx:
            return
        from PySide6.QtWidgets import QInputDialog, QLineEdit
        pw, ok = QInputDialog.getText(self, "Certificate password",
                                      "Password for the certificate:", QLineEdit.EchoMode.Password)
        if not ok:
            return
        out, _ = QFileDialog.getSaveFileName(self, "Save digitally signed PDF",
                                             self._default_name("_signed"), "PDF files (*.pdf)")
        if not out:
            return
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(prefix="pdfstudio_sign_"), "flat.pdf")
        self._write(tmp, "flatten")          # sign the flattened submission copy
        try:
            signing.sign(tmp, out, pfx, pw, reason="Signed with PDF Studio")
        except Exception as e:
            self.info("Signing failed", str(e), error=True); return
        self.info("Digitally signed", f"{os.path.basename(out)} — cryptographically signed.")

    # -- insert / markup / align (Phase 2-3) -------------------------------
    @guarded("Insert image")
    def insert_image(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        path, _ = QFileDialog.getOpenFileName(self, "Insert image", "",
                                              "Images (*.png *.jpg *.jpeg *.bmp *.webp *.gif)")
        if not path:
            return
        with open(path, "rb") as fh:
            data = fh.read()
        self.push_undo()
        self.editor.view.add_image(data)
        self.info("Image inserted", "Drag to move, corner to resize, R to rotate, D to duplicate, Del to remove.")

    def _begin_markup(self, tool: str):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        self.push_undo()
        self.editor.view.begin_tool(tool)
        self.info(tool.capitalize(), "Drag on the page to draw. Click it then Del to remove.")

    def pick_annot_color(self):
        from PySide6.QtWidgets import QColorDialog
        c = QColorDialog.getColor(QColor.fromRgbF(*self.editor.view.annot_color), self, "Markup colour")
        if c.isValid():
            self.editor.view.annot_color = (c.redF(), c.greenF(), c.blueF())

    def set_align(self, align: str):
        if self.doc is None:
            return
        if not self._apply_fmt(align=align):
            self.editor.view.set_text_align(align)

    # -- search (Phase 3) ---------------------------------------------------
    @guarded("Find")
    def find(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        from PySide6.QtWidgets import QInputDialog
        q, ok = QInputDialog.getText(self, "Find", "Find text:")
        if not ok or not q.strip():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self._matches = self.doc.search(q)
        finally:
            QApplication.restoreOverrideCursor()
        self._match_i = -1
        if not self._matches:
            self.info("Find", f"No matches for “{q}”.")
            return
        self.find_next()

    def _run_search(self, q: str):
        """Return [(page, snippet, [rects_pt]), ...] across native text + OCR cache."""
        out = []
        if not q.strip() or self.doc is None:
            return out
        ql = q.lower()
        for p in range(self.doc.page_count):
            page = self.doc.doc[p]
            native = page.get_text() or ""
            text = native if native.strip() else self.doc.ocr_text.get(p, "")
            i = text.lower().find(ql)
            if i < 0:
                continue
            snippet = " ".join(text[max(0, i - 25):i + 50].split())
            rects = [(r.x0, r.y0, r.x1, r.y1) for r in (page.search_for(q) or [])]
            out.append((p, snippet, rects))
        return out

    @guarded("Search")
    def run_search_panel(self, query: str):
        from PySide6.QtWidgets import QListWidgetItem
        self.finder.results.clear()
        if not query.strip():
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            results = self._run_search(query)
        finally:
            QApplication.restoreOverrideCursor()
        self.finder.count.setText(f"{len(results)} page(s) with matches")
        for page, snippet, rects in results:
            it = QListWidgetItem(f"Page {page + 1}:  …{snippet}…")
            it.setData(Qt.ItemDataRole.UserRole, (page, rects))
            self.finder.results.addItem(it)

    def _jump_result(self, item):
        page, rects = item.data(Qt.ItemDataRole.UserRole)
        self._show_page(page)
        if rects:
            self.editor.view.set_search_highlights(rects)
        self.switchTo(self.editor)

    def find_next(self):
        self._goto_match(1)

    def find_prev(self):
        self._goto_match(-1)

    def _goto_match(self, step: int):
        matches = getattr(self, "_matches", [])
        if not matches:
            return
        self._match_i = (getattr(self, "_match_i", -1) + step) % len(matches)
        page, rect = matches[self._match_i]
        if page != self.editor.view.page_index:
            self._show_page(page)
        same = [r for (p, r) in matches if p == self.editor.view.page_index]
        self.editor.view.set_search_highlights(same)
        self.editor.page_label.setText(
            f"Match {self._match_i + 1}/{len(matches)} on page {page + 1}")

    # -- page operations (Phase 4) -----------------------------------------
    def _reload_after_pageop(self, target_page: int):
        self.editor.view.document = self.doc
        self._populate_thumbs()
        self._show_page(max(0, min(target_page, self.doc.page_count - 1)))

    @guarded("Rotate page")
    def rotate_page(self):
        if self.doc is None:
            return
        idx = self.editor.view.page_index
        self.push_undo(); self.doc.rotate_page(idx, 90); self._reload_after_pageop(idx)

    @guarded("Delete page")
    def delete_page(self):
        if self.doc is None or self.doc.page_count <= 1:
            return
        idx = self.editor.view.page_index
        self.push_undo(); self.doc.delete_page(idx); self._reload_after_pageop(idx)

    @guarded("Insert blank page")
    def insert_blank_page(self):
        if self.doc is None:
            return
        idx = self.editor.view.page_index + 1
        self.push_undo(); self.doc.insert_blank(idx); self._reload_after_pageop(idx)

    @guarded("Move page")
    def move_page_up(self):
        if self.doc:
            idx = self.editor.view.page_index
            if idx > 0:
                self.push_undo(); self.doc.move_page(idx, idx - 1); self._reload_after_pageop(idx - 1)

    @guarded("Move page")
    def move_page_down(self):
        if self.doc:
            idx = self.editor.view.page_index
            if idx < self.doc.page_count - 1:
                self.push_undo(); self.doc.move_page(idx, idx + 1); self._reload_after_pageop(idx + 1)

    @guarded("Extract page")
    def extract_page(self):
        if self.doc is None:
            return
        idx = self.editor.view.page_index
        path, _ = QFileDialog.getSaveFileName(self, "Extract current page",
                                              self._default_name(f"_p{idx+1}"), "PDF files (*.pdf)")
        if path:
            self.doc.extract_pages([idx], path)
            self.info("Extracted", os.path.basename(path))

    @guarded("Merge PDF")
    def merge_pdf(self):
        if self.doc is None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Append a PDF", "", "PDF files (*.pdf)")
        if path:
            self.push_undo(); self.doc.merge_pdf(path); self._reload_after_pageop(self.editor.view.page_index)
            self.info("Merged", f"Appended {os.path.basename(path)}.")

    # -- view / print (Phase 4) --------------------------------------------
    def set_view_mode(self, mode: str):
        if self.doc:
            self.editor.view.set_view_mode(mode); self._update_label()

    def _on_page_changed(self, idx: int):
        self._update_label()
        self.editor.thumbs.setCurrentRow(idx)

    # -- thumbnails ---------------------------------------------------------
    def _populate_thumbs(self):
        from PySide6.QtWidgets import QListWidgetItem
        self.editor.thumbs.clear()
        if self.doc is None:
            return
        for p in range(self.doc.page_count):
            self.editor.thumbs.addItem(QListWidgetItem(f"  {p + 1}"))
        self._thumb_pending = list(range(self.doc.page_count))
        if not hasattr(self, "_thumb_timer"):
            self._thumb_timer = QTimer(self)
            self._thumb_timer.timeout.connect(self._thumb_tick)
        self._thumb_timer.start(60)

    def _thumb_tick(self):
        if self.doc is None or not getattr(self, "_thumb_pending", None):
            if hasattr(self, "_thumb_timer"):
                self._thumb_timer.stop()
            return
        p = self._thumb_pending.pop(0)
        try:
            samples, w, h, stride = self.doc.render(p, 0.16)
            from PySide6.QtGui import QImage
            img = QImage(samples, w, h, stride, QImage.Format.Format_RGB888).copy()
            item = self.editor.thumbs.item(p)
            if item is not None:
                item.setIcon(QIcon(QPixmap.fromImage(img)))
        except Exception:
            log.exception("thumbnail render failed for page %s", p)
        if not self._thumb_pending:
            self._thumb_timer.stop()

    def _thumb_clicked(self, item):
        self._show_page(self.editor.thumbs.row(item))

    def fit_width(self):
        if self.doc: self.editor.view.fit_width(); self._update_label()

    def fit_page(self):
        if self.doc: self.editor.view.fit_page(); self._update_label()

    def actual_size(self):
        if self.doc: self.editor.view.actual_size(); self._update_label()

    @guarded("Print")
    def print_doc(self):
        if self.doc is None:
            return
        import tempfile
        import fitz
        from PySide6.QtPrintSupport import QPrinter, QPrintDialog
        from PySide6.QtGui import QPainter as _QP, QImage as _QI
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        if QPrintDialog(printer, self).exec() != QPrintDialog.DialogCode.Accepted:
            return
        tmp = os.path.join(tempfile.mkdtemp(prefix="pdfstudio_print_"), "print.pdf")
        self._write(tmp, "flatten")
        pd = fitz.open(tmp)
        painter = _QP(printer)
        try:
            for i in range(pd.page_count):
                if i > 0:
                    printer.newPage()
                pix = pd[i].get_pixmap(matrix=fitz.Matrix(2, 2))
                img = _QI(pix.samples, pix.width, pix.height, pix.stride, _QI.Format.Format_RGB888).copy()
                vp = painter.viewport()
                scaled = img.scaled(vp.size(), Qt.AspectRatioMode.KeepAspectRatio,
                                    Qt.TransformationMode.SmoothTransformation)
                painter.drawImage(0, 0, scaled)
        finally:
            painter.end(); pd.close()
        self.info("Printed", f"Sent {pd.page_count} page(s) to the printer.")

    # -- dirty / recent / recovery / undo ----------------------------------
    def _mark_saved(self):
        self._saved_snapshot = docmodel.snapshot(self.doc) if self.doc else None
        self._clear_recovery()
        self._refresh_title()

    def _refresh_title(self):
        if self.doc is None:
            self.setWindowTitle("PDF Studio — OCR & Forms")
            return
        name = os.path.basename(self.save_target or self.source_path or "Untitled.pdf")
        star = " *" if self._is_dirty() else ""
        self.setWindowTitle(f"{name}{star} — PDF Studio")
        self._refresh_tab_titles()

    def _maybe_discard(self) -> bool:
        """Prompt to save when there are unsaved changes. Return False to cancel."""
        if not self._is_dirty():
            return True
        m = QMessageBox(self)
        m.setWindowTitle("PDF Studio")
        m.setText(f"Save changes to {os.path.basename(self.save_target or self.source_path or 'this document')}?")
        m.setStandardButtons(QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
                             | QMessageBox.StandardButton.Cancel)
        m.setDefaultButton(QMessageBox.StandardButton.Save)
        r = m.exec()
        if r == QMessageBox.StandardButton.Save:
            self.save_pdf()
            return not self._is_dirty()
        return r == QMessageBox.StandardButton.Discard

    def _is_dirty(self) -> bool:
        if self.doc is None:
            return False
        try:
            return docmodel.snapshot(self.doc) != self._saved_snapshot
        except Exception:
            return True

    def _add_recent(self, path: str):
        recent = self._recent()
        recent = [p for p in recent if os.path.normcase(p) != os.path.normcase(path)]
        recent.insert(0, path)
        self._settings.setValue("recent", recent[:10])

    def _recent(self) -> list:
        r = self._settings.value("recent", []) or []
        return list(r) if isinstance(r, (list, tuple)) else [r]

    def _populate_recent(self, menu):
        menu.clear()
        items = [p for p in self._recent() if os.path.exists(p)]
        if not items:
            a = Action("(no recent files)", menu); a.setEnabled(False); menu.addAction(a)
            return
        for p in items:
            menu.addAction(Action(os.path.basename(p), menu,
                                  triggered=lambda _=False, pp=p: self._open_path(pp)))

    def push_undo(self):
        if self.doc is not None:
            self._undo.append(docmodel.snapshot(self.doc))
            self._undo = self._undo[-50:]
            self._redo.clear()

    def undo(self):
        if self.doc is None or not self._undo:
            return
        self._redo.append(docmodel.snapshot(self.doc))
        docmodel.restore(self.doc, self._undo.pop())
        self._detected = set(self.doc.fields.keys())
        self._refresh_current()

    def redo(self):
        if self.doc is None or not self._redo:
            return
        self._undo.append(docmodel.snapshot(self.doc))
        docmodel.restore(self.doc, self._redo.pop())
        self._detected = set(self.doc.fields.keys())
        self._refresh_current()

    def _refresh_current(self):
        self.editor.view.render_page()
        self._update_label()

    def _recovery_dir(self) -> str:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "PDF Studio", "recovery")
        os.makedirs(d, exist_ok=True)
        return d

    def _setup_autosave(self):
        self._autosave_timer = QTimer(self)
        self._autosave_timer.timeout.connect(self._autosave)
        self._autosave_timer.start(60000)

    def _autosave(self):
        if self.doc is None or not self._is_dirty():
            return
        try:
            self._write(os.path.join(self._recovery_dir(), "autosave.pdf"), "save")
            with open(os.path.join(self._recovery_dir(), "autosave.txt"), "w", encoding="utf-8") as fh:
                fh.write(self.save_target or self.source_path or "")
            log.info("autosaved recovery copy")
        except Exception:
            log.exception("autosave failed")

    def _clear_recovery(self):
        for n in ("autosave.pdf", "autosave.txt"):
            try:
                os.remove(os.path.join(self._recovery_dir(), n))
            except Exception:
                pass

    def _check_recovery(self):
        p = os.path.join(self._recovery_dir(), "autosave.pdf")
        if not os.path.exists(p):
            return
        r = QMessageBox.question(self, "PDF Studio",
                                 "Recover the unsaved document from your last session?",
                                 QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self._open_path(p)
            try:
                meta = open(os.path.join(self._recovery_dir(), "autosave.txt"), encoding="utf-8").read().strip()
                self.save_target = meta if meta.lower().endswith(".pdf") else None
            except Exception:
                pass
        else:
            self._clear_recovery()

    def _session_dirty(self, s) -> bool:
        try:
            return docmodel.snapshot(s["doc"]) != s["saved_snapshot"]
        except Exception:
            return True

    def closeEvent(self, e):
        self._stash_active()
        # Prompt for each tab that has unsaved changes.
        for i, s in enumerate(self.sessions):
            if self._session_dirty(s):
                self.editor.tabs.setCurrentIndex(i)   # activate it so Save targets it
                if not self._maybe_discard():
                    e.ignore(); return
        self._clear_recovery()
        try:
            self._autosave_timer.stop()
        except Exception:
            pass
        super().closeEvent(e)

    def _apply_fmt(self, **attrs) -> bool:
        """Apply formatting attributes to the focused field; True if one existed."""
        f = self.editor.view.current_field()
        if f is None:
            return False
        self.push_undo()
        for k, v in attrs.items():
            setattr(f, k, v)
        if not self.editor.view.restyle_focused():
            self.editor.view.set_page_fields(self.doc.fields.get(self.editor.view.page_index, []))
        return True

    def set_ink(self, name: str):
        self.ink_name = name
        self.ink_rgb = INK_COLORS[name]
        for n, act in self.color_actions.items():
            act.setChecked(n == name)
        if not self._apply_fmt(color=self.ink_rgb):
            self.editor.view.set_ink(_hex(self.ink_rgb))

    def set_font_family(self, label: str):
        key, qt_family, bold = FONTS.get(label, FONTS["Helvetica"])
        self.font_key = key
        self._font_qt = (qt_family, bold)
        fam = fontmatch._GLOBAL_FAMILY.get(key, "helv")
        if not self._apply_fmt(font=fam, bold=(key in fontmatch._BOLD_KEYS)):
            self.editor.view.set_font(qt_family, bold, self.font_size)

    def set_font_size(self, text: str):
        self.font_size = None if text == "Auto" else float(text)
        qt_family, bold = getattr(self, "_font_qt", ("Arial", False))
        if not self._apply_fmt(size=self.font_size):
            self.editor.view.set_font(qt_family, bold, self.font_size)

    # -- rich text + format painter (fidelity) -----------------------------
    def toggle_bold(self):
        f = self.editor.view.current_field()
        if f:
            self._apply_fmt(bold=not f.bold)

    def toggle_italic(self):
        f = self.editor.view.current_field()
        if f:
            self._apply_fmt(italic=not f.italic)

    def toggle_underline(self):
        f = self.editor.view.current_field()
        if f:
            self._apply_fmt(underline=not f.underline)

    def copy_format(self):
        f = self.editor.view.current_field()
        if f is None:
            self.info("Format painter", "Click into a field first, then copy its format.", error=True)
            return
        self._fmt_clip = dict(font=f.font, size=f.size, bold=f.bold, italic=f.italic,
                              underline=f.underline, align=f.align, color=f.color)
        self.info("Format copied", "Now click another field and choose Paste formatting.")

    def paste_format(self):
        if not self._fmt_clip:
            self.info("Format painter", "Copy a field's format first.", error=True)
            return
        if not self._apply_fmt(**self._fmt_clip):
            self.info("Format painter", "Click into a destination field first.", error=True)

    def toggle_match_original(self):
        self.match_original = not self.match_original
        self.info("Match Original Form",
                  "On — new fields match the form's fonts." if self.match_original
                  else "Off — fields use your chosen font.")

    # -- autofill profile ---------------------------------------------------
    def _profile(self) -> dict:
        import json
        raw = self._settings.value("profile", "")
        try:
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def edit_profile(self):
        from PySide6.QtWidgets import QDialog, QFormLayout, QDialogButtonBox, QLineEdit as QLE
        prof = self._profile()
        dlg = QDialog(self); dlg.setWindowTitle("Autofill profile")
        form = QFormLayout(dlg)
        form.addRow(BodyLabel("Stored once, reused to auto-fill matching fields."))
        edits = {}
        for key, label in PROFILE_KEYS:
            e = QLE(prof.get(key, "")); edits[key] = e; form.addRow(label + ":", e)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(dlg.accept); bb.rejected.connect(dlg.reject)
        form.addRow(bb)
        if dlg.exec():
            import json
            data = {k: e.text().strip() for k, e in edits.items() if e.text().strip()}
            self._settings.setValue("profile", json.dumps(data))
            self.info("Profile saved", f"{len(data)} field(s) stored.")

    @staticmethod
    def _match_profile_key(label: str):
        l = label
        rules = [
            ("full_name", lambda l: "full name" in l or "applicant name" in l
             or "name of applicant" in l or l.strip(" :*") == "name"),
            ("first_name", lambda l: "first name" in l or "given name" in l),
            ("last_name", lambda l: "last name" in l or "surname" in l or "family name" in l),
            ("email", lambda l: "email" in l or "e-mail" in l),
            ("phone", lambda l: any(k in l for k in ("phone", "mobile", "contact", "cell", "tel"))),
            ("dob", lambda l: any(k in l for k in ("date of birth", "dob", "d.o.b"))),
            ("pan", lambda l: "pan" in l),
            ("ifsc", lambda l: "ifsc" in l),
            ("account", lambda l: any(k in l for k in ("account no", "account number", "a/c"))),
            ("address", lambda l: "address" in l),
            ("city", lambda l: "city" in l or "town" in l),
            ("state", lambda l: "state" in l or "province" in l),
            ("zip", lambda l: any(k in l for k in ("zip", "pin code", "pincode", "postal"))),
        ]
        for key, fn in rules:
            if fn(l):
                return key
        return None

    @guarded("Autofill form")
    def autofill_form(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True); return
        prof = self._profile()
        if not prof:
            self.info("Set up a profile", "Add your details in Autofill → Edit profile first.")
            self.edit_profile(); return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        filled = 0
        try:
            self.push_undo()
            for p in range(self.doc.page_count):
                self._ensure_fields(p)
                page = self.doc.doc[p]
                for f in self.doc.fields.get(p, []):
                    if f.kind not in ("text", "multiline") or f.value.strip():
                        continue
                    key = self._match_profile_key(fontmatch.label_text(page, f.rect))
                    if key and prof.get(key):
                        f.value = prof[key]; filled += 1
        finally:
            QApplication.restoreOverrideCursor()
        self._refresh_current()
        self.info("Autofill", f"Filled {filled} field(s) from your profile.")

    @guarded("Signature")
    def add_signature(self):
        if self.doc is None:
            self.info("No document", "Open a PDF first.", error=True)
            return
        dlg = SignatureDialog(QColor.fromRgbF(*self.ink_rgb), self, recents=self._recent_signatures())
        if dlg.exec() and dlg.get_png():
            png = dlg.get_png()
            self.push_undo()
            self.editor.view.add_signature(png)
            self._remember_signature(png)
            self.info("Signature added", "Drag to position, corner to resize, R rotate, D duplicate, Del remove.")

    def _recent_signatures(self) -> list:
        import base64
        raw = self._settings.value("recent_sigs", []) or []
        out = []
        for s in (raw if isinstance(raw, (list, tuple)) else [raw]):
            try:
                out.append(base64.b64decode(s))
            except Exception:
                pass
        return out

    def _remember_signature(self, png: bytes):
        import base64
        b = base64.b64encode(png).decode("ascii")
        raw = list(self._settings.value("recent_sigs", []) or [])
        raw = [s for s in raw if s != b]
        raw.insert(0, b)
        self._settings.setValue("recent_sigs", raw[:6])

    def reset_zoom(self):
        if self.doc:
            self.editor.view.set_zoom(1.5)
            self._update_label()

    def _add_shortcuts(self):
        """Window-level keyboard shortcuts (work regardless of which tab is shown)."""
        def sc(seq, slot):
            s = QShortcut(QKeySequence(seq), self)
            s.setContext(Qt.ShortcutContext.WindowShortcut)
            s.activated.connect(slot)
            return s

        sc(QKeySequence.StandardKey.Open, self.open_pdf)
        sc(QKeySequence.StandardKey.Save, self.save_pdf)
        sc("Ctrl+Shift+S", self.save_as)
        sc("Ctrl+Z", self.undo)
        sc("Ctrl+Y", self.redo)
        sc("Ctrl+D", self.add_signature)       # signature
        sc("Ctrl+I", self.toggle_italic)       # italic (rich text)
        sc("Ctrl+Shift+I", self.insert_image)  # insert image
        sc("Ctrl+T", self.start_textbox)
        sc("Ctrl+B", self.toggle_bold)
        sc("Ctrl+U", self.toggle_underline)
        sc("Ctrl+R", self.run_ocr_page)
        sc(QKeySequence.StandardKey.Find, self.find)   # Ctrl+F
        sc("F3", self.find_next)
        sc("Shift+F3", self.find_prev)
        sc(QKeySequence.StandardKey.Print, self.print_doc)   # Ctrl+P
        sc("Ctrl+Shift+D", self.detect_fields)
        sc(QKeySequence.StandardKey.ZoomIn, self.zoom_in)
        sc("Ctrl+=", self.zoom_in)        # common alias for zoom-in
        sc(QKeySequence.StandardKey.ZoomOut, self.zoom_out)
        sc("Ctrl+0", self.reset_zoom)
        sc("Ctrl+PgDown", self.next_page)
        sc("Ctrl+PgUp", self.prev_page)

    def toggle_theme(self):
        setTheme(Theme.LIGHT if isDarkTheme() else Theme.DARK)
