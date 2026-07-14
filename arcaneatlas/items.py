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

"""Graphics items for the shared QGraphicsScene used by both windows.

Movable/scalable/rotatable map & token items, plus the player-view framing rect
and the welcome card. These reach the owning MainWindow at runtime through
view.window() (never by import), so this module has no dependency on main.py or
the Canvas/MainWindow classes — which keeps the import graph acyclic.
"""
from PySide6.QtCore import (QUrl, Qt, QRectF, QSize, QSizeF, QPointF,
    QVariantAnimation, QEasingCurve)
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetrics, QIcon, QImageReader,
    QMovie, QPainter, QPainterPath, QPen, QPixmap, QTextOption, QTransform)
from PySide6.QtWidgets import (QGraphicsItem, QGraphicsItemGroup, QGraphicsLineItem,
    QGraphicsObject, QGraphicsPixmapItem, QGraphicsRectItem, QMenu, QStyle)
from PySide6.QtMultimedia import QMediaPlayer

from arcaneatlas.tokenizer import BORDER_COLORS
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
import logging

log = logging.getLogger("arcaneatlas.items")


def _painting_in_player_view(widget):
    """True when this paint pass is for the PlayerWindow's canvas (tagged
    `is_player_view` by MainWindow), not the GM view. Lets a map item hide itself
    from the player while staying visible (with a marker) to the GM — both windows
    share one scene, so visibility is decided per paint, not per item."""
    view = widget.parent() if widget else None
    return bool(getattr(view, "is_player_view", False))


def _paint_hidden_marker(item, painter):
    """GM-only overlay marking an item that's hidden from the player view: a red
    wash + thick diagonal stripes + bold border. The item's content still shows
    through, so the GM sees what it is and that it won't reach the players.

    Stripes are drawn by hand in SCENE units (not a QBrush hatch pattern, whose
    1px cosmetic lines vanish when the view is zoomed out) so they stay clearly
    visible at any zoom."""
    r = item.boundingRect()
    STRIPE_W = 7        # stripe thickness, scene px (72px = 1 inch)
    STRIPE_GAP = 34     # spacing between stripes, scene px
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    # translucent wash so the marking reads even where stripes are sparse
    painter.fillRect(r, QColor(255, 45, 45, 55))
    # diagonal stripes, clipped to the item
    painter.setClipRect(r)
    painter.setPen(QPen(QColor(255, 45, 45, 150), STRIPE_W))
    x = r.left() - r.height()
    while x < r.right():
        painter.drawLine(QPointF(x, r.bottom()), QPointF(x + r.height(), r.top()))
        x += STRIPE_GAP
    painter.setClipping(False)
    # bold border
    painter.setPen(QPen(QColor(255, 45, 45, 220), STRIPE_W))
    painter.setBrush(Qt.NoBrush)
    painter.drawRect(r)
    painter.restore()


def _paint_player_control_ring(item, painter):
    """GM-only BLUE dashed ring marking a token that remote web players may drag.
    Drawn at the SAME ellipse/width as the gold party ring and on TOP of it, with
    short dashes and wide gaps so most of the gold shows between them. Scene units."""
    r = item.boundingRect()
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    pen = QPen(QColor(80, 180, 255), 5)             # blue, same width as the gold ring
    pen.setCapStyle(Qt.FlatCap)                     # don't let square caps lengthen the dashes
    pen.setDashPattern([2, 4])                      # short dash, wide gap (in pen-width units)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(r.adjusted(2.5, 2.5, -2.5, -2.5))   # exactly over the gold ring
    painter.restore()


def _paint_party_ring(item, painter):
    """Gold ring marking a token whose asset is in the saved party — a
    character-identity marker, so it's drawn in BOTH the GM and player views
    (unlike the GM-only player-control ring). Scene units; hugs the token edge."""
    r = item.boundingRect()
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(QPen(QColor(255, 200, 60), 5))   # gold, scene-px width
    painter.setBrush(Qt.NoBrush)
    painter.drawEllipse(r.adjusted(2.5, 2.5, -2.5, -2.5))
    painter.restore()


def _toggle_player_visibility(item, view):
    """Flip player visibility, repaint both views, refresh the Layers list (which
    tags hidden rows), and mark the map dirty. Shared by all item context menus.

    If the clicked item is part of a multi-selection, the SAME new state (derived
    from the clicked item) is applied to every selected media item at once."""
    new_state = not getattr(item, "visible_to_player", True)
    targets = [item]
    scene = item.scene()
    if scene is not None and item.isSelected():
        media = (InteractivePixmapItem, InteractiveVideoItem, AnimatedItem, TextBoxItem)
        sel = [it for it in scene.selectedItems() if isinstance(it, media)]
        if len(sel) > 1:
            targets = sel
    for it in targets:
        it.visible_to_player = new_state
        it.update()                             # GM marker + player hide/show
    mw = view.window()
    pw = getattr(mw, "player_window", None)
    if pw is not None:
        pw.canvas_view.viewport().update()
    if hasattr(mw, "update_layers_list"):
        mw.update_layers_list()
    _mark_item_dirty(item)


def grow_canvas_for(item):
    """Ask the owning MainWindow to grow the canvas to fit `item` (after a
    drop/move/resize). Safe no-op if the item isn't in a GM scene/view yet."""
    sc = item.scene()
    if not sc:
        return
    for v in sc.views():
        mw = v.window()
        if hasattr(mw, "refresh_canvas_extent"):
            mw.refresh_canvas_extent()
            return

def _owner_window(item):
    """Return the MainWindow that owns `item`'s scene, or None."""
    sc = item.scene()
    if not sc:
        return None
    for v in sc.views():
        mw = v.window()
        if hasattr(mw, "mark_dirty"):
            return mw
    return None

def _mark_item_dirty(item):
    """Flag the owning map as having unsaved changes (after an item edit)."""
    mw = _owner_window(item)
    if mw:
        mw.mark_dirty()

def _token_targets(item):
    """The set of tokens a per-token context action applies to: every selected
    token if `item` is part of a multi-selection, else just `item`."""
    scene = item.scene()
    if scene is not None and item.isSelected():
        sel = [it for it in scene.selectedItems()
               if isinstance(it, InteractivePixmapItem) and getattr(it, "is_token", False)]
        if len(sel) > 1:
            return sel
    return [item]

class InteractivePixmapItem(QGraphicsPixmapItem):
    HANDLE_SIZE = 30
    visible_to_player = True        # False → hidden in the player view (GM still sees it)
    is_token = False                # True → round VTT token: fixed size, re-edited via the tokenizer
    token_color_override = None     # per-instance token border colour (hex), None = use library PNG as-is
    token_id = None                 # stable per-item id (uuid hex) — assigned lazily, used by web sharing
    player_controllable = False     # True → remote web clients may drag this token
    in_party = False                # True → this token's asset is in the saved party (gold ring); kept in sync by MainWindow._refresh_party_token_rings

    def __init__(self, pixmap):
        super().__init__(pixmap)
        # selectable and movable; geometry-change notifications drive the
        # token grid-snap in itemChange().
        self.setFlags(
            QGraphicsItem.ItemIsSelectable |
            QGraphicsItem.ItemIsMovable |
            QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        # smooth rendering for the live scale-preview during resize
        self.setTransformationMode(Qt.SmoothTransformation)

        self.active_handle = None
        self.original_pixmap = pixmap
        self.original_rect = QRectF()
        self.anchor_scene = QPointF()

    def itemChange(self, change, value):
        # Tokens always snap to the canvas grid. The token pixmap is a whole
        # number of inches square (N×72px), so snapping its top-left origin to a
        # grid-spacing multiple lands it exactly on N cells (the grid lines fall
        # on multiples of grid_size in scene coords — see Canvas.create_grid).
        if change == QGraphicsItem.ItemPositionChange and self.is_token:
            grid = 72
            scene = self.scene()
            if scene is not None and scene.views():
                grid = getattr(scene.views()[0], "grid_size", grid) or grid
            return QPointF(round(value.x() / grid) * grid,
                           round(value.y() / grid) * grid)
        return super().itemChange(change, value)

    def contextMenuEvent(self, event):
        # only show menu if this item is selected
        if not self.isSelected():
            event.ignore()
            return


        # disable any stuck panning on the view
        view = self.scene().views()[0]
        view._panning = False
        view.viewport().setCursor(Qt.ArrowCursor)

        menu = QMenu()
        a_remove = menu.addAction("Remove")
        a_rotate = menu.addAction("Rotate 90°")
        a_top  = menu.addAction("Bring to Top")
        a_edit = menu.addAction("Edit Token…") if self.is_token else None
        # Per-instance token border colour (does NOT touch the shared library PNG).
        color_actions = {}
        if self.is_token:
            color_menu = menu.addMenu("Change Token Color")
            for hexc in BORDER_COLORS:
                act = color_menu.addAction(hexc)
                sw = QPixmap(16, 16); sw.fill(QColor(hexc))
                act.setIcon(QIcon(sw))
                color_actions[act] = hexc
        # Web sharing: let remote players drag this token (per-instance, persisted).
        a_pc = a_party = None
        if self.is_token:
            a_pc = menu.addAction("Allow Player Control")
            a_pc.setCheckable(True); a_pc.setChecked(self.player_controllable)
            mw = view.window()
            a_party = menu.addAction("Add to Party")
            a_party.setCheckable(True)
            a_party.setChecked(hasattr(mw, "is_in_party") and mw.is_in_party(self))
        a_vis = menu.addAction("Show to Players" if not self.visible_to_player
                               else "Hide from Players")
        action = menu.exec(event.screenPos())

        if action == a_vis:
            _toggle_player_visibility(self, view)

        if a_pc is not None and action == a_pc:
            new_val = a_pc.isChecked()
            for it in _token_targets(self):
                it.player_controllable = new_val
                it.update()                          # show/hide the GM player-control ring
            _mark_item_dirty(self)

        if a_party is not None and action == a_party:
            mw = view.window()
            if hasattr(mw, "set_party_membership"):
                mw.set_party_membership(_token_targets(self), a_party.isChecked())

        if action in color_actions:
            mw = view.window()
            if hasattr(mw, "recolor_tokens"):
                mw.recolor_tokens(_token_targets(self), color_actions[action])

        if a_edit is not None and action == a_edit:
            mw = view.window()
            if hasattr(mw, "edit_token"):
                mw.edit_token(self)

        if action == a_remove:
            # drop this item from the canvas, then reclaim any freed space
            mw = view.window()
            self.scene().removeItem(self)
            if hasattr(mw, "refresh_canvas_extent"):
                mw.refresh_canvas_extent()
            if hasattr(mw, "update_layers_list"):
                mw.update_layers_list()
            if hasattr(mw, "mark_dirty"):
                mw.mark_dirty()

        if action == a_rotate:
            t = QTransform().rotate(90)
            # keep the full-res source rotated so later resize bakes stay
            # correctly oriented and crisp
            self.original_pixmap = self.original_pixmap.transformed(t)
            # rotate what's actually displayed so the image keeps its current
            # size instead of snapping back to full resolution
            self.setPixmap(self.pixmap().transformed(t))
            _mark_item_dirty(self)

        if action == a_top:
            self.bring_to_top()
            # now refresh the Layers list in the MainWindow
            mw = self.scene().views()[0].window()
            mw.update_layers_list()
            _mark_item_dirty(self)

        
        # prevent further handling
        event.accept()

    def bring_to_top(self):
        # Raise within this item's own layer (backgrounds/objects/tokens) — never
        # above the layer stacked above it. MainWindow owns the layer rules.
        scene = self.scene()
        if not scene:
            return
        scene.views()[0].window().bring_item_to_layer_top(self)

    def paint(self, painter, option, widget=None):
        in_player = _painting_in_player_view(widget)
        if not self.visible_to_player and in_player:
            return                              # hidden from the players
        if in_player:                           # no built-in selection outline for players
            option.state &= ~QStyle.State_Selected
        super().paint(painter, option, widget)
        if not self.visible_to_player:
            _paint_hidden_marker(self, painter)  # GM-only "hidden" overlay
        if self.is_token and self.in_party:
            _paint_party_ring(self, painter)            # gold "party member" ring (both views)
        if self.is_token and self.player_controllable and not in_player:
            _paint_player_control_ring(self, painter)   # green dashed, GM-only, over the gold
        if self.isSelected() and not in_player:  # selection chrome is GM-only
            pen = QPen(QColor(0,255,0), 1, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(self.boundingRect())
            painter.setBrush(Qt.white)
            painter.setPen(Qt.black)
            for rect in self._handle_positions().values():
                painter.drawRect(rect)

    def hoverMoveEvent(self, event):
        # Force Items to StandDown if we are in Fog of War Reveal
        view = self.scene().views()[0]
        if getattr(view.window(), "_fog_reveal_mode", False):
            # force crosshair, skip all your resize/selection cursors
            view.viewport().setCursor(Qt.CrossCursor)
            return
        
        self.active_handle = None
        for name, rect in self._handle_positions().items():
            if rect.contains(event.pos()):
                self.active_handle = name
                # diagonal cursors
                if name in ("tl","br"):
                    self.setCursor(Qt.SizeFDiagCursor)
                else:
                    self.setCursor(Qt.SizeBDiagCursor)
                return
        # anywhere else: arrow (and becomes movable by default)
        self.setCursor(Qt.ArrowCursor)

    def _handle_positions(self):
        # Tokens have a fixed, tokenizer-controlled size → no resize handles.
        if self.is_token:
            return {}
        r = self.boundingRect()
        s = self.HANDLE_SIZE
        return {
            "tl": QRectF(r.topLeft().x(),       r.topLeft().y(),       s, s),
            "tr": QRectF(r.topRight().x() - s,  r.topRight().y(),      s, s),
            "bl": QRectF(r.bottomLeft().x(),    r.bottomLeft().y() - s,s, s),
            "br": QRectF(r.bottomRight().x() - s,r.bottomRight().y() - s, s, s),
        }

    def _opposite_corner(self, handle):
        opp_corner = {
            'tl': self.boundingRect().bottomRight(),
            'tr': self.boundingRect().bottomLeft(),
            'bl': self.boundingRect().topRight(),
            'br': self.boundingRect().topLeft(),
        }[handle]
        return self.mapToScene(opp_corner)

    def mousePressEvent(self, event):
        # remember where we started so release can tell if anything actually moved
        self._press_pos = self.pos()
        if self.active_handle:
            # begin a resize
            self.original_rect = self.boundingRect()

            # lock the *opposite* image corner in place…
            self.anchor_scene = self._opposite_corner(self.active_handle)

            # …and record how far you clicked *into* the dragged corner
            # (use the original_rect you saved above)
            corner = {
                'tl': self.original_rect.topLeft(),
                'tr': self.original_rect.topRight(),
                'bl': self.original_rect.bottomLeft(),
                'br': self.original_rect.bottomRight(),
            }[self.active_handle]
            corner_scene = self.mapToScene(corner)
            self.handle_offset = event.scenePos() - corner_scene

            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.active_handle:
            # compute new size preserving aspect ratio
            #delta = event.scenePos() - self.anchor_scene
            # pretend the user is dragging the actual image corner,
            # not the raw mouse‐pos inside the handle:
            pointer_corner = event.scenePos() - self.handle_offset
            delta          = pointer_corner - self.anchor_scene
            orig_w = self.original_pixmap.width()
            orig_h = self.original_pixmap.height()
            ratio = orig_w / orig_h

            # choose the dominant direction
            if abs(delta.x()) > abs(delta.y()) * ratio:
                new_w = abs(delta.x())
                new_h = new_w / ratio
            else:
                new_h = abs(delta.y())
                new_w = new_h * ratio

            # enforce minimum
            if new_w < 20 or new_h < 20:
                return

            # Live preview via a cheap item scale instead of resampling the
            # full-resolution pixmap every move (that resample is what made
            # image resize stutter; video just changes size). The crisp
            # smooth-scaled pixmap is baked once, on release.
            self.setScale(new_w / self.original_rect.width())

            # keep the opposite corner pinned (scale grows from the local origin)
            opp_local = {
                "tl": self.original_rect.bottomRight(),
                "tr": self.original_rect.bottomLeft(),
                "bl": self.original_rect.topRight(),
                "br": self.original_rect.topLeft(),
            }[self.active_handle]
            offset = self.anchor_scene - self.mapToScene(opp_local)
            self.moveBy(offset.x(), offset.y())

            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        # a resize is in progress if a handle was grabbed on press
        did_resize = self.active_handle is not None
        # bake the previewed scale into a crisp pixmap once the drag ends
        if self.active_handle and self.scale() != 1.0:
            final_w = self.original_rect.width()  * self.scale()
            final_h = self.original_rect.height() * self.scale()
            scaled = self.original_pixmap.scaled(
                final_w, final_h, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
            self.setScale(1.0)
            self.setPixmap(scaled)

            # re-pin the opposite corner now that the pixmap carries the size
            b = self.boundingRect()
            opp_local = {
                "tl": b.bottomRight(),
                "tr": b.bottomLeft(),
                "bl": b.topRight(),
                "br": b.topLeft(),
            }[self.active_handle]
            self.setPos(self.anchor_scene - opp_local)

        # clear handle on release
        self.active_handle = None
        super().mouseReleaseEvent(event)
        # Tokens never overlap — settle dropped token(s) onto free cells. A
        # multi-selection drags as a group but only the grabbed item gets the
        # release event, so resolve EVERY selected token (self first, to anchor it
        # where it was dropped when that cell is free).
        if self.is_token and self.scene() and self.scene().views():
            mw = self.scene().views()[0].window()
            if hasattr(mw, "_resolve_token_overlap"):
                sel = [it for it in self.scene().selectedItems()
                       if isinstance(it, InteractivePixmapItem) and getattr(it, "is_token", False)]
                for it in [self] + [s for s in sel if s is not self]:
                    mw._resolve_token_overlap(it)
        # a move or resize may have pushed the map past the edge — grow to fit
        grow_canvas_for(self)
        # flag unsaved changes only if the item was actually moved or resized
        if did_resize or self.pos() != getattr(self, "_press_pos", self.pos()):
            _mark_item_dirty(self)

class InteractiveVideoItem(QGraphicsVideoItem):
    HANDLE_SIZE = 30
    visible_to_player = True        # False → hidden in the player view (GM still sees it)

    def __init__(self, video_path):
        super().__init__()
        self._video_path = video_path
        self.player = QMediaPlayer()
        self.player.setVideoOutput(self)
        self.player.setSource(QUrl.fromLocalFile(video_path))

        # ← listen on the VIDEO ITEM, not the player
        self.nativeSizeChanged.connect(self._on_video_size)

        # Loop via the backend (same as the asset-preview player) instead of the
        # old positionChanged→setPosition(0) hack: that never re-issued play(), so
        # if the clip hit EndOfMedia (→ Stopped) before the near-end position tick
        # fired, the seek-to-0 wouldn't resume and the frame went blank. Backend
        # looping has no such race and far less signal churn.
        self.player.setLoops(QMediaPlayer.Infinite)

        # Surface problems (e.g. FFmpeg backend errors under several concurrent
        # decoders) so a blanked video leaves a trail in app.log instead of
        # silently disappearing.
        self.player.errorOccurred.connect(self._on_media_error)
        self.player.mediaStatusChanged.connect(self._on_media_status)

        self.player.play()

        # 2) flags so it’s movable & selectable
        self.setFlags(
            QGraphicsItem.ItemIsSelectable |
            QGraphicsItem.ItemIsMovable
        )
        self.setAcceptHoverEvents(True)

        # ── resizing fields ──
        self.active_handle = None
        self.opp_handle    = None
        self.original_size = QSizeF()
        self.anchor_scene  = QPointF()
        self.handle_offset = QPointF()

    def teardown_player(self):
        """Stop playback and detach the video sink before this item is removed
        or the scene is cleared. Without this, Qt's FFmpeg backend can deliver a
        decoded frame to an already-freed QGraphicsVideoItem sink → intermittent
        native crash with no Python traceback. Safe to call more than once."""
        try:
            self.player.stop()
            self.player.setVideoOutput(None)
        except (RuntimeError, AttributeError):
            pass

    def contextMenuEvent(self, event):
        # only show menu if this item is selected
        if not self.isSelected():
            event.ignore()
            return
        
        # disable any stuck panning on the view
        view = self.scene().views()[0]
        view._panning = False
        view.viewport().setCursor(Qt.ArrowCursor)

        menu = QMenu()
        a_remove = menu.addAction("Remove")
        a_rotate = menu.addAction("Rotate 90°")
        a_top = menu.addAction("Bring to Top")
        a_vis = menu.addAction("Show to Players" if not self.visible_to_player
                               else "Hide from Players")
        action = menu.exec(event.screenPos())

        if action == a_vis:
            _toggle_player_visibility(self, view)

        if action == a_remove:
            mw = view.window()
            self.teardown_player()      # stop decode before the sink is freed
            self.scene().removeItem(self)
            if hasattr(mw, "refresh_canvas_extent"):
                mw.refresh_canvas_extent()
            if hasattr(mw, "update_layers_list"):
                mw.update_layers_list()
            if hasattr(mw, "mark_dirty"):
                mw.mark_dirty()

        if action == a_rotate:
            # rotate the video item around its center
            self._set_origin_keep_pos(self.boundingRect().center())
            self.setRotation(self.rotation() + 90)
            _mark_item_dirty(self)

            # reset stuck pan on the view
            view = self.scene().views()[0]
            view._panning = False
            view.viewport().setCursor(Qt.ArrowCursor)

        if action == a_top:
            self.bring_to_top()
            # now refresh the Layers list in the MainWindow
            mw = self.scene().views()[0].window()
            mw.update_layers_list()
            _mark_item_dirty(self)

        # prevent further handling
        event.accept()

    def bring_to_top(self):
        # Raise within this item's own layer (backgrounds/objects/tokens) — never
        # above the layer stacked above it. MainWindow owns the layer rules.
        scene = self.scene()
        if not scene:
            return
        scene.views()[0].window().bring_item_to_layer_top(self)

    def _on_video_size(self, size):
        # QSize comes in as (w,h)
        if size.isEmpty():
            return
        # nativeSizeChanged can fire asynchronously after the item was removed
        # from the scene — bail rather than deref a None scene().
        sc = self.scene()
        if sc is None:
            return
        # 1) resize the item to match the video pixels
        self.setSize(QSizeF(size.width(), size.height()))
        self.update()   # repaint (so your border redraws snugly)

        # 2) grow the canvas (grid + fog) so this new size is covered
        views = sc.views()
        if views:
            mw = views[0].window()
            if hasattr(mw, "refresh_canvas_extent"):
                mw.refresh_canvas_extent()
    
    def _on_media_error(self, error, error_string=""):
        # Logged, not raised: a single failed decoder shouldn't take down editing.
        if error != QMediaPlayer.NoError:
            log.warning("Video playback error for %s: %s (%s)",
                        getattr(self, "_video_path", "?"), error_string, error)

    def _on_media_status(self, status):
        # With Infinite looping the player shouldn't reach EndOfMedia; if it does
        # (or otherwise stops) the frame would go blank — re-issue play() once to
        # recover and leave a trail.
        if status == QMediaPlayer.EndOfMedia:
            log.warning("Video %s hit EndOfMedia despite looping; restarting.",
                        getattr(self, "_video_path", "?"))
            try:
                self.player.setPosition(0)
                self.player.play()
            except (RuntimeError, AttributeError):
                pass

    def boundingRect(self):
        # use the video’s current size
        return QRectF(0, 0, self.size().width(), self.size().height())
    
    
    def _handle_rects(self):
        r = self.boundingRect()
        s = self.HANDLE_SIZE
        return {
            'tl': QRectF(r.topLeft(), QSizeF(s, s)),
            'tr': QRectF(r.topRight() - QPointF(s, 0), QSizeF(s, s)),
            'bl': QRectF(r.bottomLeft() - QPointF(0, s), QSizeF(s, s)),
            'br': QRectF(r.bottomRight() - QPointF(s, s), QSizeF(s, s)),
        }
    
    def paint(self, painter, option, widget):
        in_player = _painting_in_player_view(widget)
        if not self.visible_to_player and in_player:
            return                              # hidden from the players
        if in_player:                           # no built-in selection outline for players
            option.state &= ~QStyle.State_Selected
        # draw the video frame itself
        super().paint(painter, option, widget)
        if not self.visible_to_player:
            _paint_hidden_marker(self, painter)  # GM-only "hidden" overlay
        if self.isSelected() and not in_player:  # selection chrome is GM-only
            pen = QPen(Qt.green, 1, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(self.boundingRect())
            painter.setBrush(Qt.white)
            painter.setPen(Qt.black)
            for rect in self._handle_rects().values():
                painter.drawRect(rect)

    def hoverMoveEvent(self, event):
        # Force Items to StandDown if we are in Fog of War Reveal
        view = self.scene().views()[0]
        if getattr(view.window(), "_fog_reveal_mode", False):
            # force crosshair, skip all your resize/selection cursors
            view.viewport().setCursor(Qt.CrossCursor)
            return
        
        self.active_handle = None
        for name, rect in self._handle_rects().items():
            if rect.contains(event.pos()):
                self.active_handle = name
                # compute diagonal direction based on handle vs. center
                center = self.boundingRect().center()
                handle_center = rect.center()
                delta = handle_center - center
                # rotate into screen space so the cursor matches the handle's
                # on-screen diagonal, not its local one (the two swap at 90°)
                delta = QTransform().rotate(self.rotation()).map(delta)
                # if dx*dy>0, it's NW–SE (SizeF); else NE–SW (SizeBDiag)
                if delta.x() * delta.y() > 0:
                    cursor = Qt.SizeFDiagCursor
                else:
                    cursor = Qt.SizeBDiagCursor
                self.setCursor(cursor)
                return
        # outside handles: open-hand when selected, arrow otherwise
        self.setCursor(Qt.OpenHandCursor if self.isSelected() else Qt.ArrowCursor)
        self.setCursor(Qt.OpenHandCursor if self.isSelected() else Qt.ArrowCursor)

    # helper to get the four corners in local coordinates
    def _corner_point(self, corner):
        b = self.boundingRect()
        return {
            'tl': b.topLeft(),
            'tr': b.topRight(),
            'bl': b.bottomLeft(),
            'br': b.bottomRight(),
        }[corner]

    def _set_origin_keep_pos(self, new_origin):
        # Moving the transform origin while the item is rotated shifts it on
        # screen by (I - R)*delta, because Qt rotates around the origin but does
        # not adjust pos. Compensate so the item stays visually put — otherwise
        # the anchored corner appears to jump when you grab a resize handle.
        if self.transformOriginPoint() == new_origin:
            return
        before = self.mapToScene(new_origin)
        self.setTransformOriginPoint(new_origin)
        after = self.mapToScene(new_origin)
        self.moveBy(before.x() - after.x(), before.y() - after.y())

    def mousePressEvent(self, event):
        # remember start position so release can detect an actual move
        self._press_pos = self.pos()
        if event.button() == Qt.LeftButton and self.active_handle:
            # 1) lock pivot at the current center (so rotation stays consistent)
            self._set_origin_keep_pos(self.boundingRect().center())

            # 2) record which corner anchors throughout the drag
            self.opp_handle = {
                'tl': 'br', 'br': 'tl',
                'tr': 'bl', 'bl': 'tr'
            }[self.active_handle]

            # 3) record start state
            self.original_size = self.size()

            # 4) map the true opposite‐corner into scene coords
            local_opp        = self._corner_point(self.opp_handle)
            self.anchor_scene = self.mapToScene(local_opp)

            # 5) compute mouse offset into the exact clicked corner
            local_clicked     = self._corner_point(self.active_handle)
            clicked_scene     = self.mapToScene(local_clicked)
            self.handle_offset = event.scenePos() - clicked_scene

            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.active_handle:
            # 1) pretend you’re dragging the exact corner, not the raw mouse
            pointer_corner = event.scenePos() - self.handle_offset
            delta          = pointer_corner - self.anchor_scene

            # express the drag diagonal in the item's own (unrotated) frame so
            # width/height are measured along the item's axes, not the scene's —
            # otherwise a rotated item resizes along the wrong axis.
            delta = QTransform().rotate(-self.rotation()).map(delta)

            # 2) compute new size (preserve aspect)
            ow, oh = self.original_size.width(), self.original_size.height()
            ratio  = ow / oh
            if abs(delta.x()) > abs(delta.y()) * ratio:
                nw, nh = abs(delta.x()), abs(delta.x()) / ratio
            else:
                nh, nw = abs(delta.y()), abs(delta.y()) * ratio

            # 3) enforce a sensible minimum
            if nw < 20 or nh < 20:
                return

            # 4) apply it
            self.setSize(QSizeF(nw, nh))

            # 5) snap the opposite corner right back to anchor_scene
            new_local_opp = self._corner_point(self.opp_handle)
            new_opp_scene = self.mapToScene(new_local_opp)
            offset        = self.anchor_scene - new_opp_scene
            self.moveBy(round(offset.x()), round(offset.y()))

            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        did_resize = self.active_handle is not None
        self.setCursor(Qt.ArrowCursor)
        self.active_handle = None
        self.opp_handle    = None
        super().mouseReleaseEvent(event)
        # a move or resize may have pushed the map past the edge — grow to fit
        grow_canvas_for(self)
        # flag unsaved changes only if the item was actually moved or resized
        if did_resize or self.pos() != getattr(self, "_press_pos", self.pos()):
            _mark_item_dirty(self)


class AnimatedItem(QGraphicsObject):
    """A movable/scalable/rotatable animated *object* with per-pixel alpha.

    Renders an animated WebP through Qt's image pipeline (QMovie) rather than the
    video player, because the FFmpeg video backend strips WebM alpha (see the
    transparent-objects note near transcode_to_animated_webp). The geometry/
    interaction model mirrors InteractiveVideoItem: a target self._size that the
    current frame is drawn into, with corner-handle resize and 90° rotation."""
    HANDLE_SIZE = 30
    visible_to_player = True        # False → hidden in the player view (GM still sees it)

    def __init__(self, path):
        super().__init__()
        # Native size from the header (cheap, no full decode).
        nat = QImageReader(path).size()
        if not nat.isValid() or nat.isEmpty():
            nat = QSize(100, 100)
        self._size = QSizeF(nat)

        self._movie = QMovie(path)
        self._movie.setParent(self)            # tie movie lifetime to this item
        self._movie.setCacheMode(QMovie.CacheAll)
        self._movie.frameChanged.connect(self._on_frame)
        self._movie.start()

        self.setFlags(
            QGraphicsItem.ItemIsSelectable |
            QGraphicsItem.ItemIsMovable
        )
        self.setAcceptHoverEvents(True)

        # ── resizing fields (same model as InteractiveVideoItem) ──
        self.active_handle = None
        self.opp_handle    = None
        self.original_size = QSizeF()
        self.anchor_scene  = QPointF()
        self.handle_offset = QPointF()

    def _on_frame(self, _frame):
        self.update()

    def teardown_player(self):
        """Stop the animation timer before removal/scene.clear(). Named to match
        InteractiveVideoItem so _stop_all_videos can treat both uniformly."""
        try:
            self._movie.stop()
        except (RuntimeError, AttributeError):
            pass

    # ── geometry ──
    def setSize(self, sz):
        self.prepareGeometryChange()
        self._size = QSizeF(sz)

    def size(self):
        return self._size

    def boundingRect(self):
        return QRectF(0, 0, self._size.width(), self._size.height())

    def paint(self, painter, option, widget=None):
        in_player = _painting_in_player_view(widget)
        if not self.visible_to_player and in_player:
            return                              # hidden from the players
        img = self._movie.currentImage()
        if not img.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.drawImage(self.boundingRect(), img)
        if not self.visible_to_player:
            _paint_hidden_marker(self, painter)  # GM-only "hidden" overlay
        if self.isSelected() and not in_player:  # selection chrome is GM-only
            painter.setPen(QPen(Qt.green, 1, Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(self.boundingRect())
            painter.setBrush(Qt.white)
            painter.setPen(Qt.black)
            for rect in self._handle_rects().values():
                painter.drawRect(rect)

    def contextMenuEvent(self, event):
        if not self.isSelected():
            event.ignore()
            return
        view = self.scene().views()[0]
        view._panning = False
        view.viewport().setCursor(Qt.ArrowCursor)

        menu = QMenu()
        a_remove = menu.addAction("Remove")
        a_rotate = menu.addAction("Rotate 90°")
        a_top    = menu.addAction("Bring to Top")
        a_vis = menu.addAction("Show to Players" if not self.visible_to_player
                               else "Hide from Players")
        action = menu.exec(event.screenPos())

        if action == a_vis:
            _toggle_player_visibility(self, view)

        if action == a_remove:
            mw = view.window()
            self.teardown_player()
            self.scene().removeItem(self)
            if hasattr(mw, "refresh_canvas_extent"):
                mw.refresh_canvas_extent()
            if hasattr(mw, "update_layers_list"):
                mw.update_layers_list()
            if hasattr(mw, "mark_dirty"):
                mw.mark_dirty()
        if action == a_rotate:
            self._set_origin_keep_pos(self.boundingRect().center())
            self.setRotation(self.rotation() + 90)
            _mark_item_dirty(self)
            view._panning = False
            view.viewport().setCursor(Qt.ArrowCursor)
        if action == a_top:
            self.bring_to_top()
            view.window().update_layers_list()
            _mark_item_dirty(self)
        event.accept()

    def bring_to_top(self):
        # Raise within this item's own layer (backgrounds/objects/tokens) — never
        # above the layer stacked above it. MainWindow owns the layer rules.
        scene = self.scene()
        if not scene:
            return
        scene.views()[0].window().bring_item_to_layer_top(self)

    def _handle_rects(self):
        r = self.boundingRect()
        s = self.HANDLE_SIZE
        return {
            'tl': QRectF(r.topLeft(), QSizeF(s, s)),
            'tr': QRectF(r.topRight() - QPointF(s, 0), QSizeF(s, s)),
            'bl': QRectF(r.bottomLeft() - QPointF(0, s), QSizeF(s, s)),
            'br': QRectF(r.bottomRight() - QPointF(s, s), QSizeF(s, s)),
        }

    def _corner_point(self, corner):
        b = self.boundingRect()
        return {'tl': b.topLeft(), 'tr': b.topRight(),
                'bl': b.bottomLeft(), 'br': b.bottomRight()}[corner]

    def _set_origin_keep_pos(self, new_origin):
        # Same compensation as InteractiveVideoItem: moving the transform origin
        # while rotated would shift the item on screen; counter it so it stays put.
        if self.transformOriginPoint() == new_origin:
            return
        before = self.mapToScene(new_origin)
        self.setTransformOriginPoint(new_origin)
        after = self.mapToScene(new_origin)
        self.moveBy(before.x() - after.x(), before.y() - after.y())

    def hoverMoveEvent(self, event):
        view = self.scene().views()[0]
        if getattr(view.window(), "_fog_reveal_mode", False):
            view.viewport().setCursor(Qt.CrossCursor)
            return
        self.active_handle = None
        for name, rect in self._handle_rects().items():
            if rect.contains(event.pos()):
                self.active_handle = name
                center = self.boundingRect().center()
                delta = rect.center() - center
                delta = QTransform().rotate(self.rotation()).map(delta)
                self.setCursor(Qt.SizeFDiagCursor if delta.x() * delta.y() > 0
                               else Qt.SizeBDiagCursor)
                return
        self.setCursor(Qt.OpenHandCursor if self.isSelected() else Qt.ArrowCursor)

    def mousePressEvent(self, event):
        self._press_pos = self.pos()
        if event.button() == Qt.LeftButton and self.active_handle:
            self._set_origin_keep_pos(self.boundingRect().center())
            self.opp_handle = {'tl': 'br', 'br': 'tl',
                               'tr': 'bl', 'bl': 'tr'}[self.active_handle]
            self.original_size = self.size()
            self.anchor_scene = self.mapToScene(self._corner_point(self.opp_handle))
            clicked_scene = self.mapToScene(self._corner_point(self.active_handle))
            self.handle_offset = event.scenePos() - clicked_scene
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.active_handle:
            pointer_corner = event.scenePos() - self.handle_offset
            delta = pointer_corner - self.anchor_scene
            delta = QTransform().rotate(-self.rotation()).map(delta)
            ow, oh = self.original_size.width(), self.original_size.height()
            ratio = ow / oh
            if abs(delta.x()) > abs(delta.y()) * ratio:
                nw, nh = abs(delta.x()), abs(delta.x()) / ratio
            else:
                nh, nw = abs(delta.y()), abs(delta.y()) * ratio
            if nw < 20 or nh < 20:
                return
            self.setSize(QSizeF(nw, nh))
            new_opp_scene = self.mapToScene(self._corner_point(self.opp_handle))
            offset = self.anchor_scene - new_opp_scene
            self.moveBy(round(offset.x()), round(offset.y()))
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        did_resize = self.active_handle is not None
        self.setCursor(Qt.ArrowCursor)
        self.active_handle = None
        self.opp_handle    = None
        super().mouseReleaseEvent(event)
        grow_canvas_for(self)
        if did_resize or self.pos() != getattr(self, "_press_pos", self.pos()):
            _mark_item_dirty(self)


class TextBoxItem(QGraphicsObject):
    """A free-standing text annotation on the map — the first non-asset-backed map
    item. It has no library file; its text + styling live inline in the map JSON.

    Behaves like the other items: movable, rotatable (90° via the context menu),
    resizable (drag the right-edge handles to set the wrap width; height auto-fits),
    hide-from-players, copy/paste, and a row in the Layers tab (in the 'objects'
    band). Editing is via a dialog (double-click, or context menu → Edit Text…),
    driven by MainWindow.edit_textbox() — this class never imports main."""

    HANDLE_SIZE = 26
    PADDING = 8                 # scene-px gap between the border and the text
    MIN_WIDTH = 60
    visible_to_player = True    # False → hidden in the player view (GM still sees it)
    asset_category = "objects"  # slots into the objects z-band / Layers group

    def __init__(self, text="Text", width=220):
        super().__init__()
        self.setFlags(
            QGraphicsItem.ItemIsSelectable |
            QGraphicsItem.ItemIsMovable |
            QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        # styling (defaults chosen to read on a busy map: light text on a dark wash)
        self._text = text or ""
        self._width = float(width)
        self.font_family = "Sans Serif"
        self.font_size = 18.0                       # point size (~scene px)
        self.text_color = QColor("#ffffff")
        self.bg_color = QColor(0, 0, 0, 160)        # semi-transparent fill
        self.border_color = QColor("#ffffff")
        self.border_width = 2.0
        self.bold = False
        self.italic = False
        self.align = Qt.AlignLeft
        self.active_handle = None
        self._press_pos = QPointF()

    # ── styling / geometry helpers ───────────────────────────────────────────
    def _font(self):
        f = QFont(self.font_family)
        f.setPointSizeF(max(1.0, self.font_size))
        f.setBold(self.bold)
        f.setItalic(self.italic)
        return f

    def _inset(self):
        return self.PADDING + self.border_width

    def _text_width(self):
        return max(1.0, self._width - 2 * self._inset())

    def _text_height(self):
        fm = QFontMetrics(self._font())
        flags = int(self.align) | int(Qt.TextWordWrap)
        r = fm.boundingRect(QRectF(0, 0, self._text_width(), 100000).toRect(),
                            flags, self._text or " ")
        return max(fm.height(), r.height())

    def boundingRect(self):
        h = self._text_height() + 2 * self._inset()
        return QRectF(0, 0, self._width, h)

    # ── painting ─────────────────────────────────────────────────────────────
    def paint(self, painter, option, widget=None):
        in_player = _painting_in_player_view(widget)
        if not self.visible_to_player and in_player:
            return                               # hidden from the players
        r = self.boundingRect()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        # background fill
        if self.bg_color.alpha() > 0:
            painter.setPen(Qt.NoPen)
            painter.setBrush(self.bg_color)
            painter.drawRoundedRect(r, 6, 6)
        # border
        if self.border_width > 0 and self.border_color.alpha() > 0:
            bw = self.border_width
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(self.border_color, bw))
            painter.drawRoundedRect(r.adjusted(bw / 2, bw / 2, -bw / 2, -bw / 2), 6, 6)
        # text
        ins = self._inset()
        painter.setPen(QPen(self.text_color))
        painter.setFont(self._font())
        opt = QTextOption()
        opt.setAlignment(self.align)
        opt.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        painter.drawText(QRectF(ins, ins, self._width - 2 * ins, r.height() - 2 * ins),
                         self._text, opt)
        painter.restore()
        if not self.visible_to_player:
            _paint_hidden_marker(self, painter)  # GM-only "hidden" overlay
        if self.isSelected() and not in_player:  # selection chrome is GM-only
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor(0, 255, 0), 1, Qt.DashLine))
            painter.drawRect(r)
            painter.setBrush(Qt.white)
            painter.setPen(Qt.black)
            for rect in self._handle_positions().values():
                painter.drawRect(rect)

    # ── resize (width only; height auto-fits the text) ───────────────────────
    def _handle_positions(self):
        # Only the right-edge corners resize — dragging sets the wrap width,
        # anchored at the left edge (top-left origin stays put).
        r = self.boundingRect()
        s = self.HANDLE_SIZE
        return {
            "tr": QRectF(r.right() - s, r.top(), s, s),
            "br": QRectF(r.right() - s, r.bottom() - s, s, s),
        }

    def hoverMoveEvent(self, event):
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        if view is not None and getattr(view.window(), "_fog_reveal_mode", False):
            view.viewport().setCursor(Qt.CrossCursor)
            return
        self.active_handle = None
        for name, rect in self._handle_positions().items():
            if rect.contains(event.pos()):
                self.active_handle = name
                self.setCursor(Qt.SizeHorCursor)
                return
        self.setCursor(Qt.ArrowCursor)

    def mousePressEvent(self, event):
        self._press_pos = self.pos()
        if self.active_handle:
            event.accept()                       # begin a width-resize
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.active_handle:
            # event.pos() is in item coords (rotation-independent): the local x is
            # the distance from the left edge, i.e. the new wrap width.
            new_w = max(self.MIN_WIDTH, event.pos().x())
            if new_w != self._width:
                self.prepareGeometryChange()
                self._width = new_w
                self.update()
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        did_resize = self.active_handle is not None
        self.active_handle = None
        super().mouseReleaseEvent(event)
        grow_canvas_for(self)
        if did_resize or self.pos() != self._press_pos:
            _mark_item_dirty(self)

    def mouseDoubleClickEvent(self, event):
        view = self.scene().views()[0] if self.scene() and self.scene().views() else None
        if view is not None and hasattr(view.window(), "edit_textbox"):
            view.window().edit_textbox(self)
        event.accept()

    # ── context menu ─────────────────────────────────────────────────────────
    def contextMenuEvent(self, event):
        if not self.isSelected():
            event.ignore()
            return
        view = self.scene().views()[0]
        view._panning = False
        view.viewport().setCursor(Qt.ArrowCursor)
        menu = QMenu()
        a_edit = menu.addAction("Edit Text…")
        a_remove = menu.addAction("Remove")
        a_rotate = menu.addAction("Rotate 90°")
        a_top = menu.addAction("Bring to Top")
        a_vis = menu.addAction("Show to Players" if not self.visible_to_player
                               else "Hide from Players")
        action = menu.exec(event.screenPos())
        mw = view.window()
        if action == a_edit and hasattr(mw, "edit_textbox"):
            mw.edit_textbox(self)
        elif action == a_vis:
            _toggle_player_visibility(self, view)
        elif action == a_remove:
            self.scene().removeItem(self)
            if hasattr(mw, "refresh_canvas_extent"):
                mw.refresh_canvas_extent()
            if hasattr(mw, "update_layers_list"):
                mw.update_layers_list()
            if hasattr(mw, "mark_dirty"):
                mw.mark_dirty()
        elif action == a_rotate:
            self.setTransformOriginPoint(self.boundingRect().center())
            self.setRotation((self.rotation() + 90) % 360)
            _mark_item_dirty(self)
        elif action == a_top:
            self.bring_to_top()
            if hasattr(mw, "update_layers_list"):
                mw.update_layers_list()
            _mark_item_dirty(self)
        event.accept()

    def bring_to_top(self):
        sc = self.scene()
        if sc and sc.views():
            sc.views()[0].window().bring_item_to_layer_top(self)

    # ── serialization (self-contained; no asset file) ────────────────────────
    def to_json(self):
        return {
            "type": "text",
            "text": self._text,
            "pos": [self.pos().x(), self.pos().y()],
            "width": self._width,
            "rot": self.rotation(),
            "z": self.zValue(),
            "visibleToPlayer": self.visible_to_player,
            "fontFamily": self.font_family,
            "fontSize": self.font_size,
            "textColor": self.text_color.name(QColor.HexArgb),
            "bgColor": self.bg_color.name(QColor.HexArgb),
            "borderColor": self.border_color.name(QColor.HexArgb),
            "borderWidth": self.border_width,
            "bold": self.bold,
            "italic": self.italic,
            "align": int(self.align),
        }

    def apply_json(self, d):
        """Set styling/text/geometry from a saved dict (pos/z/rot set by caller)."""
        self.prepareGeometryChange()
        self._text = d.get("text", self._text)
        self._width = float(d.get("width", self._width))
        self.font_family = d.get("fontFamily", self.font_family)
        self.font_size = float(d.get("fontSize", self.font_size))
        self.text_color = QColor(d.get("textColor", self.text_color.name(QColor.HexArgb)))
        self.bg_color = QColor(d.get("bgColor", self.bg_color.name(QColor.HexArgb)))
        self.border_color = QColor(d.get("borderColor", self.border_color.name(QColor.HexArgb)))
        self.border_width = float(d.get("borderWidth", self.border_width))
        self.bold = bool(d.get("bold", self.bold))
        self.italic = bool(d.get("italic", self.italic))
        self.align = Qt.AlignmentFlag(int(d.get("align", int(self.align))))
        self.visible_to_player = bool(d.get("visibleToPlayer", True))
        self.update()


class PingItem(QGraphicsObject):
    """A transient 'look here' marker: expanding sonar rings + a bright core that
    fade out over ~1.4s, then the item removes itself. Added to the SHARED scene,
    so it shows in both the GM and Player views for free (and, because the web
    client is a pixel-stream of the player view, on players' phones too).

    Non-interactive (ignores mouse, never selectable/movable) and drawn in scene
    units so it scales with zoom like the map. It carries no asset, so it's
    automatically excluded from saves / canvas extent / the layers list (none of
    those isinstance() checks match it). NOTE: fog is painted in
    Canvas.drawForeground ABOVE all items, so in a fogged region the ping sits
    under the fog — GM pings tend to be on revealed areas."""

    PING_COLOR = QColor(110, 86, 169)   # arcana purple (#6e56a9 — the app accent)
    MAX_RADIUS = 66.0                   # scene px (72 = 1 inch)
    DURATION_MS = 1400
    Z = 8_000_000                       # above map/tokens/grid, below PlayerViewItem (9M)

    def __init__(self, color=None):
        super().__init__()
        self.color = QColor(color) if color else QColor(self.PING_COLOR)
        self._t = 0.0                   # animation progress 0..1
        self.setZValue(self.Z)
        self.setAcceptedMouseButtons(Qt.NoButton)   # never blocks clicks
        self._anim = None

    def start(self):
        anim = QVariantAnimation(self)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setDuration(self.DURATION_MS)
        anim.setEasingCurve(QEasingCurve.OutCubic)  # rings shoot out, then settle
        anim.valueChanged.connect(self._on_tick)
        anim.finished.connect(self._finish)
        self._anim = anim
        anim.start()

    def _on_tick(self, v):
        self._t = float(v)
        self.update()

    def _finish(self):
        sc = self.scene()
        if sc is not None:
            sc.removeItem(self)
        self.deleteLater()

    def boundingRect(self):
        r = self.MAX_RADIUS + 6.0
        return QRectF(-r, -r, 2 * r, 2 * r)

    def paint(self, painter, option, widget=None):
        t = self._t
        painter.setRenderHint(QPainter.Antialiasing, True)
        c = self.color
        # Expanding sonar rings, staggered so they emanate outward in a trail.
        RINGS = 3
        for i in range(RINGS):
            p = t - i * 0.16
            if p <= 0.0 or p >= 1.0:
                continue
            rad = self.MAX_RADIUS * p
            a = int(200 * (1.0 - p))
            pen = QPen(QColor(c.red(), c.green(), c.blue(), a))
            pen.setWidthF(4.0)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(0, 0), rad, rad)
        # Bright core dot; holds, then fades over the last 30%.
        core = 1.0 if t < 0.7 else max(0.0, 1.0 - (t - 0.7) / 0.3)
        painter.setPen(QPen(QColor(c.red(), c.green(), c.blue(), int(255 * core)), 3.0))
        painter.setBrush(QBrush(QColor(255, 255, 255, int(210 * core))))
        painter.drawEllipse(QPointF(0, 0), 7.0, 7.0)


class PlayerViewItem(QGraphicsRectItem):
    def __init__(self, x=0, y=0, width=800, height=600, title="Player View", gm_view=None):
        super().__init__(x, y, width, height)
        # Always on top of every map item. Must stay above the layer z-bands and
        # GRID_ABOVE_Z defined in main.py (PLAYERVIEW_Z) — keep these consistent.
        self.setZValue(9_000_000)
        # Only selectable; we'll implement our own drag in titlebar
        self.setFlags(QGraphicsRectItem.ItemIsSelectable)
        self.setAcceptHoverEvents(True)

        self.title = title
        self.title_height = 30
        self.border_color = QColor(255, 255, 255)
        self.title_color = QColor(20, 20, 20)
        self.text_color = QColor(255, 255, 255)
        self.font = QFont("Arial", 10, QFont.Bold)

        self._dragging = False
        self._drag_scene_offset = QPointF()

        self.gm_view = gm_view  # the GM Canvas view
        self.show_in_player = False

    def paint(self, painter: QPainter, option, widget=None):

        # Only paint when this item is being drawn in the GM view’s viewport:
        view = widget.parent() if widget else None
        # if it's not the GM view and we're not explicitly showing in player, skip drawing
        if view is not self.gm_view and not self.show_in_player:
            return

        rect = self.rect()
        # 1) Draw only the border, no fill for the main area
        painter.setPen(QPen(self.border_color, 2))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(rect)

        # 2) Draw the titlebar with its fill
        title_rect = QRectF(rect.left(), rect.top(), rect.width(), self.title_height)
        painter.setBrush(QBrush(self.title_color))
        painter.setPen(Qt.NoPen)
        painter.drawRect(title_rect)

        # 3) Draw the title text
        painter.setFont(self.font)
        painter.setPen(self.text_color)
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, "  " + self.title)

    def hoverMoveEvent(self, event):
        self.setCursor(Qt.OpenHandCursor if self._in_titlebar(event.pos()) else Qt.ArrowCursor)
        super().hoverMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._in_titlebar(event.pos()):
            self._dragging = True
            self.setCursor(Qt.ClosedHandCursor)
            # store offset in SCENE coordinates so view scaling/Wayland don’t matter
            # scenePos() is the mouse in scene space; self.pos() is our position in parent/scene space
            self._drag_scene_offset = event.scenePos() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging:
            # new position purely in scene coords; no mixing with item-local
            new_scene_pos = event.scenePos() - self._drag_scene_offset
            self.setPos(new_scene_pos)
            event.accept()

            # Sync player camera using the known GM window instead of activeWindow()
            if self.gm_view:
                mw = self.gm_view.window()
                if hasattr(mw, "sync_player_view_to_camera"):
                    mw.sync_player_view_to_camera()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._dragging = False
        self.setCursor(Qt.ArrowCursor)
        super().mouseReleaseEvent(event)

    def _in_titlebar(self, pos):
        return QRectF(self.rect().topLeft(), QSizeF(self.rect().width(), self.title_height)).contains(pos)

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        # only the titlebar is “solid”
        title_rect = QRectF(self.rect().left(),
                            self.rect().top(),
                            self.rect().width(),
                            self.title_height)
        path.addRect(title_rect)
        return path

    def contains(self, point: QPointF) -> bool:
        # only treat clicks in the titlebar as “inside” this item
        title_rect = QRectF(self.rect().left(),
                            self.rect().top(),
                            self.rect().width(),
                            self.title_height)
        return title_rect.contains(point)

WELCOME_TITLE = "Welcome to Arcane Atlas"
# Highlighted note drawn in an accent box at the top of the card.
WELCOME_NOTE = (
    "Existing maps open with “Lock Assets” ON so you don't nudge things by accident.\n"
    "To edit a map — move, resize, add, or delete items — uncheck “Lock Assets”."
)
WELCOME_BODY = (
    "Get started\n"
    "•  New map — “File ▸ New Map”, or right-click in the map browser.\n"
    "•  Open a map — double-click it in the map browser.\n"
    "•  Build a map — drag image/video files onto the canvas, or right-click ▸ Import File.\n"
    "\n"
    "Canvas controls\n"
    "•  Pan — drag with the right or middle mouse button (or two-finger scroll).\n"
    "•  Zoom — Ctrl + mouse wheel, the zoom slider, or pinch on a trackpad.\n"
    "•  Add text — right-click the canvas ▸ Add Text Box.\n"
    "•  Right-click an item for more options (rotate, hide from players, layer…).\n"
    "\n"
    "Supported files\n"
    "•  Images — .jpg  .jpeg  .png  .webp (animated, transparent)\n"
    "•  Video — .mp4  .webm  .mov  .avi  .m4v\n"
    "\n"
    "Tip: dropping a file here with no map open starts a new map automatically."
)


class WelcomeItem(QGraphicsItem):
    """Help card shown on the shared scene when no map is open. It's an ordinary
    scene item — so it appears in the player view and the GM can frame/pan the
    player view onto it — but non-interactive: it ignores mouse input (never
    selectable/draggable, even when the map is unlocked), and it's deliberately
    excluded from saves (no asset_filename), the canvas extent (_content_rect
    counts only map items), and the layers list. Sizes are scene-pixels (72 = 1in)."""
    WIDTH = 1350
    HEIGHT = 1140
    ICON_SIZE = 84                                  # app-icon size flanking the title (scene px)

    def __init__(self, icon_path=None):
        super().__init__()
        self.setZValue(99)                          # above grid/maps, below player-view rect (z=100)
        self.setAcceptedMouseButtons(Qt.NoButton)   # click-through; never grabbed/dragged
        # Pre-scale the app icon once (the path is passed in — items.py never
        # imports main). Null pixmap → title is drawn without flanking icons.
        self._icon = QPixmap()
        if icon_path:
            pm = QPixmap(icon_path)
            if not pm.isNull():
                self._icon = pm.scaled(self.ICON_SIZE, self.ICON_SIZE,
                                       Qt.KeepAspectRatio, Qt.SmoothTransformation)

    def boundingRect(self):
        return QRectF(0, 0, self.WIDTH, self.HEIGHT)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing, True)
        r = self.boundingRect()

        # translucent backdrop for readability over grid/background
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 180))
        painter.drawRoundedRect(r, 36, 36)

        inner = r.adjusted(66, 54, -66, -54)
        font = painter.font()

        # title
        font.setPixelSize(60)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(110, 86, 169))        # arcana purple
        title_rect = QRectF(inner.left(), inner.top(), inner.width(), 84)
        painter.drawText(title_rect, Qt.AlignHCenter | Qt.AlignTop, WELCOME_TITLE)

        # app icon flanking the centred title on both sides
        if not self._icon.isNull():
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            fm = QFontMetrics(font)
            tw = fm.horizontalAdvance(WELCOME_TITLE)
            cx = title_rect.center().x()
            iw, ih = self._icon.width(), self._icon.height()
            iy = title_rect.top() + fm.height() / 2.0 - ih / 2.0   # centre on the text row
            gap = 28
            painter.drawPixmap(QPointF(cx - tw / 2.0 - gap - iw, iy), self._icon)
            painter.drawPixmap(QPointF(cx + tw / 2.0 + gap, iy), self._icon)

        # highlighted note — how to make the map editable. Anchored to the BOTTOM
        # of the card, so measure it first to reserve space for the body above it.
        font.setPixelSize(31)
        font.setBold(True)
        painter.setFont(font)
        note_flags = Qt.AlignHCenter | Qt.AlignVCenter | Qt.TextWordWrap
        pad = 22
        fm_note = QFontMetrics(font)
        note_text_w = inner.width() - 2 * pad
        nh = fm_note.boundingRect(0, 0, int(note_text_w), 100000,
                                  int(note_flags), WELCOME_NOTE).height()
        note_box = QRectF(inner.left(), inner.bottom() - (nh + 2 * pad),
                          inner.width(), nh + 2 * pad)

        # body — fills the space between the title and the bottom note box
        font.setPixelSize(31)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QColor(228, 228, 228))
        body_top = title_rect.bottom() + 27
        body_rect = QRectF(inner.left(), body_top,
                           inner.width(), note_box.top() - 27 - body_top)
        painter.drawText(body_rect, Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap, WELCOME_BODY)

        # draw the highlighted note last (accent gold box, at the bottom)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(QColor(255, 200, 60), 3))       # gold border
        painter.setBrush(QColor(255, 200, 60, 38))          # translucent gold fill
        painter.drawRoundedRect(note_box, 16, 16)
        painter.setPen(QColor(255, 224, 150))
        painter.drawText(note_box.adjusted(pad, 0, -pad, 0), note_flags, WELCOME_NOTE)


