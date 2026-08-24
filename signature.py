"""Signature capture: draw freehand OR upload an image and edit it (rotate,
crop, optional transparent background). Returns a transparent PNG to stamp.
"""
from __future__ import annotations

from io import BytesIO

import numpy as np
from PIL import Image

from PySide6.QtCore import Qt, QPoint, QRect, QRectF, QBuffer
from PySide6.QtGui import QPainter, QPen, QColor, QImage, QPixmap, QTransform
from PySide6.QtWidgets import (
    QWidget, QDialog, QVBoxLayout, QHBoxLayout, QStackedWidget, QFileDialog,
)
from qfluentwidgets import PrimaryPushButton, PushButton, BodyLabel, CheckBox


# --- freehand drawing ------------------------------------------------------
class SignatureCanvas(QWidget):
    def __init__(self, ink: QColor, parent=None):
        super().__init__(parent)
        self.setFixedSize(620, 230)
        self.ink = ink
        self._strokes: list[list[QPoint]] = []
        self.setCursor(Qt.CursorShape.CrossCursor)

    def set_ink(self, ink): self.ink = ink; self.update()
    def clear(self): self._strokes.clear(); self.update()
    def is_empty(self): return not any(self._strokes)

    def mousePressEvent(self, e):
        self._strokes.append([e.position().toPoint()]); self.update()

    def mouseMoveEvent(self, e):
        if self._strokes and (e.buttons() & Qt.MouseButton.LeftButton):
            self._strokes[-1].append(e.position().toPoint()); self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor(255, 255, 255))
        p.setPen(QPen(QColor(220, 220, 220), 1))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.setPen(QPen(QColor(210, 210, 210), 1, Qt.PenStyle.DashLine))
        y = int(self.height() * 0.75)
        p.drawLine(20, y, self.width() - 20, y)
        self._paint(p, self.ink)
        p.end()

    def _paint(self, p, color, off=QPoint(0, 0)):
        pen = QPen(color, 2.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap); pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        for s in self._strokes:
            if len(s) == 1:
                p.drawPoint(s[0] - off)
            for i in range(1, len(s)):
                p.drawLine(s[i - 1] - off, s[i] - off)

    def to_png(self):
        if self.is_empty():
            return None
        pad = 8
        xs = [pt.x() for s in self._strokes for pt in s]
        ys = [pt.y() for s in self._strokes for pt in s]
        x0 = max(0, min(xs) - pad); y0 = max(0, min(ys) - pad)
        x1 = min(self.width(), max(xs) + pad); y1 = min(self.height(), max(ys) + pad)
        crop = QRect(QPoint(x0, y0), QPoint(x1, y1))
        img = QImage(crop.size(), QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._paint(p, self.ink, off=QPoint(x0, y0)); p.end()
        return _qimage_to_png(img)


# --- image upload + rotate/crop -------------------------------------------
class ImageEditor(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(620, 300)
        self._img: QImage | None = None
        self._draw = QRectF()          # where the image is painted (widget coords)
        self._sel: QRect | None = None  # crop selection (widget coords)
        self._origin = None
        self.setCursor(Qt.CursorShape.CrossCursor)

    def load(self, path: str) -> bool:
        img = QImage(path)
        if img.isNull():
            return False
        self._img = img.convertToFormat(QImage.Format.Format_ARGB32)
        self._sel = None
        self.update()
        return True

    def has_image(self): return self._img is not None

    def rotate(self, deg: int):
        if self._img is not None:
            self._img = self._img.transformed(QTransform().rotate(deg), Qt.TransformationMode.SmoothTransformation)
            self._sel = None
            self.update()

    def apply_crop(self):
        if self._img is None or self._sel is None or self._draw.width() <= 0:
            return
        sx = self._img.width() / self._draw.width()
        sy = self._img.height() / self._draw.height()
        x = int((self._sel.left() - self._draw.left()) * sx)
        y = int((self._sel.top() - self._draw.top()) * sy)
        w = int(self._sel.width() * sx); h = int(self._sel.height() * sy)
        r = QRect(x, y, w, h).intersected(QRect(0, 0, self._img.width(), self._img.height()))
        if r.width() > 4 and r.height() > 4:
            self._img = self._img.copy(r)
        self._sel = None
        self.update()

    def mousePressEvent(self, e):
        if self._img is not None:
            self._origin = e.position().toPoint(); self._sel = QRect(self._origin, self._origin); self.update()

    def mouseMoveEvent(self, e):
        if self._origin is not None:
            self._sel = QRect(self._origin, e.position().toPoint()).normalized(); self.update()

    def mouseReleaseEvent(self, e):
        self._origin = None

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        p.fillRect(self.rect(), QColor(245, 245, 245))
        if self._img is None:
            p.setPen(QColor(150, 150, 150))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "Upload an image to crop / rotate")
            p.end(); return
        # fit image
        iw, ih = self._img.width(), self._img.height()
        scale = min((self.width() - 16) / iw, (self.height() - 16) / ih)
        dw, dh = iw * scale, ih * scale
        dx = (self.width() - dw) / 2; dy = (self.height() - dh) / 2
        self._draw = QRectF(dx, dy, dw, dh)
        p.drawImage(self._draw, self._img)
        if self._sel is not None:
            p.setPen(QPen(QColor(0, 120, 215), 1, Qt.PenStyle.DashLine))
            p.drawRect(self._sel)
        p.end()

    def to_png(self, transparent_bg: bool):
        if self._img is None:
            return None
        raw = _qimage_to_png(self._img)
        if not transparent_bg:
            return raw
        arr = np.array(Image.open(BytesIO(raw)).convert("RGBA"))
        lum = arr[:, :, :3].mean(axis=2)
        arr[lum > 225, 3] = 0          # drop near-white background
        out = BytesIO(); Image.fromarray(arr, "RGBA").save(out, "PNG")
        return out.getvalue()


def _qimage_to_png(img: QImage) -> bytes:
    buf = QBuffer(); buf.open(QBuffer.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    data = bytes(buf.data()); buf.close()
    return data


# --- dialog hosting both modes --------------------------------------------
class SignatureDialog(QDialog):
    def __init__(self, ink: QColor, parent=None, recents=None):
        super().__init__(parent)
        self.setWindowTitle("Add signature")
        self.setModal(True)
        self._png: bytes | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16); root.setSpacing(10)

        # Recent / saved signatures — click to reuse.
        if recents:
            from PySide6.QtGui import QPixmap, QIcon
            from PySide6.QtCore import QSize
            from PySide6.QtWidgets import QToolButton
            root.addWidget(BodyLabel("Reuse a recent signature:"))
            rec_row = QHBoxLayout()
            for png in recents[:6]:
                pix = QPixmap(); pix.loadFromData(png)
                b = QToolButton(self)
                b.setIcon(QIcon(pix)); b.setIconSize(QSize(96, 40))
                b.setToolTip("Use this signature")
                b.clicked.connect(lambda _=False, p=png: self._use_recent(p))
                rec_row.addWidget(b)
            rec_row.addStretch(1)
            root.addLayout(rec_row)

        mode_row = QHBoxLayout()
        self.btn_draw = PushButton("✏  Draw")
        self.btn_upload = PushButton("🖼  Upload image…")
        self.btn_draw.setCheckable(True); self.btn_upload.setCheckable(True)
        self.btn_draw.setChecked(True)
        self.btn_draw.clicked.connect(lambda: self._set_mode(0))
        self.btn_upload.clicked.connect(self._upload)
        mode_row.addWidget(self.btn_draw); mode_row.addWidget(self.btn_upload); mode_row.addStretch(1)
        root.addLayout(mode_row)

        self.stack = QStackedWidget(self)
        self.canvas = SignatureCanvas(ink, self)
        self.editor = ImageEditor(self)
        self.stack.addWidget(self.canvas)
        self.stack.addWidget(self.editor)
        root.addWidget(self.stack, 0, Qt.AlignmentFlag.AlignCenter)

        # image-edit tools (shown only in image mode)
        self.tools = QWidget(self)
        tl = QHBoxLayout(self.tools); tl.setContentsMargins(0, 0, 0, 0)
        rot_l = PushButton("⟲ Rotate left"); rot_r = PushButton("⟳ Rotate right")
        crop = PushButton("Crop to selection")
        self.transparent = CheckBox("Transparent background"); self.transparent.setChecked(True)
        rot_l.clicked.connect(lambda: self.editor.rotate(-90))
        rot_r.clicked.connect(lambda: self.editor.rotate(90))
        crop.clicked.connect(self.editor.apply_crop)
        for wdg in (rot_l, rot_r, crop, self.transparent):
            tl.addWidget(wdg)
        tl.addStretch(1)
        self.tools.setVisible(False)
        root.addWidget(self.tools)

        row = QHBoxLayout()
        clear = PushButton("Clear"); cancel = PushButton("Cancel"); use = PrimaryPushButton("Use signature")
        clear.clicked.connect(self._clear); cancel.clicked.connect(self.reject); use.clicked.connect(self._accept)
        row.addWidget(clear); row.addStretch(1); row.addWidget(cancel); row.addWidget(use)
        root.addLayout(row)

    def _set_mode(self, idx):
        self.stack.setCurrentIndex(idx)
        self.btn_draw.setChecked(idx == 0)
        self.btn_upload.setChecked(idx == 1)
        self.tools.setVisible(idx == 1)

    def _upload(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose signature image", "",
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp)")
        if path and self.editor.load(path):
            self._set_mode(1)
        else:
            self.btn_upload.setChecked(False)
            self.btn_draw.setChecked(self.stack.currentIndex() == 0)

    def _use_recent(self, png: bytes):
        self._png = png
        self.accept()

    def _clear(self):
        if self.stack.currentIndex() == 0:
            self.canvas.clear()

    def _accept(self):
        if self.stack.currentIndex() == 1:
            png = self.editor.to_png(self.transparent.isChecked())
        else:
            png = self.canvas.to_png()
        if not png:
            self.reject(); return
        self._png = png
        self.accept()

    def get_png(self):
        return self._png
