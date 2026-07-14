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

"""Shared About dialog for the Arcane Tools suite (Atlas / Eye / Audio).

One reusable, theme-inheriting dialog: a branded hero (app icon + serif title +
version), a Patreon-first call to action, a short bio, the "why support" pitch,
connect links, a per-app quick-start, a thanks/credits row, and the AGPLv3
footer with a full-license viewer.

Deliberately self-contained — imports only PySide6, never an app's `main` — so
it can be dropped into all three apps verbatim. Everything app-specific (name,
version, icon, tagline, quick-start, extra credits, license path) is passed in;
the shared copy and links live here so they stay identical across the suite.
Keep the three copies in sync (same rule as the release workflows)."""

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import (QPixmap, QPainter, QPainterPath, QFont, QColor,
                           QDesktopServices, QIcon, QPalette)
from PySide6.QtWidgets import (QApplication, QDialog, QWidget, QLabel, QPushButton,
                               QFrame, QVBoxLayout, QHBoxLayout, QGridLayout,
                               QLayout, QPlainTextEdit, QSizePolicy)

# --- shared links / copy (identical across all three apps) -------------------
WEBSITE_URL = "https://arcanetools.org"
PATREON_URL = "https://www.patreon.com/EricEngineering"
GITHUB_URL = "https://github.com/EricEngineering"
YOUTUBE_URL = "https://youtube.com/@EricEngineering"
DISCORD_URL = "https://discord.gg/U7KCaJ9Pkb"
EMAIL = "engr.eric@gmail.com"

BIO = ("Hi, I'm Eric — an engineer, educator, and in-person GM. Every virtual "
       "tabletop treated at-the-table play as an afterthought, so I built my "
       "own: tools that help you prep less, not more.")

# credit = (name, role, url)  — url may be "" for no link.
ICON_CREDIT = ("Alen72948", "icon artwork", "https://www.artstation.com/alen72948")

# The full "special thanks" roster — shown in every app's About (kept in sync
# with the website's Special Thanks section).
CREDITS = [
    ICON_CREDIT,
    ("Dice Grimorium", "map content", "https://dicegrimorium.com/"),
    ("Dynamic Dungeons", "map content", "https://dynamicdungeons.com/"),
    ("Jules & Ben · JB2A", "animation content",
     "https://github.com/Jules-Bens-Aa/JB2A_DnD5e"),
]

CORAL = "#ff424d"
CORAL_HI = "#ff5b64"


def _theme(dark):
    """Accent shades tuned for the app's current light/dark palette."""
    if dark:
        return dict(panel="#26262f", border="#3b3b48", muted="#a6a6b8",
                    violet="#8b6dff", gold="#d9b25a")
    return dict(panel="#f2f3f7", border="#d8dae2", muted="#585d6e",
                violet="#6b4fe0", gold="#b0812f")


def _rounded_icon(path, px, radius):
    """Load `path` scaled to px×px with rounded corners (transparent outside)."""
    src = QPixmap(path)
    if src.isNull():
        return QPixmap()
    src = src.scaled(px, px, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                     Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(px, px)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    path_ = QPainterPath()
    path_.addRoundedRect(0, 0, px, px, radius, radius)
    p.setClipPath(path_)
    p.drawPixmap(0, 0, src)
    p.end()
    return out


class _LicenseDialog(QDialog):
    """Read-only scrollable viewer for the full license text."""

    def __init__(self, title, text, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 620)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setPlainText(text)
        view.setFont(QFont("monospace", 9))
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(view)


class AboutDialog(QDialog):
    """The suite's About dialog. Pass the per-app bits; shared copy lives here."""

    def __init__(self, *, app_name, version, icon_path, tagline,
                 credits=None, license_name="GNU AGPLv3",
                 license_path=None, parent=None):
        super().__init__(parent)
        if credits is None:
            credits = CREDITS
        self._license_name = license_name
        self._license_path = license_path
        self._app_name = app_name

        self.setWindowTitle(f"About {app_name}")
        if icon_path:
            self.setWindowIcon(QIcon(icon_path))

        dark = self.palette().color(QPalette.ColorRole.Window).lightness() < 128
        c = _theme(dark)
        self._c = c

        root = QVBoxLayout(self)
        root.setContentsMargins(26, 22, 26, 20)
        root.setSpacing(0)
        # Fixed, non-resizable: the layout sizes the dialog to its content.
        root.setSizeConstraint(QLayout.SizeConstraint.SetFixedSize)

        root.addLayout(self._build_hero(app_name, version, icon_path, tagline, c))
        root.addWidget(self._hline(c, top=16, bottom=16))
        root.addLayout(self._build_body(c))
        root.addWidget(self._build_thanks(credits, c))
        root.addLayout(self._build_footer(c))

    # -- pieces ------------------------------------------------------------
    def _hline(self, c, top=0, bottom=0):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, top, 0, bottom)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet(f"color:{c['border']}; background:{c['border']};")
        line.setFixedHeight(1)
        lay.addWidget(line)
        return w

    def _eyebrow(self, text, color):
        lbl = QLabel(text.upper())
        f = QFont()
        f.setPointSize(8)
        f.setBold(True)
        f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 112)
        lbl.setFont(f)
        lbl.setStyleSheet(f"color:{color};")
        return lbl

    def _build_hero(self, app_name, version, icon_path, tagline, c):
        row = QHBoxLayout()
        row.setSpacing(18)

        icon = QLabel()
        icon.setPixmap(_rounded_icon(icon_path, 88, 18))
        icon.setFixedSize(88, 88)
        row.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)

        col = QVBoxLayout()
        col.setSpacing(3)

        titlerow = QHBoxLayout()
        titlerow.setSpacing(9)
        title = QLabel(app_name)
        tf = QFont("Georgia")
        tf.setStyleHint(QFont.StyleHint.Serif)
        tf.setPointSize(23)
        tf.setWeight(QFont.Weight.DemiBold)
        title.setFont(tf)
        titlerow.addWidget(title, 0, Qt.AlignmentFlag.AlignVCenter)

        chip = QLabel(f"v{version}")
        cf = QFont()
        cf.setPointSize(12)
        cf.setBold(True)
        chip.setFont(cf)
        chip.setStyleSheet(
            f"color:{c['violet']}; border:1px solid {c['violet']};"
            f"border-radius:10px; padding:2px 11px;"
            f"background:rgba(124,92,255,0.12);")
        titlerow.addWidget(chip, 0, Qt.AlignmentFlag.AlignVCenter)
        titlerow.addStretch(1)
        col.addLayout(titlerow)

        tag = QLabel(tagline)
        tag.setWordWrap(True)
        tag.setFixedWidth(600)
        tf2 = QFont()
        tf2.setPointSize(11)
        tag.setFont(tf2)
        col.addWidget(tag)

        by = QLabel(f"by Eric Hernandez · part of the Arcane Tools suite")
        by.setStyleSheet(f"color:{c['muted']};")
        col.addWidget(by)

        row.addLayout(col, 1)
        return row

    def _build_body(self, c):
        cols = QHBoxLayout()
        cols.setSpacing(24)

        # ---- left: story ----
        left = QVBoxLayout()
        left.setSpacing(7)
        left.addWidget(self._eyebrow("Who makes this", c["violet"]))
        bio = QLabel(BIO)
        bio.setWordWrap(True)
        bio.setFixedWidth(400)
        left.addWidget(bio)
        left.addSpacing(10)
        left.addWidget(self._eyebrow("Why support", c["gold"]))
        why = QLabel(
            f"{self._app_name} is free forever, open-source under AGPLv3 — no "
            f"paywall, no upsell. But signing certificates (~$250/yr) and hosting "
            f"(~$50/yr) cost real money. If it saves you time at the table, "
            f"consider Patreon — it keeps development going. Thank you.")
        why.setWordWrap(True)
        why.setContentsMargins(12, 10, 12, 10)
        why.setStyleSheet(
            f"background:{c['panel']}; border:1px solid {c['border']};"
            f"border-radius:8px;")
        why.setFixedWidth(400)
        left.addWidget(why)
        left.addStretch(1)
        cols.addLayout(left)

        # ---- right: action ----
        right = QVBoxLayout()
        right.setSpacing(7)
        cta = QPushButton("♥   Consider Patreon")
        cta.setCursor(Qt.CursorShape.PointingHandCursor)
        cf = QFont()
        cf.setPointSize(11)
        cf.setBold(True)
        cta.setFont(cf)
        cta.setFixedWidth(290)
        cta.setStyleSheet(
            f"QPushButton{{color:white; border:none; border-radius:9px;"
            f"padding:11px 14px;"
            f"background:qlineargradient(x1:0,y1:0,x2:0,y2:1,"
            f"stop:0 {CORAL_HI}, stop:1 {CORAL});}}"
            f"QPushButton:hover{{background:{CORAL_HI};}}")
        cta.clicked.connect(lambda: self._open(PATREON_URL))
        right.addWidget(cta)
        sub = QLabel("Free forever — Patreon keeps it that way.")
        sub.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        sub.setStyleSheet(f"color:{c['muted']};")
        sf = QFont()
        sf.setPointSize(9)
        sub.setFont(sf)
        right.addWidget(sub)

        right.addSpacing(8)
        right.addWidget(self._eyebrow("Connect", c["violet"]))
        grid = QGridLayout()
        grid.setSpacing(8)
        chips = [("GitHub", GITHUB_URL), ("YouTube", YOUTUBE_URL),
                 ("Discord", DISCORD_URL), ("arcanetools.org", WEBSITE_URL)]
        for i, (label, url) in enumerate(chips):
            grid.addWidget(self._chip(label, url, c), i // 2, i % 2)
        right.addLayout(grid)
        # Email as a selectable + clickable text link (not a button), so the
        # address can be copied as well as opened in a mail client.
        right.addSpacing(8)
        mail = QLabel(
            f"<a href='mailto:{EMAIL}' style='color:{c['violet']};"
            f"text-decoration:none'>{EMAIL}</a>")
        mail.setTextFormat(Qt.TextFormat.RichText)
        mail.setOpenExternalLinks(True)
        mail.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        right.addWidget(mail)
        right.addStretch(1)
        cols.addLayout(right)
        return cols

    def _chip(self, label, url, c):
        btn = QPushButton(label)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton{{text-align:left; padding:7px 12px; border-radius:8px;"
            f"background:{c['panel']}; border:1px solid {c['border']};}}"
            f"QPushButton:hover{{border:1px solid {c['violet']};}}")
        btn.clicked.connect(lambda: self._open(url))
        return btn

    def _build_thanks(self, credits, c):
        wrap = QWidget()
        outer = QVBoxLayout(wrap)
        outer.setContentsMargins(0, 16, 0, 0)
        outer.setSpacing(6)
        top = QFrame()
        top.setFrameShape(QFrame.Shape.HLine)
        top.setStyleSheet(f"color:{c['border']}; background:{c['border']};")
        top.setFixedHeight(1)
        outer.addWidget(top)
        outer.addSpacing(6)
        outer.addWidget(self._eyebrow("With special thanks", c["gold"]))
        parts = []
        for name, role, url in credits:
            who = f"<b>{name}</b>"
            tail = f" — {role}"
            if url:
                shown = url.split("//")[-1].rstrip("/")
                tail += f" · <a href='{url}' style='color:{c['violet']};text-decoration:none'>{shown}</a>"
            parts.append(who + tail)
        lbl = QLabel("<br>".join(parts))
        lbl.setTextFormat(Qt.TextFormat.RichText)
        lbl.setOpenExternalLinks(True)
        lbl.setWordWrap(True)
        outer.addWidget(lbl)
        return wrap

    def _build_footer(self, c):
        row = QHBoxLayout()
        row.setContentsMargins(0, 16, 0, 0)
        lic = QLabel(
            f"Free &amp; open-source · <b>{self._license_name}</b> · "
            f"© 2024–2026 Eric Hernandez")
        lic.setTextFormat(Qt.TextFormat.RichText)
        lic.setStyleSheet(f"color:{c['muted']};")
        lf = QFont()
        lf.setPointSize(9)
        lic.setFont(lf)
        row.addWidget(lic)
        row.addStretch(1)
        show = QPushButton("Show License")
        show.setCursor(Qt.CursorShape.PointingHandCursor)
        show.clicked.connect(self._show_license)
        close = QPushButton("Close")
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.clicked.connect(self.accept)
        for b in (show, close):
            b.setStyleSheet(
                f"QPushButton{{padding:7px 15px; border-radius:8px;"
                f"border:1px solid {c['border']};}}"
                f"QPushButton:hover{{border:1px solid {c['violet']};}}")
        row.addWidget(show)
        row.addWidget(close)
        return row

    # -- actions -----------------------------------------------------------
    def _open(self, url):
        QDesktopServices.openUrl(QUrl(url))

    def _show_license(self):
        text = f"{self._license_name} — license text not found."
        if self._license_path:
            try:
                with open(self._license_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                pass
        _LicenseDialog(f"{self._app_name} — {self._license_name}", text, self).exec()
