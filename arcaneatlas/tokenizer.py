# Copyright (C) 2024–2026 Eric Hernandez
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Tokenizer dialog — turn an arbitrary image into a round VTT-style token.

Self-contained Qt widget (imports only PySide6, never ``main``). The user
pans/zooms a source image behind a fixed circular crop, picks a token size
(1×1 … 4×4 inch) and a coloured border ring (one of eight fixed colours; always
present and a fixed thin thickness); on accept the
dialog bakes a square, transparent-cornered PNG (``result_image()``) and returns a
resolution-independent parameter dict (``result_params()``) so the token can be
re-opened and re-edited later.

The crop is described in *source-image* coordinates so it survives a re-bake at a
different output size:
  * ``cx``/``cy``      — the source point that sits at the circle centre
  * ``fitDiameter``    — how many source pixels span the circle's diameter

GRID = 72 px = one inch, matching the canvas grid.
"""

from PySide6.QtCore import Qt, QPointF, QRectF, QSize, Signal
from PySide6.QtGui import (QImage, QPainter, QColor, QPen, QPainterPath,
                           QBrush)
from PySide6.QtWidgets import (QDialog, QWidget, QHBoxLayout, QVBoxLayout,
                               QFormLayout, QComboBox, QSlider,
                               QLabel, QDialogButtonBox, QGroupBox,
                               QPushButton, QButtonGroup)

GRID = 72                      # scene px per inch (one 5-ft square)
PREVIEW_PX = 360               # preview widget edge in screen px
PREVIEW_MARGIN = 14            # gap between widget edge and the crop circle

# Eight fixed ring colours. The ring is always "thin" (a fixed fraction of the
# token diameter) — the thickness picker was removed.
BORDER_COLORS = ["#000000", "#ffffff", "#c0392b", "#e67e22",
                 "#f1c40f", "#27ae60", "#2980b9", "#8e44ad"]
BORDER_WIDTH = 0.04            # fraction of diameter (thin)


class _TokenPreview(QWidget):
    """Interactive circular crop preview. Drag to pan, scroll to zoom."""

    changed = Signal()          # emitted when pan/zoom changes (to sync the slider)

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.img = image
        self.setFixedSize(PREVIEW_PX, PREVIEW_PX)
        self.setCursor(Qt.OpenHandCursor)

        w, h = max(1, image.width()), max(1, image.height())
        # Defaults: centre the image, fit its smaller dimension to the circle.
        self.cx = w / 2.0
        self.cy = h / 2.0
        self.fit_diameter = float(min(w, h))
        self.fit_base = self.fit_diameter        # 100% zoom reference

        self.border_enabled = True                # every token has a ring
        self.border_color = QColor(BORDER_COLORS[0])
        self.border_frac = BORDER_WIDTH           # fixed thin ring

        self._drag_last = None

    # ---- geometry helpers -------------------------------------------------
    def _circle_px(self):
        return PREVIEW_PX - 2 * PREVIEW_MARGIN

    def _center(self):
        return QPointF(PREVIEW_PX / 2.0, PREVIEW_PX / 2.0)

    def set_fit_diameter(self, d):
        lo = 8.0
        hi = max(self.img.width(), self.img.height()) * 6.0
        self.fit_diameter = max(lo, min(hi, float(d)))
        self.update()

    # ---- painting ---------------------------------------------------------
    def paintEvent(self, _ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)

        self._draw_checker(p)

        center = self._center()
        D = self._circle_px()
        scale = D / self.fit_diameter

        circle = QPainterPath()
        circle.addEllipse(center, D / 2.0, D / 2.0)

        # source image, clipped to the crop circle
        p.save()
        p.setClipPath(circle)
        p.translate(center)
        p.scale(scale, scale)
        p.drawImage(QPointF(-self.cx, -self.cy), self.img)
        p.restore()

        # dim everything outside the circle so the crop is obvious
        outside = QPainterPath()
        outside.addRect(QRectF(self.rect()))
        outside = outside.subtracted(circle)
        p.fillPath(outside, QColor(0, 0, 0, 130))

        # thin guide outline
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 200), 1, Qt.DashLine))
        p.drawEllipse(center, D / 2.0, D / 2.0)

        # border ring preview (drawn just inside the circle, like the bake)
        if self.border_enabled and self.border_frac > 0:
            pw = self.border_frac * D
            r = D / 2.0 - pw / 2.0
            p.setPen(QPen(self.border_color, pw))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(center, r, r)
        p.end()

    def _draw_checker(self, p):
        sq = 12
        light = QColor(210, 210, 210)
        dark = QColor(170, 170, 170)
        for y in range(0, PREVIEW_PX, sq):
            for x in range(0, PREVIEW_PX, sq):
                c = light if ((x // sq + y // sq) % 2 == 0) else dark
                p.fillRect(x, y, sq, sq, c)

    # ---- interaction ------------------------------------------------------
    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            self._drag_last = ev.position()
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, ev):
        if self._drag_last is None:
            return
        D = self._circle_px()
        scale = D / self.fit_diameter
        delta = ev.position() - self._drag_last
        self.cx -= delta.x() / scale
        self.cy -= delta.y() / scale
        self._drag_last = ev.position()
        self.update()

    def mouseReleaseEvent(self, ev):
        self._drag_last = None
        self.setCursor(Qt.OpenHandCursor)

    def wheelEvent(self, ev):
        steps = ev.angleDelta().y() / 120.0
        if not steps:
            return
        # scroll up → zoom in → fewer source px span the circle
        self.set_fit_diameter(self.fit_diameter * (0.88 ** steps))
        self.changed.emit()


class TokenizerDialog(QDialog):
    """Modal tokenizer. Construct with a source QImage (and optional saved
    params), ``exec()`` it, then read ``result_image()`` / ``result_params()``."""

    SIZES = [1, 2, 3, 4]

    def __init__(self, image: QImage, params: dict | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Create Token")
        self.setModal(True)

        if image.isNull():
            image = QImage(GRID, GRID, QImage.Format_ARGB32_Premultiplied)
            image.fill(Qt.gray)
        self._src = image

        self.preview = _TokenPreview(image, self)

        # ---- controls -----------------------------------------------------
        self.size_combo = QComboBox()
        for n in self.SIZES:
            self.size_combo.addItem(f"{n}×{n} inch ({n*GRID}px)", n)

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setRange(20, 600)        # percent of the base fit
        self.zoom_slider.setValue(100)

        # Eight fixed colour swatches (exclusive, checkable) instead of a picker.
        self.color_group = QButtonGroup(self)
        self.color_group.setExclusive(True)
        self.color_buttons = []
        self._color_row = QHBoxLayout()
        self._color_row.setSpacing(4)
        for i, hexc in enumerate(BORDER_COLORS):
            btn = self._make_swatch(hexc)
            self.color_group.addButton(btn, i)
            self.color_buttons.append(btn)
            self._color_row.addWidget(btn)
        self._color_row.addStretch(1)
        self.color_buttons[0].setChecked(True)

        # restore saved params (re-edit) or leave defaults
        if params:
            self._apply_params(params)

        # reflect current preview state onto the widgets
        self._sync_zoom_slider()
        self._select_color(self.preview.border_color)

        # ---- layout -------------------------------------------------------
        form = QFormLayout()
        form.addRow("Size:", self.size_combo)
        form.addRow("Zoom:", self.zoom_slider)

        border_box = QGroupBox("Ring Color")
        bl = QFormLayout(border_box)
        bl.addRow(self._color_row)

        right = QVBoxLayout()
        right.addWidget(QLabel("Drag to pan · scroll to zoom"))
        right.addLayout(form)
        right.addWidget(border_box)
        right.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        right.addWidget(buttons)

        body = QHBoxLayout()
        body.addWidget(self.preview)
        body.addLayout(right)
        self.setLayout(body)

        # ---- signals ------------------------------------------------------
        self.size_combo.currentIndexChanged.connect(self.preview.update)
        self.zoom_slider.valueChanged.connect(self._on_zoom_slider)
        self.preview.changed.connect(self._sync_zoom_slider)
        self.color_group.idClicked.connect(self._on_color_pick)

    # ---- param plumbing ---------------------------------------------------
    def _apply_params(self, p):
        pv = self.preview
        pv.cx = float(p.get("cx", pv.cx))
        pv.cy = float(p.get("cy", pv.cy))
        pv.fit_diameter = float(p.get("fitDiameter", pv.fit_diameter))
        pv.border_enabled = True             # every token has a ring (ignore saved off state)
        pv.border_color = QColor(p.get("borderColor", BORDER_COLORS[0]))
        pv.border_frac = BORDER_WIDTH        # always thin (ignore any saved width)
        n = int(p.get("sizeInches", 1))
        idx = self.size_combo.findData(n)
        if idx >= 0:
            self.size_combo.setCurrentIndex(idx)

    def _selected_size(self):
        return self.size_combo.currentData() or 1

    def _sync_zoom_slider(self):
        pct = int(round(self.preview.fit_base / self.preview.fit_diameter * 100))
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(max(self.zoom_slider.minimum(),
                                      min(self.zoom_slider.maximum(), pct)))
        self.zoom_slider.blockSignals(False)

    def _on_zoom_slider(self, val):
        self.preview.set_fit_diameter(self.preview.fit_base * 100.0 / max(1, val))

    # ---- border colour swatches -------------------------------------------
    def _make_swatch(self, hexc):
        btn = QPushButton()
        btn.setCheckable(True)
        btn.setFixedSize(24, 24)
        btn.setToolTip(hexc)
        btn.setStyleSheet(
            f"QPushButton {{ background:{hexc}; border:1px solid #888;"
            f" border-radius:3px; }}"
            f"QPushButton:checked {{ border:3px solid #1e90ff; }}")
        return btn

    def _select_color(self, color):
        """Check the swatch matching ``color`` (nearest by name; default first)."""
        target = QColor(color).name()
        idx = next((i for i, h in enumerate(BORDER_COLORS)
                    if QColor(h).name() == target), 0)
        self.color_buttons[idx].setChecked(True)
        self.preview.border_color = QColor(BORDER_COLORS[idx])

    def _on_color_pick(self, idx):
        self.preview.border_color = QColor(BORDER_COLORS[idx])
        self.preview.update()

    # ---- results ----------------------------------------------------------
    def result_params(self):
        pv = self.preview
        return {
            "cx": pv.cx, "cy": pv.cy, "fitDiameter": pv.fit_diameter,
            "sizeInches": self._selected_size(),
            "borderEnabled": pv.border_enabled,
            "borderColor": pv.border_color.name(QColor.HexArgb),
            "borderWidth": pv.border_frac,
        }

    def result_image(self) -> QImage:
        """Bake the round token at the chosen output size (transparent corners)."""
        return bake_token(self._src, self.result_params())


def bake_token(src: QImage, params: dict) -> QImage:
    """Bake a round, transparent-cornered token PNG from a source image and a
    tokenizer param dict (``result_params()`` shape). Standalone so callers can
    re-bake a token (e.g. a per-instance border recolor) without a dialog.

    Honoured params: ``sizeInches``, ``cx``/``cy``/``fitDiameter`` (crop in source
    coords), ``borderEnabled``, ``borderColor``, ``borderWidth`` (fraction of the
    diameter). Missing keys fall back to whole-image / no-border defaults."""
    if src is None or src.isNull():
        src = QImage(GRID, GRID, QImage.Format_ARGB32_Premultiplied)
        src.fill(Qt.gray)
    D = int(params.get("sizeInches", 1)) * GRID
    out = QImage(D, D, QImage.Format_ARGB32_Premultiplied)
    out.fill(Qt.transparent)

    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)

    circle = QPainterPath()
    circle.addEllipse(QRectF(0, 0, D, D))

    fit = float(params.get("fitDiameter", min(src.width(), src.height()) or D))
    cx = float(params.get("cx", src.width() / 2.0))
    cy = float(params.get("cy", src.height() / 2.0))
    scale = D / fit
    p.save()
    p.setClipPath(circle)
    p.translate(D / 2.0, D / 2.0)
    p.scale(scale, scale)
    p.drawImage(QPointF(-cx, -cy), src)
    p.restore()

    border_w = float(params.get("borderWidth", 0.0))
    if params.get("borderEnabled") and border_w > 0:
        pw = border_w * D
        r = D / 2.0 - pw / 2.0
        p.setPen(QPen(QColor(params.get("borderColor", "#000000")), pw))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QPointF(D / 2.0, D / 2.0), r, r)
    p.end()
    return out
