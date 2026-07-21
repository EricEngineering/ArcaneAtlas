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

import sys, os, json, shutil, math, subprocess, threading, logging, uuid
from pathlib import Path
from arcaneatlas import __version__
from arcaneatlas.about import AboutDialog
from arcaneatlas.ui_mainwindow import Ui_MainWindow
from PySide6.QtCore import (QUrl, Qt, QRectF, QSize, QSizeF, QPointF, QRect,
    QTimer, QSortFilterProxyModel, QMimeData, QModelIndex, QStandardPaths, Signal,
    QObject, QEvent, QEventLoop, QBuffer, QByteArray, QThread)

from PySide6.QtGui import (QPixmap, QPainter, QColor, QPen, QBrush, QCursor, QPalette,
    QFont, QTransform, QIcon, QPainterPath, QPolygonF, QGuiApplication,
    QImage, QImageReader, QMovie,
    QKeySequence, QShortcut, QStandardItemModel, QStandardItem, QDesktopServices)

from PySide6.QtWidgets import (QApplication, QMainWindow, QGraphicsItem, QMenu,
    QGraphicsObject,
    QGraphicsView, QGraphicsScene, QDialog, QGraphicsLineItem, QGraphicsPixmapItem,
    QGraphicsItemGroup, QVBoxLayout, QWidget, QLineEdit, QLabel, QFormLayout, QPushButton,
    QCheckBox, QGraphicsRectItem, QRadioButton, QDialogButtonBox,
    QHBoxLayout, QProgressBar, QProgressDialog, QMessageBox, QToolButton, QSlider,
    QButtonGroup, QSplitter, QTreeView, QFileSystemModel, QInputDialog, QTabWidget,
    QFileIconProvider, QStyle, QProxyStyle, QStyleFactory, QStyleOptionGraphicsItem, QStackedWidget,
    QAbstractItemView, QFileDialog, QTextBrowser, QPlainTextEdit, QComboBox,
    QSpinBox, QGroupBox)
from arcaneatlas.colorbutton import ColorPickerButton
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem, QVideoWidget
from importlib.resources import files, as_file
import arcaneatlas.resources as respkg  # <-- key change

# Module logger. Routed to the console on dev runs and to logs/app.log in the
# windowed build by _setup_diagnostics() (which redirects sys.stderr there).
log = logging.getLogger("arcaneatlas")

APP_DIRNAME = "ArcaneAtlas"  # folder name under user data
SETTINGS_FILE = "settings.json"
STARTER_VERSION = "2025.10.08"

# Global default background color (cross-platform)
DEFAULT_BGCOLOR = QColor(20, 22, 24)  # dark gray, hex #2b2b2b

def _copytree_merge(src: Path, dst: Path) -> None:
    """
    Recursively copy src -> dst, creating folders as needed,
    but do NOT overwrite existing files.
    """
    for root, dirs, files_ in os.walk(src):
        rel = Path(root).relative_to(src)
        target_root = dst / rel
        target_root.mkdir(parents=True, exist_ok=True)
        for fname in files_:
            s = Path(root) / fname
            d = target_root / fname
            if not d.exists():
                shutil.copy2(s, d)

def seed_starter_content(force: bool = False) -> None:
    """
    Copy packaged starter content into user folders on first run.
    Respects a version marker; bump STARTER_VERSION to re-seed on upgrade.
    """
    previous = SEED_MARKER.read_text().strip() if SEED_MARKER.exists() else ""
    if not force and previous == STARTER_VERSION:
        return  # already up to date

    # Arc: arcaneatlas/resources/starter/{assets, maps}
    starter_root = files(respkg) / "starter"

    # Each subfolder is optional—copy it if present.
    to_copy = [
        ("assets", ASSETS_DIR),
        ("maps",   MAPS_DIR),
    ]

    for subname, dest in to_copy:
        traversable = starter_root / subname
        try:
            if traversable.is_dir():
                # as_file gives us a real filesystem path whether running from source or PyInstaller
                with as_file(traversable) as src_dir_path:
                    _copytree_merge(Path(src_dir_path), Path(dest))
        except FileNotFoundError:
            # If a subfolder doesn't exist in the package, just skip it
            pass

    SEED_MARKER.write_text(STARTER_VERSION)

def user_data_root() -> Path:
    """Per-user writable data root in Documents/ArcaneAtlas (cross-platform)."""
    docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    root = Path(docs) / APP_DIRNAME  # "ArcaneAtlas"
    root.mkdir(parents=True, exist_ok=True)
    return root

def res_path(name: str) -> str:
    # A) PyInstaller: try the unpacked bundle first (newer PyInstaller puts files under _internal)
    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS)
        for p in (
            base / "arcaneatlas" / "resources" / name,
            base / "resources" / name,                     # legacy
            base / name,                                   # last resort
        ):
            if p.exists():
                return str(p)

    # B) importlib.resources (works in dev and frozen)
    try:
        with as_file(files(respkg) / name) as p:
            return str(p)
    except Exception:
        pass

    # C) Dev fallback (run from source)
    return str(Path(__file__).resolve().parent / "resources" / name)

# Public paths your app should use
DATA_ROOT   = user_data_root()
MAPS_DIR    = DATA_ROOT / "maps"
ASSETS_DIR  = DATA_ROOT / "assets"
SETTINGS    = DATA_ROOT / "settings.json"
SEED_MARKER = DATA_ROOT / ".starter_seeded"  # plain-text file storing the version we last seeded

# The asset library is organized into category subfolders. Map JSONs reference
# assets by library-relative POSIX path (e.g. "backgrounds/foo.png"), NOT bare
# filename — never look an asset up by basename or assume names are globally
# unique (two categories can each hold a "goblin.png"). Everything dropped on the
# canvas is currently treated as a background.
ASSET_CATEGORIES     = ("backgrounds", "objects", "tokens")
DEFAULT_DROP_CATEGORY = "backgrounds"

# Custom clipboard format for in-app copy/paste of canvas items. Put on the OS
# clipboard so the last copy wins (in-app copy vs an external image): on paste we
# prefer this format, else fall back to a pasted image/file as an asset import.
CLIP_MIME = "application/x-arcaneatlas-items"

# Layer stacking. Each category owns a z-band so backgrounds are ALWAYS below
# objects, which are ALWAYS below tokens; within a band, items keep their relative
# order. Reserved z's (grid, player-view rect) sit outside every band. The bands
# are huge so a layer can hold any realistic item count without colliding with the
# next. ASSET_CATEGORIES is ordered bottom→top, matching these bases.
# See MainWindow.restack_layers(). (PlayerViewItem hardcodes PLAYERVIEW_Z in
# items.py — keep them consistent.)
LAYER_Z_BASE = {"backgrounds": 0, "objects": 1_000_000, "tokens": 2_000_000}
GRID_ABOVE_Z = 3_000_000     # grid drawn above every map item
GRID_BELOW_Z = -1            # grid drawn below every map item
PLAYERVIEW_Z = 9_000_000     # GM-only framing rect, always on top

def ensure_asset_dirs(assets_dir: str) -> None:
    """Create the assets dir and its category subfolders, migrating a legacy
    flat 'assets-library' sibling to it (one-time, only if 'assets' is absent).
    Old maps referencing bare filenames keep resolving at the new root."""
    parent = os.path.dirname(os.path.normpath(assets_dir))
    legacy = os.path.join(parent, "assets-library")
    if not os.path.isdir(assets_dir) and os.path.isdir(legacy):
        os.rename(legacy, assets_dir)
    os.makedirs(assets_dir, exist_ok=True)
    for cat in ASSET_CATEGORIES:
        os.makedirs(os.path.join(assets_dir, cat), exist_ok=True)


MAPS_DIR.mkdir(parents=True, exist_ok=True)
# Migrate a legacy flat 'assets-library' → 'assets' and create category
# subfolders. Must run before seeding, which would otherwise create an empty
# 'assets' and block the rename.
ensure_asset_dirs(str(ASSETS_DIR))

# seed now (idempotent via the version marker)
seed_starter_content()

# Resources paths
ICON_PATH       = res_path("icon.png")
PVICON_PATH     = res_path("pvicon.png")
FOLDERICON_PATH = res_path("foldericon.png")
MAPICON_PATH    = res_path("mapicon.png")

# Fog of war is stored as a QPainterPath in scene coordinates: the union of every
# revealed brush stamp. drawForeground fills the visible area *minus* this path,
# so fog is resolution-independent, tiny in memory, and inherently world-anchored
# (scene coords don't shift when the canvas grows/shrinks). These helpers
# (de)serialize the path to/from the plain lists stored in the map JSON.
def fog_path_to_json(path):
    """Flatten a reveal path to a list of closed polygons (each a list of
    [x, y]). Circular brush stamps become bezier curves in the path; flattening
    them to line segments keeps the JSON small and is lossless in practice since
    fog is hard-edged. Holes (from the Hide tool) survive because each subpath
    boundary is emitted separately and rebuilt with the default odd-even fill."""
    polys = []
    for poly in path.toSubpathPolygons():
        pts = [[p.x(), p.y()] for p in poly]
        if pts:
            polys.append(pts)
    return polys

def fog_path_from_json(polys):
    """Rebuild a reveal path from serialized polygons (see fog_path_to_json)."""
    path = QPainterPath()
    for pts in polys or []:
        if not pts:
            continue
        path.moveTo(pts[0][0], pts[0][1])
        for x, y in pts[1:]:
            path.lineTo(x, y)
        path.closeSubpath()
    return path

from arcaneatlas.transcode import (
    HW_VIDEO_CODECS, _ffmpeg_tool, probe_video_codec, video_has_alpha,
    transcode_to_h264, transcode_to_animated_webp, _TranscodeSignals)
from arcaneatlas.items import (
    InteractivePixmapItem, InteractiveVideoItem, AnimatedItem,
    PlayerViewItem, WelcomeItem, TextBoxItem, PingItem)
from arcaneatlas.tokenizer import TokenizerDialog, bake_token


class JumpSliderStyle(QProxyStyle):
    """Makes a left-click anywhere on a slider's groove jump the handle to that
    point (instead of paging toward it), then drag normally. Uses Qt's built-in
    absolute-set style hint so positioning stays handle-aware and correct at the
    ends/any orientation — no geometry math. Apply with slider.setStyle(...)."""
    def styleHint(self, hint, opt=None, widget=None, returnData=None):
        if hint == QStyle.StyleHint.SH_Slider_AbsoluteSetButtons:
            return int(Qt.MouseButton.LeftButton.value)
        return super().styleHint(hint, opt, widget, returnData)


class MainWindow(QMainWindow, Ui_MainWindow):
    def __init__(self):
        super().__init__()
        self.setupUi(self)

        self.setWindowTitle("Arcane Atlas")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.resize(1440, 810)  # Set initial window size to 800x800

        # Variable to track the current map
        self.current_map_path = None


        # CREATE THE FILE AND WEB TOOL BUTTONS (renamed in Designer from the old
        # settings/map buttons: settings_toolbtn→file_toolbtn, map_toolbtn→web_toolbtn)
        self.web_server = None
        self.show_playerview_box = False   # runtime-only setting (Settings dialog)
        self.lock_on_open = True           # persisted setting; overridden by _load_settings
        from arcaneatlas.webserver import DEFAULT_WEB_PORT
        self.web_port_custom = False       # persisted; use a user-chosen port?
        self.web_port = DEFAULT_WEB_PORT   # persisted; the custom base port

        # ── "File" toolbutton menu ──
        file_menu = QMenu(self)
        # New Map creates a default-named map in the Maps folder and drops the
        # browser into inline-rename (same as the map-browser context menu). Save
        # is gated on having a map open (see _update_map_ui_state).
        file_menu.addAction("New Map", lambda: self._create_new_map_in(self.maps_dir))
        file_menu.addSeparator()
        self.save_action = file_menu.addAction("Save Map", self.save)
        self.save_players_action = file_menu.addAction(
            "Save Map w/ Player Tokens", self.save_with_player_tokens)
        file_menu.addSeparator()
        file_menu.addAction("Open Maps && Assets Folder", self.open_maps_assets_folder)
        file_menu.addSeparator()
        file_menu.addAction("Settings", self._open_settings_dialog)
        file_menu.addAction("Instructions", self._open_instructions_dialog)
        file_menu.addAction("About", self._open_about_dialog)
        self.file_toolbtn.setPopupMode(QToolButton.InstantPopup)
        self.file_toolbtn.setMenu(file_menu)

        # ── "Web" toolbutton → open Web Sharing (LAN) directly ──
        self.web_toolbtn.clicked.connect(self._open_web_share_dialog)

        # Player party: one saved roster of player tokens (settings-scoped),
        # stamped onto any map. The UI controls (Place/Remove/Clear buttons +
        # Members combo) are added in Qt Designer and wired in _wire_party_controls.
        self.party_members = []          # filled by _load_settings

        # asset & map subfolders (user-writable, cross-platform)
        self.asset_dir   = str(ASSETS_DIR)
        self.maps_dir    = str(MAPS_DIR)

        # If you want settings.json in the same place:
        global SETTINGS_FILE
        SETTINGS_FILE = str(SETTINGS)

        self.playerDisplayWidth  = 20.0
        self.playerDisplayHeight = 40.0
        # When True, the non-anchor player-display dimension is auto-derived from
        # the anchor × the selected screen's pixel aspect ratio (reliable, unlike
        # EDID physical size), so the player-view rect matches the panel and
        # fitInView framing has no overshoot. `player_dims_anchor` is the
        # physically-measured dimension the user typed ("width" or "height"); the
        # other follows. Manual mode stores both directly. See _refresh_auto_dims().
        self.player_dims_auto    = True
        self.player_dims_anchor  = "width"

        # Replace the graphicsView placeholder from UI with Canvas
        #   I would do the promotion in QTDesigner, but then I get a circular import error that
        #   would force me to put canvas in its own file and I'm trying to avoid that for now
        # 1. Grab the placeholder
        old = self.graphicsView
        # 2. Find its parent layout & index
        parent = old.parentWidget()
        lay    = parent.layout()
        idx    = lay.indexOf(old)



        # LOCK MAP ASSETS — "Lock Assets" governs backgrounds/objects only;
        # tokens have their own "Lock Tokens" toggle.
        self.lockmap_checkBox.toggled.connect(self._on_lock_map_toggled)
        self.locktokens_checkBox.toggled.connect(self._on_lock_tokens_toggled)

        
        # MAP BROWSER
        # 3. Remove and delete the placeholder
        lay.takeAt(idx)
        old.deleteLater()
        # create a horizontal splitter
        self.splitter = QSplitter(Qt.Horizontal, parent)

        # build a left‐side container with a vertical layout
        self.left_container = QWidget(self.splitter)
        left_layout = QVBoxLayout(self.left_container)
        left_layout.setContentsMargins(0, 0, 0, 0)

        # 1a) Add the status label
        self.map_status_label = QLabel("Current Map Unsaved", self.left_container)
        left_layout.addWidget(self.map_status_label)

        # 1b) Then add the tab widget
        self.left_tabs = QTabWidget(self.left_container)
        left_layout.addWidget(self.left_tabs)
        self.left_tabs.setVisible(True)                  # start hidden


        self.map_view = QTreeView(self.left_container)

        # LAYERS BROWSER

        # MAP BROWSER
        # ── filesystem + proxy model for .json maps only ──
        self.fs_model = MapFileModel(self)
        self.fs_model.setRootPath(self.maps_dir)
        self.fs_model.setIconProvider(MapIconProvider())

        self.fs_model.fileRenamed.connect(self._on_map_file_renamed)

        self.proxy = MapProxyModel(self)
        self.proxy.setSourceModel(self.fs_model)
        self.map_view.setModel(self.proxy)
        self.map_view.setRootIndex(
            self.proxy.mapFromSource(
                self.fs_model.index(self.maps_dir)
            )
        )

        self.map_view.setHeaderHidden(False)
        self.map_view.setColumnHidden(1, True)  # hide Size
        self.map_view.setColumnHidden(2, True)  # hide Type
        self.map_view.setDragEnabled(True)
        self.map_view.setAcceptDrops(True)
        self.map_view.setDropIndicatorShown(True)
        self.map_view.setDragDropMode(QTreeView.InternalMove)
        self.map_view.setDefaultDropAction(Qt.MoveAction)
        self.map_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.map_view.customContextMenuRequested.connect(self.on_map_context_menu)
        self.map_view.doubleClicked.connect(self.on_map_double_clicked)
        # Inline rename is triggered explicitly from the context menu (edit()),
        # so disable implicit triggers — double-click opens the map instead.
        self.map_view.setEditTriggers(QTreeView.NoEditTriggers)

        # add that tree as the first tab
        self.left_tabs.addTab(self.map_view, "Maps")

        # Add the Layers tree to the Layers Tab. Items are grouped under the three
        # layer headings (tokens on top); drag a row within its group to reorder z,
        # select a row to select the matching map item. See update_layers_list().
        layers_container = QWidget(self.left_container)
        layers_layout    = QVBoxLayout(layers_container)
        layers_layout.setContentsMargins(0,0,0,0)

        self._building_layers = False     # guards selection signals during rebuild
        self._layer_item_by_key = {}      # row key → scene item (see update_layers_list)
        self.layers_model = QStandardItemModel()
        self.layers_tree  = LayersTreeView(layers_container)
        self.layers_tree.setModel(self.layers_model)
        self.layers_tree.setHeaderHidden(True)
        self.layers_tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.layers_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.layers_tree.setDragEnabled(True)
        self.layers_tree.setAcceptDrops(True)
        self.layers_tree.setDropIndicatorShown(True)
        self.layers_tree.setDragDropMode(QAbstractItemView.InternalMove)
        self.layers_tree.setDefaultDropAction(Qt.MoveAction)
        self.layers_tree.selectionModel().selectionChanged.connect(self._on_layer_row_selected)

        layers_layout.addWidget(self.layers_tree)
        self.left_tabs.addTab(layers_container, "Layers")

        self._build_assets_tab()

        # — Canvas pane
        self.canvas_view = Canvas(self.splitter)
        # 5. Insert splitter in place of the old placeholder
        lay.insertWidget(idx, self.splitter)
        # 6. (Optional) Set initial splitter sizes: 0 for map, rest for canvas
        self.splitter.setSizes([200, self.width()-200])

        # I need to set stretch again after inserting the widget. necessary on windows
        # After you do: lay.insertWidget(idx, self.splitter)
        # and set splitter sizes …
        if lay:
            # remove vertical gap between toolbar and content
            lay.setContentsMargins(0, 0, 0, 0)
            lay.setSpacing(0)

            # Ensure the toolbar row doesn't consume stretch, and the content does
            i_toolbar  = lay.indexOf(self.toolbar_widget)
            i_content  = lay.indexOf(self.splitter)
            if i_toolbar != -1:
                lay.setStretch(i_toolbar, 0)
            if i_content != -1:
                lay.setStretch(i_content, 1)

        # Start Zoomed Out to 50%
        self.canvas_view.resetTransform()
        self.canvas_view.scale(0.5, 0.5)

        # Store the shared scene reference
        self.scene = self.canvas_view.scene

        # Variable to store selected screen index
        # instead of -1, default to the last (highest) screen index:
        screens = QApplication.screens()
        self.selected_screen_index = len(screens) - 1

        # Create progress bar overlay for saving files
        self.progressBar = QProgressBar(self)
        self.progressBar.setVisible(False)
        self.progressBar.setFixedSize(200, 16)
        self.progressBar.setRange(0, 100)   # we’ll reset later

        # --------------------------- NEW TOOLBAR ---------------------------

        # PLAYER VIEW--------------------------------------------------------
        self.show_playerview_checkBox.toggled.connect(self.toggle_player_view)
        self.hide_playerview_checkBox.toggled.connect(self._on_hide_playerview_toggled)
        self.dimensions_btn.clicked.connect(self.show_dimensions_dialog)

        # Display Selection ComboBox
        # 1) Populate the combo box with each screen
        self.displaySelect_comboBox.clear()
        screens = QApplication.screens()
        for i, screen in enumerate(screens, start=1):
            self.displaySelect_comboBox.addItem(f"Display {i}")
        # 2) Make sure it shows your last-used selection (if you’re storing it)
        self.displaySelect_comboBox.setCurrentIndex(self.selected_screen_index)
        # 3) Hook up its change signal
        self.displaySelect_comboBox.currentIndexChanged.connect(
            self._on_display_combo_changed
        )

        # Display Change Signals
        self._setup_screen_monitoring_signals()

        # Player display mode ("Fullscreen" / "Windowed" / "SingleSplit").
        # Source of truth — set from the Settings dialog (see _open_settings_dialog).
        self.player_screen_mode = "Fullscreen"  # default

        # Will hold the QRect we want applied once player_window shows:
        self._pending_player_geom = None
        # If you already have a `player_window` instance at init,
        # hook its windowHandle().visibleChanged:
        QTimer.singleShot(0, self._install_player_visibility_hook)

        # FOG OF WAR---------------------------------------------------------
        # Fog Reveal Tool
        self.fog_enable_checkBox.toggled.connect(self._on_fog_toggled)
        self.fog_reset_btn.clicked.connect(self._on_fog_reset)
        # make the button stay on when pressed
        self.fog_revealtool_btn.setCheckable(True)
        self.fog_revealtool_btn.toggled.connect(self._enter_reveal_mode)
        # disable the reveal tool if fog of war is off or is turned off while revealtool is active
        self.fog_enable_checkBox.toggled.connect(lambda on: self.fog_revealtool_btn.setChecked(False) if not on else None)

        # Fog Hide Tool
        self.fog_hidetool_btn.setCheckable(True)
        self.fog_hidetool_btn.toggled.connect(self._enter_hide_mode)
        # if the fog system is turned off, drop hide-mode
        self.fog_enable_checkBox.toggled.connect(lambda on: self.fog_hidetool_btn.setChecked(False) if not on else None)

        #NEED TO IMPLEMENT #fog_hidetool_btn #fog_circle_radio #fog_square_radio
        # 1) Group the two radios so they stay mutually exclusive
        group = QButtonGroup(self)
        group.addButton(self.fog_circle_radio)
        group.addButton(self.fog_square_radio)
        group.setExclusive(True)
        # 2) Default to Circle
        self.fog_circle_radio.setChecked(True)
        # 3) Connect their toggles into the canvas setter
        self.fog_circle_radio.toggled.connect(
            lambda checked: self._set_brush_shape("circle") if checked else None
        )
        self.fog_square_radio.toggled.connect(
            lambda checked: self._set_brush_shape("square") if checked else None
        )

        # Fog Reveal/Hide Tool Brush Slider
        default_brush_size = 144
        self.brush_step = 36
        self.brush_slider.setRange(36, 288)  # radius in pixels
        self.brush_slider.setSingleStep(self.brush_step)  # Step size of 36
        self.brush_slider.setTickInterval(self.brush_step)  # Optional: shows ticks at intervals
        self.brush_slider.setTickPosition(QSlider.TicksBelow)  # Optional: displays tick marks
        self.brush_slider.valueChanged.connect(self._on_brush_changed)
        self.brush_slider.setValue(default_brush_size)  # set the default brush size
        self.canvas_view.fog_brush_radius = self.brush_slider.value() # Apply to Canvas immediately

        #GM Fog Slider
        gmfog_opacity = 0.5  # Default to 50% opacity for GM view
        self.gmfog_slider.setRange(0, 100)
        self.gmfog_slider.setValue(gmfog_opacity * 100)  # Set slider to 50%
        self.gmfog_slider.valueChanged.connect(self._on_gmfog_slider_changed)
        self.gmfog_label.setText(f"GM Fog Opacity: {gmfog_opacity * 100}%")

        # ensure reveal‐mode flag always exists
        self._fog_reveal_mode  = False

        # GRID INIT CONTROLS AND COLOR
        # Adjust text spacing
        self.gridcolor_toolbtn.setText("\u00A0Color")
        # When the user picks a new grid color, update & redraw
        self.gridcolor_toolbtn.colorChanged.connect(self.on_grid_color_changed)
        # Mark the map dirty on user-driven background/grid edits. Connected to the
        # widget signals (not the handler methods) so the programmatic re-apply that
        # refresh_canvas_extent does via on_grid_*_toggled() doesn't falsely flag it.
        self.gridcolor_toolbtn.colorChanged.connect(self.mark_dirty)
        # Initial color — a neutral gray that reads on the dark canvas.
        self.default_gridcolor = QColor(128, 128, 128, 160)
        self.gridcolor_toolbtn.setColor(self.default_gridcolor) # Apply color to Colorpicker
        self.canvas_view.grid_color = self.default_gridcolor # Apply color to Canvas
        # Recreate grid and update the views
        self.canvas_view.create_grid()
        self.canvas_view.viewport().update()
        self.gridcolor_reset_toolbtn.clicked.connect(self.on_reset_grid_color)
        self.gridcolor_reset_toolbtn.clicked.connect(self.mark_dirty)
        # wire the “Grid Above” checkbox
        self.gridabove_checkBox.toggled.connect(self.on_grid_above_toggled)
        self.gridabove_checkBox.toggled.connect(self.mark_dirty)
        # ensure initial state matches the checkbox
        self.on_grid_above_toggled(self.gridabove_checkBox.isChecked())

        # --- CANVAS BGCOLOR (force dark dark gray default) ---
        self._bgcolor_default = QColor(DEFAULT_BGCOLOR)
        self.bgcolor_toolbtn.setColor(self._bgcolor_default)
        self.canvas_view.scene.setBackgroundBrush(QBrush(self._bgcolor_default))
        self.canvas_view.viewport().update()
        # When user picks a new color, apply immediately
        self.bgcolor_toolbtn.colorChanged.connect(self.on_bg_color_changed)
        self.bgcolor_toolbtn.colorChanged.connect(self.mark_dirty)
        # Reset BG to the app’s default
        self.bgcolor_reset_toolbtn.clicked.connect(self.on_reset_bg_color)
        self.bgcolor_reset_toolbtn.clicked.connect(self.mark_dirty)

        # wire the “Enable Grid” checkbox
        self.grid_enable_checkBox.toggled.connect(self.on_grid_enable_toggled)
        self.grid_enable_checkBox.toggled.connect(self.mark_dirty)
        # Set Default to Checked
        self.grid_enable_checkBox.setChecked(True)
        # initialize grid visibility
        self.on_grid_enable_toggled(self.grid_enable_checkBox.isChecked())


        # REVEAL/HIDE TOOL ESC CANCEL (Maybe Other Tools Too As Needed)
        # make Esc emit a “cancel reveal” signal
        self.esc_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), self)
        self.esc_shortcut.activated.connect(self.cancel_tool)

        # load saved settings if they exist
        self._load_settings()
        self.set_theme()

        # Wire the Designer-added party controls (after settings load the roster).
        self._wire_party_controls()

        # ── Zoom slider (the widget comes from the .ui) ──
        self.zoom_slider.setRange(10, 200)               # 10%–200%
        self.zoom_slider.setSingleStep(10)
        self.zoom_slider.setTickInterval(10)
        self.zoom_slider.setTickPosition(QSlider.TicksBelow)
        # initialize to whatever the canvas is already at (50% out of the box)
        init_pct = int(self.canvas_view.transform().m11() * 100)
        self.zoom_slider.setValue(init_pct)
        self.zoom_slider.valueChanged.connect(self.on_zoom_slider_changed)
        # End Zoom slider---

        # Make all three sliders click-to-jump (left-click the track moves the
        # handle to that point, then drags). One shared proxy style, kept on self
        # so it isn't garbage-collected while the sliders reference it.
        # Base it on a *fresh* Fusion style: a QProxyStyle with a NULL base wraps
        # the platform's *native desktop* style (QStyleFactory.create of the desktop
        # style key) — NOT the Fusion style set_theme() applied via setStyle("Fusion").
        # On Linux that key is ~Fusion so it looked fine, but on macOS it's "macintosh"
        # (Aqua), so these three sliders rendered native — oversized and ignoring the
        # arcana purple Highlight from the palette. Pass an explicit new Fusion instance
        # instead. QProxyStyle *takes ownership* of the base, so this must be a fresh
        # instance we don't otherwise hold — do NOT pass the shared app style
        # (self.style()), which QApplication also owns → double-free at teardown → segfault.
        self._jump_slider_style = JumpSliderStyle(QStyleFactory.create("Fusion"))
        for _s in (self.brush_slider, self.gmfog_slider, self.zoom_slider):
            _s.setStyle(self._jump_slider_style)


        self.left_tabs.setStyleSheet("QTabBar::tab:disabled { color: grey; }")

        # Startup shows a pristine, empty map — start clean. This must run at the
        # very end of __init__: the setChecked/setColor calls during setup above
        # trip the dirty flag through the bg/grid widget signals. Same reset that
        # save()/open_map()/new_map() do, so a fresh blank map never nags to save.
        self._mark_clean()
        # No map open yet → show the welcome screen and disable map-only controls.
        self._update_map_ui_state()

        # The FFmpeg multimedia backend is now warmed eagerly at startup behind
        # the splash screen (see main() → _ensure_preview_player), so there's no
        # deferred warm-up timer here. _ensure_preview_player() stays idempotent,
        # so a direct call or a video preview before it is still safe.

        # Copy/paste canvas items (Ctrl+C / Ctrl+V). Paste lands at the cursor.
        QShortcut(QKeySequence.Copy, self, activated=self.copy_selection)
        QShortcut(QKeySequence.Paste, self, activated=self.paste_clipboard)

        # F2 renames the selected item in whichever browser has focus. Scoped to
        # each view (WidgetShortcut) so F2 only fires for the focused browser.
        for v, fn in ((self.map_view, self._rename_selected_map),
                      (self.asset_view, lambda: self._rename_selected_asset(self.asset_view)),
                      (self.token_view, lambda: self._rename_selected_asset(self.token_view))):
            sc = QShortcut(QKeySequence("F2"), v)
            sc.setContext(Qt.WidgetShortcut)
            sc.activated.connect(fn)

    def _rename_selected_map(self):
        idx = self.map_view.currentIndex()
        if idx.isValid():
            self.map_view.edit(idx)          # inline rename (proxy handles .json ext)

    def _rename_selected_asset(self, view):
        idx = view.currentIndex()
        if not idx.isValid():
            return
        path = self.asset_fs_model.filePath(idx)
        if self._is_category_root(path):
            return                           # category roots can't be renamed
        self._rename_asset(path)

    def _setup_screen_monitoring_signals(self):
        """
        Connect to Qt's screen add/remove signals and refresh once now.
        """
        app = QGuiApplication.instance()
        if app is None:
            # Shouldn't happen in normal Qt apps, but guard anyway.
            return

        # Disconnect first only if we previously connected — attempting a
        # disconnect on the first call (nothing connected yet) makes PySide6 print
        # a noisy "Failed to disconnect" RuntimeWarning even though it's caught.
        if getattr(self, "_screen_sigs_connected", False):
            try:
                if hasattr(app, "screenAdded"):
                    app.screenAdded.disconnect(self._on_screens_changed)  # type: ignore
            except (RuntimeError, TypeError):
                pass
            try:
                if hasattr(app, "screenRemoved"):
                    app.screenRemoved.disconnect(self._on_screens_changed)  # type: ignore
            except (RuntimeError, TypeError):
                pass

        # Connect handlers
        if hasattr(app, "screenAdded"):
            app.screenAdded.connect(self._on_screens_changed)
        if hasattr(app, "screenRemoved"):
            app.screenRemoved.connect(self._on_screens_changed)
        self._screen_sigs_connected = True

        # Do one initial refresh to ensure combo matches current reality
        self._on_screens_changed()

    def mark_dirty(self, *args):
        # Record that the current map has unsaved item/background/grid changes.
        # (Player-view position and fog are saved automatically on leave, so
        # they intentionally do NOT set this flag.) The actual "are there unsaved
        # changes?" decision is content-based (see _has_unsaved_changes) — this
        # flag is just a cheap hint that *something* touched the map.
        self._dirty = True

    def _saveable_signature(self):
        """A stable string capturing the map's SAVEABLE content — the items that
        get written to disk (player tokens excluded, since they aren't saved) plus
        grid + background. Reuses _clip_entry_for_item (the same serializer as
        copy/paste) so it can't drift from what's actually persisted. Fog and the
        player-view rect are excluded (auto-saved, never part of the prompt)."""
        party_assets = {m.get("asset") for m in self.party_members}
        entries = []
        for it in self.canvas_view.scene.items():
            if isinstance(it, TextBoxItem):
                entries.append(json.dumps(it.to_json(), sort_keys=True))
                continue
            if not isinstance(it, (InteractivePixmapItem, InteractiveVideoItem, AnimatedItem)):
                continue
            if not (getattr(it, "asset_filename", None) or getattr(it, "asset_path", None)):
                continue                          # welcome/non-asset items
            if getattr(it, "is_token", False) and getattr(it, "asset_filename", None) in party_assets:
                continue                          # player tokens aren't saved
            entries.append(json.dumps(self._clip_entry_for_item(it), sort_keys=True))
        entries.sort()
        grid = self.canvas_view.grid_color
        bg = getattr(self, "_bgcolor", None)
        return json.dumps({
            "items": entries,
            "grid": [grid.red(), grid.green(), grid.blue(), grid.alpha()],
            "gridEnabled": self.grid_enable_checkBox.isChecked(),
            "gridAbove": self.gridabove_checkBox.isChecked(),
            "bg": [bg.red(), bg.green(), bg.blue(), bg.alpha()] if bg else None,
        }, sort_keys=True)

    def _mark_clean(self):
        """Map now matches disk: clear the dirty hint and snapshot the saveable
        signature as the baseline future edits are compared against."""
        self._dirty = False
        self._saved_sig = self._saveable_signature()

    def _has_unsaved_changes(self):
        """True only when the SAVEABLE content actually differs from the last
        save/load. Operations on player tokens (not saved) leave the signature
        unchanged, so they don't trigger the unsaved-changes prompt; neither does
        an edit that was undone back to the saved state."""
        return self._saveable_signature() != getattr(self, "_saved_sig", None)

    def _update_map_ui_state(self):
        # Single source of truth for "is a map open?". Disables Save (nothing to
        # save to) and shows the welcome card when no map is open. Fog keeps its
        # normal behavior — it just isn't persisted without a map. Call after
        # current_map_path changes.
        has_map = self.current_map_path is not None
        if hasattr(self, "save_action"):
            self.save_action.setEnabled(has_map)
        if hasattr(self, "save_players_action"):
            self.save_players_action.setEnabled(has_map)
        self._set_welcome_item_visible(not has_map)
        # Place/Remove Party depend on a map being open.
        if hasattr(self, "_refresh_party_ui"):
            self._refresh_party_ui()

    def _set_welcome_item_visible(self, visible):
        # The welcome card is a scene item, so it shows on both the GM and player
        # views (shared scene) for free. Find/remove existing ones by type instead
        # of holding a reference across scene.clear() (which deletes the C++ object).
        scene = self.canvas_view.scene
        for it in list(scene.items()):
            if isinstance(it, WelcomeItem):
                scene.removeItem(it)
        if visible:
            item = WelcomeItem(ICON_PATH)
            cr = self.canvas_view.canvas_rect
            item.setPos(cr.center().x() - WelcomeItem.WIDTH / 2.0,
                        cr.center().y() - WelcomeItem.HEIGHT / 2.0)
            scene.addItem(item)
            # The GM view isn't otherwise centred on the scene, so the card sat a
            # little high (top clipped). Frame it explicitly (also re-run on resize).
            self._center_view_on_welcome()

    def _center_view_on_welcome(self):
        """Centre the GM viewport on the welcome card so it's fully framed. No-op
        when a map is open (don't fight the user's pan) or the card isn't present."""
        if getattr(self, "current_map_path", None) is not None:
            return
        view = getattr(self, "canvas_view", None)
        if view is None:
            return
        for it in view.scene.items():
            if isinstance(it, WelcomeItem):
                view.centerOn(it)
                break

    def _on_screens_changed(self, *args):
        """
        When monitors are added/removed:
        - Rebuild displaySelect_comboBox
        - Default to highest-numbered display
        - Force PlayerView into Windowed mode
        - Recenter PlayerView on selected display
        """
        screens = QGuiApplication.screens()

        # Rebuild the display selector
        self.displaySelect_comboBox.blockSignals(True)
        self.displaySelect_comboBox.clear()
        for i in range(len(screens)):
            self.displaySelect_comboBox.addItem(f"Display {i+1}")

        # Default to the last (highest-numbered) display
        self.selected_screen_index = len(screens) - 1 if screens else -1
        if self.selected_screen_index >= 0:
            self.displaySelect_comboBox.setCurrentIndex(self.selected_screen_index)
        self.displaySelect_comboBox.blockSignals(False)

        # If we have a Player window, force Windowed mode on screen topology change
        if hasattr(self, "player_window") and self.player_window:
            # 1) Apply the mode change programmatically (ensures internal toggles;
            #    also updates self.player_screen_mode).
            try:
                self._on_screenmode_changed("Windowed")
            except Exception:
                # Fallback: explicitly turn both off if handler signature ever changes
                try:
                    self._on_debug_fullscreen_toggled(False)
                    self._on_debug_singlescreen_toggled(False)
                except Exception:
                    pass

            # 2) Recenter the Player on the (new) selected display
            try:
                self.center_on_selected_display(self.player_window)
            except Exception:
                pass

    def sync_fog_alignment(self):
        """Refresh the fog overlay and keep the player view in sync.

        The reveal path lives in scene (world) coordinates, so it stays aligned
        across canvas grow/shrink with no work — this used to resize/realign the
        fog mask, but a scene-space path needs none of that. Retained as a thin
        hook so the existing call sites (extent refresh, resize, toggle) still
        force a redraw and re-sync the player."""
        self.canvas_view.viewport().update()
        if hasattr(self, "player_window"):
            self.sync_fog_to_player_view()
            self.player_window.canvas_view.viewport().update()

    def _reset_fog_mask(self, hidden=True):
        """Reset fog to a clean state: fully fogged (empty reveal path) by
        default, or fully revealed (whole scene rect) when hidden=False."""
        gv = self.canvas_view
        if hidden:
            gv.fog_reveal_path = QPainterPath()
        else:
            p = QPainterPath()
            p.addRect(self.scene.sceneRect())
            gv.fog_reveal_path = p

    def refresh_canvas_extent(self):
        """Grow the canvas to fit placed maps, rebuild the grid, realign fog.

        Called after maps are dropped, moved, resized, removed, or loaded."""
        if self.canvas_view.update_extent():
            # grid_group was rebuilt, so re-apply its visibility and z-order…
            self.on_grid_enable_toggled(self.grid_enable_checkBox.isChecked())
            self.on_grid_above_toggled(self.gridabove_checkBox.isChecked())
            # …and grow the fog mask to match the new scene rect.
            self.sync_fog_alignment()
            return True
        return False

    def on_reset_bg_color(self):
        """Reset canvas (and player, if open) to the app’s default background color."""
        # Fallback if _bgcolor_default wasn’t set for some reason
        default_bg = getattr(
            self, "_bgcolor_default",
            self.canvas_view.viewport().palette().color(QPalette.Base)
        )
        self._bgcolor = QColor(default_bg)

        # Reflect on the picker
        self.bgcolor_toolbtn.setColor(self._bgcolor)

        # Apply to GM
        brush = QBrush(self._bgcolor)
        self.canvas_view.scene.setBackgroundBrush(brush)
        self.canvas_view.viewport().update()

        # Keep Player in sync if it exists
        if hasattr(self, "player_window") and self.player_window:
            self.player_window.canvas_view.scene.setBackgroundBrush(brush)
            self.player_window.canvas_view.viewport().update()

        # Optional: UI nicety
        self.statusBar().showMessage("Background color reset to default.", 2000)

    def on_zoom_slider_changed(self, percent: int):
        """
        Reset the view’s transform to exactly `percent`% zoom.
        """
        factor = percent / 100.0
        self.canvas_view.resetTransform()
        self.canvas_view.scale(factor, factor)

    # ── Layer / z-order engine ───────────────────────────────────────────────
    # Map items are partitioned into three layers (backgrounds < objects < tokens)
    # by z-band; within a band they keep their relative order. restack_layers() is
    # the single enforcer — call it after any add/remove/reorder/load.

    def _map_items(self):
        """Every real map item on the scene (excludes grid, fog, welcome card,
        player-view rect)."""
        return [it for it in self.canvas_view.scene.items()
                if isinstance(it, (InteractivePixmapItem, InteractiveVideoItem,
                                   AnimatedItem, TextBoxItem))]

    def _item_layer(self, item):
        """The layer ('backgrounds'/'objects'/'tokens') an item belongs to —
        from its asset_category, falling back to the category prefix of its
        library-relative asset ref, then DEFAULT_DROP_CATEGORY."""
        cat = getattr(item, "asset_category", None)
        if cat in ASSET_CATEGORIES:
            return cat
        ref = getattr(item, "asset_filename", "") or ""
        head = ref.split("/", 1)[0] if "/" in ref else ""
        return head if head in ASSET_CATEGORIES else DEFAULT_DROP_CATEGORY

    def _restack_grid(self):
        grid = getattr(self.canvas_view, "grid_group", None)
        if grid is not None:
            grid.setZValue(GRID_ABOVE_Z if self.gridabove_checkBox.isChecked()
                           else GRID_BELOW_Z)

    def restack_layers(self):
        """Enforce backgrounds < objects < tokens, preserving each layer's own
        order (by current z). Also reasserts the grid and player-view rect z."""
        for layer in ASSET_CATEGORIES:                       # bottom → top
            items = sorted((it for it in self._map_items()
                            if self._item_layer(it) == layer),
                           key=lambda it: it.zValue())
            base = LAYER_Z_BASE[layer]
            for i, it in enumerate(items):
                it.setZValue(base + i)
        self._restack_grid()
        if hasattr(self, "player_view_item"):
            self.player_view_item.setZValue(PLAYERVIEW_Z)

    def bring_item_to_layer_top(self, item):
        """Raise an item to the top of *its own* layer (never above the layer
        above it)."""
        layer = self._item_layer(item)
        sibs = [it for it in self._map_items()
                if self._item_layer(it) == layer and it is not item]
        top = max((it.zValue() for it in sibs), default=LAYER_Z_BASE[layer] - 1)
        item.setZValue(top + 1)
        self.restack_layers()
        self.update_layers_list()
        self.mark_dirty()

    def _item_display_name(self, item):
        """Friendly name for the Layers tree — the asset's file name, else its
        kind."""
        if isinstance(item, TextBoxItem):
            snippet = " ".join((item._text or "").split())[:24] or "(empty)"
            base = f"“{snippet}”"
        else:
            ref = getattr(item, "asset_filename", None) or getattr(item, "asset_path", "") or ""
            base = os.path.basename(ref) if ref else ""
        if not base:
            kind = {InteractivePixmapItem: "image", AnimatedItem: "animation",
                    InteractiveVideoItem: "video"}.get(type(item), "item")
            base = f"({kind})"
        if not getattr(item, "visible_to_player", True):
            base += "  · hidden"
        return base

    def update_layers_list(self):
        """Rebuild the Layers tree: one group per layer (tokens on top), each
        listing its items top-of-stack first, by friendly asset name. Each child
        row stores a string key (Qt.UserRole) mapping to its scene item via
        self._layer_item_by_key; each group stores its layer (Qt.UserRole + 1).

        Why a string key and not the item itself: an internal drag-move makes
        QStandardItemModel *serialize* the row (QDataStream), which silently drops
        a stored Python/QGraphicsItem object — a serializable key survives."""
        self._building_layers = True
        self.layers_model.clear()
        self._layer_item_by_key = {}
        group_flags = Qt.ItemIsEnabled | Qt.ItemIsDropEnabled
        child_flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
        # Tokens shown at the top of the list = top of the visual stack.
        for layer in reversed(ASSET_CATEGORIES):
            group = QStandardItem(layer.capitalize())
            group.setFlags(group_flags)
            group.setData(layer, Qt.UserRole + 1)
            items = sorted((it for it in self._map_items()
                            if self._item_layer(it) == layer),
                           key=lambda it: it.zValue(), reverse=True)   # top first
            for it in items:
                key = str(id(it))
                self._layer_item_by_key[key] = it
                child = QStandardItem(self._item_display_name(it))
                child.setFlags(child_flags)
                child.setData(key, Qt.UserRole)
                group.appendRow(child)
            self.layers_model.appendRow(group)
        self.layers_tree.expandAll()
        self._building_layers = False

    def _on_layer_row_selected(self, *_):
        """Selecting a row in the Layers tree selects the matching map item."""
        if getattr(self, "_building_layers", False):
            return
        idxs = self.layers_tree.selectionModel().selectedIndexes()
        key = idxs[0].data(Qt.UserRole) if idxs else None
        it = self._layer_item_by_key.get(key) if key else None
        if it is None:
            return
        scene = self.canvas_view.scene
        try:
            scene.clearSelection()
            it.setSelected(True)
            it.setFocus()
        except RuntimeError:
            self.update_layers_list()        # stale ref → rebuild

    def _commit_layer_reorder(self):
        """Called after a drag-move in the Layers tree: recompute z from the new
        row order. Cross-layer drops are rejected (snapped back)."""
        model = self.layers_model
        ordered = {}                          # layer → [items, top → bottom]
        valid = True
        for r in range(model.rowCount()):
            group = model.item(r)
            if group is None:
                continue
            layer = group.data(Qt.UserRole + 1)
            kids = []
            for c in range(group.rowCount()):
                child = group.child(c)
                key = child.data(Qt.UserRole) if child else None
                it = self._layer_item_by_key.get(key) if key else None
                if it is None:
                    continue
                if self._item_layer(it) != layer:
                    valid = False             # dragged into a different layer
                kids.append(it)
            ordered[layer] = kids
        if not valid:
            self.update_layers_list()         # revert the visual move
            return
        for layer, kids in ordered.items():
            base = LAYER_Z_BASE[layer]
            n = len(kids)
            for i, it in enumerate(kids):
                it.setZValue(base + (n - 1 - i))   # top row = highest z
        self.restack_layers()
        self.update_layers_list()
        self.mark_dirty()

    def on_grid_enable_toggled(self, enabled: bool):
        """
        Show the grid_group when checked; hide it when unchecked.
        """
        self.canvas_view.grid_group.setVisible(enabled)
        # repaint immediately
        self.canvas_view.viewport().update()

    def _is_token_item(self, item):
        """True for a placed token (round InteractivePixmapItem under tokens/)."""
        return isinstance(item, InteractivePixmapItem) and getattr(item, "is_token", False)

    def _on_lock_map_toggled(self, locked: bool):
        """Lock/unlock backgrounds and objects (NOT tokens — those follow the
        separate "Lock Tokens" toggle). When locked, drop the selection and turn
        off selecting/moving for every non-token item; unlocking restores them."""
        # 1) drop any current selection
        self.canvas_view.scene.clearSelection()

        # 2) walk every item and turn its flags on or off
        for item in self.canvas_view.scene.items():
            if isinstance(item, (QGraphicsLineItem, QGraphicsItemGroup)):
                continue                            # never touch the grid lines
            if self._is_token_item(item):
                continue                            # tokens → Lock Tokens governs them
            # disable both selecting & dragging when locked
            item.setFlag(QGraphicsItem.ItemIsSelectable, not locked)
            item.setFlag(QGraphicsItem.ItemIsMovable,    not locked)

    def _on_lock_tokens_toggled(self, locked: bool):
        """Lock/unlock token items only, leaving backgrounds/objects alone."""
        self.canvas_view.scene.clearSelection()
        for item in self.canvas_view.scene.items():
            if self._is_token_item(item):
                item.setFlag(QGraphicsItem.ItemIsSelectable, not locked)
                item.setFlag(QGraphicsItem.ItemIsMovable,    not locked)

    def _on_lock_on_open_toggled(self, on: bool):
        """Settings toggle: whether opening a content-bearing map locks its assets
        by default (persisted as `lockOnOpen`)."""
        self.lock_on_open = bool(on)

    # ── Copy / paste ─────────────────────────────────────────────────────────
    # Ctrl+C serialises the selected canvas items onto the OS clipboard under a
    # private MIME type; Ctrl+V prefers that (clone at the cursor) and otherwise
    # treats a clipboard image / file as a fresh asset import (same as a drag).
    def _media_item_types(self):
        return (InteractivePixmapItem, InteractiveVideoItem, AnimatedItem, TextBoxItem)

    def _clip_entry_for_item(self, it):
        """Serialise one canvas item to a clipboard dict (mirrors the map-JSON
        item schema, plus the source path/category needed to rebuild it)."""
        if isinstance(it, TextBoxItem):
            return it.to_json()                  # self-contained; no asset/src needed
        if isinstance(it, InteractivePixmapItem):
            w, h, typ = it.pixmap().width(), it.pixmap().height(), "image"
        elif isinstance(it, AnimatedItem):
            sz = it.size(); w, h, typ = sz.width(), sz.height(), "anim"
        else:
            sz = it.size(); w, h, typ = sz.width(), sz.height(), "video"
        return {
            "type": typ,
            "asset": getattr(it, "asset_filename", None),
            "srcPath": getattr(it, "asset_path", None),
            "category": getattr(it, "asset_category", DEFAULT_DROP_CATEGORY),
            "pos": [it.pos().x(), it.pos().y()],
            "size": [w, h], "rot": it.rotation(), "z": it.zValue(),
            "visibleToPlayer": getattr(it, "visible_to_player", True),
            "isToken": getattr(it, "is_token", False),
            "tokenColor": getattr(it, "token_color_override", None),
            "playerControllable": getattr(it, "player_controllable", False),
        }

    def copy_selection(self):
        items = [it for it in self.canvas_view.scene.selectedItems()
                 if isinstance(it, self._media_item_types())]
        entries = [self._clip_entry_for_item(it) for it in items]
        if not entries:
            return
        payload = json.dumps({"items": entries}).encode("utf-8")
        mime = QMimeData()
        mime.setData(CLIP_MIME, QByteArray(payload))
        QApplication.clipboard().setMimeData(mime)
        self.statusBar().showMessage(
            f"Copied {len(entries)} item(s)", 3000)

    def _paste_scene_pos(self):
        """Scene point under the mouse if it's over the canvas, else view centre."""
        vp = self.canvas_view.viewport()
        local = vp.mapFromGlobal(QCursor.pos())
        if not vp.rect().contains(local):
            local = vp.rect().center()
        return self.canvas_view.mapToScene(local)

    def paste_clipboard(self):
        scene_pos = self._paste_scene_pos()
        md = QApplication.clipboard().mimeData()
        if md.hasFormat(CLIP_MIME):
            self._paste_internal_items(bytes(md.data(CLIP_MIME)), scene_pos)
        elif md.hasUrls():
            # Files copied from a file manager → import each like a drag.
            for url in md.urls():
                p = url.toLocalFile()
                if p:
                    self._place_asset(p, scene_pos)
        elif md.hasImage():
            img = md.imageData()
            if isinstance(img, QImage) and not img.isNull():
                tmp = self._write_clipboard_image_tmp(img)
                if tmp:
                    self._place_asset(tmp, scene_pos)

    def _write_clipboard_image_tmp(self, img):
        """Save a clipboard QImage to a temp PNG so _place_asset can import it.
        Uses a friendly filename ("Pasted Image.png") inside a temp dir so the
        imported asset/token gets a sensible default name (collision-renamed in
        the library) rather than a random temp string the user must rename."""
        import tempfile
        tmp = os.path.join(tempfile.mkdtemp(prefix="aa_paste_"), "Pasted Image.png")
        if img.save(tmp, "PNG"):
            return tmp
        try:
            os.remove(tmp)
        except OSError:
            pass
        return None

    def _item_from_clip_entry(self, e):
        """Rebuild a canvas item from a clipboard dict (or None if its asset is
        gone). Position/selection are set by the caller."""
        if e.get("type") == "text":
            tb = TextBoxItem()
            tb.apply_json(e)                     # text/styling (pos/z/rot by caller)
            return tb
        asset_rel = e.get("asset")
        asset_path = None
        if asset_rel:
            cand = os.path.join(self.asset_dir, asset_rel)
            if os.path.exists(cand):
                asset_path = cand
        if asset_path is None:
            src = e.get("srcPath")
            if src and os.path.exists(src):
                asset_path = src
        if asset_path is None:
            return None
        typ = e.get("type", "image")
        if typ == "image":
            pix = QPixmap(asset_path)
            it = InteractivePixmapItem(pix)
            w, h = e.get("size", [pix.width(), pix.height()])
            it.setPixmap(pix.scaled(w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        elif typ == "anim":
            it = AnimatedItem(asset_path)
            it.setSize(QSizeF(*e.get("size", [it.size().width(), it.size().height()])))
        else:
            it = InteractiveVideoItem(asset_path)
            try:
                it.nativeSizeChanged.disconnect(it._on_video_size)
            except (TypeError, RuntimeError):
                pass
            it.setSize(QSizeF(*e.get("size", [256, 256])))
        if asset_rel:
            it.asset_filename = asset_rel
        else:
            it.asset_path = asset_path          # not yet in library → imported on save
        it.asset_category = e.get("category", DEFAULT_DROP_CATEGORY)
        if isinstance(it, InteractivePixmapItem):
            it.is_token = bool(e.get("isToken", it.asset_category == "tokens"))
            tc = e.get("tokenColor")
            if it.is_token and tc:
                self._bake_token_with_color(it, tc)
            if it.is_token:
                # A pasted token is a new instance → fresh id (don't clone it).
                it.token_id = uuid.uuid4().hex
                it.player_controllable = bool(e.get("playerControllable", False))
        return it

    def _paste_internal_items(self, data, scene_pos):
        try:
            payload = json.loads(bytes(data).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        entries = payload.get("items", [])
        if not entries:
            return
        # Paste needs a map to live in (mirrors _place_asset auto-create).
        if self.current_map_path is None:
            self._create_new_map_in(self.maps_dir, start_editing=False)
            if self.current_map_path is None:
                return
        # Translate the group so its bounding-box centre lands at the cursor.
        # Text entries carry "width" (auto height) instead of a "size" pair.
        def _wh(e):
            return e.get("size", [e.get("width", 0), 0])
        x0 = min(e.get("pos", [0, 0])[0] for e in entries)
        y0 = min(e.get("pos", [0, 0])[1] for e in entries)
        x1 = max(e.get("pos", [0, 0])[0] + _wh(e)[0] for e in entries)
        y1 = max(e.get("pos", [0, 0])[1] + _wh(e)[1] for e in entries)
        dx = scene_pos.x() - (x0 + x1) / 2.0
        dy = scene_pos.y() - (y0 + y1) / 2.0

        new_items = []
        for e in entries:
            it = self._item_from_clip_entry(e)
            if it is None:
                continue
            ox, oy = e.get("pos", [0, 0])
            it.visible_to_player = e.get("visibleToPlayer", True)
            it.setZValue(e.get("z", 0))
            # Rotated video/anim/text items pivot about their CENTER at edit time,
            # and the stored pos assumes that origin — match it or they rotate
            # about (0,0) and shift ~their width to the left.
            if e.get("rot"):
                it.setTransformOriginPoint(it.boundingRect().center())
            it.setRotation(e.get("rot", 0))
            it.setFlag(QGraphicsItem.ItemIsSelectable, True)
            it.setFlag(QGraphicsItem.ItemIsMovable, True)
            self.canvas_view.scene.addItem(it)
            it.setPos(ox + dx, oy + dy)         # after addItem so token snap applies
            # Tokens never overlap — resolve against already-placed (incl. earlier
            # pasted) tokens so a multi-token paste fans out instead of stacking.
            if getattr(it, "is_token", False):
                self._resolve_token_overlap(it)
            new_items.append(it)
        if not new_items:
            return
        self.canvas_view.scene.clearSelection()
        for it in new_items:
            it.setSelected(True)
        self.restack_layers()
        self.refresh_canvas_extent()
        self.update_layers_list()
        self.mark_dirty()
        self.statusBar().showMessage(f"Pasted {len(new_items)} item(s)", 3000)

    # ── Text boxes ────────────────────────────────────────────────────────────
    def add_ping(self, scene_pos):
        """Drop a transient 'look here' ping at scene_pos. Shows in both the GM and
        Player views (shared scene) and self-removes after ~1.4s. Works with or
        without a map open (like fog); triggered by Alt+click or the canvas
        'Ping Here' menu entry."""
        ping = PingItem()
        self.canvas_view.scene.addItem(ping)
        ping.setPos(scene_pos)
        ping.start()

    def add_text_box(self, scene_pos=None):
        """Create a text box centred on scene_pos (or the view centre), open the
        editor, and keep it only if the user doesn't cancel the brand-new box."""
        if self.current_map_path is None:
            self._create_new_map_in(self.maps_dir, start_editing=False)
            if self.current_map_path is None:
                return
        if scene_pos is None:
            scene_pos = self._paste_scene_pos()
        tb = TextBoxItem(text="Text")
        self.canvas_view.scene.addItem(tb)
        br = tb.boundingRect()
        tb.setPos(scene_pos.x() - br.width() / 2, scene_pos.y() - br.height() / 2)
        # A fresh box is movable even if the objects layer is locked.
        tb.setFlag(QGraphicsItem.ItemIsSelectable, True)
        tb.setFlag(QGraphicsItem.ItemIsMovable, True)
        if not self.edit_textbox(tb, is_new=True):
            self.canvas_view.scene.removeItem(tb)
            return
        self.canvas_view.scene.clearSelection()
        tb.setSelected(True)
        self.restack_layers()
        self.refresh_canvas_extent()
        self.update_layers_list()
        self.mark_dirty()

    def import_file(self, scene_pos=None):
        """Pick a media file via a dialog and import it onto the map exactly like a
        drag-drop — same accepted types and same background/object/token category
        prompt (both funnel through _place_asset, which auto-creates a map if none
        is open)."""
        if scene_pos is None:
            scene_pos = self._paste_scene_pos()
        exts = " ".join(self.ASSET_EXTS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Import File", "", f"Media files ({exts})")
        if path:
            self._place_asset(path, scene_pos)

    def edit_textbox(self, item, is_new=False):
        """Modal editor for a TextBoxItem's text + styling. Returns True if applied
        (OK), False if cancelled. Live-previews changes on the item while open and
        reverts to the original styling on cancel."""
        before = item.to_json()                       # snapshot for cancel-revert
        dlg = TextBoxEditDialog(self, item)
        applied = dlg.exec() == QDialog.Accepted
        if applied:
            dlg.apply_to(item)
            item.update()
            if not is_new:
                self.refresh_canvas_extent()
                self.update_layers_list()
                self.mark_dirty()
        else:
            item.apply_json(before)                   # discard live preview
        return applied

    def on_grid_above_toggled(self, checked: bool):
        """Place the grid above every map item (above the tokens band) or below
        every map item (below the backgrounds band). The map-item bands are fixed
        by restack_layers(), so the grid just snaps to GRID_ABOVE_Z/GRID_BELOW_Z."""
        self._restack_grid()
        self.canvas_view.viewport().update()
        self.update_layers_list()

    def on_show_playerview_box_toggled(self, checked: bool):
        self.show_playerview_box = bool(checked)   # remembered for the Settings dialog
        # update the item if it already exists
        if hasattr(self, "player_view_item"):
            self.player_view_item.show_in_player = checked
        # force a repaint on the player window if it's open
        if hasattr(self, "player_window"):
            self.player_window.canvas_view.viewport().update()

    def on_reset_grid_color(self):
        self.gridcolor_toolbtn.setColor(self.default_gridcolor)
        self.canvas_view.grid_color = self.default_gridcolor
        self.canvas_view.create_grid()
        self.canvas_view.viewport().update()

    def on_bg_color_changed(self, color: QColor):
        # keep the alpha the button supplies (or default to opaque if missing)
        self._bgcolor = QColor(color)
        self.canvas_view.scene.setBackgroundBrush(QBrush(self._bgcolor))
        self.canvas_view.viewport().update()

        # also sync the player window if it’s open
        if hasattr(self, "player_window") and self.player_window:
            self.player_window.canvas_view.scene.setBackgroundBrush(QBrush(self._bgcolor))
            self.player_window.canvas_view.viewport().update()

    def on_grid_color_changed(self, color: QColor):
        # keep whatever alpha you like—here we preserve the existing alpha:
        alpha = self.canvas_view.grid_color.alpha()
        new_col = QColor(color.red(), color.green(), color.blue(), alpha)
        self.canvas_view.grid_color = new_col

        # rebuild and repaint the grid
        self.canvas_view.create_grid()

        # 3) reposition the grid relative to your items
        #    so it stays above (or below) as the checkbox dictates
        self.on_grid_above_toggled(self.gridabove_checkBox.isChecked())

        # 4) (optional) re-apply visibility
        self.on_grid_enable_toggled(self.grid_enable_checkBox.isChecked())

        self.canvas_view.viewport().update()

    def cancel_tool(self):
        if self.fog_revealtool_btn.isChecked():
            self.fog_revealtool_btn.setChecked(False)
        if self.fog_hidetool_btn.isChecked():
            self.fog_hidetool_btn.setChecked(False)

    def on_map_double_clicked(self, idx):
        # Double-click a map in the browser to open it. Directories fall through
        # to the default expand/collapse behaviour.
        if not idx.isValid():
            return
        proxy = self.map_view.model()
        src   = proxy.mapToSource(idx)
        fs    = proxy.sourceModel()
        path  = fs.filePath(src)
        if os.path.isdir(path):
            return
        self.open_map(path)

    # ── Assets browser ──────────────────────────────────────────────────────
    ASSET_EXTS = ("*.png", "*.jpg", "*.jpeg", "*.webp",
                  "*.mp4", "*.webm", "*.mov", "*.avi", "*.m4v")

    def _build_assets_tab(self):
        # A read-only tree over assets/ (categories as folders) + a preview pane.
        # Drag an asset to the canvas, or double-click to add it to the current map.
        container = QWidget(self.left_container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self.asset_fs_model = AssetFileModel(self, self)
        # Same folder icon as the maps browser (the .json branch never matches an
        # asset file, so reusing MapIconProvider just gives us the folder icon).
        self.asset_fs_model.setIconProvider(MapIconProvider())
        self.asset_fs_model.setNameFilters(self.ASSET_EXTS)
        self.asset_fs_model.setNameFilterDisables(False)   # hide non-matching files
        self.asset_fs_model.setRootPath(self.asset_dir)

        self.asset_view = QTreeView(container)
        self.asset_view.setModel(self.asset_fs_model)
        self.asset_view.setRootIndex(self.asset_fs_model.index(self.asset_dir))
        self.asset_view.setHeaderHidden(True)
        for col in (1, 2, 3):                              # hide Size / Type / Date
            self.asset_view.setColumnHidden(col, True)
        # Drag out to the canvas AND drag-move files/folders within the tree.
        # Internal moves go through AssetFileModel.dropMimeData → _move_assets_into
        # so map references are rewritten. Inline editing stays off so the only
        # rename path is the context menu (which also rewrites refs).
        self.asset_view.setDragEnabled(True)
        self.asset_view.setAcceptDrops(True)
        self.asset_view.setDropIndicatorShown(True)
        self.asset_view.setDragDropMode(QAbstractItemView.DragDrop)
        self.asset_view.setDefaultDropAction(Qt.MoveAction)
        self.asset_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.asset_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.asset_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.asset_view.customContextMenuRequested.connect(
            lambda p: self.on_asset_context_menu(p, self.asset_view))
        self.asset_view.doubleClicked.connect(self._on_asset_double_clicked)
        self.asset_view.selectionModel().currentChanged.connect(self._on_asset_selected)
        # Tokens get their own tab, so hide the tokens/ row here once it loads.
        self.asset_fs_model.directoryLoaded.connect(self._on_asset_dir_loaded)
        layout.addWidget(self.asset_view, 1)

        # ── preview pane ──
        self.asset_preview_label = QLabel("Select an asset to preview")
        self.asset_preview_label.setAlignment(Qt.AlignCenter)
        self.asset_preview_label.setWordWrap(True)

        # The video preview (QVideoWidget/QMediaPlayer) is created lazily on first
        # video selection — constructing QVideoWidget spins up the Qt FFmpeg
        # multimedia backend (~2s). No QAudioOutput is used (silent preview; see
        # _ensure_preview_player — it avoids a pipewire cold-start segfault).
        self.asset_preview_video = None
        self.asset_preview_player = None
        self.asset_preview_audio = None             # kept None — never a QAudioOutput now

        self.asset_preview_stack = QStackedWidget(container)
        self.asset_preview_stack.addWidget(self.asset_preview_label)   # index 0
        # index 1 (the video widget) is added on demand by _ensure_preview_player()
        self.asset_preview_stack.setFixedHeight(220)
        layout.addWidget(self.asset_preview_stack, 0)

        self.left_tabs.addTab(container, "Assets")
        self._build_tokens_tab()

    def _build_tokens_tab(self):
        # A second tree over the SAME asset model, rooted at tokens/ — so tokens
        # live in their own tab, separate from backgrounds/objects. Tokens are
        # always static PNGs, so the preview is a plain image label (no video
        # backend, unlike the Assets tab).
        container = QWidget(self.left_container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        self.token_view = QTreeView(container)
        self.token_view.setModel(self.asset_fs_model)
        self.token_view.setRootIndex(
            self.asset_fs_model.index(os.path.join(self.asset_dir, "tokens")))
        self.token_view.setHeaderHidden(True)
        for col in (1, 2, 3):
            self.token_view.setColumnHidden(col, True)
        self.token_view.setDragEnabled(True)
        self.token_view.setAcceptDrops(True)
        self.token_view.setDropIndicatorShown(True)
        self.token_view.setDragDropMode(QAbstractItemView.DragDrop)
        self.token_view.setDefaultDropAction(Qt.MoveAction)
        self.token_view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.token_view.setSelectionMode(QAbstractItemView.SingleSelection)
        self.token_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.token_view.customContextMenuRequested.connect(
            lambda p: self.on_asset_context_menu(p, self.token_view))
        self.token_view.doubleClicked.connect(self._on_asset_double_clicked)
        self.token_view.selectionModel().currentChanged.connect(self._on_token_selected)
        layout.addWidget(self.token_view, 1)

        # ── image-only preview pane ──
        self.token_preview_label = QLabel("Select a token to preview")
        self.token_preview_label.setAlignment(Qt.AlignCenter)
        self.token_preview_label.setWordWrap(True)
        self.token_preview_label.setFixedHeight(160)   # shorter → more room for the token tree
        layout.addWidget(self.token_preview_label, 0)

        self.left_tabs.addTab(container, "Tokens")
        # The model may have already cached asset_dir before our directoryLoaded
        # slot was connected — apply the split once now (idempotent).
        self._on_asset_dir_loaded(self.asset_dir)

    def _on_asset_dir_loaded(self, path):
        """When the asset library root finishes (re)loading: hide the tokens/ row
        in the Assets view (tokens have their own tab) and point the Tokens view
        at the tokens/ folder."""
        if os.path.normpath(path) != os.path.normpath(self.asset_dir):
            return
        if not hasattr(self, "token_view"):
            return                       # tokens tab not built yet
        tokens_dir = os.path.join(self.asset_dir, "tokens")
        tok_idx = self.asset_fs_model.index(tokens_dir)
        if tok_idx.isValid():
            root_idx = self.asset_fs_model.index(self.asset_dir)
            self.asset_view.setRowHidden(tok_idx.row(), root_idx, True)
            self.token_view.setRootIndex(tok_idx)

    def _on_token_selected(self, current, _previous):
        path = self._asset_path_for_index(current)
        if not path:
            self.token_preview_label.setText("Select a token to preview")
            return
        pix = QPixmap(path)
        if pix.isNull():
            self.token_preview_label.setText(os.path.basename(path))
        else:
            # Show at up to 2x the token's native resolution — roughly halfway
            # between native and filling the pane — capped to the pane. Keeps
            # low-res tokens a reasonable size without the pixelation of a full
            # upscale (a 72px 1-inch token shows at ~144px, not ~220px).
            lbl = self.token_preview_label.size()
            w = min(lbl.width(), pix.width() * 2)
            h = min(lbl.height(), pix.height() * 2)
            self.token_preview_label.setPixmap(pix.scaled(
                w, h, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def _ensure_preview_player(self):
        """Create the video-preview widgets on first use. Deferred because
        constructing QVideoWidget initialises the FFmpeg multimedia backend
        (~2s) — see _build_assets_tab. Safe to call repeatedly."""
        if self.asset_preview_player is not None:
            return
        self.asset_preview_video = QVideoWidget()
        self.asset_preview_player = QMediaPlayer(self)
        # NO QAudioOutput on purpose. The preview is silent anyway, and
        # constructing/muting a QAudioOutput opens the system audio backend
        # (pipewire), which intermittently SEGFAULTS on a cold start
        # (libpipewire-module-protocol-native, in _ensure_preview_player). Without
        # an audio output the player just plays video with no sound — exactly the
        # muted preview we want, and it matches InteractiveVideoItem (video-only).
        self.asset_preview_audio = None
        self.asset_preview_player.setVideoOutput(self.asset_preview_video)
        self.asset_preview_player.setLoops(QMediaPlayer.Infinite)   # loop endlessly
        self.asset_preview_stack.addWidget(self.asset_preview_video)   # index 1

    def _asset_path_for_index(self, idx):
        # Resolve a tree index to an existing asset file path (None for folders).
        if not idx.isValid():
            return None
        path = self.asset_fs_model.filePath(idx)
        return None if os.path.isdir(path) else path

    def _on_asset_selected(self, current, _previous):
        path = self._asset_path_for_index(current)
        # Stop any preview playback before swapping sources/widgets. The player is
        # created lazily, so it may not exist yet (no video previewed this session).
        if self.asset_preview_player is not None:
            self.asset_preview_player.stop()
        self._stop_preview_movie()
        if not path:
            self.asset_preview_label.setText("Select an asset to preview")
            self.asset_preview_stack.setCurrentIndex(0)
            return
        low = path.lower()
        if low.endswith((".png", ".jpg", ".jpeg")):
            pix = QPixmap(path)
            if pix.isNull():
                self.asset_preview_label.setText(os.path.basename(path))
            else:
                self.asset_preview_label.setPixmap(pix.scaled(
                    self.asset_preview_stack.size(),
                    Qt.KeepAspectRatio, Qt.SmoothTransformation))
            self.asset_preview_stack.setCurrentIndex(0)
        elif low.endswith(".webp"):
            # Animated objects render through QMovie (the image pipeline), not the
            # video player — that's the whole point of the WebP path (alpha).
            self.asset_preview_movie = QMovie(path)
            self.asset_preview_movie.setScaledSize(
                QImageReader(path).size().scaled(
                    self.asset_preview_stack.size(), Qt.KeepAspectRatio))
            self.asset_preview_label.setMovie(self.asset_preview_movie)
            self.asset_preview_movie.start()
            self.asset_preview_stack.setCurrentIndex(0)
        else:  # video → play it on loop, muted
            self._ensure_preview_player()           # builds the widgets on first use
            self.asset_preview_stack.setCurrentIndex(1)
            self.asset_preview_player.setSource(QUrl.fromLocalFile(path))
            self.asset_preview_player.play()

    def _stop_preview_movie(self):
        """Stop/clear any animated-WebP preview so it doesn't keep running or
        linger on the label when another asset is selected."""
        mv = getattr(self, "asset_preview_movie", None)
        if mv is not None:
            mv.stop()
            self.asset_preview_label.setMovie(None)
            self.asset_preview_movie = None

    def _on_asset_double_clicked(self, idx):
        path = self._asset_path_for_index(idx)
        if not path:
            return
        # Place at the center of the GM's current view.
        center = self.canvas_view.mapToScene(self.canvas_view.viewport().rect().center())
        self._place_asset(path, center)

    def _asset_is_in_library(self, path):
        """True if `path` already lives under the asset library (i.e. it came
        from the Assets browser or a loaded map, not an external file drop)."""
        abs_src = os.path.abspath(path)
        abs_lib = os.path.abspath(self.asset_dir)
        return abs_src == abs_lib or abs_src.startswith(abs_lib + os.sep)

    def _library_category_of(self, path):
        """The library category subfolder a library asset sits in (e.g.
        'objects'), or DEFAULT_DROP_CATEGORY if it's at the library root."""
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(self.asset_dir))
        head = rel.replace(os.sep, "/").split("/", 1)[0]
        return head if head in ASSET_CATEGORIES else DEFAULT_DROP_CATEGORY

    # ── Asset library organisation (subfolders, rename, move, delete) ────────
    # Asset refs in map JSON are library-relative POSIX paths ("backgrounds/x.png"),
    # so moving/renaming a file breaks every map that uses it. Because the move
    # happens inside the app we always know old→new and rewrite refs everywhere
    # (every map on disk + the open map's live items). Deletes can't be repaired,
    # so they warn first; open_map() also skips any ref whose file is gone.

    def _is_category_root(self, path):
        """True for the three fixed top-level category folders, which the user
        may add subfolders to but must not rename/move/delete."""
        p = os.path.abspath(path)
        return (os.path.dirname(p) == os.path.abspath(self.asset_dir)
                and os.path.basename(p) in ASSET_CATEGORIES)

    @staticmethod
    def _ref_matches(ref, rel, is_dir):
        """Does a stored asset ref point at `rel` (a file), or live under it
        (a folder)? Both are library-relative POSIX paths."""
        if not ref:
            return False
        if is_dir:
            return ref.startswith(rel.rstrip("/") + "/")
        return ref == rel

    def _remap_ref(self, ref, old_rel, new_rel, is_dir):
        """Return the rewritten ref if `ref` is affected by moving old_rel→new_rel,
        else None. For a folder, the path under it is preserved."""
        if not self._ref_matches(ref, old_rel, is_dir):
            return None
        if not is_dir:
            return new_rel
        tail = ref[len(old_rel.rstrip("/")) + 1:]
        return new_rel.rstrip("/") + "/" + tail

    def _maps_referencing(self, rel, is_dir):
        """Map files (paths relative to maps_dir) that reference `rel`."""
        hits = []
        for root, _dirs, files in os.walk(self.maps_dir):
            for fn in files:
                if not fn.lower().endswith(".json"):
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p) as f:
                        data = json.load(f)
                except Exception:
                    continue
                if any(self._ref_matches(it.get("asset", ""), rel, is_dir)
                       for it in data.get("items", [])):
                    hits.append(os.path.relpath(p, self.maps_dir).replace(os.sep, "/"))
        return hits

    def _rewrite_asset_refs(self, old_rel, new_rel, is_dir):
        """Rewrite every map reference from old_rel→new_rel — both in the map
        JSONs on disk and in the currently-open map's live scene items."""
        for root, _dirs, files in os.walk(self.maps_dir):
            for fn in files:
                if not fn.lower().endswith(".json"):
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p) as f:
                        data = json.load(f)
                except Exception:
                    continue
                changed = False
                for it in data.get("items", []):
                    nr = self._remap_ref(it.get("asset", ""), old_rel, new_rel, is_dir)
                    if nr is not None:
                        it["asset"] = nr
                        changed = True
                if changed:
                    with open(p, "w") as f:
                        json.dump(data, f, indent=2)
        # Keep the open map's items in sync so a later save writes the new ref
        # (and doesn't try to re-import from the now-moved asset_path).
        for it in self.canvas_view.scene.items():
            ref = getattr(it, "asset_filename", None)
            nr = self._remap_ref(ref, old_rel, new_rel, is_dir) if ref else None
            if nr is not None:
                it.asset_filename = nr
                it.asset_path = os.path.join(self.asset_dir, nr)
        # Keep the saved party roster in sync — a renamed/moved token must stay a
        # member (membership is keyed by asset path), or it would lose its gold
        # ring and break Place/dedup.
        party_changed = False
        for mem in getattr(self, "party_members", []):
            nr = self._remap_ref(mem.get("asset", ""), old_rel, new_rel, is_dir)
            if nr is not None:
                mem["asset"] = nr
                mem["name"] = os.path.splitext(os.path.basename(nr))[0]
                party_changed = True
        if party_changed:
            self._refresh_party_ui()
        # On-map items now carry the new refs → re-sync the gold rings against the
        # (also-updated) roster.
        self._refresh_party_token_rings()

    def _relocate_asset(self, src, dst):
        """Move/rename a library file or folder src→dst and rewrite map refs.
        Returns True on success. Rejects collisions and folder-into-itself."""
        src, dst = os.path.abspath(src), os.path.abspath(dst)
        if src == dst:
            return False
        is_dir = os.path.isdir(src)
        if is_dir and (dst == src or dst.startswith(src + os.sep)):
            QMessageBox.warning(self, "Move", "Can't move a folder into itself.")
            return False
        # Keep assets inside their own top-level category (backgrounds/objects/
        # tokens) — moving across categories would change an asset's category.
        if self._library_category_of(src) != self._library_category_of(dst):
            QMessageBox.warning(
                self, "Move",
                "Assets can only be moved within their own category folder "
                "(backgrounds, objects, or tokens).")
            return False
        if os.path.exists(dst):
            QMessageBox.warning(
                self, "Move",
                f"“{os.path.basename(dst)}” already exists in that folder.")
            return False
        old_rel = os.path.relpath(src, self.asset_dir).replace(os.sep, "/")
        new_rel = os.path.relpath(dst, self.asset_dir).replace(os.sep, "/")
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(src, dst)
        except OSError as e:
            QMessageBox.critical(self, "Move Failed", str(e))
            return False
        # Carry a token's ".tokenmeta" sidecar alongside its baked PNG (folder
        # moves take their contents with them, so only the file case needs this).
        if not is_dir:
            sc_src = self._token_meta_path(src)
            if os.path.exists(sc_src):
                try:
                    shutil.move(sc_src, self._token_meta_path(dst))
                except OSError as e:
                    log.warning("Couldn't move token sidecar: %s", e)
        self._rewrite_asset_refs(old_rel, new_rel, is_dir)
        return True

    def _move_assets_into(self, srcs, dest_dir):
        """Move each src into dest_dir (used by drag-drop and the Move dialog)."""
        moved = False
        for src in srcs:
            if not self._asset_is_in_library(src):
                continue
            if self._is_category_root(src):
                QMessageBox.warning(self, "Move", "Category folders can't be moved.")
                continue
            if os.path.dirname(os.path.abspath(src)) == os.path.abspath(dest_dir):
                continue                                    # already in place
            if self._relocate_asset(src, os.path.join(dest_dir, os.path.basename(src))):
                moved = True
        return moved

    def on_asset_context_menu(self, point, view=None):
        view = view or self.asset_view
        idx = view.indexAt(point)
        if idx.isValid():
            path = self.asset_fs_model.filePath(idx)
        else:
            # Empty-space click → act on the view's root directory, so you can
            # still create a subfolder there (e.g. in the Tokens tab, whose root
            # is tokens/). The library root itself has the fixed categories, so
            # offer nothing there.
            path = self.asset_fs_model.filePath(view.rootIndex())
            if not path or os.path.abspath(path) == os.path.abspath(self.asset_dir):
                return

        is_dir = os.path.isdir(path)
        is_cat = self._is_category_root(path)

        menu = QMenu(self)
        a_add = a_add_hidden = a_edit = None
        if not is_dir:                   # files can be placed on the current map
            a_add = menu.addAction("Add to Map")
            # Place already hidden from players — lets the GM stage monsters/
            # objects/backgrounds before revealing them (visible in the GM view,
            # skipped in the player view; see Per-item player visibility).
            a_add_hidden = menu.addAction("Add to Map (Hidden)")
            if self._library_category_of(path) == "tokens":
                a_edit = menu.addAction("Edit Token…")
            menu.addSeparator()
        a_new = menu.addAction("New Folder…") if is_dir else None
        a_dup = menu.addAction("Duplicate") if not is_dir else None
        a_ren = a_move = a_del = None
        if not is_cat:                   # the fixed category roots are protected
            a_ren  = menu.addAction("Rename…")
            a_move = menu.addAction("Move to…")
            menu.addSeparator()
            a_del  = menu.addAction("Delete")
        if menu.isEmpty():
            return
        act = menu.exec(view.viewport().mapToGlobal(point))
        if act is None:
            return
        if act == a_add or act == a_add_hidden:
            # Same as double-click: place at the centre of the GM view.
            center = self.canvas_view.mapToScene(
                self.canvas_view.viewport().rect().center())
            item = self._place_asset(path, center)
            if item and act == a_add_hidden:        # falsy on a cancelled import
                self._hide_item_from_players(item)
        elif act == a_edit:
            self._edit_token_file(path)
        elif act == a_new:
            self._new_asset_folder(path)
        elif act == a_dup:
            self._duplicate_asset(path)
        elif act == a_ren:
            self._rename_asset(path)
        elif act == a_move:
            self._move_asset_dialog(path)
        elif act == a_del:
            self._delete_asset(path)

    def _hide_item_from_players(self, item):
        """Mark a freshly placed item hidden from players (GM still sees it, with
        the hidden marker), and refresh both views + the Layers list. Mirrors the
        state changes in items._toggle_player_visibility for a single new item."""
        item.visible_to_player = False
        item.update()                               # GM marker + player hide
        pw = getattr(self, "player_window", None)
        if pw is not None:
            pw.canvas_view.viewport().update()
        self.update_layers_list()
        self.mark_dirty()

    def _new_asset_folder(self, dir_path):
        if not os.path.isdir(dir_path):
            dir_path = os.path.dirname(dir_path)
        name, ok = QInputDialog.getText(self, "New Folder", "Folder name:")
        name = name.strip() if ok else ""
        if not name:
            return
        try:
            os.makedirs(os.path.join(dir_path, name), exist_ok=False)
        except OSError as e:
            QMessageBox.warning(self, "New Folder", str(e))

    def _rename_asset(self, path):
        old = os.path.basename(path)
        name, ok = QInputDialog.getText(self, "Rename", "New name:", text=old)
        name = name.strip() if ok else ""
        if not name or name == old:
            return
        # Preserve the file's extension. The browser filters to ASSET_EXTS and
        # HIDES non-matching files, so a token renamed to e.g. "goblin" (dropping
        # the ".png") would silently vanish from the list. Re-append the original
        # extension when the typed name doesn't already end with it. Folders have
        # no extension, so this is a no-op for them.
        ext = os.path.splitext(old)[1]
        if ext and not name.lower().endswith(ext.lower()):
            name += ext
        self._relocate_asset(path, os.path.join(os.path.dirname(path), name))

    def _duplicate_asset(self, path):
        """Copy a library asset to a new collision-free "<name> copy" file. For a
        token, the `.tokenmeta` sidecar is copied too so the duplicate is a fully
        independent, re-editable token. The new file is a brand-new asset (no map
        references it), so nothing needs rewriting; the browser auto-refreshes."""
        if os.path.isdir(path):
            return
        directory = os.path.dirname(path)
        stem, ext = os.path.splitext(os.path.basename(path))
        candidate = os.path.join(directory, f"{stem} copy{ext}")
        i = 2
        while os.path.exists(candidate):
            candidate = os.path.join(directory, f"{stem} copy {i}{ext}")
            i += 1
        try:
            shutil.copy2(path, candidate)
        except OSError as e:
            QMessageBox.warning(self, "Duplicate", str(e))
            return
        sidecar = self._token_meta_path(path)        # tokens/<name>.tokenmeta
        if os.path.exists(sidecar):
            try:
                shutil.copy2(sidecar, self._token_meta_path(candidate))
            except OSError as e:
                log.warning("Couldn't copy token sidecar: %s", e)
        self.statusBar().showMessage(
            f"Duplicated → {os.path.basename(candidate)}", 3000)

    def _move_asset_dialog(self, path):
        dest = QFileDialog.getExistingDirectory(
            self, "Move to folder", self.asset_dir)
        if not dest:
            return
        if not self._asset_is_in_library(dest):
            QMessageBox.warning(
                self, "Move", "Choose a folder inside the asset library.")
            return
        self._move_assets_into([path], dest)

    def _delete_asset(self, path):
        if self._is_category_root(path):
            QMessageBox.warning(self, "Delete", "Category folders can't be deleted.")
            return
        is_dir = os.path.isdir(path)
        rel = os.path.relpath(path, self.asset_dir).replace(os.sep, "/")
        refs = self._maps_referencing(rel, is_dir)
        name = os.path.basename(path)
        msg = f"Delete “{name}”?"
        if refs:
            shown = "\n".join("  • " + m for m in refs[:12])
            more = f"\n  …and {len(refs) - 12} more" if len(refs) > 12 else ""
            msg += ("\n\nThese maps reference it and will show it as missing "
                    f"until you replace it:\n{shown}{more}")
        if QMessageBox.question(self, "Delete Asset", msg,
                                QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            if is_dir:
                shutil.rmtree(path)
            else:
                os.remove(path)
                # Remove a token's ".tokenmeta" sidecar along with its PNG.
                sc = self._token_meta_path(path)
                if os.path.exists(sc):
                    os.remove(sc)
        except OSError as e:
            QMessageBox.critical(self, "Delete Failed", str(e))

    def _ask_asset_category(self, filename):
        """Ask whether a freshly imported asset is a Background, Object, or Token.
        Presented as radio buttons; the last choice is remembered and preset for
        the next import. Returns the category folder name
        ('backgrounds'/'objects'/'tokens') or None if cancelled."""
        last = getattr(self, "_last_asset_category", DEFAULT_DROP_CATEGORY)

        dlg = QDialog(self)
        dlg.setWindowTitle("Import Asset")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel(f"Add “{filename}” as:"))

        radios = {}
        for cat, label in (("backgrounds", "Background"),
                           ("objects", "Object"),
                           ("tokens", "Token")):
            rb = QRadioButton(label)
            rb.setChecked(cat == last)
            lay.addWidget(rb)
            radios[cat] = rb
        if not any(rb.isChecked() for rb in radios.values()):
            radios[DEFAULT_DROP_CATEGORY].setChecked(True)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)

        if dlg.exec() != QDialog.Accepted:
            return None
        chosen = next(cat for cat, rb in radios.items() if rb.isChecked())
        self._last_asset_category = chosen          # remember for next time
        return chosen

    def _place_asset(self, path, scene_pos=None):
        # Shared "add an asset to the current map" path used by the canvas drop and
        # the Assets browser (double-click). Returns the placed item (truthy) if one
        # was added, else False — callers that only test truthiness are unaffected;
        # place_party() uses the returned item to set per-instance fields.
        low = path.lower()
        if low.endswith((".jpg", ".jpeg", ".png")):
            kind = "image"
        elif low.endswith(".webp"):
            kind = "anim"                          # animated WebP object (with alpha)
        elif low.endswith((".mp4", ".avi", ".mov", ".m4v", ".webm")):
            kind = "video"
        else:
            return False

        # Library assets (from the Assets browser / a loaded map) keep their
        # existing category; only external file drops prompt for one.
        in_library = self._asset_is_in_library(path)
        if in_library:
            category = self._library_category_of(path)
        else:
            category = self._ask_asset_category(os.path.basename(path))
            if category is None:
                return False                       # user cancelled the import

        # Tokens are images only for now (round VTT-style crop). Reject video/anim
        # before we go any further (auto-creating a map, etc.).
        if category == "tokens" and kind != "image":
            QMessageBox.information(
                self, "Tokens",
                "Tokens currently support image files only.")
            return False

        # No map open (welcome screen) → start a fresh, default-named map first.
        if self.current_map_path is None:
            self._create_new_map_in(self.maps_dir, start_editing=False)
            if self.current_map_path is None:
                return False

        # A freshly dropped/added asset is almost certainly meant to be moved, so
        # release the lock that governs its category (tokens → Lock Tokens, else
        # Lock Assets) so the new item is immediately movable.
        if category == "tokens":
            if self.locktokens_checkBox.isChecked():
                self.locktokens_checkBox.setChecked(False)
        elif self.lockmap_checkBox.isChecked():
            self.lockmap_checkBox.setChecked(False)

        if kind == "image":
            if category == "tokens":
                if in_library:
                    # Already a baked round token asset → place it directly
                    # (don't re-open the tokenizer for a library drag).
                    rel = os.path.relpath(path, self.asset_dir).replace(os.sep, "/")
                    item = InteractivePixmapItem(QPixmap(path))
                    item.asset_filename = rel
                    item.asset_category = "tokens"
                    item.is_token = True
                else:
                    # External image → run it through the tokenizer first.
                    item = self._build_token_item(path)
                    if item is None:
                        return False               # user cancelled the tokenizer
            else:
                item = InteractivePixmapItem(QPixmap(path))
                item.asset_path = path             # imported into the library on save
                item.asset_category = category     # …into this category subfolder
        else:
            # Resolve the library asset, then choose the item type from the FINAL
            # extension — a transparency conversion can fall back to a plain video.
            if kind == "anim":
                asset = self._import_asset(path, category)   # no-op copy if in library
            elif category == "objects" and not in_library \
                    and video_has_alpha(path) and _ffmpeg_tool("ffmpeg") is not None:
                # Transparent video dropped as an object → convert to animated WebP
                # so its alpha actually renders (the video player would strip it).
                asset = self._import_animated_object(path, category)
            else:
                asset = self._import_video(path, category)   # no-op copy if in library
            if asset is None:
                return False                       # user cancelled the import
            if asset.lower().endswith(".webp"):
                item = AnimatedItem(os.path.join(self.asset_dir, asset))
            else:
                item = InteractiveVideoItem(os.path.join(self.asset_dir, asset))
            item.asset_filename = asset

        self.canvas_view.scene.addItem(item)
        if scene_pos is not None:
            item.setPos(scene_pos)
        # Tokens never overlap — bump a freshly placed one to the nearest free cell.
        if getattr(item, "is_token", False):
            self._resolve_token_overlap(item)
        # A freshly added item becomes the sole selection — drop any prior one.
        self.canvas_view.scene.clearSelection()
        item.setSelected(True)
        item.setFocus()

        # Slot it onto the top of its own layer (backgrounds/objects/tokens); this
        # also restacks, refreshes the Layers tree, and marks dirty.
        self.bring_item_to_layer_top(item)
        self.refresh_canvas_extent()
        if getattr(item, "is_token", False):
            self._refresh_party_token_rings()    # gold ring if this token's asset is in the party
        return item

    # ── Tokens ───────────────────────────────────────────────────────────
    # A token is a baked round PNG in the tokens/ library (rendered by a plain
    # InteractivePixmapItem) plus a sibling ".tokenmeta" sidecar holding the
    # tokenizer settings + the (downscaled) source image, so it can be re-edited.

    def _token_meta_path(self, png_path):
        return os.path.splitext(png_path)[0] + ".tokenmeta"

    def _unique_token_png(self, stem):
        """Return a non-colliding tokens/<stem>.png absolute path."""
        tokens_dir = os.path.join(self.asset_dir, "tokens")
        os.makedirs(tokens_dir, exist_ok=True)
        stem = stem.strip() or "token"
        candidate = os.path.join(tokens_dir, stem + ".png")
        i = 1
        while os.path.exists(candidate):
            candidate = os.path.join(tokens_dir, f"{stem} {i}.png")
            i += 1
        return candidate

    def _prep_token_source(self, img, limit=1600):
        """Cap the source resolution so the re-edit sidecar stays small. Params
        are taken in this image's coordinate space, so the same (capped) image is
        what the sidecar stores — keeping re-edit consistent."""
        if max(img.width(), img.height()) > limit:
            img = img.scaled(limit, limit, Qt.KeepAspectRatio,
                             Qt.SmoothTransformation)
        return img

    def _encode_image_b64(self, image):
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QBuffer.WriteOnly)
        image.save(buf, "PNG")
        buf.close()
        return bytes(ba.toBase64()).decode("ascii")

    def _write_token_sidecar(self, png_path, params, src_image):
        meta = dict(params)
        meta["version"] = 1
        meta["source"] = self._encode_image_b64(src_image)
        try:
            with open(self._token_meta_path(png_path), "w") as f:
                json.dump(meta, f)
        except OSError as e:
            log.warning("Couldn't write token sidecar: %s", e)

    def _read_token_sidecar(self, png_path):
        """Return (params_dict, source_QImage) or (None, None) if no sidecar."""
        mp = self._token_meta_path(png_path)
        if not os.path.exists(mp):
            return None, None
        try:
            with open(mp) as f:
                meta = json.load(f)
        except (OSError, ValueError):
            return None, None
        img = QImage()
        b64 = meta.get("source")
        if b64:
            img.loadFromData(QByteArray.fromBase64(b64.encode("ascii")), "PNG")
        return meta, img

    def _build_token_item(self, source_path):
        """Run an external image through the tokenizer, bake it into the library,
        and return a locked token InteractivePixmapItem (or None if cancelled)."""
        img = QImage(source_path)
        if img.isNull():
            QMessageBox.warning(self, "Tokenizer",
                                f"Couldn't load image:\n{source_path}")
            return None
        img = self._prep_token_source(img)
        dlg = TokenizerDialog(img, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return None
        baked = dlg.result_image()
        stem = os.path.splitext(os.path.basename(source_path))[0]
        png_path = self._unique_token_png(stem)
        if not baked.save(png_path, "PNG"):
            QMessageBox.warning(self, "Tokenizer", "Failed to save the token image.")
            return None
        self._write_token_sidecar(png_path, dlg.result_params(), img)
        rel = os.path.relpath(png_path, self.asset_dir).replace(os.sep, "/")
        item = InteractivePixmapItem(QPixmap.fromImage(baked))
        item.asset_filename = rel
        item.asset_category = "tokens"
        item.is_token = True
        return item

    def edit_token(self, item):
        """Re-open a placed token in the tokenizer and re-bake it in place."""
        rel = getattr(item, "asset_filename", None)
        if not rel:
            return
        self._edit_token_file(os.path.join(self.asset_dir, rel))

    def _edit_token_file(self, png_path):
        """Re-open a library token PNG in the tokenizer, re-bake it in place, and
        refresh any placed items that reference it. Used by both the placed-item
        "Edit Token…" menu and the Assets-browser token context menu."""
        params, src = self._read_token_sidecar(png_path)
        if src is None or src.isNull():
            # No saved source (legacy token / sidecar lost) → fall back to the
            # baked image itself; the crop can't be widened but size/border still
            # work.
            src = QImage(png_path)
            params = None
        dlg = TokenizerDialog(src, params, parent=self)
        if dlg.exec() != QDialog.Accepted:
            return
        baked = dlg.result_image()
        if not baked.save(png_path, "PNG"):
            QMessageBox.warning(self, "Tokenizer", "Failed to save the token image.")
            return
        self._write_token_sidecar(png_path, dlg.result_params(), src)
        # Update every placed item that references this token (a token PNG can be
        # reused across placements; editing the library file updates them all).
        rel = os.path.relpath(png_path, self.asset_dir).replace(os.sep, "/")
        pm = QPixmap.fromImage(baked)
        for it in self._map_items():
            if (getattr(it, "is_token", False)
                    and getattr(it, "asset_filename", None) == rel):
                # A placed item with a per-instance colour keeps it (re-baked over
                # the freshly-edited base); others take the new baked PNG as-is.
                if getattr(it, "token_color_override", None):
                    self._bake_token_with_color(it, it.token_color_override)
                else:
                    it.setPixmap(pm)
                    it.original_pixmap = pm      # keep rotate bakes correct
                    it.update()
        self.refresh_canvas_extent()
        self.update_layers_list()
        # Refresh the Tokens-tab preview so the edited thumbnail isn't stale.
        if hasattr(self, "token_view"):
            self._on_token_selected(self.token_view.currentIndex(), None)
        self.mark_dirty()

    def _bake_token_with_color(self, item, color_hex):
        """Re-bake a placed token's pixmap with a per-instance border colour,
        WITHOUT touching the shared library PNG. `color_hex` None removes the
        override (reverts to the library PNG). Returns True on success."""
        rel = getattr(item, "asset_filename", None)
        if not rel:
            return False
        png_path = os.path.join(self.asset_dir, rel)
        if color_hex is None:
            # Revert: reload the library PNG straight from disk.
            pm = QPixmap(png_path)
            if pm.isNull():
                return False
            item.token_color_override = None
        else:
            params, src = self._read_token_sidecar(png_path)
            if src is None or src.isNull():
                src = QImage(png_path)           # legacy/lost sidecar → bake over PNG
                params = {}
            params = dict(params or {})
            params["borderEnabled"] = True
            params["borderColor"] = color_hex
            baked = bake_token(src, params)
            if baked.isNull():
                return False
            pm = QPixmap.fromImage(baked)
            item.token_color_override = color_hex
        item.setPixmap(pm)
        item.original_pixmap = pm                # keep rotate bakes correct
        item.update()
        return True

    def recolor_tokens(self, items, color_hex):
        """Apply a per-instance border colour to one or more placed tokens."""
        changed = False
        for it in items:
            if getattr(it, "is_token", False) and self._bake_token_with_color(it, color_hex):
                changed = True
        if not changed:
            return
        pw = getattr(self, "player_window", None)
        if pw is not None:
            pw.canvas_view.viewport().update()
        self.update_layers_list()
        self.mark_dirty()

    def _resolve_token_overlap(self, item):
        """Nudge `item` (a placed token) to the nearest free grid cell so tokens
        never overlap each other. Works off scene bounding rects (so rotation is
        handled) and moves in whole grid steps (preserving the grid snap). Tokens
        may still sit on top of backgrounds/objects — only token↔token overlap is
        resolved. No-op if the slot is already clear."""
        if not getattr(item, "is_token", False):
            return
        grid = self.canvas_view.grid_size
        # QGraphicsPixmapItem.boundingRect() carries a 0.5px margin per side, so a
        # cell-filling token is ~73px — shrink by 1px so tokens in *adjacent* cells
        # (edges touching) don't read as overlapping; only real overlap does.
        shrink = lambda r: r.adjusted(1, 1, -1, -1)
        others = [shrink(it.sceneBoundingRect()) for it in self._map_items()
                  if it is not item and getattr(it, "is_token", False)]
        if not others:
            return
        rect = shrink(item.sceneBoundingRect())
        overlaps = lambda r: any(r.intersects(o) for o in others)
        if not overlaps(rect):
            return
        # Spiral outward ring by ring (Chebyshev distance in cells); within a ring
        # pick the candidate closest to the original spot. First free wins.
        for ring in range(1, 256):
            best = None
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) != ring:
                        continue                 # only the new outer ring
                    if overlaps(rect.translated(dx * grid, dy * grid)):
                        continue
                    d = dx * dx + dy * dy
                    if best is None or d < best[0]:
                        best = (d, dx, dy)
            if best is not None:
                _, dx, dy = best
                item.moveBy(dx * grid, dy * grid)
                return

    # ── Player party (one saved roster, in settings.json) ────────────────────
    # A party member references a library token by its asset path and captures the
    # token's ring colour + player-control flag at add time. Identity = asset path.
    def _party_key(self, item):
        return getattr(item, "asset_filename", None)

    def _party_index(self, asset):
        for i, m in enumerate(self.party_members):
            if m.get("asset") == asset:
                return i
        return -1

    def is_in_party(self, item):
        key = self._party_key(item)
        return bool(key) and self._party_index(key) >= 0

    def set_party_membership(self, items, member):
        """Add or remove tokens from the party (called from the token context
        menu, possibly for a multi-selection)."""
        changed = False
        for it in items:
            key = self._party_key(it)
            if not key or not getattr(it, "is_token", False):
                continue
            idx = self._party_index(key)
            if member and idx < 0:
                self.party_members.append({
                    "asset": key,
                    "name": os.path.splitext(os.path.basename(key))[0],
                    "color": getattr(it, "token_color_override", None),
                    "controllable": getattr(it, "player_controllable", False),
                })
                changed = True
            elif member and idx >= 0:
                # Refresh the captured appearance from the live token.
                self.party_members[idx]["color"] = getattr(it, "token_color_override", None)
                self.party_members[idx]["controllable"] = getattr(it, "player_controllable", False)
            elif not member and idx >= 0:
                self.party_members.pop(idx)
                changed = True
        if changed:
            self._refresh_party_ui()
            self._refresh_party_token_rings()    # update gold rings on matching tokens
            self.statusBar().showMessage(
                f"Party: {len(self.party_members)} member(s)", 3000)

    def _assets_on_map(self):
        """Set of asset paths for the token items currently on the map."""
        return {getattr(it, "asset_filename", None)
                for it in self._map_items() if getattr(it, "is_token", False)}

    def _refresh_party_token_rings(self):
        """Keep each placed token's `in_party` flag (the gold ring) in sync with the
        roster. Membership is by asset, so every on-map token whose asset is in the
        party gets the ring. Cheap — called after roster/placement/load changes."""
        assets = {m.get("asset") for m in self.party_members}
        changed = False
        for it in self._map_items():
            if not getattr(it, "is_token", False):
                continue
            new = getattr(it, "asset_filename", None) in assets
            if getattr(it, "in_party", False) != new:
                it.in_party = new
                it.update()                      # repaints GM + player (shared scene)
                changed = True
        if changed:
            pw = getattr(self, "player_window", None)
            if pw is not None:
                pw.canvas_view.viewport().update()

    # Player (party) tokens are a session overlay: not saved with any map, and
    # they travel to whatever map you open next, keeping their positions (bumping
    # only where they'd collide with the new map's tokens).
    def _snapshot_player_tokens(self):
        """Capture the on-canvas player tokens' state before a map switch."""
        assets = {m.get("asset") for m in self.party_members}
        snaps = []
        for it in self._map_items():
            if getattr(it, "is_token", False) and getattr(it, "asset_filename", None) in assets:
                pm = it.pixmap()
                snaps.append({
                    "asset": it.asset_filename,
                    "pos": (it.pos().x(), it.pos().y()),
                    "rot": it.rotation(), "z": it.zValue(),
                    "size": (pm.width(), pm.height()),
                    "color": getattr(it, "token_color_override", None),
                    "controllable": getattr(it, "player_controllable", False),
                    "id": getattr(it, "token_id", None),
                })
        return snaps

    def _restore_player_tokens(self, snaps):
        """Re-add carried-over player tokens to the freshly loaded map at their
        prior positions; `_resolve_token_overlap` bumps any that collide with the
        new map's tokens. Skips an asset already present (e.g. a legacy map that
        still has the token saved)."""
        if not snaps:
            return
        present = self._assets_on_map()
        for s in snaps:
            if s["asset"] in present:
                continue
            abs_path = os.path.join(self.asset_dir, s["asset"])
            if not os.path.exists(abs_path):
                continue
            pix = QPixmap(abs_path)
            it = InteractivePixmapItem(pix)
            w, h = s["size"]
            it.setPixmap(pix.scaled(int(w), int(h), Qt.KeepAspectRatio, Qt.SmoothTransformation))
            it.asset_filename = s["asset"]
            it.asset_category = "tokens"
            it.is_token = True
            it.in_party = True
            it.token_id = s["id"] or uuid.uuid4().hex
            it.player_controllable = bool(s["controllable"])
            self.canvas_view.scene.addItem(it)
            it.setZValue(s["z"])
            it.setRotation(s["rot"])
            it.setPos(*s["pos"])                 # token grid-snap via itemChange
            if s["color"]:
                self._bake_token_with_color(it, s["color"])
            self._resolve_token_overlap(it)      # bump if it lands on a map token

    def place_party(self):
        """Stamp every party member that isn't already on the current map, at the
        GM-view centre, fanned out by the overlap resolver. Existing party tokens
        are left untouched (dedup by asset path)."""
        if not self.party_members:
            QMessageBox.information(self, "Party",
                                    "The party is empty. Add tokens via a token's "
                                    "right-click menu → “Add to Party”.")
            return
        present = self._assets_on_map()
        center = self.canvas_view.mapToScene(
            self.canvas_view.viewport().rect().center())
        missing_files, placed = [], 0
        for m in self.party_members:
            asset = m.get("asset")
            if not asset or asset in present:
                continue                          # already on this map → skip
            abs_path = os.path.join(self.asset_dir, asset)
            if not os.path.exists(abs_path):
                missing_files.append(asset)
                continue
            item = self._place_asset(abs_path, center)
            if not item:
                continue
            item.player_controllable = bool(m.get("controllable"))
            if m.get("color"):
                self._bake_token_with_color(item, m["color"])
            placed += 1
        self._refresh_party_token_rings()        # gold rings on the freshly placed tokens
        if placed:
            self.statusBar().showMessage(f"Placed {placed} party token(s)", 3000)
        elif not missing_files:
            self.statusBar().showMessage("All party members are already on this map.", 3000)
        if missing_files:
            shown = "\n".join("  • " + a for a in missing_files[:12])
            QMessageBox.warning(
                self, "Party",
                "These party tokens are missing from the library and were "
                f"skipped:\n{shown}")

    def remove_party_from_map(self):
        """Remove every token on the current map that belongs to the party."""
        keys = {m.get("asset") for m in self.party_members}
        removed = False
        for it in list(self._map_items()):
            if getattr(it, "is_token", False) and getattr(it, "asset_filename", None) in keys:
                self.canvas_view.scene.removeItem(it)
                removed = True
        if removed:
            self.refresh_canvas_extent()
            self.update_layers_list()
            self.mark_dirty()
            self.statusBar().showMessage("Removed the party from this map.", 3000)

    def clear_party(self):
        if not self.party_members:
            return
        if QMessageBox.question(
                self, "Disband Party",
                f"Disband the party — remove all {len(self.party_members)} member(s) "
                "from the roster and take their tokens off this map?"
        ) == QMessageBox.Yes:
            # Pull the player tokens off the map FIRST (it matches by roster asset),
            # then empty the roster.
            self.remove_party_from_map()
            self.party_members = []
            self._refresh_party_ui()
            self._refresh_party_token_rings()    # drop any remaining gold rings
            self.statusBar().showMessage("Party disbanded.", 3000)

    def _wire_party_controls(self):
        """Connect the Designer-added party widgets (all optional — guarded with
        getattr so the app still runs if they haven't been added yet):
          • placeParty_btn     (QPushButton)  → place_party
          • removeParty_btn    (QPushButton)  → remove_party_from_map
          • clearParty_btn     (QPushButton)  → clear_party
          • partyMembers_comboBox (QComboBox) → live roster display
        """
        b = getattr(self, "placeParty_btn", None)
        if b is not None:
            b.clicked.connect(self.place_party)
            b.setToolTip("Place the party's tokens on the current map "
                         "(skips any that are already on it).")
        b = getattr(self, "removeParty_btn", None)
        if b is not None:
            b.clicked.connect(self.remove_party_from_map)
            b.setToolTip("Remove all of the party's tokens from the current map "
                         "(the saved party roster is kept).")
        b = getattr(self, "clearParty_btn", None)
        if b is not None:
            b.clicked.connect(self.clear_party)
            b.setToolTip("Empty the saved party roster "
                         "(tokens already placed on maps are not affected).")
        c = getattr(self, "partyMembers_comboBox", None)
        if c is not None:
            c.setToolTip("Current party members. Add or remove members from a "
                         "token's right-click menu → “Add to Party”.")
        self._refresh_party_ui()

    def _refresh_party_ui(self):
        """Repopulate the Members combo and enable/disable the party buttons to
        match the current roster + whether a map is open. Call after any change to
        self.party_members."""
        has_members = bool(self.party_members)
        has_map = getattr(self, "current_map_path", None) is not None
        combo = getattr(self, "partyMembers_comboBox", None)
        if combo is not None:
            combo.blockSignals(True)
            combo.clear()
            if has_members:
                for m in self.party_members:
                    abs_path = os.path.join(self.asset_dir, m.get("asset", ""))
                    name = m.get("name") or m.get("asset", "?")
                    if os.path.exists(abs_path):
                        combo.addItem(QIcon(QPixmap(abs_path).scaled(
                            20, 20, Qt.KeepAspectRatio, Qt.SmoothTransformation)), name)
                    else:
                        combo.addItem(name)
            else:
                combo.addItem("(empty)")
            combo.blockSignals(False)
        for attr, need_map in (("placeParty_btn", True), ("removeParty_btn", True),
                               ("clearParty_btn", False)):
            w = getattr(self, attr, None)
            if w is not None:
                w.setEnabled(has_members and (has_map or not need_map))

    # ── LAN web sharing ──────────────────────────────────────────────────────
    def _token_id(self, item):
        """Stable per-item id for the web protocol, assigned lazily."""
        tid = getattr(item, "token_id", None)
        if not tid:
            tid = uuid.uuid4().hex
            item.token_id = tid
        return tid

    def apply_token_move(self, token_id, nx, ny):
        """Single mutation chokepoint for a remote token drag. `nx`/`ny` are the
        token's new CENTRE in normalised player-viewport coords (0..1). Finds the
        matching *player-controllable* token, moves it (reusing the grid-snap +
        non-overlap resolve), and marks the map dirty. The shared scene means the
        GM + Player views update for free, and the next stream tick reflects it."""
        pw = getattr(self, "player_window", None)
        cv = getattr(pw, "canvas_view", None) if pw else None
        if cv is None:
            return                               # no player view → nothing to map against
        target = None
        for it in self._map_items():
            if (getattr(it, "is_token", False)
                    and getattr(it, "player_controllable", False)
                    and self._token_id(it) == token_id):
                target = it
                break
        if target is None:
            return                               # unknown / not permitted → ignore
        vp = cv.viewport()
        center = cv.mapToScene(int(nx * (vp.width() or 1)),
                               int(ny * (vp.height() or 1)))
        pm = target.pixmap()
        target.setPos(center.x() - pm.width() / 2.0, center.y() - pm.height() / 2.0)
        self._resolve_token_overlap(target)      # snap + keep tokens from overlapping
        self.update_layers_list()
        self.mark_dirty()

    def _web_sharing_active(self):
        return self.web_server is not None and self.web_server.running

    def _web_base_port(self):
        """The port sharing should try first: the user's custom value if enabled,
        else the app default. start() auto-falls-back from here if it's taken."""
        from arcaneatlas.webserver import DEFAULT_WEB_PORT
        return int(self.web_port) if self.web_port_custom else DEFAULT_WEB_PORT

    def _start_web_sharing(self):
        """Start the LAN server; returns the join URL or None on failure."""
        if self.web_server is None:
            from arcaneatlas.webserver import WebServer
            self.web_server = WebServer(self)
        # Apply the currently-configured base port each start (it may have changed
        # in the dialog, and a prior run may have left a fallback value behind).
        self.web_server.http_port = self._web_base_port()
        self.web_server.ws_port = self.web_server.http_port + 1
        url = self.web_server.start()
        if url:
            self.statusBar().showMessage(f"Web sharing at {url}", 0)
        return url

    def _stop_web_sharing(self):
        if self.web_server is not None:
            self.web_server.stop()
        self.statusBar().clearMessage()

    def _open_web_share_dialog(self):
        from arcaneatlas.webserver import WebShareDialog
        WebShareDialog(self).exec()

    # ── File-menu dialogs: Settings / Instructions / About ───────────────────
    def _open_settings_dialog(self):
        """A small table of app settings. Toggles apply immediately (and persist
        where applicable — lockOnOpen). Add new rows here as settings grow."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Settings")
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("<b>Settings</b>"))

        cb_pv = QCheckBox("Show Playerview Box in Playerview Window")
        cb_pv.setChecked(self.show_playerview_box)
        cb_pv.toggled.connect(self.on_show_playerview_box_toggled)
        lay.addWidget(cb_pv)

        cb_lock = QCheckBox("Lock Assets when Opening Maps")
        cb_lock.setChecked(self.lock_on_open)
        cb_lock.toggled.connect(self._on_lock_on_open_toggled)
        lay.addWidget(cb_lock)

        # Player display mode. This is the sole control for the mode now —
        # internal values ("Fullscreen"/"Windowed"/"SingleSplit") map to friendly
        # labels; changing it applies immediately via _on_screenmode_changed.
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Player Display Mode:"))
        cb_mode = QComboBox()
        for value, label in (
            ("Fullscreen", "Fullscreen"),
            ("Windowed", "Windowed"),
            ("SingleSplit", "Split Screen"),
        ):
            cb_mode.addItem(label, value)
        idx = cb_mode.findData(self.player_screen_mode)
        cb_mode.setCurrentIndex(idx if idx != -1 else 0)
        cb_mode.currentIndexChanged.connect(
            lambda i, c=cb_mode: self._on_screenmode_changed(c.itemData(i))
        )
        mode_row.addWidget(cb_mode, 1)
        lay.addLayout(mode_row)

        lay.addStretch(1)
        close = QPushButton("Close"); close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        dlg.resize(360, 200)
        dlg.exec()

    def _open_instructions_dialog(self):
        """Show resources/instructions.md in a scrollable, rendered view."""
        self._show_text_dialog(
            "Instructions", res_path("instructions.md"),
            markdown=True, missing="Instructions file not found.")

    def _open_about_dialog(self):
        AboutDialog(
            parent=self,
            app_name="Arcane Atlas",
            version=__version__,
            icon_path=ICON_PATH,
            tagline="Lay out maps, fog, and tokens — mirrored to the screen "
                    "your players see.",
            license_path=res_path("license.txt"),
        ).exec()

    def _show_text_dialog(self, title, path, markdown=False, missing="File not found."):
        """Generic read-only text/markdown viewer dialog."""
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        lay = QVBoxLayout(dlg)
        view = QTextBrowser(); view.setOpenExternalLinks(True)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
            if markdown:
                view.setMarkdown(text)
            else:
                view.setPlainText(text)
        except OSError:
            view.setPlainText(missing)
        lay.addWidget(view, 1)
        close = QPushButton("Close"); close.clicked.connect(dlg.accept)
        lay.addWidget(close)
        dlg.resize(640, 680)
        dlg.exec()

    def _unused_map_path(self, directory, base="New Map"):
        # Return a non-colliding "<base>.json" / "<base> N.json" path in directory.
        candidate = os.path.join(directory, base + ".json")
        i = 1
        while os.path.exists(candidate):
            candidate = os.path.join(directory, f"{base} {i}.json")
            i += 1
        return candidate

    def _create_new_map_in(self, directory, start_editing=True):
        # Clear the canvas to a fresh state, write an empty map with a default
        # (unused) name into `directory`, and — if requested — drop the browser
        # straight into inline-rename on the new file so the user can name it.
        if not self.new_map():
            return
        path = self._unused_map_path(directory)
        self._save_to_path(path)
        if start_editing:
            self._rename_map_in_browser(path)

    def _rename_map_in_browser(self, path):
        # Select `path` in the Maps tree and start inline editing of its name.
        # QFileSystemModel populates a directory asynchronously, so the row may
        # not exist the instant after we write the file — retry on directoryLoaded.
        def try_edit():
            pidx = self.proxy.mapFromSource(self.fs_model.index(path))
            if not pidx.isValid():
                return False
            self.left_tabs.setCurrentWidget(self.map_view)
            self.map_view.setCurrentIndex(pidx)
            self.map_view.scrollTo(pidx)
            self.map_view.edit(pidx)
            return True

        if try_edit():
            return

        def on_loaded(_dir):
            if try_edit():
                try:
                    self.fs_model.directoryLoaded.disconnect(on_loaded)
                except (TypeError, RuntimeError):
                    pass

        self.fs_model.directoryLoaded.connect(on_loaded)

    def _on_map_file_renamed(self, folder, old_name, new_name):
        # Fires for every rename done through the file model (inline edit).
        old_path = os.path.join(folder, old_name)
        new_path = os.path.join(folder, new_name)
        # Relocate any legacy companion fog alongside the renamed map.
        if old_name.lower().endswith(".json"):
            old_fog = os.path.splitext(old_path)[0] + "_fog.png"
            if os.path.exists(old_fog):
                try:
                    os.rename(old_fog, os.path.splitext(new_path)[0] + "_fog.png")
                except OSError:
                    pass
        # Keep the currently-open map's path/marker in sync if it was the one renamed.
        if self.current_map_path and \
           os.path.abspath(old_path) == os.path.abspath(self.current_map_path):
            self.current_map_path = new_path
            self.proxy.setCurrentFile(new_path)
            self.map_status_label.setText(f"Opened Map: {os.path.basename(new_path)}")

    def on_map_context_menu(self, point):
        idx = self.map_view.indexAt(point)
        gs   = self.map_view.viewport().mapToGlobal(point)
        menu = QMenu(self)

        # root‐level → “New Map” + “New Folder”
        if not idx.isValid():
            a_newmap = menu.addAction("New Map…")
            a_new    = menu.addAction("New Folder…")
            act      = menu.exec(gs)
            if act == a_newmap:
                self._create_new_map_in(self.maps_dir)
            elif act == a_new:
                name, ok = QInputDialog.getText(self, "Create Folder", "Folder name:")
                if ok and name:
                    os.makedirs(os.path.join(self.maps_dir, name), exist_ok=True)
            return

        # map to real path
        proxy = self.map_view.model()
        src   = proxy.mapToSource(idx)
        fs    = proxy.sourceModel()
        path  = fs.filePath(src)
        is_dir = os.path.isdir(path)

        # The directory the right-click targets: the folder itself, or the
        # parent folder of the clicked map file.
        target_dir = path if is_dir else os.path.dirname(path)

        # Actions
        a_save = a_save_players = None
        # Map Only Actions
        if not is_dir:
            a_open   = menu.addAction("Open")
            # "Save Map" only for the currently-open map (saving to a *different*
            # file would be Save As, which the app deliberately omits).
            if self.current_map_path and \
               os.path.abspath(path) == os.path.abspath(self.current_map_path):
                a_save = menu.addAction("Save Map")
                a_save_players = menu.addAction("Save Map w/ Player Tokens")
        # Common Actions
        a_newmap = menu.addAction("New Map…")
        a_rename = menu.addAction("Rename")
        a_del    = menu.addAction("Delete")
        # Directory Only Actions
        if is_dir:
            a_new = menu.addAction("New Folder…")

        act = menu.exec(gs)

        # Handle Open
        if not is_dir and act == a_open:
            # call your existing loader
            self.open_map(path)
            return

        # Handle Save (current map only)
        if a_save is not None and act == a_save:
            self.save()
            return

        # Handle Save w/ Player Tokens (current map only)
        if a_save_players is not None and act == a_save_players:
            self.save_with_player_tokens()
            return

        # Handle New Map (placed in the right-clicked directory)
        if act == a_newmap:
            self._create_new_map_in(target_dir)
            return

        # Handle Delete
        if act == a_del:
            if is_dir:
                shutil.rmtree(path)
            else:
                os.remove(path)
                # also remove companion fog
                fog = os.path.splitext(path)[0] + "_fog.png"
                if os.path.exists(fog):
                    os.remove(fog)
                # if the deleted file *was* the one we had open, reset everything:
                if self.current_map_path \
                   and os.path.abspath(path) == os.path.abspath(self.current_map_path):
                    # Forget the path first so new_map() doesn't try to save the
                    # player-view/fog back to the file we just deleted, and skip
                    # the unsaved-changes prompt (the map is already gone).
                    self.current_map_path = None
                    self.new_map(confirm=False)
                    self.statusBar().showMessage(
                        f"Deleted open map. Canvas cleared.", 5000
                    )
        # Handle Rename — edit the name inline in the tree. The proxy hides/re-adds
        # the ".json" extension and renames any legacy companion fog (see setData).
        elif act == a_rename:
            self.map_view.edit(idx)
        elif is_dir and act == a_new:
            # New Folder inside the right-clicked directory. Guarded by is_dir so
            # a_new (only created in the is_dir branch above) is always bound here.
            name, ok = QInputDialog.getText(self, "Create Folder", "Folder name:")
            if ok and name:
                os.makedirs(os.path.join(path, name), exist_ok=True)

    def _on_gmfog_slider_changed(self, value):
        # Updates the GM fog opacity based on the gmfog_slider value.
        #  actually changing the opacity is done in the canvas.drawforeground
        self.gmfog_label.setText(f"GM Fog Opacity: {value}%")
        percent = value / 100.0
        self.canvas_view.set_gm_fog_opacity(percent)

    # whenever slider moves, update the Canvas’s brush radius *and* the label
    def _on_brush_changed(self, v):
        snapped_value = round(v / self.brush_step) * self.brush_step
        self.brush_slider.setValue(snapped_value)  # Ensure the slider always snaps
        self.canvas_view.fog_brush_radius = snapped_value
        self.brush_label.setText(f"Brush Size: {snapped_value/72:.2f}in")

    def _set_brush_shape(self, shape: str):
        # update GM view
        self.canvas_view.set_brush_shape(shape)
        # update player view if it exists
        if hasattr(self, "player_window"):
            self.player_window.canvas_view.set_brush_shape(shape)

    def _install_player_visibility_hook(self):
        if not hasattr(self, "player_window"):
            return
        wh = self.player_window.windowHandle()
        if not wh:
            QTimer.singleShot(0, self._install_player_visibility_hook)
            return
        wh.visibleChanged.connect(self._on_player_visibility_changed)

    def _on_player_visibility_changed(self, visible: bool):
        if visible and self._pending_player_geom:
            r = self._pending_player_geom
            # apply with move()/resize() to avoid any Windows clamping warning
            self.player_window.move(r.x(), r.y())
            self.player_window.resize(r.width(), r.height())
            self._pending_player_geom = None
    
    def _on_screenmode_changed(self, text: str):
        """
        Apply the selected player view screen mode immediately, without requiring
        the Playerview enabled toggle to be cycled.
        """
        # Remember the choice — this is the source of truth read by toggle_player_view.
        self.player_screen_mode = text

        # If the user has Playerview enabled but the window hasn't been created yet,
        # ensure we have a player window so the mode change is visible right away.
        if self.show_playerview_checkBox.isChecked() and not hasattr(self, "player_window"):
            self.toggle_player_view(True)

        if text == "Fullscreen":
            if hasattr(self, "player_window") and self.player_window:
                try:
                    self.player_window.setWindowFlag(Qt.FramelessWindowHint, True)
                    self.player_window.show()
                except Exception:
                    pass
                self.player_window.showMaximized()
            self.showMaximized()

        elif text == "SingleSplit":
            # Use your existing helpers to tile main + player side-by-side.
            self._on_debug_fullscreen_toggled(False)
            self._on_debug_singlescreen_toggled(True)

        else:  # "Windowed"
            self.showNormal()
            if hasattr(self, "player_window") and self.player_window:
                # Make sure we’re not frameless when windowed (optional UX tweak)
                try:
                    self.player_window.setWindowFlag(Qt.FramelessWindowHint, False)
                    self.player_window.show()  # apply flag change
                except Exception:
                    pass

                self.player_window.showNormal()
                # 💄 UX touch: size to a pleasant aspect that fits on the selected display
                self.resize_player_to_aspect()   # <- new
                # Center on the selected monitor so the change is obvious
                self.center_on_selected_display(self.player_window)

    def _on_display_combo_changed(self, index: int):
        """When the user picks a new screen from the combo box."""
        self.selected_screen_index = index
        # The new screen may have a different aspect ratio → re-derive dims.
        self._refresh_auto_dims()
        # If the player window is already showing, re-center it there:
        if hasattr(self, "player_window"):
            self.center_on_selected_display(self.player_window)
            if hasattr(self, "player_view_item"):
                self.player_view_item.setRect(
                    0, 0, self.playerDisplayWidth * 72, self.playerDisplayHeight * 72)
                self.sync_player_view_to_camera()

    def _enter_reveal_mode(self, active: bool):
        if not self.fog_enable_checkBox.isChecked():
            self.fog_revealtool_btn.setChecked(False)
            return

        # turn off the other tool
        if active:
            self.fog_hidetool_btn.setChecked(False)

        self._fog_reveal_mode = active
        curs = Qt.CrossCursor if active else Qt.ArrowCursor
        self.canvas_view.viewport().setCursor(curs)
        self.force_redraw()

    def resize_player_to_aspect(self, aspect_ratio: tuple[int, int] | None = None, max_fraction: float = 0.9):
        """
        Resize the player window to a nice aspect ratio that fits on the selected display.
        - aspect_ratio: (w, h). If None, tries canvas or defaults to 16:9.
        - max_fraction: cap of display area used (both width & height).
        """
        if not hasattr(self, "player_window") or not self.player_window:
            return

        # Target screen/geometry
        screen = getattr(self, "get_selected_qscreen", None)
        qscreen = screen() if callable(screen) else QApplication.primaryScreen()
        available = qscreen.availableGeometry()

        # Derive aspect if not provided
        if aspect_ratio is None:
            # Try explicit defaults if you keep them
            w = getattr(self, "player_default_width", None)
            h = getattr(self, "player_default_height", None)
            if w and h and h > 0:
                aspect_ratio = (int(w), int(h))
            else:
                # Fall back to canvas size if available, else 16:9
                if hasattr(self, "canvas_widget") and self.canvas_widget.height() > 0:
                    aspect_ratio = (self.canvas_widget.width(), self.canvas_widget.height())
                else:
                    aspect_ratio = (16, 9)

        arw, arh = aspect_ratio
        if arw <= 0 or arh <= 0:
            arw, arh = 16, 9

        # Max box we allow
        max_w = int(available.width() * max_fraction)
        max_h = int(available.height() * max_fraction)

        # Fit aspect box into max box
        target_w = max_w
        target_h = int(target_w * arh / arw)
        if target_h > max_h:
            target_h = max_h
            target_w = int(target_h * arw / arh)

        # Apply size (don’t force fixed; let users resize later)
        self.player_window.resize(max(320, target_w), max(180, target_h))

    def _enter_hide_mode(self, active: bool):
        # disable if fog is off
        if not self.fog_enable_checkBox.isChecked():
            self.fog_hidetool_btn.setChecked(False)
            return

        # turn off the other tool
        if active:
            self.fog_revealtool_btn.setChecked(False)

        # record mode and change cursor
        self._fog_hide_mode = active
        curs = Qt.CrossCursor if active else Qt.ArrowCursor
        self.canvas_view.viewport().setCursor(curs)
        self.force_redraw()


    def _on_fog_toggled(self, enabled: bool):
        """
        Enable or disable fog overlays, but keep the mask in memory
        so that toggling off → on will restore exactly what was there.
        """
        gv = self.canvas_view  # GM view

        # Toggle fog visibility (the reveal path persists either way, so
        # toggling off → on restores exactly what was revealed)
        gv.fog_enabled = enabled
        gv.viewport().update()

        # Sync Player view if it exists
        self.sync_fog_to_player_view()

        # 🩶 NEW: Cancel reveal/hide mode if fog is disabled
        if not enabled:
            self._fog_reveal_mode = False
            self._fog_hide_mode = False
            self.fog_revealtool_btn.setChecked(False)
            self.fog_hidetool_btn.setChecked(False)
            gv.viewport().setCursor(Qt.ArrowCursor)

    def _on_fog_reset(self):
        """
        Wipe out existing fog and start over with fresh masks for both views.
        """
        # Reset re-fogs the whole map, discarding every reveal — and it isn't
        # undoable, so confirm first.
        if QMessageBox.question(
                self, "Reset Fog",
                "Re-fog the entire map? This clears everything you've revealed and "
                "can't be undone.",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No) != QMessageBox.Yes:
            return

        gv = self.canvas_view  # GM view

        # Reset only the reveal mask. Do NOT force fog_enabled on — that would make
        # fog visible while the Enable Fog checkbox stays unchecked (a desync). Reset
        # respects the current enable state: fogged now if enabled, otherwise the
        # cleared mask just waits in memory until fog is turned on.
        self._reset_fog_mask()

        self.force_redraw()
        self.sync_fog_to_player_view() # Sync Player view if it exists
        # NEW: keep mask size aligned to scene
        self.sync_fog_alignment()

    def force_redraw(self):
        """
        Forces an immediate redraw of both the GM and Player views.
        """
        # Redraw GM view
        self.canvas_view.viewport().update()

        # Redraw Player view (if it exists)
        if hasattr(self, "player_window"):
            self.player_window.canvas_view.viewport().update()

    def _reveal_at(self, scene_pos):
        # Reveal fog at the specified position in both GM and Player views.
        gv = self.canvas_view  # GM view
        gv.reveal_at(scene_pos, gv.fog_brush_radius // 2)
        self._push_fog_to_player()

    def _hide_at(self, scene_pos):
        gv = self.canvas_view
        gv.hide_at(scene_pos, gv.fog_brush_radius // 2)
        self._push_fog_to_player()

    def _push_fog_to_player(self):
        """Copy the GM's reveal path to the player view and repaint it.

        reveal_at/hide_at replace fog_reveal_path with a *new* path object, so
        (unlike the old shared-mask QImage) the player's reference goes stale
        after each stroke and must be re-pointed."""
        if hasattr(self, "player_window") and self.player_window is not None:
            pv = self.player_window.canvas_view
            pv.fog_reveal_path = self.canvas_view.fog_reveal_path
            # Mirror the live stroke too, so the player sees the dab in progress
            # (not just the committed result on release).
            pv._fog_stroke = self.canvas_view._fog_stroke
            pv._fog_stroke_mode = self.canvas_view._fog_stroke_mode
            pv.viewport().update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        margin = 10
        w, h = self.width(), self.height()
        pw, ph = self.progressBar.width(), self.progressBar.height()
        self.progressBar.move(w - pw - margin, h - ph - margin)

        # NEW: keep mask size aligned to scene
        self.sync_fog_alignment()

    def _save_playerview_position_only(self, path: str) -> None:
        """
        Update only the 'playerView' block in an existing map JSON on disk.
        No asset copying, no item rewriting—just x/y/w/h from the current playerview.
        Silently no-ops on errors.
        """
        try:
            if not (hasattr(self, "player_view_item") and self.player_view_item):
                return
            if not path or not os.path.isfile(path):
                return

            # Read existing JSON
            with open(path, "r") as f:
                data = json.load(f)

            # Pull current PV geom
            pv  = self.player_view_item
            r   = pv.rect()
            pos = pv.pos()

            data["playerView"] = {
                "x": pos.x(),
                "y": pos.y(),
                "w": r.width(),
                "h": r.height(),
            }

            # Write back
            with open(path, "w") as f:
                json.dump(data, f, indent=2)

            # (Do not touch self.current_map_path/UI here; we’re only updating on disk)
        except Exception:
            # Keep this silent; we don’t want to block map switches on a PV write.
            pass

    def _save_fog_only(self, path: str) -> None:
        """
        Update only the fog fields in an existing map JSON on disk:
        - fogEnabled
        - fogReveal (the serialized reveal path)
        No asset copying or other fields are touched. Silent no-op on errors.
        """
        try:
            if not path or not os.path.isfile(path):
                return

            # Read current JSON (don’t lose anything else)
            with open(path, "r") as f:
                data = json.load(f)

            # Write fog-enabled state and the serialized reveal path
            data["fogEnabled"] = bool(getattr(self.canvas_view, "fog_enabled", False))
            self.canvas_view.simplify_fog()
            data["fogReveal"] = fog_path_to_json(self.canvas_view.fog_reveal_path)

            # Write back
            with open(path, "w") as f:
                json.dump(data, f, indent=2)

        except Exception:
            # Keep failures silent; we don’t want to block map switches or shutdowns.
            pass

    def _stop_all_videos(self):
        """Stop every video item's player before a scene.clear() so the FFmpeg
        backend doesn't deliver a frame to a sink that's about to be freed."""
        for it in self.canvas_view.scene.items():
            if isinstance(it, (InteractiveVideoItem, AnimatedItem)):
                it.teardown_player()

    def new_map(self, confirm=True):
        # Only warn when there's unsaved work to lose. Player-view position and
        # fog are always saved on leave (below), so they don't count as "unsaved";
        # this guards item/background/grid edits made since the last save.
        # Yes → save then start new; No → discard and start new; Cancel → stay put.
        if confirm and self._has_unsaved_changes():
            response = QMessageBox.question(
                self,
                "New Map",
                "You have unsaved changes. Save current map?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
            )
            if response == QMessageBox.Cancel:
                return False
            if response == QMessageBox.Yes:
                self.save()
                # If the save was cancelled (e.g. the Save As dialog was
                # dismissed for an unsaved map), don't clear the canvas.
                if self._has_unsaved_changes():
                    return False

        # Save just the playerview position to the *current* map (if one is open)
        if self.current_map_path:
            self._save_playerview_position_only(self.current_map_path)
            self._save_fog_only(self.current_map_path)   # ← add this line

        # We want to always preserve the player view object
        # so we need to detach from the scene so that we can clear
        # without destroying it.
        pv = None
        if hasattr(self, "player_view_item"):
            pv = self.player_view_item
            self.scene.removeItem(pv)   # detach so scene.clear() won’t destroy it

        # 1) Clear the scene completely (stop video decoders first)
        self._stop_all_videos()
        self.canvas_view.scene.clear()

        # readd the playerview to the cleared scene
        if pv:
            self.scene.addItem(pv)
            pv.setVisible(self.show_playerview_checkBox.isChecked())
            # Default to center for a *new* map:
            self._center_playerview()

        # Reset the canvas to base size, then build a fresh (fully fogged) mask
        self.canvas_view.reset_extent()
        self._reset_fog_mask()
        self.canvas_view.viewport().update()
        self.sync_fog_alignment()

        # Reset background color to the pinned app default
        default_bg = self._bgcolor_default
        ###default_bg = getattr(self, "_bgcolor_default", 
        ###                    self.canvas_view.viewport().palette().color(QPalette.Base))
        self._bgcolor = QColor(default_bg)
        self.bgcolor_toolbtn.setColor(self._bgcolor)
        self.canvas_view.scene.setBackgroundBrush(QBrush(self._bgcolor))
        self.canvas_view.viewport().update()

        # Reset grid color to the pinned default too (otherwise a new map would
        # inherit the last-opened map's grid color). setColor() only updates the
        # swatch — it doesn't emit colorChanged — so this won't mark the map dirty.
        self.canvas_view.grid_color = QColor(self.default_gridcolor)
        self.gridcolor_toolbtn.setColor(self.default_gridcolor)

        # Sync Player view if it exists
        if hasattr(self, "player_window"):
            self.sync_fog_to_player_view()

        # 3) Recreate the grid
        self.canvas_view.create_grid()

        # 4) Update the view to reflect changes
        self.canvas_view.viewport().update()
        if hasattr(self, "player_window"):
            self.player_window.canvas_view.viewport().update()

        if self.show_playerview_checkBox.isChecked():
            self.toggle_player_view(True)

        self.current_map_path = None
        self.map_status_label.setText("Current Map Unsaved")
        self.proxy.setCurrentFile(None)   # no map is “current” yet
        self.statusBar().showMessage("New Map (unsaved)", 5000)

        # Reapply Any Global Settings
        # re-apply lock state to any new items (assets + tokens use separate locks)
        self._on_lock_map_toggled(self.lockmap_checkBox.isChecked())
        self._on_lock_tokens_toggled(self.locktokens_checkBox.isChecked())

        # Refresh the Layers List
        self.update_layers_list()

        # Fresh, empty canvas — nothing to lose yet.
        self._mark_clean()
        # current_map_path is now None → back to the welcome screen.
        self._update_map_ui_state()

        return True

    def open_map(self, path):

        # Offer to save unsaved item/background/grid edits before switching. Fog
        # and player-view changes aren't counted here — they're saved on leave
        # below. Yes → save then open; No → discard and open; Cancel → stay put.
        if self._has_unsaved_changes():
            response = QMessageBox.question(
                self,
                "Open Map",
                "You have unsaved changes. Save current map?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
            )
            if response == QMessageBox.Cancel:
                return
            if response == QMessageBox.Yes:
                self.save()
                # If the save was cancelled (e.g. the Save As dialog was
                # dismissed for an unsaved map), stay on the current map.
                if self._has_unsaved_changes():
                    return

        # If we’re switching away from a different map, persist ONLY the playerview to that map
        if self.current_map_path and os.path.abspath(path) != os.path.abspath(self.current_map_path):
            self._save_playerview_position_only(self.current_map_path)
            self._save_fog_only(self.current_map_path)

        # Load the JSON
        try:
            with open(path, 'r') as f:
                # Valid file found
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Load Error", f"Could not load map:\n{e}")
            return
        
        # ✅ Save PlayerViewItem (the real one with the fake title bar) if it exists
        # Player tokens travel to the newly-opened map — snapshot them before the
        # scene is cleared (scene.clear() destroys the C++ items).
        player_token_snaps = self._snapshot_player_tokens()

        player_view_item = None
        if hasattr(self, "player_view_item"):
            player_view_item = self.player_view_item
            self.scene.removeItem(self.player_view_item)  # Temporarily remove

        # 1) Completely clear scene and rebuild grid at the base extent
        #    (update_extent will fit it to the loaded maps afterwards)
        self._stop_all_videos()
        self.canvas_view.scene.clear()
        self.canvas_view.reset_extent()

        # ── restore grid settings from JSON ──
        # color — always reflect the opened map: use its saved gridColor, or the
        # pinned default if it has none, so the picker button never shows a stale
        # color carried over from a previously-open map.
        gc = data.get("gridColor")
        col = QColor(*gc) if gc else QColor(self.default_gridcolor)
        # 1) update the picker button so its swatch matches the loaded color
        self.gridcolor_toolbtn.setColor(col)
        # 2) apply to the canvas and redraw the grid
        self.canvas_view.grid_color = col
        self.canvas_view.create_grid()

        # above/below
        self.gridabove_checkBox.setChecked(data.get("gridAbove", False))
        # enable/disable visibility
        self.grid_enable_checkBox.setChecked(data.get("gridEnabled", True))
        # apply visibility immediately
        self.on_grid_enable_toggled(self.grid_enable_checkBox.isChecked())

        # NEW: restore background color if present
        bgc = data.get("bgColor")
        if bgc:
            col = QColor(*bgc)
            self._bgcolor = col
            self.bgcolor_toolbtn.setColor(col)  # reflect in the picker
            self.canvas_view.scene.setBackgroundBrush(QBrush(col))
            self.canvas_view.viewport().update()

        # 2) Recreate each saved item
        missing = []
        for entry in data.get("items", []):
            # Text boxes are self-contained (no asset file) — build directly.
            if entry.get("type") == "text":
                tb = TextBoxItem()
                tb.apply_json(entry)
                tb.setZValue(entry.get("z", 0))
                tb.setPos(*entry.get("pos", [0, 0]))
                if entry.get("rot"):
                    tb.setTransformOriginPoint(tb.boundingRect().center())
                    tb.setRotation(entry.get("rot", 0))
                self.canvas_view.scene.addItem(tb)
                continue
            asset_path = os.path.join(self.asset_dir, entry["asset"])
            # Safety net: an asset may have been moved/deleted outside the app
            # (in-app moves rewrite refs, but OS file-manager moves don't). Skip
            # it rather than spawning a broken pixmap / black video box.
            if not os.path.exists(asset_path):
                missing.append(entry["asset"])
                continue
            if entry["type"] == "image":
                pix = QPixmap(asset_path)
                it  = InteractivePixmapItem(pix)
                # restore saved size
                w, h = entry["size"]
                scaled = pix.scaled(w, h,
                                    Qt.KeepAspectRatio,
                                    Qt.SmoothTransformation)
                it.setPixmap(scaled)

            elif entry["type"] == "anim":   # animated WebP object (alpha)
                it = AnimatedItem(asset_path)
                it.setSize(QSizeF(*entry["size"]))

            else:  # video
                it = InteractiveVideoItem(asset_path)
                # ── prevent the nativeSizeChanged signal from stomping your loaded size ──
                try:
                    it.nativeSizeChanged.disconnect(it._on_video_size)
                except (TypeError, RuntimeError):
                    pass
                # now apply the saved size
                it.setSize(QSizeF(*entry["size"]))

            # restore position & rotation
            it.asset_filename = entry["asset"]
            it.asset_category = self._item_layer(it)     # layer for restacking
            # Tokens (images living under tokens/) are fixed-size, re-edited via
            # the tokenizer — flag them so resize handles stay disabled.
            if isinstance(it, InteractivePixmapItem):
                it.is_token = (it.asset_category == "tokens")
                # Re-apply a per-instance token border colour (re-bakes the pixmap;
                # the library PNG is untouched). asset_filename is set above.
                tc = entry.get("tokenColor")
                if it.is_token and tc:
                    self._bake_token_with_color(it, tc)
                if it.is_token:
                    it.token_id = entry.get("id") or uuid.uuid4().hex
                    it.player_controllable = bool(entry.get("playerControllable", False))
            it.visible_to_player = entry.get("visibleToPlayer", True)
            it.setZValue(entry.get("z", 0))              # legacy maps: 0 → array order
            it.setPos(*entry.get("pos", [0, 0]))
            rot = entry.get("rot", 0)
            # Video/anim items rotate about their CENTER at edit time
            # (_set_origin_keep_pos) and the saved pos assumes that origin. Match
            # it here — with rotation still 0 this doesn't move the item — or the
            # item would rotate about (0,0) and shift ~its width to the left.
            if rot:
                it.setTransformOriginPoint(it.boundingRect().center())
            it.setRotation(rot)

            # add it into the scene
            self.canvas_view.scene.addItem(it)

        if missing:
            shown = "\n".join("  • " + m for m in missing[:12])
            more = f"\n  …and {len(missing) - 12} more" if len(missing) > 12 else ""
            QMessageBox.warning(
                self, "Missing Assets",
                "Some assets referenced by this map could not be found and were "
                f"skipped (they may have been moved or deleted):\n{shown}{more}")

        # Player tokens travel in from the previous map, at their old positions,
        # bumping where they'd collide with this map's tokens (the map's items are
        # all in the scene now, so collision resolution is correct).
        self._restore_player_tokens(player_token_snaps)

        # Enforce the layer bands (backgrounds < objects < tokens), preserving each
        # layer's saved order; legacy maps (no per-item z) fall back to array order.
        self.restack_layers()

        # Grow the canvas to fit the loaded maps *before* touching fog, so the
        # fog mask aligns to the final scene rect.
        self.refresh_canvas_extent()

        # Load the reveal path (scene coordinates). No backward compatibility:
        # maps without "fogReveal" simply open fully fogged (empty path).
        self.canvas_view.fog_reveal_path = fog_path_from_json(data.get("fogReveal"))
        self.canvas_view.viewport().update()

        # Set the fog enabled state based on saved data
        fog_enabled = data.get("fogEnabled", False)
        self.fog_enable_checkBox.setChecked(fog_enabled)
        self._on_fog_toggled(fog_enabled)
        # Push the loaded path + enabled state to the player view
        self.sync_fog_to_player_view()

        # ✅ Restore the original PlayerViewItem
        if player_view_item:
            self.player_view_item = player_view_item
            self.scene.addItem(self.player_view_item)  # Re-add to scene

        pv_info = data.get("playerView")
        if pv_info and hasattr(self, "player_view_item"):
            pv = self.player_view_item
            pv.setRect(0, 0, pv_info.get("w", pv.rect().width()), pv_info.get("h", pv.rect().height()))
            pv.setPos(pv_info.get("x", pv.pos().x()), pv_info.get("y", pv.pos().y()))
        else:
            # No saved info? Optionally center it on first open.
            self._center_playerview()

        if hasattr(self, "player_view_item"):
            QTimer.singleShot(100, self.sync_player_view_to_camera)

        # Highlight file in filebrowser
        self.proxy.setCurrentFile(path)
        # tell proxy which file to bold
        self.map_status_label.setText(f"Opened Map: {os.path.basename(path)}")
        # Update what file is opened so that we properly save/saveas
        self.current_map_path = path
        # Show status of succesful load
        self.statusBar().showMessage("Map loaded successfully.", 5000)

        # Refresh the LayersList
        self.update_layers_list()
        # Gold rings on any loaded tokens whose asset is in the saved party.
        self._refresh_party_token_rings()

        # Lock-on-open: a content-bearing map opens with Lock Assets enabled (per
        # the setting) so you don't accidentally nudge the layout; a fresh/empty
        # map stays unlocked. Dragging in a new asset unlocks again (_place_asset).
        # Tokens keep their own Lock Tokens state.
        if getattr(self, "lock_on_open", True):
            self.lockmap_checkBox.setChecked(bool(self._map_items()))
        # Apply the (possibly unchanged) lock flags to the freshly loaded items.
        self._on_lock_map_toggled(self.lockmap_checkBox.isChecked())
        self._on_lock_tokens_toggled(self.locktokens_checkBox.isChecked())

        # Just-loaded map matches disk — clear the unsaved-changes flag (the
        # setChecked/setColor calls above tripped it through the widget signals).
        self._mark_clean()
        # A map is open now → hide the welcome screen, enable map-only controls.
        self._update_map_ui_state()

    def _save_to_path(self, path: str, include_player_tokens: bool = False):
        """Internal: copy assets, write JSON, update UI for a given path. Player
        (party) tokens are normally excluded (they travel across maps as a session
        overlay); `include_player_tokens=True` writes them into the map too (used by
        "Save Map w/ Player Tokens")."""
        # ensure .json extension
        if not path.lower().endswith(".json"):
            path += ".json"

        # copy assets into self.asset_dir…
        items = [it for it in self.canvas_view.scene.items() if hasattr(it, "asset_path")]
        self.progressBar.setRange(0, len(items))
        self.progressBar.setValue(0)
        self.progressBar.setVisible(True)
        for i, it in enumerate(items, start=1):
            # Honour the category chosen at drop time (defaults to backgrounds).
            dst = self._import_asset(it.asset_path,
                                     getattr(it, "asset_category", DEFAULT_DROP_CATEGORY))
            it.asset_filename = dst
            # Repoint at the library copy so a later save in this same session
            # sees it as already-imported and doesn't duplicate it.
            it.asset_path = os.path.join(self.asset_dir, dst)
            self.progressBar.setValue(i)
            QApplication.processEvents()
        self.progressBar.setVisible(False)

        # build JSON payload…
        # Player (party) tokens are NOT saved with the map by default — they're a
        # session overlay that travels across maps (see _snapshot/_restore_player_
        # tokens). "Save Map w/ Player Tokens" passes include_player_tokens=True.
        party_assets = set() if include_player_tokens else {m.get("asset") for m in self.party_members}
        out = []
        for it in self.canvas_view.scene.items():
            if isinstance(it, TextBoxItem):
                out.append(it.to_json())          # non-asset item, self-serializing
                continue
            if not hasattr(it, "asset_filename"):
                continue
            if getattr(it, "is_token", False) and it.asset_filename in party_assets:
                continue
            if isinstance(it, InteractivePixmapItem):
                w, h, typ = it.pixmap().width(), it.pixmap().height(), "image"
            elif isinstance(it, AnimatedItem):
                sz = it.size(); w, h, typ = sz.width(), sz.height(), "anim"
            else:
                sz = it.size(); w, h, typ = sz.width(), sz.height(), "video"
            entry = {"type": typ, "asset": it.asset_filename,
                     "pos": [it.pos().x(), it.pos().y()],
                     "size": [w, h], "rot": it.rotation(),
                     "z": it.zValue(),
                     "visibleToPlayer": getattr(it, "visible_to_player", True)}
            # Per-instance token border colour (omitted when there's no override).
            if getattr(it, "token_color_override", None):
                entry["tokenColor"] = it.token_color_override
            # Token web-sharing fields: stable id + player-controllable flag.
            if getattr(it, "is_token", False):
                entry["id"] = self._token_id(it)
                if getattr(it, "player_controllable", False):
                    entry["playerControllable"] = True
            out.append(entry)

        col = self.canvas_view.grid_color
        data = {
                "items": out,
                "fogEnabled": self.fog_enable_checkBox.isChecked(),
                # ── include grid settings ──
                "gridEnabled": self.grid_enable_checkBox.isChecked(),
                "gridAbove": self.gridabove_checkBox.isChecked(),
                "gridColor": [col.red(), col.green(), col.blue(), col.alpha()]
                }
        
        # NEW: include background color
        bg = self._bgcolor if hasattr(self, "_bgcolor") else \
            self.canvas_view.scene.backgroundBrush().color()
        data["bgColor"] = [bg.red(), bg.green(), bg.blue(), bg.alpha()]

        self.canvas_view.simplify_fog()
        data["fogReveal"] = fog_path_to_json(self.canvas_view.fog_reveal_path)

        if hasattr(self, "player_view_item"):
            pv = self.player_view_item
            r  = pv.rect()                    # local rect (0,0,w,h) you set via setRect
            pos = pv.pos()                    # scene position of its top-left
            data["playerView"] = {
                "x": pos.x(),
                "y": pos.y(),
                "w": r.width(),
                "h": r.height()
            }

        with open(path, "w") as f:
            json.dump(data, f, indent=2)

        # update “current” state & UI
        self.current_map_path = path
        self.proxy.setCurrentFile(path)
        name = os.path.basename(path)
        self.map_status_label.setText(f"Opened Map: {name}")
        self.statusBar().showMessage(f"Saved Map: {name}", 5000)

        # On disk now — no unsaved changes.
        self._mark_clean()
        # A map is open/saved → ensure welcome is hidden and controls enabled.
        self._update_map_ui_state()

    def save(self):
        """Save to the current map file. No-op when no map is open (the Save
        action is disabled in that state). New maps are always file-backed, so
        there's no Save As path — a map's location is managed from the browser."""
        if self.current_map_path:
            self._save_to_path(self.current_map_path)

    def save_with_player_tokens(self):
        """Save the current map INCLUDING the player (party) tokens at their
        current positions (they're normally excluded and travel between maps)."""
        if self.current_map_path:
            self._save_to_path(self.current_map_path, include_player_tokens=True)


    def _import_asset(self, src_path: str, category: str = DEFAULT_DROP_CATEGORY) -> str:
        # Copy src_path into the given category subfolder of the asset library,
        # renaming if needed to avoid collisions. Returns the library-relative
        # POSIX path used to reference it (e.g. "backgrounds/foo.png").

        # If the source already lives anywhere in the library, it's already
        # imported — return its relative path without copying. Without this,
        # re-saving a map whose items still point at a library file duplicates it
        # (foo.png → foo_1.png → foo_1_1.png on each save).
        abs_src = os.path.abspath(src_path)
        abs_lib = os.path.abspath(self.asset_dir)
        if abs_src == abs_lib or abs_src.startswith(abs_lib + os.sep):
            return os.path.relpath(abs_src, abs_lib).replace(os.sep, "/")

        dest_dir = os.path.join(self.asset_dir, category)
        os.makedirs(dest_dir, exist_ok=True)
        base = os.path.basename(src_path)
        name, ext = os.path.splitext(base)
        dest_name = base
        dest      = os.path.join(dest_dir, dest_name)
        i = 1
        while os.path.exists(dest):
            dest_name = f"{name}_{i}{ext}"
            dest      = os.path.join(dest_dir, dest_name)
            i += 1

        shutil.copy(src_path, dest)
        return os.path.join(category, dest_name).replace(os.sep, "/")

    def _import_video(self, src_path: str, category: str = DEFAULT_DROP_CATEGORY) -> str:
        """Import a video into the asset library. If its codec can't be decoded
        in hardware, ask the user whether to transcode it to H264 (smooth GPU
        playback) or keep the original (software decode). Returns the library-
        relative path used to reference it (e.g. "backgrounds/foo.mp4")."""
        codec = probe_video_codec(src_path)
        if codec is None or codec in HW_VIDEO_CODECS or _ffmpeg_tool("ffmpeg") is None:
            # already hardware-friendly (or ffmpeg unavailable) → plain copy
            return self._import_asset(src_path, category)

        # Non-hardware codec + ffmpeg available: let the user decide (the prompt
        # also cues them why an accepted conversion makes the drop take longer).
        resp = QMessageBox.question(
            self, "Convert video for hardware playback?",
            f"“{os.path.basename(src_path)}” uses the {codec.upper()} codec, which "
            f"your GPU can’t decode in hardware, so it will play back with slower "
            f"software decoding.\n\nConvert it to H.264 now for smooth hardware "
            f"playback? This can take a little while for large videos.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if resp != QMessageBox.Yes:
            # Keep the original; software decode will handle it.
            return self._import_asset(src_path, category)

        # pick a unique <name>.mp4 in the category subfolder
        dest_dir = os.path.join(self.asset_dir, category)
        os.makedirs(dest_dir, exist_ok=True)
        name = os.path.splitext(os.path.basename(src_path))[0]
        dest_name = name + ".mp4"
        dest = os.path.join(dest_dir, dest_name)
        i = 1
        while os.path.exists(dest):
            dest_name = f"{name}_{i}.mp4"
            dest = os.path.join(dest_dir, dest_name)
            i += 1

        ok, _cancelled = self._run_transcode(
            transcode_to_h264, src_path, dest,
            title="Converting Video",
            label=f"Converting “{os.path.basename(src_path)}” to H.264\n"
                  f"for smooth hardware playback…")
        if ok:
            self.statusBar().showMessage(f"Converted to H264: {dest_name}", 5000)
            return os.path.join(category, dest_name).replace(os.sep, "/")
        # Cancelled or failed → fall back to copying the original (software
        # decode) so the asset still imports rather than the drop silently failing.
        self.statusBar().showMessage("Using original (software decode).", 5000)
        return self._import_asset(src_path, category)

    def _run_transcode(self, fn, *args, title="Converting", label="Converting…", **kwargs):
        """Run a blocking ffmpeg transcode `fn` on a worker thread while the GUI
        stays responsive behind a modal progress dialog (with Cancel). `fn` must
        accept progress_cb and cancel_cb kwargs (the transcode_* helpers do).
        Returns (ok: bool, cancelled: bool).

        A nested QEventLoop keeps this call synchronous for the import flow while
        still pumping paint/dialog events — the same pattern QMessageBox.exec()
        uses. The dialog is window-modal, so the user can't edit mid-import."""
        sig = _TranscodeSignals()
        cancel = threading.Event()
        state = {"ok": False, "determinate": False}
        loop = QEventLoop()

        dlg = QProgressDialog(label, "Cancel", 0, 100, self)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        # Use our own bar with the % text hidden: until real progress arrives we
        # animate it as an activity "pulse" (see below), where a percentage would
        # be misleading.
        bar = QProgressBar(dlg)
        bar.setRange(0, 100)
        bar.setTextVisible(False)
        dlg.setBar(bar)

        # Some encoders — notably libwebp_anim — buffer the whole clip and report
        # progress only once, at the very end (verified: a single end block, no
        # intermediate frames). A real bar would just sit at 0 then jump to 100.
        # So we animate a left↔right pulse on a GUI-thread timer (which fires in
        # the nested loop) to show activity, and switch to a true percentage bar
        # only if genuine intermediate progress arrives (e.g. H.264 on a long
        # video). Driving our own QTimer avoids the style's busy-bar indicator,
        # which renders as a static full bar here.
        pulse = QTimer(dlg)
        pulse.setInterval(40)
        phase = {"v": 0, "dir": 4}
        def _pulse():
            v = phase["v"] + phase["dir"]
            if v >= 100 or v <= 0:
                phase["dir"] = -phase["dir"]
                v = max(0, min(100, v))
            phase["v"] = v
            bar.setValue(v)
        pulse.timeout.connect(_pulse)

        def on_progress(p):
            if dlg.wasCanceled():
                return
            if 0 < p < 100:                 # real intermediate progress available
                if not state["determinate"]:
                    state["determinate"] = True
                    pulse.stop()
                    bar.setTextVisible(True)
                bar.setValue(p)
        sig.progress.connect(on_progress)
        sig.done.connect(lambda ok: (state.update(ok=ok), loop.quit()))
        dlg.canceled.connect(cancel.set)

        kwargs["progress_cb"] = lambda frac: sig.progress.emit(int(frac * 100))
        kwargs["cancel_cb"]   = cancel.is_set

        def work():
            ok = False
            try:
                ok = fn(*args, **kwargs)
            finally:
                sig.done.emit(bool(ok))     # queued → runs on the GUI thread
        t = threading.Thread(target=work, daemon=True)
        dlg.show()                          # show now; don't wait on the auto-show heuristic
        pulse.start()
        t.start()
        loop.exec()                         # returns when work() emits done
        pulse.stop()
        t.join(timeout=2)
        # Read the cancel flag BEFORE closing — QProgressDialog.close() itself
        # emits canceled(), which would otherwise set the flag spuriously.
        cancelled = cancel.is_set()
        dlg.canceled.disconnect(cancel.set)
        dlg.close()
        return state["ok"], cancelled

    def _import_animated_object(self, src_path: str, category: str) -> str | None:
        """Convert a transparent video into an animated WebP (alpha-preserving)
        and import it into `category`. Returns the library-relative path, or None
        if the user cancels. Falls back to a plain video import if conversion
        fails (the asset still appears, just without transparency)."""
        codec = probe_video_codec(src_path)
        # pick a unique <name>.webp in the category subfolder
        dest_dir = os.path.join(self.asset_dir, category)
        os.makedirs(dest_dir, exist_ok=True)
        name = os.path.splitext(os.path.basename(src_path))[0]
        dest_name = name + ".webp"
        dest = os.path.join(dest_dir, dest_name)
        i = 1
        while os.path.exists(dest):
            dest_name = f"{name}_{i}.webp"
            dest = os.path.join(dest_dir, dest_name)
            i += 1

        ok, cancelled = self._run_transcode(
            transcode_to_animated_webp, src_path, dest, src_codec=codec,
            title="Importing Object",
            label=f"Converting “{os.path.basename(src_path)}”\n"
                  f"to a transparent animation…")
        if ok:
            self.statusBar().showMessage(f"Imported transparent animation: {dest_name}", 5000)
            return os.path.join(category, dest_name).replace(os.sep, "/")
        if cancelled:
            self.statusBar().showMessage("Import cancelled.", 5000)
            return None                       # abort the drop (no item added)
        # Conversion failed → fall back to a normal (opaque) video import so the
        # asset still lands on the map instead of the drop silently failing.
        self.statusBar().showMessage(
            "Transparency conversion failed; importing as a normal video.", 5000)
        return self._import_video(src_path, category)

    def _update_map_refs(self, old_name: str, new_name: str):
        """Rewrite map JSONs that reference an asset by name (used after a
        library video is transcoded and its extension changes)."""
        for root, _dirs, files in os.walk(self.maps_dir):
            for fn in files:
                if not fn.lower().endswith(".json"):
                    continue
                p = os.path.join(root, fn)
                try:
                    with open(p) as f:
                        data = json.load(f)
                except Exception:
                    continue
                changed = False
                for it in data.get("items", []):
                    if it.get("asset") == old_name:
                        it["asset"] = new_name
                        changed = True
                if changed:
                    with open(p, "w") as f:
                        json.dump(data, f, indent=2)

    def open_maps_assets_folder(self):
        """
        Opens the ArcaneAtlas data root (contains maps/ and assets/) in the OS file manager.
        """
        try:
            # Prefer the unified root so users can see both maps and assets in one place.
            target = Path(self.maps_dir).resolve().parent if hasattr(self, "maps_dir") else Path(str(DATA_ROOT))
            target.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target)))
        except Exception as e:
            QMessageBox.warning(self, "Open Folder", f"Could not open folder:\n{e}")


    def set_theme(self):
        QApplication.setStyle("Fusion")
        arcana_palette = QPalette()
        arcana_palette.setColor(QPalette.Window, QColor(42, 46, 50))
        arcana_palette.setColor(QPalette.WindowText, QColor(252, 252, 252))
        arcana_palette.setColor(QPalette.Base, QColor(27, 30, 32))
        arcana_palette.setColor(QPalette.AlternateBase, QColor(35, 38, 41))
        arcana_palette.setColor(QPalette.ToolTipBase, QColor(49, 54, 59))
        arcana_palette.setColor(QPalette.ToolTipText, QColor(252, 252, 252))
        arcana_palette.setColor(QPalette.Text, QColor(252, 252, 252))
        arcana_palette.setColor(QPalette.Button, QColor(49, 54, 59))
        arcana_palette.setColor(QPalette.ButtonText, QColor(252, 252, 252))
        arcana_palette.setColor(QPalette.BrightText, QColor(75, 75, 75))
        arcana_palette.setColor(QPalette.Link, QColor(209, 199, 242))
        arcana_palette.setColor(QPalette.Highlight, QColor(110, 86, 169))
        arcana_palette.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
        QApplication.setPalette(arcana_palette)
        for widget in QApplication.allWidgets():
            widget.setPalette(arcana_palette)
            widget.style().polish(widget)
            widget.repaint()

    def toggle_player_view(self, checked):
        """
        Toggles the Player view on or off.
        If enabled, it creates and syncs the Player view with the GM view.
        """
        if not checked:
            if hasattr(self, "player_window"):
                self.player_window.hide()        # keep it, don’t delete
            if hasattr(self, "player_view_item"):
                self.player_view_item.setVisible(False)  # keep it in the scene
            return
        
        # new_map does a scene.clear() which removes playerviewitem, but the reference still exists
        # but the act of calling .scene in self.player_view_item.scene() is None
        # causes a RuntimeError. So checking if it exists in order to delete before creating it again
        # has issues, the following code alleviates this with the try
        # If scene.clear() nuked our old item (or deleted its C++), drop the Python attr
        if hasattr(self, "player_view_item"):
            stale = False
            try:
                # if it’s no longer in any scene, mark stale
                if self.player_view_item.scene() is None:
                    stale = True
            except RuntimeError:
                # accessing .scene() on a freed C++ object also means stale
                stale = True
            if stale:
                delattr(self, "player_view_item")
        
        # compute pixel dims (auto-derive the non-anchor dimension from the
        # selected screen's aspect first, so the player-view rect matches the panel)
        self._refresh_auto_dims()
        w_px = self.playerDisplayWidth  * 72
        h_px = self.playerDisplayHeight * 72

        # 1) Lazily create the Player window if it doesn't exist
        if not hasattr(self, "player_window"):
            self.player_window = PlayerWindow()
            self.player_window.canvas_view.setScene(self.scene)  # Shared scene with GM
            # Honour the "Hide Map" shield if it was ticked before the window existed.
            self.player_window.canvas_view.blank_view = self.hide_playerview_checkBox.isChecked()
            # Keep the checkbox in sync if the user closes the window themselves.
            self.player_window.closed.connect(self._on_player_window_closed)

        # 2) Sync fog immediately
        self.sync_fog_to_player_view()

        # 3) Position the Player window
        self.center_on_selected_display(self.player_window)

        # 4) Set the display mode (Fullscreen, Windowed, SingleSplit)
        mode = self.player_screen_mode
        if mode == "Fullscreen":
            self.player_window.showFullScreen()
        elif mode == "Windowed":
            self.player_window.showNormal()
        elif mode == "SingleSplit":
            self.player_window.showNormal()
            screen = QApplication.screens()[self.selected_screen_index]
            geom = screen.geometry()
            target_h = int(geom.height() * 0.7)
            ratio_in = self.playerDisplayWidth / self.playerDisplayHeight
            target_w = int(ratio_in * target_h)
            self.player_window.resize(target_w, target_h)
            self.player_window.show()
        else:
            self.player_window.showNormal()  # Default to normal windowed

        # 5) Immediately sync the fog to ensure consistency
        self.sync_fog_to_player_view()

        # NEW: ensure sizes/rects are matched right away
        self.sync_fog_alignment()

        if not hasattr(self, "player_view_item"):
            # new item, give it the right size immediately
            self.player_view_item = PlayerViewItem(0, 0, w_px, h_px, title="Player View", gm_view=self.canvas_view)
            self.scene.addItem(self.player_view_item)
            self._center_playerview()
        else:
            # already there: just resize its rect
            self.player_view_item.setRect(0, 0, w_px, h_px)
            self.player_view_item.setVisible(True)   # ← add this line
    

        # 5) Sync the camera to that rectangle
        
        # have an issue of needing to wait 100ms for sync to work after the
        # window is created, I'm hoping there is a better way of doing this in the future
        QTimer.singleShot(100, self.sync_player_view_to_camera)

    def _on_hide_playerview_toggled(self, checked):
        """Blank the Player Window to solid black (GM setup shield). The flag lives
        on the player canvas and is honoured in Canvas.drawForeground; a repaint
        applies it. The LAN web stream grabs the same viewport, so browsers black
        out too. No-op if the player window isn't open yet — reapplied on create."""
        if hasattr(self, "player_window") and self.player_window:
            self.player_window.canvas_view.blank_view = checked
            self.player_window.canvas_view.viewport().update()

    def _on_player_window_closed(self):
        # The user closed the player window (not a minimize). Uncheck the box so
        # state stays consistent. Deferred one tick so we don't mutate window/UI
        # state from inside the window's own closeEvent; setChecked is idempotent
        # (no toggled when already off) so this can't loop.
        QTimer.singleShot(0, lambda: self.show_playerview_checkBox.setChecked(False))

    def _center_playerview(self):
        if hasattr(self, "player_view_item"):
            rect = self.canvas_view.scene.sceneRect()
            pv   = self.player_view_item
            pv.setPos(rect.center() - pv.boundingRect().center())

    def sync_fog_to_player_view(self):
        """
        Sync the reveal path + enabled state from the GM view to the Player view.
        The player renders the same path at full opacity (see drawForeground).
        """
        if not hasattr(self, "player_window"):
            return  # No Player view, nothing to sync

        pv = self.player_window.canvas_view  # Player view
        pv.fog_enabled = self.canvas_view.fog_enabled
        pv.setSceneRect(self.canvas_view.scene.sceneRect())
        pv.fog_reveal_path = self.canvas_view.fog_reveal_path
        pv.viewport().update()


    def _selected_screen_aspect(self):
        """Pixel aspect ratio (width / height) of the selected player screen, or
        None if unavailable. Pixels are square on modern panels, so this equals
        the physical aspect — reliable even when EDID physical size isn't (TVs
        over HDMI routinely report bogus physical sizes)."""
        screens = QApplication.screens()
        idx = self.selected_screen_index
        if 0 <= idx < len(screens):
            g = screens[idx].geometry()
            if g.width() > 0 and g.height() > 0:
                return g.width() / g.height()
        return None

    def _selected_screen_aspect_label(self):
        """Human-readable '<w>×<h>  (<a>:<b>)' for the selected screen, or None
        if no screen aspect is available (→ auto mode can't be used)."""
        screens = QApplication.screens()
        idx = self.selected_screen_index
        if 0 <= idx < len(screens):
            g = screens[idx].geometry()
            if g.width() > 0 and g.height() > 0:
                d = math.gcd(g.width(), g.height())
                return f"{g.width()}×{g.height()}  ({g.width() // d}:{g.height() // d})"
        return None

    def _refresh_auto_dims(self):
        """In auto mode, derive the non-anchor display dimension from the anchor
        × the selected screen's pixel aspect, so the player-view rect matches the
        panel and fitInView shows exactly the framed region (no overshoot). No-op
        in manual mode or when the screen aspect can't be determined."""
        if not getattr(self, "player_dims_auto", True):
            return
        aspect = self._selected_screen_aspect()
        if not aspect:
            return
        if getattr(self, "player_dims_anchor", "width") == "height":
            self.playerDisplayWidth = round(self.playerDisplayHeight * aspect, 3)
        else:   # width is the anchor
            self.playerDisplayHeight = round(self.playerDisplayWidth / aspect, 3)

    def show_dimensions_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Display Dimensions")
        form = QFormLayout(dlg)
        w_edit = QLineEdit(str(self.playerDisplayWidth))
        h_edit = QLineEdit(str(self.playerDisplayHeight))
        aspect = self._selected_screen_aspect()
        aspect_text = self._selected_screen_aspect_label()

        auto_chk = QCheckBox("Auto (lock to display aspect ratio)")
        aspect_lbl = QLabel()
        if aspect_text:
            aspect_lbl.setText(f"Detected display: {aspect_text}")
            auto_chk.setChecked(getattr(self, "player_dims_auto", True))
        else:
            # No screen aspect available → auto is impossible; force manual.
            aspect_lbl.setText("Display aspect: not detected — enter both dimensions")
            auto_chk.setChecked(False)
            auto_chk.setEnabled(False)

        form.addRow("Width (in):",  w_edit)
        form.addRow("Height (in):", h_edit)
        form.addRow("", auto_chk)
        form.addRow("", aspect_lbl)

        # Guard against the programmatic setText below re-triggering the editing
        # handlers (which would ping-pong width↔height forever).
        updating = {"busy": False}

        def derive_from(anchor):
            """In auto mode, recompute the non-anchor field from the one the user
            just edited; that edited field becomes the persisted anchor."""
            if not (auto_chk.isChecked() and aspect) or updating["busy"]:
                return
            try:
                val = float((w_edit if anchor == "width" else h_edit).text())
            except ValueError:
                return
            self.player_dims_anchor = anchor
            updating["busy"] = True
            if anchor == "width":
                h_edit.setText(str(round(val / aspect, 3)))
            else:
                w_edit.setText(str(round(val * aspect, 3)))
            updating["busy"] = False

        def on_auto_toggled(checked):
            # Both fields stay editable in auto (either can drive); turning auto
            # on re-syncs the non-anchor from the current anchor.
            if checked:
                derive_from(getattr(self, "player_dims_anchor", "width"))

        w_edit.textEdited.connect(lambda _=None: derive_from("width"))
        h_edit.textEdited.connect(lambda _=None: derive_from("height"))
        auto_chk.toggled.connect(on_auto_toggled)

        btns = QHBoxLayout()
        ok     = QPushButton("OK")
        cancel = QPushButton("Cancel")
        btns.addWidget(ok); btns.addWidget(cancel)
        form.addRow(btns)

        def accept():
            self.player_dims_auto = auto_chk.isChecked()
            try:
                self.playerDisplayWidth  = float(w_edit.text())
                self.playerDisplayHeight = float(h_edit.text())
            except ValueError:
                pass
            # Re-derive in auto mode to guarantee width/height stay consistent.
            self._refresh_auto_dims()

            # if the viewport is already in the scene, resize it now:
            if hasattr(self, "player_view_item"):
                w_px = self.playerDisplayWidth  * 72
                h_px = self.playerDisplayHeight * 72
                self.player_view_item.setRect(0, 0, w_px, h_px)
                self.sync_player_view_to_camera()
            dlg.accept()

        ok.clicked.connect(accept)
        cancel.clicked.connect(dlg.reject)

        dlg.exec()

    def center_on_selected_display(self, player_window):

        # Center the player window on the selected screen.
        if self.selected_screen_index == -1:
            log.warning("No display selected; cannot center the player window.")
            return
        
        # Center the player window on the selected screen.
        screen = QApplication.screens()[self.selected_screen_index]  # Get the selected screen

        # Get the geometry of the selected screen
        screen_geometry = screen.geometry()

        # Move the player window to the center of the selected screen
        window_rect = player_window.geometry()
        window_width = window_rect.width()
        window_height = window_rect.height()

        new_x = screen_geometry.left() + (screen_geometry.width() - window_width) // 2
        new_y = screen_geometry.top() + (screen_geometry.height() - window_height) // 2

        player_window.move(new_x, new_y)
            
    def wheelEvent(self, event):
        """Handle Ctrl + MouseWheel zooming."""
        if event.modifiers() == Qt.ControlModifier:
            # nudge the slider by one step instead
            delta = event.angleDelta().y()
            step  = self.zoom_slider.singleStep()
            if delta > 0:
                new_val = min(self.zoom_slider.value() + step, self.zoom_slider.maximum())
            else:
                new_val = max(self.zoom_slider.value() - step, self.zoom_slider.minimum())
            self.zoom_slider.setValue(new_val)

            event.accept()  # Mark the event as handled
        else:
            super().wheelEvent(event)  # Otherwise, handle normally

    def sync_player_view_to_camera(self):
        # Scale & center the player’s canvas_view so that the
        #   player_view_item fills the window (keeping aspect).
        # get the rectangle in scene-coords
        rect = self.player_view_item.sceneBoundingRect()
        # grab the PlayerWindow’s view
        view = self.player_window.canvas_view
        # fit that rect into the view area
        view.fitInView(rect, Qt.KeepAspectRatio)

        # Not sure if this is needed
        self.sync_fog_to_player_view()
        # NEW: keep mask size aligned to scene
        self.sync_fog_alignment()

    def _load_settings(self):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
            # guard against missing or bogus keys
            w = data.get("playerDisplayWidth", self.playerDisplayWidth)
            h = data.get("playerDisplayHeight", self.playerDisplayHeight)
            # sanity‐check numeric types
            if isinstance(w, (int, float)) and isinstance(h, (int, float)):
                self.playerDisplayWidth = w
                self.playerDisplayHeight = h
            # accept the legacy "playerDimsAutoWidth" key too
            auto = data.get("playerDimsAuto", data.get("playerDimsAutoWidth"))
            if isinstance(auto, bool):
                self.player_dims_auto = auto
            anchor = data.get("playerDimsAnchor")
            if anchor in ("width", "height"):
                self.player_dims_anchor = anchor
            # restore splitter sizes if present
            sizes = data.get("splitterSizes")
            if isinstance(sizes, list) and all(isinstance(x, (int, float)) for x in sizes):
                # apply the saved left/right widths
                self.splitter.setSizes([int(s) for s in sizes])
            # restore map‐browser column widths if present
            hdr_sizes = data.get("mapHeaderSizes")
            if isinstance(hdr_sizes, list) and len(hdr_sizes) == self.map_view.header().count():
                header = self.map_view.header()
                for idx, w in enumerate(hdr_sizes):
                    if isinstance(w, (int, float)):
                        header.resizeSection(idx, int(w))
            # restore the saved player party (roster of token references)
            members = (data.get("party") or {}).get("members")
            if isinstance(members, list):
                self.party_members = [m for m in members
                                      if isinstance(m, dict) and isinstance(m.get("asset"), str)]
            # whether opening a content-bearing map locks its assets by default
            # (the Settings dialog reads self.lock_on_open when opened)
            loo = data.get("lockOnOpen")
            if isinstance(loo, bool):
                self.lock_on_open = loo
            # LAN web-sharing port preference
            wpc = data.get("webPortCustom")
            if isinstance(wpc, bool):
                self.web_port_custom = wpc
            wp = data.get("webPort")
            if isinstance(wp, int) and 1024 <= wp <= 65534:
                self.web_port = wp
        except (OSError, ValueError):
            # no file or invalid JSON → keep defaults
            pass

    def closeEvent(self, event):
        # Stop LAN web sharing (close sockets) before teardown.
        if getattr(self, "web_server", None) is not None:
            self.web_server.stop()
        # save current dimensions
        try:
            with open(SETTINGS_FILE, 'w') as f:
                # collect map browser header (column) widths
                header = self.map_view.header()
                header_sizes = [header.sectionSize(i) for i in range(header.count())]

                json.dump({
                    "playerDisplayWidth":   self.playerDisplayWidth,
                    "playerDisplayHeight":  self.playerDisplayHeight,
                    "playerDimsAuto":       self.player_dims_auto,
                    "playerDimsAnchor":     self.player_dims_anchor,
                    "splitterSizes":        self.splitter.sizes(),
                    "mapHeaderSizes":       header_sizes,
                    "party":                {"members": self.party_members},
                    "lockOnOpen":           self.lock_on_open,
                    "webPortCustom":        self.web_port_custom,
                    "webPort":              self.web_port,
                }, f, indent=2)
        except OSError as e:
            log.warning("Failed to save settings: %s", e)
        
        # If a map is open, persist just the playerview position
        if getattr(self, "current_map_path", None):
            self._save_playerview_position_only(self.current_map_path)
            self._save_fog_only(self.current_map_path)

        # Stop all video decoders before teardown — an active QMediaPlayer being
        # destroyed during app shutdown can crash the FFmpeg backend (Bus error
        # in QObject teardown).
        self._stop_all_videos()

        # also tear down player window if open
        if hasattr(self, "player_window"):
            self.player_window.close()
        super().closeEvent(event)

    def _position_tiled_windows(self):
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        avail  = screen.availableGeometry()

        # 2/3 : 1/3 split
        main_w = int(avail.width() * 2/3)
        fudge  = 30
        main_h = max(self.minimumHeight(), avail.height() - fudge)
        main_x, main_y = avail.x(), avail.y()

        # 1) Place & size main window via move()/resize()
        if self.isVisible():
            self.move(main_x, main_y)
            self.resize(main_w, main_h)
        else:
            # (optional) stash main if you ever need it later
            pass

        # 2) Compute player-rect and stash it
        player_rect = QRect(
            main_x + main_w,
            main_y,
            avail.width() - main_w,
            main_h
        )
        self._pending_player_geom = player_rect

        # 3) If already visible, apply immediately
        if hasattr(self, "player_window") and self.player_window.isVisible():
            self.player_window.move(player_rect.x(), player_rect.y())
            self.player_window.resize(player_rect.width(), player_rect.height())

    def _on_debug_fullscreen_toggled(self, checked: bool):
        # toggled ON → go back to full/full
        if checked:
            # uncheck SingleScreen when we go back into Fullscreen
            self.singlescreen_action.setChecked(False)

            # use maximized instead of full‐screen so the title bar stays
            self.showMaximized()
            if hasattr(self, "player_window"):
                self.player_window.showMaximized()
            return


        # toggled OFF → compute side-by-side geometry
        # find the QScreen this window is on
        win_handle = self.windowHandle()
        screen    = win_handle.screen() if win_handle else QApplication.primaryScreen()
        geom      = screen.geometry()

        # compute the player’s width to preserve its aspect ratio
        ratio    = self.playerDisplayWidth / self.playerDisplayHeight
        screen_h = geom.height()
        player_w = int(ratio * screen_h)

        # main window takes the leftover width
        main_w = geom.width() - player_w

        # un-maximize both
        self.showNormal()
        if hasattr(self, "player_window"):
            self.player_window.showNormal()

        # tile them full-height, side-by-side
        self.setGeometry(geom.x(),
                         geom.y(),
                         main_w,
                         screen_h)

    def _on_debug_singlescreen_toggled(self, checked: bool):
        if not checked:
            return

        # ➜ ensure normal state and clear fullscreen QAction
        self.showNormal()
        if hasattr(self, "ui") and hasattr(self.ui, "actionFullscreen"):
            self.ui.actionFullscreen.setChecked(False)

        # ➜ tile both windows
        self._position_tiled_windows()

        # ➜ now show the player window (which will pick up the stashed geom)
        if hasattr(self, "player_window"):
            self.player_window.showNormal()


class TextBoxEditDialog(QDialog):
    """Rich editor for a TextBoxItem: text, font size, bold/italic, alignment,
    text/background/border colours (alpha-capable via ColorPickerButton), and
    border width. Live-previews onto the item while open; MainWindow.edit_textbox
    reverts to the pre-edit styling on Cancel."""

    ALIGN_OPTIONS = [("Left", Qt.AlignLeft), ("Center", Qt.AlignHCenter),
                     ("Right", Qt.AlignRight)]

    def __init__(self, parent, item):
        super().__init__(parent)
        self.setWindowTitle("Edit Text")
        self.setModal(True)
        self._item = item
        root = QVBoxLayout(self)

        self.text_edit = QPlainTextEdit(item._text)
        self.text_edit.setMinimumSize(340, 120)
        root.addWidget(self.text_edit)

        form = QFormLayout()
        self.size_spin = QSpinBox(); self.size_spin.setRange(6, 300)
        self.size_spin.setValue(int(round(item.font_size)))
        form.addRow("Font size:", self.size_spin)

        style_row = QHBoxLayout()
        self.bold_chk = QCheckBox("Bold"); self.bold_chk.setChecked(item.bold)
        self.italic_chk = QCheckBox("Italic"); self.italic_chk.setChecked(item.italic)
        style_row.addWidget(self.bold_chk); style_row.addWidget(self.italic_chk)
        style_row.addStretch(1)
        form.addRow("Style:", self._wrap(style_row))

        self.align_combo = QComboBox()
        for label, flag in self.ALIGN_OPTIONS:
            self.align_combo.addItem(label, int(flag))
        cur = next((i for i, (_l, f) in enumerate(self.ALIGN_OPTIONS)
                    if int(f) == int(item.align)), 0)
        self.align_combo.setCurrentIndex(cur)
        form.addRow("Align:", self.align_combo)

        self.text_color_btn = ColorPickerButton(initial=QColor(item.text_color))
        self.text_color_btn.setText(" Color")
        form.addRow("Text:", self.text_color_btn)

        self.bg_chk = QCheckBox("Fill")
        self.bg_chk.setChecked(item.bg_color.alpha() > 0)
        self.bg_color_btn = ColorPickerButton(initial=self._opaque(item.bg_color))
        self.bg_color_btn.setText(" Color")
        bg_row = QHBoxLayout(); bg_row.addWidget(self.bg_chk)
        bg_row.addWidget(self.bg_color_btn); bg_row.addStretch(1)
        form.addRow("Background:", self._wrap(bg_row))

        self.border_chk = QCheckBox("Show")
        self.border_chk.setChecked(item.border_width > 0 and item.border_color.alpha() > 0)
        self.border_color_btn = ColorPickerButton(initial=self._opaque(item.border_color))
        self.border_color_btn.setText(" Color")
        self.border_w_spin = QSpinBox(); self.border_w_spin.setRange(1, 20)
        self.border_w_spin.setValue(max(1, int(round(item.border_width))))
        bd_row = QHBoxLayout(); bd_row.addWidget(self.border_chk)
        bd_row.addWidget(self.border_color_btn)
        bd_row.addWidget(QLabel("Width:")); bd_row.addWidget(self.border_w_spin)
        bd_row.addStretch(1)
        form.addRow("Border:", self._wrap(bd_row))

        root.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)
        root.addWidget(bb)

        # Live preview: any change re-applies onto the item.
        for sig in (self.text_edit.textChanged, self.size_spin.valueChanged,
                    self.bold_chk.toggled, self.italic_chk.toggled,
                    self.align_combo.currentIndexChanged, self.bg_chk.toggled,
                    self.border_chk.toggled, self.border_w_spin.valueChanged):
            sig.connect(self._preview)
        for btn in (self.text_color_btn, self.bg_color_btn, self.border_color_btn):
            btn.colorChanged.connect(self._preview)

    def _wrap(self, layout):
        w = QWidget(); w.setLayout(layout); return w

    @staticmethod
    def _opaque(color):
        c = QColor(color)
        if c.alpha() == 0:
            c.setAlpha(255)
        return c

    def apply_to(self, item):
        item.prepareGeometryChange()
        item._text = self.text_edit.toPlainText()
        item.font_size = float(self.size_spin.value())
        item.bold = self.bold_chk.isChecked()
        item.italic = self.italic_chk.isChecked()
        item.align = Qt.AlignmentFlag(int(self.align_combo.currentData()))
        item.text_color = QColor(self.text_color_btn._color)
        if self.bg_chk.isChecked():
            item.bg_color = self._opaque(self.bg_color_btn._color)
        else:
            item.bg_color = QColor(0, 0, 0, 0)
        if self.border_chk.isChecked():
            item.border_color = self._opaque(self.border_color_btn._color)
            item.border_width = float(self.border_w_spin.value())
        else:
            item.border_width = 0.0
        item.update()

    def _preview(self, *_):
        self.apply_to(self._item)


class Canvas(QGraphicsView):
    # True only on the PlayerWindow's canvas (set in PlayerWindow.__init__). Items
    # read it during paint to hide themselves from the player view (see items.py
    # _painting_in_player_view).
    is_player_view = False

    # When True on the player canvas, drawForeground paints solid black over the
    # whole view instead of the scene — lets the GM set up/switch maps unseen
    # while the Player Window stays open (see MainWindow._on_hide_playerview_toggled).
    blank_view = False

    def __init__(self, parent=None):
        super().__init__(parent)

        self._panning = False
        self._pan_start = None
        self._pan_moved = False        # did the current right-drag actually pan?

        # your existing scene setup…
        self.scene = QGraphicsScene(self)
        # Use no spatial index: the default BSP tree caches item bounding rects
        # and can dereference a stale entry during paint while an item's geometry
        # is changing (live resize = setScale/moveBy every mouse-move), crashing
        # in QGraphicsItemPrivate::effectiveBoundingRect. Linear lookups are
        # plenty fast for a battle map's item count and remove that crash class.
        self.scene.setItemIndexMethod(QGraphicsScene.NoIndex)
        self.setScene(self.scene)
        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)

        # Left-drag on empty canvas marquee-selects items (multi-select); dragging
        # an item still moves it, right-drag still pans. PlayerWindow turns this off.
        self.setDragMode(QGraphicsView.RubberBandDrag)

        # grid & other init…
        self.grid_size = 72  # 1-inch at 72dpi
        # default grid color (semi-transparent gray); MainWindow overrides this
        # with self.default_gridcolor on startup.
        self.grid_color = QColor(128, 128, 128, 160)

        # Canvas auto-sizes (grow-only) to fit the maps placed on it, starting
        # from BASE_EXTENT centered on the origin. canvas_rect is the current
        # extent and the single source of truth for grid + scene + fog size.
        self.canvas_rect = self._snap_rect_out(self._base_rect())

        # **NEW** — create the grid group container up front
        self.grid_group = QGraphicsItemGroup()
        self.scene.addItem(self.grid_group)

        self.create_grid()

        # **IMPORTANT** enable drops on the *viewport* as well as the view
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)

        # make zooming under cursor…
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)

        # ── Fog state ──
        self.fog_enabled     = False
        # Reveal path: union of revealed brush stamps, in scene coordinates.
        # drawForeground fills the visible rect minus this path. Empty = fully fogged.
        self.fog_reveal_path = QPainterPath()
        self.fog_color       = QColor(0, 0, 0)  # fog fill color
        # In-progress brush stroke (accumulated cheaply, committed on release).
        self._fog_stroke      = None   # QPainterPath while a stroke is active
        self._fog_stroke_mode = None   # "reveal" | "hide"
        self.fog_brush_radius = 20
        self.fog_brush_shape  = "circle"
        # ── for brush‐preview outline when in reveal mode ──
        self.fog_preview_pos = None  # in scene coords

        self.gmfog_opacity = 0.5

    def resizeEvent(self, event):
        # Fires with the viewport's real size (unlike MainWindow.resizeEvent,
        # which runs before this child is laid out), so it's the right place to
        # keep the welcome card framed on first show and on window resizes.
        super().resizeEvent(event)
        if not self.is_player_view:
            mw = self.window()
            if hasattr(mw, "_center_view_on_welcome"):
                mw._center_view_on_welcome()

    def set_brush_shape(self, shape: str):
        """shape = 'circle' or 'square'"""
        if shape not in ("circle", "square"):
            return
        self.fog_brush_shape = shape

    def _brush_stamp(self, scene_pos, radius: int):
        """A QPainterPath for one brush stamp (circle or square) at scene_pos.

        The "circle" is approximated by a polygon, NOT addEllipse: a bezier
        ellipse is ~13 path elements, so unioning many of them balloons the fog
        path into tens of thousands of curve elements and every boolean op /
        repaint slows to a crawl (and worsens as more is revealed). A straight-
        segment polygon keeps the merged fog path an order of magnitude smaller,
        so reveal/hide and rendering stay fast — visually identical for soft fog.
        Segment count scales with radius so big brushes don't look faceted."""
        stamp = QPainterPath()
        x, y = scene_pos.x(), scene_pos.y()
        if self.fog_brush_shape == "circle":
            n = max(20, min(64, int(radius * 0.5)))   # ~ one vertex per 2px of arc
            poly = QPolygonF()
            for i in range(n):
                a = 2 * math.pi * i / n
                poly.append(QPointF(x + radius * math.cos(a), y + radius * math.sin(a)))
            stamp.addPolygon(poly)
            stamp.closeSubpath()
        else:
            stamp.addRect(x - radius, y - radius, 2 * radius, 2 * radius)
        return stamp

    def _stamp_stroke(self, scene_pos, radius: int, mode: str):
        """Add one brush stamp to the in-progress stroke.

        The stroke is kept as its own *separate, continuously-simplified* path
        (just the current drag, not the whole revealed history). Each step unions
        one stamp into that small outline and re-simplifies — cheap and bounded no
        matter how much was revealed before, because the big committed
        `fog_reveal_path` is untouched until release. That's the fix for the brush
        lagging the cursor: the old code unioned each stamp into the ever-growing
        committed path (O(n²) over a stroke)."""
        if not self.fog_enabled:
            return
        if self._fog_stroke is None or self._fog_stroke_mode != mode:
            self.commit_fog_stroke()           # flush any stroke from the other tool
            self._fog_stroke = QPainterPath()
            self._fog_stroke_mode = mode
        self._fog_stroke = self._fog_stroke.united(
            self._brush_stamp(scene_pos, radius)).simplified()
        self.viewport().update()

    def reveal_at(self, scene_pos, radius: int):
        """Reveal (remove fog from) a brush stamp at scene_pos (scene coords)."""
        self._stamp_stroke(scene_pos, radius, "reveal")

    def hide_at(self, scene_pos, radius: int):
        """Re-hide (add fog back over) a brush stamp at scene_pos (scene coords)."""
        self._stamp_stroke(scene_pos, radius, "hide")

    def commit_fog_stroke(self):
        """Fold the in-progress stroke into fog_reveal_path with one boolean op,
        then simplify. Called on stroke end (mouseRelease) and before save."""
        if self._fog_stroke is None or self._fog_stroke.isEmpty():
            self._fog_stroke = None
            self._fog_stroke_mode = None
            return
        if self._fog_stroke_mode == "hide":
            self.fog_reveal_path = self.fog_reveal_path.subtracted(self._fog_stroke)
        else:
            self.fog_reveal_path = self.fog_reveal_path.united(self._fog_stroke)
        self.fog_reveal_path = self.fog_reveal_path.simplified()
        self._fog_stroke = None
        self._fog_stroke_mode = None

    def simplify_fog(self):
        """Commit any pending stroke and collapse the path to a minimal form.
        Called before save to bound element count / serialized JSON size."""
        self.commit_fog_stroke()
        if not self.fog_reveal_path.isEmpty():
            self.fog_reveal_path = self.fog_reveal_path.simplified()

    def set_gm_fog_opacity(self, opacity: float):
        # Directly sets the GM fog opacity (0.0 - 1.0) for this Canvas.
        self.gmfog_opacity = opacity
        self.viewport().update()  # Trigger re-draw

    def drawForeground(self, painter, rect):
        # After all items are drawn, paint the fog on top, and show the brush preview.
        super().drawForeground(painter, rect)

        # "Hide Map": black out the player view entirely (GM setup shield). rect is
        # the exposed scene region, so filling it covers the whole viewport. Skip
        # fog + brush previews below — nothing should show through.
        if self.is_player_view and self.blank_view:
            painter.fillRect(rect, Qt.black)
            return

        if self.fog_enabled:
            painter.save()
            painter.setPen(Qt.NoPen)

            # Check if this view is in the PlayerWindow or the MainWindow
            if self.window().__class__.__name__ == "PlayerWindow":
                painter.setOpacity(1.0)  # 100% for player view

            else:
                # Use the gm_fog_opacity value for the GM view
                painter.setOpacity(self.gmfog_opacity)

            # Fill the exposed area minus what's been revealed. The reveal path is
            # in scene coords (the painter is too), so this is correct for both
            # the GM scene and the player's shared scene with no world-anchor
            # bookkeeping — the player canvas's self.scene attribute points at its
            # own empty scene, but we never consult it here.
            fogged = QPainterPath()
            fogged.addRect(rect)
            if not self.fog_reveal_path.isEmpty():
                fogged = fogged.subtracted(self.fog_reveal_path)

            # Fold the in-progress stroke into the fogged region before filling, so
            # it's all one fill at the right opacity (no Clear-compositing, which
            # would zero the opaque viewport to black instead of revealing scene):
            #   reveal → subtract the stroke (less fog)
            #   hide   → unite the stroke   (more fog)
            if self._fog_stroke is not None and not self._fog_stroke.isEmpty():
                if self._fog_stroke_mode == "hide":
                    fogged = fogged.united(self._fog_stroke)
                else:
                    fogged = fogged.subtracted(self._fog_stroke)

            painter.fillPath(fogged, self.fog_color)
            painter.restore()

            # Player tokens (and pings) stay visible THROUGH fog. Fog is drawn here
            # in the foreground (above every item regardless of z), so the only way
            # to keep an item on top of it is to re-paint that item now, over the
            # fog. Use the real attached scene (the player canvas's `self.scene`
            # attribute is its own empty scene), and pass the viewport as the
            # widget so each item's paint() picks the correct per-view chrome and
            # honours visible_to_player.
            scene = QGraphicsView.scene(self)
            if scene is not None:
                opt = QStyleOptionGraphicsItem()
                vp = self.viewport()
                for it in scene.items():
                    party_token = (getattr(it, "is_token", False)
                                   and getattr(it, "in_party", False) and it.isVisible())
                    if party_token or isinstance(it, PingItem):
                        painter.save()
                        painter.setTransform(it.sceneTransform(), True)
                        it.paint(painter, opt, vp)
                        painter.restore()

        # Restore Brush Preview (Revealer Tool)
        if getattr(self.window(), "_fog_reveal_mode", False) and self.fog_preview_pos:
            pen = QPen(QColor(255, 255, 255, 200), 2, Qt.SolidLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)

            r = self.fog_brush_radius // 2
            x0 = self.fog_preview_pos.x() - r
            y0 = self.fog_preview_pos.y() - r
            w = h = 2 * r

            if self.fog_brush_shape == "circle":
                painter.drawEllipse(QRectF(x0, y0, w, h))
            else:
                painter.drawRect(QRectF(x0, y0, w, h))

        # Restore Brush Preview (Hider Tool)
        if getattr(self.window(), "_fog_hide_mode", False) and self.fog_preview_pos:
            pen = QPen(QColor(255, 255, 255, 200), 2, Qt.SolidLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)

            r = self.fog_brush_radius // 2
            x0 = self.fog_preview_pos.x() - r
            y0 = self.fog_preview_pos.y() - r
            w = h = 2 * r

            if self.fog_brush_shape == "circle":
                painter.drawEllipse(QRectF(x0, y0, w, h))
            else:
                painter.drawRect(QRectF(x0, y0, w, h))

    def keyPressEvent(self, event):
        # If the user hits Delete, remove every selected item
        if event.key() == Qt.Key_Delete:
            removed = False
            for item in list(self.scene.selectedItems()):
                # Stop any video/animation decode before the item is freed (the
                # FFmpeg sink can otherwise crash; a QMovie timer would leak).
                if isinstance(item, (InteractiveVideoItem, AnimatedItem)):
                    item.teardown_player()
                self.scene.removeItem(item)
                removed = True
            if removed:
                mw = self.window()
                if hasattr(mw, "refresh_canvas_extent"):
                    mw.refresh_canvas_extent()
                if hasattr(mw, "update_layers_list"):
                    mw.update_layers_list()
                if hasattr(mw, "mark_dirty"):
                    mw.mark_dirty()
            return
        super().keyPressEvent(event)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if not url.isLocalFile():
                    continue
                ext = url.toLocalFile().lower().rsplit(".",1)[-1]
                if ext in ("jpg", "jpeg", "png", "webp", "mp4", "avi", "mov", "m4v", "webm"):
                    # Force Copy: a drag from the Assets tree defaults to Move, and
                    # accepting that would make the tree delete the source file.
                    event.setDropAction(Qt.CopyAction)
                    event.accept()
                    return

    # NOTE: no contextMenuEvent override. On this platform the context-menu event
    # fires on right-button *press*, so showing a (blocking) menu or clearing
    # _panning there would kill right-drag panning before the drag even starts.
    # Item context menus still work via the default QGraphicsView handling; the
    # empty-space "Add Text Box" menu is driven from mouseReleaseEvent (a right
    # *click* with no drag) instead — see _maybe_show_canvas_menu().

    def _maybe_show_canvas_menu(self, vpos, gpos):
        """Right-click (no drag) on empty canvas → offer 'Add Text Box'. Skipped on
        the player window, in a fog tool, and when a *selected* map item is under
        the cursor (that item shows its own menu via the default context-menu
        path). Called from mouseReleaseEvent AFTER _panning is cleared, so it can't
        leave the view stuck panning."""
        if getattr(self, "is_player_view", False):
            return
        mw = self.window()
        if getattr(mw, "_fog_reveal_mode", False) or getattr(mw, "_fog_hide_mode", False):
            return
        hits = [it for it in self.items(vpos)
                if isinstance(it, (InteractivePixmapItem, InteractiveVideoItem,
                                   AnimatedItem, TextBoxItem))]
        if any(it.isSelected() for it in hits):
            return                              # its own context menu handles it
        menu = QMenu(self)
        # Ping is always available — attention markers matter most on a locked
        # map mid-encounter.
        a_ping = menu.addAction("Ping Here")
        # Text boxes / imports are 'objects' — only while Lock Assets is off.
        lock = getattr(mw, "lockmap_checkBox", None)
        a_text = a_import = None
        if not (lock is not None and lock.isChecked()):
            menu.addSeparator()
            a_text = menu.addAction("Add Text Box")
            a_import = menu.addAction("Import File…")
        act = menu.exec(gpos)
        if act is None:
            return
        scene_pos = self.mapToScene(vpos)
        if act == a_ping and hasattr(mw, "add_ping"):
            mw.add_ping(scene_pos)
        elif act == a_text and hasattr(mw, "add_text_box"):
            mw.add_text_box(scene_pos)
        elif act == a_import and hasattr(mw, "import_file"):
            mw.import_file(scene_pos)

    def mouseMoveEvent(self, event):
        # 0) If we are currently panning (right OR middle button held), do that first
        if (self._panning and self._pan_start
                and (event.buttons() & (Qt.RightButton | Qt.MiddleButton))):
            delta = event.position() - self._pan_start
            if abs(delta.x()) + abs(delta.y()) > 2:
                self._pan_moved = True          # a real drag → suppress the context menu
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x()
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y()
            )
            event.accept()
            return

        # 1) Reveal tool: update preview; draw only with left-button held
        if getattr(self.window(), "_fog_reveal_mode", False):
            self.fog_preview_pos = self.mapToScene(event.position().toPoint())
            self.viewport().update()
            if event.buttons() & Qt.LeftButton:
                self.window()._reveal_at(self.fog_preview_pos)
            return

        # 1b) Hide tool: update preview; draw only with left-button held
        if getattr(self.window(), "_fog_hide_mode", False):
            self.fog_preview_pos = self.mapToScene(event.position().toPoint())
            self.viewport().update()
            if event.buttons() & Qt.LeftButton:
                self.window()._hide_at(self.fog_preview_pos)
            return

        # 2) Default behaviour
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        # 0) Always allow panning (right OR middle button), even when a tool is
        # active. Middle-drag is the standard VTT/CAD pan; it reuses the exact
        # right-drag path below (the context menu on release stays right-only).
        if event.button() in (Qt.RightButton, Qt.MiddleButton):
            self._panning = True
            self._pan_start = event.position()
            self._pan_moved = False        # becomes True only if the drag moves
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        # 0b) Alt+Left-click drops a ping — modeless, bypasses any active tool and
        # never disturbs selection. GM view only (the player canvas is locked).
        if (event.button() == Qt.LeftButton
                and (event.modifiers() & Qt.AltModifier)
                and not getattr(self, "is_player_view", False)):
            mw = self.window()
            if hasattr(mw, "add_ping"):
                mw.add_ping(self.mapToScene(event.position().toPoint()))
            event.accept()
            return

        # 1) Reveal tool: preview + reveal on left-click
        if getattr(self.window(), "_fog_reveal_mode", False):
            pos = self.mapToScene(event.position().toPoint())
            self.fog_preview_pos = pos
            self.viewport().update()
            if event.button() == Qt.LeftButton:
                self.window()._reveal_at(pos)
            return

        # 1b) Hide tool: preview + hide on left-click
        if getattr(self.window(), "_fog_hide_mode", False):
            pos = self.mapToScene(event.position().toPoint())
            self.fog_preview_pos = pos
            self.viewport().update()
            if event.button() == Qt.LeftButton:
                self.window()._hide_at(pos)
            return

        # 2) Fallback
        super().mousePressEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.setDropAction(Qt.CopyAction)         # never let the tree Move-delete
            event.accept()
        else:
            event.ignore()

    def dropEvent(self, event):
        # Add the dropped asset to the current map at the cursor position. Handles
        # both external file drops and drags from the Assets browser (library files
        # are detected as already-imported, so no duplication). If no map is open,
        # _place_asset auto-creates one. Item creation/import lives in _place_asset
        # so the drop and the Assets-tab double-click share one code path.
        mw = self.window()
        scene_pos = self.mapToScene(event.position().toPoint())
        for url in event.mimeData().urls():
            if mw._place_asset(url.toLocalFile(), scene_pos):
                # Copy, not the proposed Move — an asset dragged from the tree must
                # stay in the library (Move would trigger the tree to delete it).
                event.setDropAction(Qt.CopyAction)
                event.accept()
                return
        event.ignore()

    def mouseReleaseEvent(self, event):
        if event.button() in (Qt.RightButton, Qt.MiddleButton):
            # Clear pan state FIRST so a menu shown below can't leave us stuck.
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            was_click = not self._pan_moved     # a click (no drag) = a menu request
            self._pan_moved = False
            event.accept()
            # Context menu is RIGHT-click only — middle-release never opens it.
            if event.button() == Qt.RightButton and was_click:
                self._maybe_show_canvas_menu(event.position().toPoint(),
                                             event.globalPosition().toPoint())
        else:
            # End of a reveal/hide stroke: collapse the accumulated stamps so the
            # path stays cheap to union/subtract and small to serialize.
            if event.button() == Qt.LeftButton and (
                getattr(self.window(), "_fog_reveal_mode", False)
                or getattr(self.window(), "_fog_hide_mode", False)
            ):
                self.commit_fog_stroke()
                if hasattr(self.window(), "_push_fog_to_player"):
                    self.window()._push_fog_to_player()
            super().mouseReleaseEvent(event)

    def setPixmap(self, pixmap):
        self.prepareGeometryChange()
        super().setPixmap(pixmap)
        
        views = self.scene().views()
        if views:
            views[0].viewport().update()

    def create_grid(self):
        # only remove the old grid if it still lives in the scene
        try:
            # QGraphicsItem.scene() returns None if the item has been deleted
            if hasattr(self, "grid_group") and self.grid_group.scene() is not None:
                self.scene.removeItem(self.grid_group)
        except RuntimeError:
            # in case the C++ object was already freed, just ignore
            pass

        # now build a fresh group
        self.grid_group = QGraphicsItemGroup()
        # make this grid group unselectable and unmoveable
        self.grid_group.setFlag(QGraphicsItem.ItemIsSelectable, False)
        self.grid_group.setFlag(QGraphicsItem.ItemIsMovable,    False)
        self.grid_group.setAcceptedMouseButtons(Qt.NoButton)

        # Build grid lines covering the current canvas_rect. Lines fall on
        # multiples of the spacing so squares stay aligned to the origin as the
        # canvas grows (update_extent snaps canvas_rect to the spacing).
        spacing = self.grid_size
        r = self.canvas_rect
        left, top, right, bottom = r.left(), r.top(), r.right(), r.bottom()

        # Vertical lines
        x = math.floor(left / spacing) * spacing
        while x <= right:
            line = QGraphicsLineItem(x, top, x, bottom)
            line.setPen(QPen(self.grid_color))
            line.setFlag(QGraphicsItem.ItemIsSelectable, False)
            line.setFlag(QGraphicsItem.ItemIsMovable,    False)
            line.setAcceptedMouseButtons(Qt.NoButton)
            self.grid_group.addToGroup(line)
            x += spacing

        # Horizontal lines
        y = math.floor(top / spacing) * spacing
        while y <= bottom:
            line = QGraphicsLineItem(left, y, right, y)
            line.setPen(QPen(self.grid_color))
            line.setFlag(QGraphicsItem.ItemIsSelectable, False)
            line.setFlag(QGraphicsItem.ItemIsMovable,    False)
            line.setAcceptedMouseButtons(Qt.NoButton)
            self.grid_group.addToGroup(line)
            y += spacing

        self.grid_group.setZValue(0)  # Ensure it's above the image
        self.scene.addItem(self.grid_group)

        # The grid defines the canvas; the scene rect follows it exactly.
        self.setSceneRect(self.canvas_rect)

    # ── Canvas auto-size (grow + lazy shrink) ──────────────────────────────
    BASE_EXTENT    = 5000   # minimum canvas size, centered on origin
    CONTENT_MARGIN = 360    # ~5 squares of buffer kept around placed maps
    SHRINK_SLACK   = 720    # only reclaim a side once it exceeds the target by
                            # more than this (~10 squares) — avoids grow/shrink
                            # thrash while editing

    def _base_rect(self):
        h = self.BASE_EXTENT / 2.0
        return QRectF(-h, -h, self.BASE_EXTENT, self.BASE_EXTENT)

    def _content_rect(self):
        """Bounding rect (scene coords) of the placed map items only."""
        rect = QRectF()
        for it in self.scene.items():
            if isinstance(it, (InteractivePixmapItem, InteractiveVideoItem,
                               AnimatedItem, TextBoxItem)):
                rect = rect.united(it.sceneBoundingRect())
        return rect

    def _snap_rect_out(self, rect):
        """Expand rect outward so its edges land on grid-spacing multiples."""
        sp = self.grid_size
        left   = math.floor(rect.left()   / sp) * sp
        top    = math.floor(rect.top()    / sp) * sp
        right  = math.ceil (rect.right()  / sp) * sp
        bottom = math.ceil (rect.bottom() / sp) * sp
        return QRectF(left, top, right - left, bottom - top)

    def reset_extent(self):
        """Shrink back to the base size (used when starting/loading a fresh map)."""
        self.canvas_rect = self._snap_rect_out(self._base_rect())
        self.create_grid()

    def update_extent(self):
        """Resize the canvas to fit placed maps: grow eagerly, shrink lazily.

        Growing covers maps + CONTENT_MARGIN immediately. Shrinking pulls a side
        in only once it exceeds the target by more than SHRINK_SLACK, so normal
        editing doesn't thrash. Never goes below the base size, and only ever
        trims empty space — the map bounding rect (which the meaningful fog
        reveals live inside) is always kept. Returns True if the canvas changed
        (grid rebuilt)."""
        target = self._base_rect()
        content = self._content_rect()
        if content.isValid():
            target = target.united(content.adjusted(
                -self.CONTENT_MARGIN, -self.CONTENT_MARGIN,
                 self.CONTENT_MARGIN,  self.CONTENT_MARGIN))
        target = self._snap_rect_out(target)

        cur = self.canvas_rect
        if target == cur:
            return False

        if not cur.contains(target):
            # need more room somewhere → grow to cover target (no shrink)
            new = cur.united(target)
        else:
            # cur already covers target; reclaim only sides with > SLACK to spare
            l, t, r, b = cur.left(), cur.top(), cur.right(), cur.bottom()
            if target.left()   - cur.left()      > self.SHRINK_SLACK: l = target.left()
            if cur.right()     - target.right()  > self.SHRINK_SLACK: r = target.right()
            if target.top()    - cur.top()       > self.SHRINK_SLACK: t = target.top()
            if cur.bottom()    - target.bottom() > self.SHRINK_SLACK: b = target.bottom()
            new = QRectF(l, t, r - l, b - t)

        new = self._snap_rect_out(new)
        if new != cur:
            self.canvas_rect = new
            self.create_grid()
            return True
        return False


    def viewportEvent(self, event):
        # macOS trackpad pinch-to-zoom arrives as a QNativeGestureEvent on the
        # viewport (not as Ctrl+wheel), so drive the SAME zoom_slider the wheelEvent
        # uses. These events never fire on Windows/Linux, so this is a pure
        # pass-through there; two-finger scroll stays a plain wheelEvent → pan.
        if event.type() == QEvent.Type.NativeGesture \
                and event.gestureType() == Qt.ZoomNativeGesture:
            mw = self.window()
            # Never zoom the locked player canvas; guard against a window with no
            # slider (the player window). Fall through to default handling.
            if not getattr(self, "is_player_view", False) and hasattr(mw, "zoom_slider"):
                step = int(round(event.value() * 100))     # value() = incremental scale delta
                if step == 0:                              # keep tiny pinches responsive
                    step = 1 if event.value() > 0 else -1
                cur = mw.zoom_slider.value()
                new_val = max(mw.zoom_slider.minimum(),
                              min(mw.zoom_slider.maximum(), cur + step))
                if new_val != cur:
                    mw.zoom_slider.setValue(new_val)
                event.accept()
                return True
        return super().viewportEvent(event)

    def wheelEvent(self, event):
        if event.modifiers() == Qt.ControlModifier:
            # update the MainWindow’s slider
            mw = self.window()
            delta = event.angleDelta().y()
            step  = mw.zoom_slider.singleStep()
            if delta > 0:
                val = min(mw.zoom_slider.value() + step, mw.zoom_slider.maximum())
            else:
                val = max(mw.zoom_slider.value() - step, mw.zoom_slider.minimum())
            mw.zoom_slider.setValue(val)

            event.accept()
        else:
            super().wheelEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        # No need to draw the grid here manually, as it's already in the scene.

class PlayerWindow(QMainWindow):
    # Emitted when the user closes the window themselves (X / taskbar / Alt+F4).
    # Minimizing does NOT fire closeEvent, so it intentionally doesn't emit this.
    closed = Signal()

    def closeEvent(self, event):
        self.closed.emit()
        super().closeEvent(event)

    def __init__(self, shared_scene=None):
        super().__init__()
        self.setWindowTitle("Player Window")
        
        self.setWindowIcon(QIcon(PVICON_PATH))

        self.canvas_view = Canvas()
        self.canvas_view.is_player_view = True   # items hide player-hidden content here
        self.canvas_view.setDragMode(QGraphicsView.NoDrag)   # no marquee-select for players

        # 1) Disable built-in scrollbars
        self.canvas_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.canvas_view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # 2) Patch out your right-button panning:
        #    simply restore the base behavior so no panning ever happens.
        self.canvas_view._panning = False
        self.canvas_view._pan_start = None

        # (Optional) disable zooming via wheel, if you want absolutely no movement:
        def ignore_wheel(ev):
            ev.ignore()
        self.canvas_view.wheelEvent = ignore_wheel

        if shared_scene:
            self.canvas_view.setScene(shared_scene)

        self.setCentralWidget(self.canvas_view)

class MapIconProvider(QFileIconProvider):
    # Cache the custom icons once — icon() is called per entry while the tree
    # populates, and rebuilding a QIcon from disk each time slows loading.
    _folder_icon = None
    _map_icon = None

    def icon(self, fileInfo):
        # folders → custom folder icon
        if fileInfo.isDir():
            if MapIconProvider._folder_icon is None:
                MapIconProvider._folder_icon = QIcon(FOLDERICON_PATH)
            return MapIconProvider._folder_icon
        elif fileInfo.suffix().lower() == "json":
            if MapIconProvider._map_icon is None:
                MapIconProvider._map_icon = QIcon(MAPICON_PATH)
            return MapIconProvider._map_icon
        # else → fallback
        return super().icon(fileInfo)

class AssetFileModel(QFileSystemModel):
    """Asset-library model that supports internal drag-move while routing every
    move through MainWindow so that map references get rewritten (see
    MainWindow._move_assets_into). Files can still be dragged OUT to the canvas
    (text/uri-list) — that drag is initiated by the view and is unaffected by the
    drop override here, which only handles drops landing back on this tree."""
    def __init__(self, window, parent=None):
        super().__init__(parent)
        self._window = window
        # Writable so flags can advertise drag/drop; the view keeps
        # NoEditTriggers so inline rename never bypasses our ref-rewriting path.
        self.setReadOnly(False)

    def supportedDropActions(self):
        # Copy as well as Move: dragging an asset OUT to the canvas resolves as a
        # Copy (the canvas forces it), so the view never removes the library file.
        # Internal tree drops resolve as Move (the view's default drop action).
        return Qt.CopyAction | Qt.MoveAction

    def flags(self, index: QModelIndex):
        f = super().flags(index)
        if not index.isValid():
            return f | Qt.ItemIsDropEnabled            # drop onto the root
        f |= Qt.ItemIsDragEnabled
        if self.isDir(index):
            f |= Qt.ItemIsDropEnabled                  # only folders accept drops
        return f

    def canDropMimeData(self, data, action, row, col, parent: QModelIndex):
        # Any local-file drag is acceptable; dropping onto a file is treated as a
        # drop into its containing folder (resolved in dropMimeData).
        return data.hasUrls()

    def dropMimeData(self, data: QMimeData, action, row, col, parent: QModelIndex):
        if not data.hasUrls():
            return False
        dest = self.filePath(parent) if parent.isValid() else self.rootPath()
        if not os.path.isdir(dest):
            dest = os.path.dirname(dest)
        srcs = [u.toLocalFile() for u in data.urls() if u.isLocalFile()]
        # Route through MainWindow: it moves each file and rewrites map refs.
        return self._window._move_assets_into(srcs, dest)


class LayersTreeView(QTreeView):
    """Tree of map items grouped by layer. After an internal drag-move, defers to
    MainWindow._commit_layer_reorder() (on the next event loop turn, once the model
    has finished mutating) to recompute z from the new order and reject any
    cross-layer move."""
    def dropEvent(self, event):
        super().dropEvent(event)
        mw = self.window()
        if hasattr(mw, "_commit_layer_reorder"):
            QTimer.singleShot(0, mw._commit_layer_reorder)


class MapFileModel(QFileSystemModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        # only .json files
        self.setNameFilters(["*.json"])
        self.setNameFilterDisables(False)
        # writable so the tree can rename files/folders inline (Qt::ItemIsEditable)
        self.setReadOnly(False)

    def supportedDropActions(self):
        # tell Qt we only support Move drops
        return Qt.MoveAction

    def flags(self, index: QModelIndex):
        f = super().flags(index)
        # allow drag on files & drop in folders / root
        if index.isValid():
            return f | Qt.ItemIsDragEnabled | Qt.ItemIsDropEnabled
        else:
            return f | Qt.ItemIsDropEnabled

    def dropMimeData(self, data: QMimeData, action, row, col, parent: QModelIndex):
        if action == Qt.MoveAction and data.hasUrls():
            dest = self.filePath(parent)
            for url in data.urls():
                src = url.toLocalFile()
                base, ext = os.path.splitext(src)
                dst = os.path.join(dest, os.path.basename(src))
                shutil.move(src, dst)
                # if it’s a .json map, also move its _fog.png
                if ext.lower() == ".json":
                    fog = base + "_fog.png"
                    if os.path.exists(fog):
                        shutil.move(fog, os.path.join(dest, os.path.basename(fog)))
            return True
        return super().dropMimeData(data, action, row, col, parent)

class MapProxyModel(QSortFilterProxyModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.current_file = None

    def setCurrentFile(self, filepath: str):
        """Call this whenever you open or save a map."""
        self.current_file = os.path.abspath(filepath) if filepath else None
        self.invalidateFilter()
        self.layoutChanged.emit()

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        # only customize column 0
        if index.column() == 0 and self.current_file:
            src = self.mapToSource(index)
            path = self.sourceModel().filePath(src)
            if os.path.abspath(path) == self.current_file:

                # 1) Bold font
                if role == Qt.FontRole:
                    font = QFont()
                    font.setBold(True)
                    return font

                # 2) Decoration: overlay checkmark on top of base icon
                if role == Qt.DecorationRole:
                    # a) get the “base” icon for this file
                    #    (QFileSystemModel provides fileIcon())
                    base_icon = self.sourceModel().fileIcon(src)
                    # choose the icon size you use in your view (e.g. 16x16)
                    sz = QSize(16, 16)
                    base_pix = base_icon.pixmap(sz)

                    # b) get the checkmark pixmap (half the size, bottom-right)
                    chk_icon = QApplication.style().standardIcon(QStyle.SP_DialogApplyButton)
                    chk_pix  = chk_icon.pixmap(sz)
                    painter = QPainter(base_pix)
                    painter.drawPixmap(0, 0, chk_pix)
                    painter.end()

                    return QIcon(base_pix)

        # … your existing DisplayRole “.json” strip and fallback …
        # EditRole shares the strip so the inline rename editor shows just the
        # name (no ".json"); setData re-appends the extension on commit.
        if role in (Qt.DisplayRole, Qt.EditRole) and index.column() == 0:
            name = super().data(index, role)
            if isinstance(name, str) and name.lower().endswith(".json"):
                return name[:-5]
            return name

        return super().data(index, role)

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole):
        # Inline rename. The editor edited the extension-less name, so re-append
        # ".json" for map files (folders keep their typed name) before handing the
        # rename to QFileSystemModel.
        if index.column() == 0 and role == Qt.EditRole:
            src      = self.mapToSource(index)
            old_path = self.sourceModel().filePath(src)
            new      = str(value).strip()
            if not new:
                return False
            is_dir = os.path.isdir(old_path)
            if not is_dir and not new.lower().endswith(".json"):
                new += ".json"
            if new == os.path.basename(old_path):
                return False
            # The actual rename (and companion-fog relocation / open-map path sync,
            # via QFileSystemModel.fileRenamed) is handled by the source model.
            return self.sourceModel().setData(src, new, role)
        return super().setData(index, value, role)

# --- Diagnostics / crash logging -------------------------------------------
# The windowed (console=False) PyInstaller build has no console, so
# sys.stdout/sys.stderr are None. That has three consequences we route to log
# files under <user data>/logs/ so an installed-app crash leaves a trail:
#   * faulthandler.enable() raises "sys.stderr is None"  -> crash.log (native/Qt faults)
#   * uncaught Python exceptions die with no traceback    -> app.log (via excepthook)
#   * stray diagnostic print()s are silently dropped      -> app.log (stdout/stderr capture)
# On dev/console runs (stderr present) stdout/stderr are left untouched and
# faulthandler dumps to the console as before. All of this is best-effort:
# diagnostics must never block startup.
_LOG_HANDLES = []  # keep log files alive for the process lifetime

def _trim_log(path: Path, max_bytes: int = 1_000_000) -> None:
    """Roll a log over to a single '.1' backup once it grows past max_bytes, so
    logs can't accumulate without bound across launches (checked at startup)."""
    try:
        if path.exists() and path.stat().st_size > max_bytes:
            backup = path.with_name(path.name + ".1")
            backup.unlink(missing_ok=True)
            path.replace(backup)
    except Exception:
        pass

def _setup_diagnostics() -> None:
    """Wire up crash/exception logging; see the module note above. No-op-safe."""
    import faulthandler
    have_console = sys.stderr is not None

    log_dir = None
    try:
        log_dir = user_data_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _trim_log(log_dir / "crash.log")
        _trim_log(log_dir / "app.log")
    except Exception:
        log_dir = None

    # Native/Qt faults: console stderr on dev runs, else crash.log. faulthandler
    # writes via the raw fd at fault time, so a line-buffered text file is fine.
    if have_console:
        faulthandler.enable()
    elif log_dir is not None:
        try:
            f = open(log_dir / "crash.log", "a", buffering=1, encoding="utf-8")
            _LOG_HANDLES.append(f)
            faulthandler.enable(file=f)
        except Exception:
            pass

    # No console: redirect print()s and exception tracebacks to app.log instead
    # of the void. Pointing sys.stderr at the file is enough for the default
    # sys.excepthook to capture otherwise-invisible uncaught exceptions there
    # (in the windowed build those just exit 1 with no output) — no custom hook
    # needed, which also avoids logging each traceback twice.
    if not have_console and log_dir is not None:
        try:
            app_log = open(log_dir / "app.log", "a", buffering=1, encoding="utf-8")
            _LOG_HANDLES.append(app_log)
            sys.stdout = app_log
            sys.stderr = app_log
        except Exception:
            pass

    # Route the app logger to wherever stderr now points (console on dev,
    # app.log in the windowed build). Done after the redirect above so the
    # handler binds to the final stream.
    try:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
            stream=sys.stderr,
        )
    except Exception:
        pass

class SplashScreen(QWidget):
    """Frameless startup splash: enlarged app icon, a title, and a progress bar.

    Startup (chiefly the FFmpeg multimedia backend init) blocks the GUI thread,
    so the bar can't animate *during* those calls — `advance_to()` tweens the bar
    smoothly between the checkpoints that main() sets, with a small per-step pause
    so the motion reads as a real loading bar (i.e. a faked fill)."""

    def __init__(self, icon_path, title=" Arcane Atlas"):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.SplashScreen)
        self.setObjectName("splash")
        self.setStyleSheet(
            "#splash{background:#1e2128;border:1px solid #3a3f4b;border-radius:10px;}"
            "QLabel{color:#e8e8e8;background:transparent;}"
            "QProgressBar{background:#2b2f38;border:1px solid #3a3f4b;"
            "border-radius:7px;height:14px;}"
            "QProgressBar::chunk{background:#6e56a9;border-radius:6px;}")  # arcana purple
        lay = QVBoxLayout(self)
        lay.setContentsMargins(48, 40, 48, 30)
        lay.setSpacing(20)

        icon = QLabel(); icon.setAlignment(Qt.AlignCenter)
        pm = QPixmap(icon_path)
        if not pm.isNull():
            icon.setPixmap(pm.scaled(220, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        lay.addWidget(icon)

        title_lbl = QLabel(title); title_lbl.setAlignment(Qt.AlignCenter)
        tf = title_lbl.font(); tf.setPointSize(24); tf.setBold(True); title_lbl.setFont(tf)
        title_lbl.setStyleSheet("color:#6e56a9;")   # arcana purple
        lay.addWidget(title_lbl)

        self.bar = QProgressBar()
        self.bar.setRange(0, 100); self.bar.setValue(0); self.bar.setTextVisible(False)
        lay.addWidget(self.bar)

        self._value = 0.0
        self.setMinimumWidth(380)
        self.adjustSize()
        scr = QApplication.primaryScreen()
        if scr is not None:
            self.move(scr.availableGeometry().center() - self.rect().center())

    def _set_bar(self):
        self.bar.setValue(int(round(self._value)))

    def ensure_visible(self):
        """Pump the event loop until the splash is actually mapped + painted, so
        it's on screen before the caller starts any blocking work. `show()` alone
        only queues the paint — on a real compositor the first paint needs a few
        event-loop cycles, which is why the splash otherwise flashes at the end."""
        self.raise_()
        self._value = max(self._value, 3.0)
        self._set_bar()
        handle = self.windowHandle()
        for _ in range(60):                    # bounded (~≤600ms); usually far less
            QApplication.processEvents(QEventLoop.AllEvents, 10)
            if handle is not None and handle.isExposed():
                break
        self.repaint()                          # force a synchronous paint

    def advance_to(self, target, pace_ms=6):
        """Smoothly tween the bar up to `target` (0-100), never backwards. The
        per-step `processEvents` + `QThread.msleep` makes the fill visible. Use it
        only for non-blocking steps — nothing animates during a blocking call."""
        target = max(self._value, min(100.0, float(target)))
        while self._value < target - 0.5:
            self._value = min(target, self._value + 1)
            self._set_bar()
            QApplication.processEvents()       # repaint the bar
            if pace_ms:
                QThread.msleep(pace_ms)         # brief pause so the fill is visible
        self._value = target
        self._set_bar()
        QApplication.processEvents()

def main() -> int:
    _setup_diagnostics()

    app = QApplication(sys.argv)
    # Large map images can decode past QImage's default 256 MB load limit and
    # would otherwise be silently rejected on drop/load. 0 = no limit.
    QImageReader.setAllocationLimit(0)

    splash = SplashScreen(ICON_PATH)
    splash.show()
    splash.ensure_visible()     # paint the splash BEFORE any blocking work

    # Build the app and warm the FFmpeg backend on the main thread (the ~2s global
    # plugin load). It can't animate the bar during that call — a worker thread
    # can't help either, because the load holds the Python GIL and starves the
    # main thread's timer (measured: ~2 ticks in 2s). So we fill the bar to ~80
    # first (visible, smooth), the splash stays painted while it holds there, then
    # we finish to 100. The splash is on screen the whole time.
    splash.advance_to(45, pace_ms=6)
    win = MainWindow()          # wires up Ui_MainWindow, loads settings, builds tabs
    splash.advance_to(80, pace_ms=6)

    win._ensure_preview_player()    # the ~2s FFmpeg load; bar holds at 80, splash visible
    splash.advance_to(96, pace_ms=4)

    win.show()
    splash.advance_to(100, pace_ms=4)
    splash.close()
    return app.exec()

if __name__ == "__main__":
    raise SystemExit(main())