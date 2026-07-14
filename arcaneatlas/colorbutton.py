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

from PySide6.QtWidgets import QToolButton, QColorDialog
from PySide6.QtGui     import QIcon, QPixmap, QColor
from PySide6.QtCore    import QSize, Qt, Signal

class ColorPickerButton(QToolButton):
    # custom signal to emit when a color is changed
    colorChanged = Signal(QColor)

    def __init__(self, parent=None, initial=QColor("white")):
        super().__init__(parent)
        self.setText("Grid Color")
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.setIconSize(QSize(16, 16))
        self._color = None
        self.setColor(initial)
        self.clicked.connect(self.on_clicked)

    def setColor(self, color):
        if not isinstance(color, QColor):
            color = QColor(color)
        self._color = color
        pix = QPixmap(self.iconSize())
        pix.fill(color)
        self.setIcon(QIcon(pix))

    def on_clicked(self):
        c = QColorDialog.getColor(self._color, self.window(),
                                  "Pick Grid Color",
                                  QColorDialog.ShowAlphaChannel)
        if c.isValid():
            self.setColor(c)
            # if you need to notify MainWindow, emit a custom signal here
            self.colorChanged.emit(c)