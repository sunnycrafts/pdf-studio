"""Page viewer: renders a PDF page and floats editable widgets over each field,
plus movable/resizable signature images.

Rendering is done at the current display zoom, so a field at PDF point (x, y)
lands at pixel (x * zoom, y * zoom) - no second coordinate system to juggle.
Each overlay widget writes straight back into its Field object; each signature
item writes its rectangle (in PDF points) back into the document's signature
list, so the model the rest of the app saves from is always current.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QRectF, QPointF, QRect
from PySide6.QtGui import (
    QImage, QPixmap, QPainter, QPen, QColor, QFont, QTransform, QPolygonF, QBrush,
)
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsItem, QLineEdit, QTextEdit,
    QCheckBox, QComboBox, QRubberBand,
)
import math

from pdfcore import (
    Field, KIND_TEXT, KIND_MULTILINE, KIND_CHECKBOX, KIND_RADIO, KIND_CHOICE, KIND_COMB,
)


def _field_qss(color_hex: str) -> str:
    return f"""
    QLineEdit, QTextEdit, QComboBox {{
        background: rgba(120, 180, 255, 30);
        border: 1px solid rgba(0, 120, 215, 160);
        border-radius: 3px;
        padding: 0 3px;
        color: {color_hex};
        selection-background-color: #0078d4;
    }}
    QLineEdit:focus, QTextEdit:focus, QComboBox:focus {{
        background: rgba(255, 255, 255, 235);
        border: 1px solid #0078d4;
    }}
    QCheckBox {{ background: transparent; }}
    """


class PdfView(QGraphicsView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(Qt.GlobalColor.transparent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.setAcceptDrops(True)         # accept files dropped onto the page area
        self.on_file_dropped = None       # callback(path) set by the main window
        self.on_field_created = None      # callback() after a manual text box / annotation
        self._tool = None                 # None|'text'|'pen'|'rect'|'ellipse'|'line'|'arrow'|'highlight'
        self.annot_color = (0.85, 0.1, 0.1)
        self.annot_width = 1.6
        self.text_align = "left"
        self._last_field = None            # most recently focused field overlay
        self._rubber = None
        self._rb_origin = None
        self._pen_pts = []                # current freehand stroke (scene px)
        self.document = None
        self.page_index = 0
        self.zoom = 1.5
        self.view_mode = "single"         # "single" | "continuous" | "two"
        self._layout = []                 # [{page, ox, oy, w, h}] in scene px
        self.on_page_changed = None       # callback(index) as you scroll
        self.verticalScrollBar().valueChanged.connect(self._sync_current_page)
        self.ink_hex = "#111111"          # overlay text colour (matches chosen ink)
        self.font_family = "Arial"        # overlay font (matches chosen base-14 font)
        self.font_bold = False
        self.font_size_pt = None          # None = auto-fit
        self._proxies = []                # editable field overlays
        self._page_fields = []            # Field objects shown on the current page

    # -- drag & drop --------------------------------------------------------
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls() and self.on_file_dropped:
            for u in e.mimeData().urls():
                if u.isLocalFile():
                    e.acceptProposedAction()
                    self.on_file_dropped(u.toLocalFile())
                    return
        super().dropEvent(e)

    # -- manual text box ----------------------------------------------------
    def begin_add_text(self):
        """Enter 'draw a text box' mode: next drag on the page creates a field."""
        self.begin_tool("text")

    def begin_tool(self, tool: str):
        """Arm a click-drag tool: text box, pen, shape or highlight."""
        if self.document is None:
            return
        self._tool = tool
        self.viewport().setCursor(Qt.CursorShape.CrossCursor)

    def _cancel_tool(self):
        self._tool = None
        if self._rubber:
            self._rubber.hide(); self._rubber = None
        self._rb_origin = None
        self._pen_pts = []
        self.viewport().unsetCursor()

    def _pt_on_page(self, vp_point, page):
        """Convert a viewport point to PDF points relative to `page`'s block."""
        s = self.mapToScene(vp_point)
        b = self._block_for_page(page)
        ox, oy = (b["ox"], b["oy"]) if b else (0.0, 0.0)
        return ((s.x() - ox) / self.zoom, (s.y() - oy) / self.zoom)

    def mousePressEvent(self, e):
        if self._tool and e.button() == Qt.MouseButton.LeftButton:
            self._rb_origin = e.position().toPoint()
            self._tool_page = self._page_at(self.mapToScene(self._rb_origin))[0]
            if self._tool == "pen":
                self._pen_pts = [self._pt_on_page(self._rb_origin, self._tool_page)]
            else:
                self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())
                self._rubber.setGeometry(QRect(self._rb_origin, self._rb_origin))
                self._rubber.show()
            e.accept(); return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._tool == "pen" and self._rb_origin is not None:
            self._pen_pts.append(self._pt_on_page(e.position().toPoint(), self._tool_page))
            self.viewport().update()
            e.accept(); return
        if self._tool and self._rubber is not None:
            self._rubber.setGeometry(QRect(self._rb_origin, e.position().toPoint()).normalized())
            e.accept(); return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._tool and self._rb_origin is not None:
            tool = self._tool
            page = getattr(self, "_tool_page", self.page_index)
            if tool == "pen":
                pts = self._pen_pts[:]
                self._cancel_tool()
                if len(pts) >= 2:
                    self._add_annotation(page, {"type": "pen", "points": pts,
                                                "color": list(self.annot_color), "width": self.annot_width})
            else:
                tl = self._pt_on_page(self._rb_origin, page)
                br = self._pt_on_page(e.position().toPoint(), page)
                self._cancel_tool()
                rect = (min(tl[0], br[0]), min(tl[1], br[1]), max(tl[0], br[0]), max(tl[1], br[1]))
                if tool == "text":
                    self._create_text_field(page, rect)
                elif tool in ("line", "arrow"):
                    self._add_annotation(page, {"type": tool, "rect": [tl[0], tl[1], br[0], br[1]],
                                                "color": list(self.annot_color), "width": self.annot_width})
                elif (rect[2] - rect[0]) > 4 and (rect[3] - rect[1]) > 4:
                    self._add_annotation(page, {"type": tool, "rect": list(rect),
                                                "color": list(self.annot_color), "width": self.annot_width})
            e.accept(); return
        super().mouseReleaseEvent(e)

    def _create_text_field(self, page, rect_pt):
        if self.document is None or (rect_pt[2] - rect_pt[0]) < 4 or (rect_pt[3] - rect_pt[1]) < 4:
            return
        kind = KIND_MULTILINE if (rect_pt[3] - rect_pt[1]) >= 34 else KIND_TEXT
        page_fields = self.document.fields.setdefault(page, [])
        f = Field(name=f"textbox_{len(page_fields)+1}", kind=kind, rect=tuple(rect_pt),
                  page=page, source="detected", align=self.text_align)
        page_fields.append(f)
        self.render_page()
        if self._proxies:
            self._proxies[-1].widget().setFocus()
        if self.on_field_created:
            self.on_field_created()

    def _add_annotation(self, page, ann: dict):
        self.document.annotations.setdefault(page, []).append(ann)
        ox, oy = self._placement_offset(page)
        self._add_annotation_item(ann, page, ox, oy)
        if self.on_field_created:
            self.on_field_created()

    # -- loading / navigation ----------------------------------------------
    def load(self, document):
        self.document = document
        self.page_index = 0
        self._page_fields = []
        self.render_page()

    def goto(self, index: int):
        if not self.document:
            return
        index = max(0, min(index, self.document.page_count - 1))
        self.page_index = index
        if self.view_mode == "single":
            self.render_page()
        else:
            if not self._layout:
                self.render_page()
            self._scroll_to_page(index)

    def next_page(self):
        self.goto(self.page_index + 1)

    def prev_page(self):
        self.goto(self.page_index - 1)

    def set_zoom(self, zoom: float):
        self.zoom = max(0.25, min(6.0, zoom))
        self.render_page()

    def fit_width(self):
        if self.document:
            pw, _ = self.document.page_size(self.page_index)
            self.set_zoom((self.viewport().width() - 24) / max(1, pw))

    def fit_page(self):
        if self.document:
            pw, ph = self.document.page_size(self.page_index)
            self.set_zoom(min((self.viewport().width() - 24) / max(1, pw),
                              (self.viewport().height() - 24) / max(1, ph)))

    def actual_size(self):
        self.set_zoom(96 / 72)        # 1 pt -> 1 px at 96 dpi

    def set_search_highlights(self, rects_pt):
        for it in getattr(self, "_search_items", []):
            if it.scene():
                self._scene.removeItem(it)
        self._search_items = []
        z = self.zoom
        ox, oy = self._placement_offset(self.page_index)
        for (x0, y0, x1, y1) in rects_pt:
            r = self._scene.addRect(QRectF(ox + x0 * z, oy + y0 * z, (x1 - x0) * z, (y1 - y0) * z),
                                    QPen(Qt.PenStyle.NoPen), QBrush(QColor(255, 230, 0, 110)))
            r.setZValue(5)
            self._search_items.append(r)

    def set_ink(self, color_hex: str):
        self.ink_hex = color_hex
        qss = _field_qss(color_hex)
        for p in self._proxies:
            w = p.widget()
            if w is not None:
                w.setStyleSheet(qss)

    def set_font(self, family: str, bold: bool, size_pt):
        self.font_family = family
        self.font_bold = bold
        self.font_size_pt = size_pt
        for p in self._proxies:
            w = p.widget()
            if w is not None:
                w.setFont(self._overlay_font())

    def set_text_align(self, align: str):
        """Apply alignment to the current page's text fields and to new boxes."""
        self.text_align = align
        if self.document:
            for f in self.document.fields.get(self.page_index, []):
                if f.kind in (KIND_TEXT, KIND_MULTILINE):
                    f.align = align
        self.render_page()

    def _overlay_font(self) -> QFont:
        f = QFont(self.font_family)
        f.setBold(self.font_bold)
        if self.font_size_pt:
            # On the page, 1 pt renders as `zoom` pixels, so match it on screen.
            f.setPixelSize(max(6, int(self.font_size_pt * self.zoom)))
        return f

    def _field_font(self, f) -> QFont:
        """A QFont reflecting this field's own font/size/style (fidelity)."""
        import fontmatch
        family = fontmatch.QT_FAMILY.get(f.font, self.font_family) if f.font else self.font_family
        qf = QFont(family)
        qf.setBold(bool(f.bold) or self.font_bold)
        qf.setItalic(bool(f.italic))
        qf.setUnderline(bool(f.underline))
        pt = f.size or self.font_size_pt
        if pt:
            qf.setPixelSize(max(6, int(pt * self.zoom)))
        return qf

    def _field_color_hex(self, f) -> str:
        if f.color is not None:
            return "#%02x%02x%02x" % tuple(int(c * 255) for c in f.color)
        return self.ink_hex

    def eventFilter(self, obj, event):
        from PySide6.QtCore import QEvent
        if not hasattr(obj, "_field"):
            return super().eventFilter(obj, event)
        if event.type() == QEvent.Type.FocusIn:
            self._last_field = obj._field
        elif event.type() == QEvent.Type.KeyPress:
            k = event.key(); shift = bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier)
            if k == Qt.Key.Key_Tab:
                self.focus_sibling(1); return True
            if k == Qt.Key.Key_Backtab:
                self.focus_sibling(-1); return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and obj._field.kind != KIND_MULTILINE:
                self.focus_sibling(-1 if shift else 1); return True
        return super().eventFilter(obj, event)

    def focus_sibling(self, direction: int):
        """Move focus to the next/previous field overlay in reading order."""
        order = sorted(self._proxies, key=lambda p: (round(p.pos().y()), round(p.pos().x())))
        widgets = [p for p in order if p.widget() is not None]
        if not widgets:
            return
        cur = next((i for i, p in enumerate(widgets) if p.widget().hasFocus()), None)
        nxt = widgets[((cur if cur is not None else 0) + direction) % len(widgets)]
        nxt.widget().setFocus()
        self.ensureVisible(nxt)

    def current_field(self):
        """The most recently focused field overlay (survives focus moving to a
        toolbar combo). Returns None if it's no longer on the page."""
        f = self._last_field
        if f is None or self.document is None:
            return None
        if f in self.document.fields.get(f.page, []):
            return f
        self._last_field = None
        return None

    def restyle_focused(self) -> bool:
        """Re-apply font/colour/alignment to the focused overlay in place
        (keeps the cursor). Returns True if a focused overlay was restyled."""
        for p in self._proxies:
            w = p.widget()
            if w is not None and w.hasFocus() and hasattr(w, "_field"):
                fld = w._field
                w.setStyleSheet(_field_qss(self._field_color_hex(fld)))
                if fld.kind not in (KIND_CHECKBOX, KIND_RADIO):
                    w.setFont(self._field_font(fld))
                    amap = {"left": Qt.AlignmentFlag.AlignLeft, "center": Qt.AlignmentFlag.AlignHCenter,
                            "right": Qt.AlignmentFlag.AlignRight}
                    al = amap.get(fld.align, Qt.AlignmentFlag.AlignLeft)
                    if isinstance(w, (QLineEdit, QTextEdit)):
                        w.setAlignment(al if isinstance(w, QLineEdit) else (al | Qt.AlignmentFlag.AlignTop))
                return True
        return False

    # -- rendering (single / continuous / two-page, virtualised) -----------
    def set_view_mode(self, mode: str):
        self.view_mode = mode
        self.render_page()
        if mode != "single":
            self._scroll_to_page(self.page_index)

    GAP = 16

    def _pages_to_show(self):
        if not self.document:
            return []
        if self.view_mode == "single":
            return [self.page_index]
        return list(range(self.document.page_count))

    def _build_layout(self):
        """Compute page block rectangles (scene px) from page sizes, no render."""
        z = self.zoom
        self._layout = []
        pages = self._pages_to_show()
        if self.view_mode == "two":
            y = 0.0; i = 0
            while i < len(pages):
                row = pages[i:i + 2]; x = 0.0; rowh = 0.0
                for p in row:
                    pw, ph = self.document.page_size(p)
                    w, h = pw * z, ph * z
                    self._layout.append({"page": p, "ox": x, "oy": y, "w": w, "h": h})
                    x += w + self.GAP; rowh = max(rowh, h)
                y += rowh + self.GAP; i += 2
        else:
            y = 0.0
            for p in pages:
                pw, ph = self.document.page_size(p)
                w, h = pw * z, ph * z
                self._layout.append({"page": p, "ox": 0.0, "oy": y, "w": w, "h": h})
                y += h + self.GAP
        maxx = max((b["ox"] + b["w"] for b in self._layout), default=1.0)
        maxy = max((b["oy"] + b["h"] for b in self._layout), default=1.0)
        self._scene.setSceneRect(QRectF(0, 0, maxx, maxy))

    def render_page(self):
        self._scene.clear()
        self._proxies.clear()
        self._search_items = []
        self._rendered = {}            # page -> background pixmap item
        if not self.document:
            return
        self._build_layout()
        self._render_visible()

    def _render_visible(self):
        """Render pixmaps + overlays for page blocks near the viewport, lazily.

        We add pages as they scroll into view but do NOT tear them down again:
        removing form-field proxy widgets mid-scroll is unstable. Pages are only
        cleared on a full re-render (zoom / mode / page change)."""
        if not self.document:
            return
        vis = self.mapToScene(self.viewport().rect()).boundingRect()
        vis.adjust(0, -vis.height(), 0, vis.height())   # 1-screen margin
        for b in self._layout:
            if b["page"] in self._rendered:
                continue
            if vis.intersects(QRectF(b["ox"], b["oy"], b["w"], b["h"])):
                self._render_block(b)

    def _render_block(self, b):
        p, ox, oy = b["page"], b["ox"], b["oy"]
        samples, w, h, stride = self.document.render(p, self.zoom)
        img = QImage(samples, w, h, stride, QImage.Format.Format_RGB888).copy()
        pix = self._scene.addPixmap(QPixmap.fromImage(img))
        pix.setPos(ox, oy)
        pix.setData(0, p)
        self._rendered[p] = pix
        for f in self.document.fields.get(p, []):
            self._add_overlay(f, ox, oy, p)
        for ann in self.document.annotations.get(p, []):
            self._add_annotation_item(ann, p, ox, oy)
        for pl in self.document.images.get(p, []):
            self._add_image_item(pl, p, ox, oy)
        for pl in self.document.signatures.get(p, []):
            self._add_signature_item(pl, p, ox, oy)

    def _clear_block(self, page):
        # Remove the page bitmap and any items whose data marks this page.
        for it in list(self._scene.items()):
            if it.data(0) == page:
                self._scene.removeItem(it)
        self._proxies = [pr for pr in self._proxies if pr.data(0) != page]
        self._rendered.pop(page, None)

    def _sync_current_page(self):
        if not self.document or self.view_mode == "single":
            return
        self._render_visible()
        top = self.mapToScene(self.viewport().rect().center()).y()
        for b in self._layout:
            if b["oy"] <= top <= b["oy"] + b["h"]:
                if b["page"] != self.page_index:
                    self.page_index = b["page"]
                    if self.on_page_changed:
                        self.on_page_changed(b["page"])
                break

    def _block_for_page(self, page):
        for b in self._layout:
            if b["page"] == page:
                return b
        return None

    def _scroll_to_page(self, page):
        b = self._block_for_page(page)
        if b:
            sb = self.verticalScrollBar()
            sb.blockSignals(True)
            self.centerOn(b["ox"] + b["w"] / 2, b["oy"] + 20)
            sb.blockSignals(False)
            self._render_visible()

    def _page_at(self, scene_pt):
        """Return (page, ox, oy) for the block containing a scene point."""
        for b in self._layout:
            if (b["ox"] <= scene_pt.x() <= b["ox"] + b["w"]
                    and b["oy"] <= scene_pt.y() <= b["oy"] + b["h"]):
                return b["page"], b["ox"], b["oy"]
        return (self._layout[0]["page"], self._layout[0]["ox"], self._layout[0]["oy"]) if self._layout else (self.page_index, 0, 0)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.view_mode != "single":
            self._render_visible()

    # -- fields -------------------------------------------------------------
    def set_page_fields(self, fields):
        """Re-render the page(s); fields already live in the document model."""
        self.render_page()

    def _add_overlay(self, f, ox=0.0, oy=0.0, page=None):
        x0, y0, x1, y1 = (v * self.zoom for v in f.rect)
        wpx, hpx = max(12, x1 - x0), max(12, y1 - y0)

        if f.kind in (KIND_CHECKBOX, KIND_RADIO):
            widget = QCheckBox()
            widget.setChecked(f.value.strip().lower() in ("1", "true", "yes", "on", "x", "checked"))
            widget.toggled.connect(lambda on, fld=f: _set(fld, "on" if on else ""))
        elif f.kind == KIND_CHOICE:
            widget = QComboBox()
            widget.setEditable(True)
            widget.addItems(f.options)
            widget.setCurrentText(f.value)
            widget.currentTextChanged.connect(lambda t, fld=f: _set(fld, t))
        elif f.kind == KIND_MULTILINE:
            widget = QTextEdit()
            widget.setPlainText(f.value)
            widget.textChanged.connect(lambda fld=f, wdg=widget: _set(fld, wdg.toPlainText()))
        elif f.kind == KIND_COMB:
            widget = QLineEdit()
            widget.setText(f.value)
            widget.setMaxLength(len(f.cells) or 64)   # one character per cell
            widget.setPlaceholderText("·" * len(f.cells))
            widget.textChanged.connect(lambda t, fld=f: _set(fld, t))
        else:  # KIND_TEXT
            widget = QLineEdit()
            widget.setText(f.value)
            widget.setFrame(False)
            widget.textChanged.connect(lambda t, fld=f: _set(fld, t))
            _apply_intent(widget, getattr(f, "intent", ""))

        widget.setStyleSheet(_field_qss(self._field_color_hex(f)))
        if f.kind not in (KIND_CHECKBOX, KIND_RADIO):
            widget.setFont(self._field_font(f))
            amap = {"left": Qt.AlignmentFlag.AlignLeft, "center": Qt.AlignmentFlag.AlignHCenter,
                    "right": Qt.AlignmentFlag.AlignRight}
            al = amap.get(getattr(f, "align", "left"), Qt.AlignmentFlag.AlignLeft)
            if isinstance(widget, (QLineEdit, QTextEdit)):
                widget.setAlignment(al if isinstance(widget, QLineEdit) else (al | Qt.AlignmentFlag.AlignTop))
        widget.setFixedSize(int(wpx), int(hpx))
        widget._field = f                # for current-field lookups (rich text, painter)
        widget.installEventFilter(self)  # track last-focused field
        proxy = self._scene.addWidget(widget)
        proxy.setPos(ox + x0, oy + y0)
        proxy.setZValue(10)
        if page is not None:
            proxy.setData(0, page)
        self._proxies.append(proxy)

    def _placement_offset(self, page):
        b = self._block_for_page(page)
        return (b["ox"], b["oy"]) if b else (0.0, 0.0)

    # -- signatures ---------------------------------------------------------
    def add_signature(self, png_bytes: bytes):
        """Place a freshly drawn signature on the current page, centred."""
        if not self.document:
            return
        pix = QPixmap()
        pix.loadFromData(png_bytes)
        aspect = (pix.height() / pix.width()) if pix.width() else 0.3
        w_pt = 200.0
        h_pt = w_pt * aspect
        pw, ph = self.document.page_size(self.page_index)
        x0 = max(10.0, (pw - w_pt) / 2)
        y0 = max(10.0, (ph - h_pt) / 2)
        placement = {"rect": (x0, y0, x0 + w_pt, y0 + h_pt), "png": png_bytes, "rotation": 0}
        self.document.signatures.setdefault(self.page_index, []).append(placement)
        ox, oy = self._placement_offset(self.page_index)
        self._add_signature_item(placement, self.page_index, ox, oy)
        if self.on_field_created:
            self.on_field_created()

    def _add_signature_item(self, placement, page, ox=0.0, oy=0.0):
        it = SignatureItem(placement, self.document.signatures[page], self, ox, oy)
        it.setData(0, page)
        self._scene.addItem(it)

    # -- images -------------------------------------------------------------
    def add_image(self, png_bytes: bytes):
        """Insert an image on the current page, centred."""
        if not self.document:
            return
        pix = QPixmap(); pix.loadFromData(png_bytes)
        aspect = (pix.height() / pix.width()) if pix.width() else 0.6
        w_pt = 220.0
        h_pt = w_pt * aspect
        pw, ph = self.document.page_size(self.page_index)
        x0 = max(10.0, (pw - w_pt) / 2); y0 = max(10.0, (ph - h_pt) / 2)
        placement = {"rect": (x0, y0, x0 + w_pt, y0 + h_pt), "png": png_bytes, "rotation": 0}
        self.document.images.setdefault(self.page_index, []).append(placement)
        ox, oy = self._placement_offset(self.page_index)
        self._add_image_item(placement, self.page_index, ox, oy)
        if self.on_field_created:
            self.on_field_created()

    def _add_image_item(self, placement, page, ox=0.0, oy=0.0):
        it = SignatureItem(placement, self.document.images[page], self, ox, oy)
        it.setData(0, page)
        self._scene.addItem(it)

    # -- annotations --------------------------------------------------------
    def _add_annotation_item(self, ann, page, ox=0.0, oy=0.0):
        it = AnnotationItem(ann, self.document.annotations[page], self, ox, oy)
        it.setData(0, page)
        self._scene.addItem(it)


def _set(field, value: str):
    field.value = value


def _apply_intent(widget: QLineEdit, intent: str):
    """Field-type validation: numeric phones/amounts, upper-cased IFSC/PAN."""
    if not intent:
        return
    from PySide6.QtGui import QRegularExpressionValidator
    from PySide6.QtCore import QRegularExpression
    if intent in ("phone", "account"):
        widget.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9 +()\-]*")))
    elif intent == "amount":
        widget.setValidator(QRegularExpressionValidator(QRegularExpression(r"[0-9.,]*")))
    elif intent == "ifsc":
        widget.setMaxLength(11)
        widget.textEdited.connect(lambda t, w=widget: w.text() != t.upper() and w.setText(t.upper()))
    elif intent == "pan":
        widget.setMaxLength(10)
        widget.textEdited.connect(lambda t, w=widget: w.text() != t.upper() and w.setText(t.upper()))


_HANDLE = 14  # px size of the resize grip


class SignatureItem(QGraphicsItem):
    """A movable, aspect-preserving, resizable signature image. Geometry is kept
    in scene pixels and mirrored back to `placement['rect']` in PDF points."""

    def __init__(self, placement: dict, placements: list, view: PdfView, ox=0.0, oy=0.0):
        super().__init__()
        self._placement = placement
        self._placements = placements
        self._view = view
        self._ox, self._oy = ox, oy
        self._pix = QPixmap()
        self._pix.loadFromData(placement["png"])
        self._rotation = int(placement.get("rotation", 0))
        self._resizing = False
        self._update_pix()

        z = view.zoom
        x0, y0, x1, y1 = placement["rect"]
        self._w = max(20.0, (x1 - x0) * z)
        self._h = max(20.0, (y1 - y0) * z)
        self.setPos(QPointF(ox + x0 * z, oy + y0 * z))

        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsFocusable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setZValue(20)
        self.setCursor(Qt.CursorShape.SizeAllCursor)

    def boundingRect(self) -> QRectF:
        return QRectF(-1, -1, self._w + _HANDLE + 2, self._h + _HANDLE + 2)

    def _handle_rect(self) -> QRectF:
        return QRectF(self._w - _HANDLE / 2, self._h - _HANDLE / 2, _HANDLE, _HANDLE)

    def _update_pix(self):
        if self._rotation:
            self._dpix = self._pix.transformed(QTransform().rotate(self._rotation),
                                               Qt.TransformationMode.SmoothTransformation)
        else:
            self._dpix = self._pix
        self._aspect = (self._dpix.height() / self._dpix.width()) if self._dpix.width() else 0.3

    def rotate_90(self):
        self.prepareGeometryChange()
        self._rotation = (self._rotation + 90) % 360
        self._update_pix()
        self._w, self._h = self._h, self._w        # keep image proportions
        self._placement["rotation"] = self._rotation
        self.update(); self._sync()

    def duplicate(self):
        x0, y0, x1, y1 = self._placement["rect"]
        new = {"rect": (x0 + 12, y0 + 12, x1 + 12, y1 + 12),
               "png": self._placement["png"], "rotation": self._rotation}
        self._placements.append(new)
        if self.scene():
            item = SignatureItem(new, self._placements, self._view, self._ox, self._oy)
            item.setData(0, self.data(0))
            self.scene().addItem(item)
            self.setSelected(False); item.setSelected(True)

    def paint(self, p: QPainter, option, widget=None):
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.drawPixmap(QRectF(0, 0, self._w, self._h), self._dpix, QRectF(self._dpix.rect()))
        if self.isSelected():
            p.setPen(QPen(QColor(0, 120, 215), 1, Qt.PenStyle.DashLine))
            p.drawRect(QRectF(0, 0, self._w, self._h))
            p.fillRect(self._handle_rect(), QColor(0, 120, 215))

    def mousePressEvent(self, e):
        if self._handle_rect().contains(e.pos()):
            self._resizing = True
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._resizing:
            self.prepareGeometryChange()
            self._w = max(24.0, e.pos().x())
            self._h = self._w * self._aspect
            self.update()
            self._sync()
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if self._resizing:
            self._resizing = False
            self._sync()
            e.accept()
            return
        super().mouseReleaseEvent(e)
        self._sync()

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._sync()
        return super().itemChange(change, value)

    def keyPressEvent(self, e):
        k = e.key()
        if k in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            for i, pl in enumerate(self._placements):
                if pl is self._placement:
                    del self._placements[i]; break
            if self.scene():
                self.scene().removeItem(self)
            e.accept(); return
        if k == Qt.Key.Key_R:
            self.rotate_90(); e.accept(); return
        if k == Qt.Key.Key_D:
            self.duplicate(); e.accept(); return
        super().keyPressEvent(e)

    def _sync(self):
        z = self._view.zoom
        x0 = (self.x() - self._ox) / z
        y0 = (self.y() - self._oy) / z
        self._placement["rect"] = (x0, y0, x0 + self._w / z, y0 + self._h / z)


class AnnotationItem(QGraphicsItem):
    """A markup annotation (pen / rect / ellipse / line / arrow / highlight).
    Movable and deletable; geometry mirrored back to the model in PDF points."""

    def __init__(self, ann: dict, anns: list, view: PdfView, ox=0.0, oy=0.0):
        super().__init__()
        self._ann = ann
        self._anns = anns
        self._view = view
        self._ox, self._oy = ox, oy
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemIsFocusable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setZValue(15)
        self._build()
        self.setPos(ox, oy)

    def _build(self):
        z = self._view.zoom
        self._color = QColor.fromRgbF(*self._ann.get("color", (0.85, 0.1, 0.1)))
        self._width = max(1.0, self._ann.get("width", 1.6) * z)
        t = self._ann["type"]
        if t == "pen":
            self._poly = QPolygonF([QPointF(x * z, y * z) for x, y in self._ann["points"]])
            self._rect = self._poly.boundingRect()
        else:
            x0, y0, x1, y1 = self._ann["rect"]
            self._rect = QRectF(QPointF(x0 * z, y0 * z), QPointF(x1 * z, y1 * z))
            if t in ("rect", "ellipse", "highlight"):
                self._rect = self._rect.normalized()
        self._origin = self.pos()

    def boundingRect(self) -> QRectF:
        return self._rect.adjusted(-self._width - 2, -self._width - 2, self._width + 2, self._width + 2)

    def paint(self, p: QPainter, option, widget=None):
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self._ann["type"]
        pen = QPen(self._color, self._width)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        if t == "pen":
            p.drawPolyline(self._poly)
        elif t == "rect":
            p.drawRect(self._rect)
        elif t == "ellipse":
            p.drawEllipse(self._rect)
        elif t == "highlight":
            c = QColor(self._color); c.setAlphaF(0.35)
            p.fillRect(self._rect, c)
        else:  # line / arrow
            a = self._rect.topLeft(); b = self._rect.bottomRight()
            p.drawLine(a, b)
            if t == "arrow":
                ang = math.atan2(b.y() - a.y(), b.x() - a.x())
                size = max(8.0, self._width * 4)
                for d in (math.pi - 0.5, math.pi + 0.5):
                    p.drawLine(b, QPointF(b.x() + size * math.cos(ang + d),
                                          b.y() + size * math.sin(ang + d)))
        if self.isSelected():
            p.setPen(QPen(QColor(0, 120, 215), 1, Qt.PenStyle.DashLine))
            p.drawRect(self._rect.adjusted(-2, -2, 2, 2))

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._commit_move()
        return super().itemChange(change, value)

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            for i, a in enumerate(self._anns):
                if a is self._ann:
                    del self._anns[i]; break
            if self.scene():
                self.scene().removeItem(self)
            e.accept(); return
        super().keyPressEvent(e)

    def _commit_move(self):
        z = self._view.zoom
        dx = (self.x() - self._ox) / z; dy = (self.y() - self._oy) / z   # moved, in points
        if not dx and not dy:
            return
        if self._ann["type"] == "pen":
            self._ann["points"] = [(x + dx, y + dy) for x, y in self._ann["points"]]
        else:
            x0, y0, x1, y1 = self._ann["rect"]
            self._ann["rect"] = [x0 + dx, y0 + dy, x1 + dx, y1 + dy]
        self.prepareGeometryChange(); self._build(); self.setPos(self._ox, self._oy); self.update()
