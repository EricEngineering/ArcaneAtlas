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

"""LAN web-sharing server for ArcaneAtlas.

Streams the GM's **Player Window** to browsers on the local network (a cheap
MJPEG-over-WebSocket pixel stream) and lets remote clients **drag GM-permitted
tokens** (the state-sync path). Designed for the in-person table: LAN only, no
port forwarding, the GM toggles it on and players join by URL.

Architecture — everything runs on the **GUI thread**, event-driven:
  * ``QTcpServer``       serves one self-contained HTML/JS page (``GET /``).
  * ``QWebSocketServer`` carries the rest both ways on the next port:
        server → client : binary JPEG frames (~12 fps) + token-state JSON
        client → server : token-move commands
  * a GUI-thread ``QTimer`` grabs ``player_window.canvas_view`` → JPEG → broadcast.

No worker thread / asyncio: the only two things touched (grabbing the player
view and mutating the scene) both have GUI-thread affinity anyway, so keeping the
sockets on the GUI thread removes all cross-thread marshalling. Token moves are
applied through ``MainWindow.apply_token_move`` — the single mutation chokepoint.

Imports only PySide6, never ``main`` (same one-way rule as items/transcode); the
owning ``MainWindow`` constructs a ``WebServer`` and passes itself in.
"""

import sys
import json
import socket
import logging
import subprocess

from PySide6.QtCore import QObject, QTimer, QByteArray, QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QColor, QPixmap, QGuiApplication
from PySide6.QtNetwork import QTcpServer, QHostAddress, QAbstractSocket, QNetworkInterface
from PySide6.QtWebSockets import QWebSocketServer
from PySide6.QtWidgets import (
    QDialog, QLabel, QPushButton, QVBoxLayout, QHBoxLayout, QCheckBox,
    QSpinBox, QLineEdit, QFrame)

log = logging.getLogger("arcaneatlas.webserver")

FRAME_INTERVAL_MS = 80      # ~12.5 fps — smooth enough for a battle map, light on CPU
JPEG_QUALITY = 70
MAX_FRAME_WIDTH = 1280      # cap streamed width to keep LAN bandwidth modest

# Default LAN-sharing port. Deliberately high/uncommon (not 8080/8081, which
# collide with dev servers/proxies) so a conflict is rare; start() auto-falls-back
# to the next free port anyway, and the QR/URL always reflect the real bound port.
DEFAULT_WEB_PORT = 47800
PORT_SCAN_SPAN = 40         # how many ports to probe upward before giving up

# Single-page client. __WS_PORT__ is substituted at serve time. The page draws
# the streamed frames onto a <canvas> and overlays draggable handles for the
# GM-permitted tokens; dragging sends a normalised-coordinate move back.
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>Arcane Atlas — Player View</title>
<style>
  html,body{margin:0;height:100%;background:#111;color:#ccc;font-family:sans-serif;overflow:hidden;}
  #stage{position:fixed;inset:0;overflow:hidden;touch-action:none;background:#111;}
  #view{position:absolute;top:0;left:0;transform-origin:0 0;will-change:transform;}
  canvas{display:block;background:#000;}
  #overlay{position:absolute;top:0;left:0;}
  #msg{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;
       opacity:.8;pointer-events:none;font-size:1.1rem;padding:1rem;}
  #hint{position:fixed;left:8px;bottom:8px;opacity:.45;font-size:.72rem;pointer-events:none;}
  #rotbtn{position:fixed;right:10px;top:10px;width:46px;height:46px;border-radius:50%;
          border:none;background:rgba(40,44,52,.85);color:#cfe6ff;font-size:1.5rem;
          line-height:46px;text-align:center;cursor:pointer;touch-action:manipulation;
          box-shadow:0 1px 4px rgba(0,0,0,.5);}
  .tok{position:absolute;border-radius:50%;border:2px dashed rgba(80,180,255,.9);
       background:rgba(80,180,255,.12);box-sizing:border-box;cursor:grab;touch-action:none;}
  .tok.drag{cursor:grabbing;border-style:solid;}
</style>
</head>
<body>
<div id="stage">
  <div id="view">
    <canvas id="cv" width="16" height="9"></canvas>
    <div id="overlay"></div>
  </div>
</div>
<button id="rotbtn" title="Rotate the map 90°">⟳</button>
<div id="msg">Connecting…</div>
<div id="hint">drag to pan · pinch / scroll to zoom · ⟳ rotate · double-tap to reset</div>
<script>
const WS_PORT = __WS_PORT__;
const stage = document.getElementById('stage'), view = document.getElementById('view');
const cv = document.getElementById('cv'), ctx = cv.getContext('2d');
const overlay = document.getElementById('overlay'), msg = document.getElementById('msg');
let tokens = [], dragId = null, dragLocal = null, hasView = false, fitted = false;
let scale = 1, tx = 0, ty = 0, rot = 0;  // pan / zoom / rotation (deg) of the #view layer

// ---- pan / zoom / rotate transform ----
// transform = translate(tx,ty) scale(scale) rotate(rot); origin 0,0.
function applyT(){ view.style.transform = `translate(${tx}px,${ty}px) scale(${scale}) rotate(${rot}deg)`; }
function clampScale(s){ return Math.max(0.1, Math.min(8, s)); }
function rotCorners(){    // canvas corners after rotation only (scale/translate applied later)
  const a = rot*Math.PI/180, c = Math.cos(a), s = Math.sin(a);
  return [[0,0],[cv.width,0],[cv.width,cv.height],[0,cv.height]]
    .map(([x,y]) => ({x: x*c - y*s, y: x*s + y*c}));
}
function fitView(){
  const sw = stage.clientWidth, sh = stage.clientHeight;
  if (!cv.width || !cv.height || !sw || !sh) return;
  const cs = rotCorners();
  const minx = Math.min(...cs.map(p=>p.x)), maxx = Math.max(...cs.map(p=>p.x));
  const miny = Math.min(...cs.map(p=>p.y)), maxy = Math.max(...cs.map(p=>p.y));
  const bw = maxx-minx, bh = maxy-miny;          // rotated bounding box (unscaled)
  scale = Math.min(sw/bw, sh/bh);
  tx = (sw - bw*scale)/2 - minx*scale;           // centre the rotated content
  ty = (sh - bh*scale)/2 - miny*scale;
  applyT();
}
function zoomAt(cx, cy, factor){           // keep screen point (cx,cy) fixed while zooming
  const ns = clampScale(scale * factor), k = ns / scale;
  tx = cx - (cx - tx) * k;
  ty = cy - (cy - ty) * k;
  scale = ns; applyT();
}
function screenToCanvas(sx, sy){           // invert translate→scale→rotate (rotation-proof)
  const X = (sx - tx)/scale, Y = (sy - ty)/scale;
  const a = rot*Math.PI/180, c = Math.cos(a), s = Math.sin(a);
  return { x: X*c + Y*s, y: -X*s + Y*c };
}
document.getElementById('rotbtn').addEventListener('click', () => { rot = (rot + 90) % 360; fitView(); });

// ---- WebSocket ----
function connect(){
  const ws = new WebSocket('ws://' + location.hostname + ':' + WS_PORT);
  ws.binaryType = 'blob';
  window._ws = ws;
  ws.onopen = () => { msg.textContent = ''; };
  ws.onclose = () => { msg.textContent = 'Disconnected — retrying…'; setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string'){ onState(JSON.parse(ev.data)); return; }
    if (!hasView) return;                  // ignore stale frames once the GM hid the view
    const url = URL.createObjectURL(ev.data);
    const img = new Image();
    img.onload = () => {
      if (cv.width !== img.width || cv.height !== img.height){ cv.width = img.width; cv.height = img.height; fitted = false; }
      ctx.drawImage(img, 0, 0); URL.revokeObjectURL(url);
      if (!fitted){ fitView(); fitted = true; }
      layout();
    };
    img.src = url;
  };
}
function onState(s){
  hasView = !!s.view;
  if (!hasView){
    msg.textContent = 'Waiting for the GM to open the Player View…';
    ctx.clearRect(0, 0, cv.width, cv.height);   // drop the last frame so it doesn't linger
    tokens = []; layout(); return;
  }
  if (msg.textContent.startsWith('Waiting') || msg.textContent.startsWith('Connecting')) msg.textContent = '';
  tokens = s.tokens || []; layout();
}

// ---- token handles (positioned in canvas-natural px; the #view transform scales them) ----
function layout(){
  const byId = {};
  tokens.forEach(t => byId[t.id] = t);
  [...overlay.children].forEach(el => { if (!byId[el.dataset.id]) el.remove(); });
  tokens.forEach(t => {
    let el = overlay.querySelector('[data-id="'+CSS.escape(t.id)+'"]');
    if (!el){ el = document.createElement('div'); el.className = 'tok'; el.dataset.id = t.id;
              attachDrag(el); overlay.appendChild(el); }
    const useLocal = (dragId === t.id && dragLocal);
    const nx = useLocal ? dragLocal.nx : t.nx, ny = useLocal ? dragLocal.ny : t.ny;
    el.style.left   = (nx * cv.width)  + 'px';
    el.style.top    = (ny * cv.height) + 'px';
    el.style.width  = (t.nw * cv.width)  + 'px';
    el.style.height = (t.nh * cv.height) + 'px';
  });
}
function attachDrag(el){
  el.addEventListener('pointerdown', (e) => {
    dragId = el.dataset.id; el.classList.add('drag'); el.setPointerCapture(e.pointerId);
    e.preventDefault(); e.stopPropagation();          // don't let the stage pan
  });
  el.addEventListener('pointermove', (e) => {
    if (dragId !== el.dataset.id) return;
    const t = tokens.find(x => x.id === dragId); if (!t) return;
    const p = screenToCanvas(e.clientX, e.clientY);   // inverse of pan/zoom/rotate
    let nx = p.x/cv.width  - t.nw/2;                   // top-left from centre under finger
    let ny = p.y/cv.height - t.nh/2;
    dragLocal = { nx, ny }; layout(); e.stopPropagation();
  });
  const end = (e) => {
    if (dragId !== el.dataset.id) return;
    el.classList.remove('drag');
    const t = tokens.find(x => x.id === dragId);
    if (t && dragLocal && window._ws && window._ws.readyState === 1){
      window._ws.send(JSON.stringify({type:'move', id:dragId, nx:dragLocal.nx + t.nw/2, ny:dragLocal.ny + t.nh/2}));
    }
    dragId = null; dragLocal = null;
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', end);
}

// ---- pan / pinch-zoom on the stage (ignored when a token grabbed the pointer) ----
const ptrs = new Map(); let panPrev = null, pinchPrev = null, lastTap = 0;
function pinchInfo(){
  const a = [...ptrs.values()];
  return { dist: Math.hypot(a[0].x-a[1].x, a[0].y-a[1].y) || 1,
           cx: (a[0].x+a[1].x)/2, cy: (a[0].y+a[1].y)/2 };
}
stage.addEventListener('pointerdown', (e) => {
  ptrs.set(e.pointerId, {x:e.clientX, y:e.clientY});
  if (ptrs.size === 1){
    panPrev = {x:e.clientX, y:e.clientY};
    const now = Date.now(); if (now - lastTap < 300) fitView(); lastTap = now;   // double-tap → reset
  } else if (ptrs.size === 2){ panPrev = null; pinchPrev = pinchInfo(); }
});
stage.addEventListener('pointermove', (e) => {
  if (!ptrs.has(e.pointerId)) return;
  ptrs.set(e.pointerId, {x:e.clientX, y:e.clientY});
  if (ptrs.size === 1 && panPrev){
    tx += e.clientX - panPrev.x; ty += e.clientY - panPrev.y; panPrev = {x:e.clientX, y:e.clientY}; applyT();
  } else if (ptrs.size === 2 && pinchPrev){
    const pi = pinchInfo();
    zoomAt(pi.cx, pi.cy, pi.dist / pinchPrev.dist);   // pinch zoom around the midpoint
    tx += pi.cx - pinchPrev.cx; ty += pi.cy - pinchPrev.cy; applyT();   // two-finger pan
    pinchPrev = pi;
  }
});
function stageUp(e){
  ptrs.delete(e.pointerId);
  pinchPrev = (ptrs.size === 2) ? pinchInfo() : null;
  panPrev = (ptrs.size === 1) ? (([p]) => ({x:p.x, y:p.y}))([...ptrs.values()]) : null;
}
stage.addEventListener('pointerup', stageUp);
stage.addEventListener('pointercancel', stageUp);
stage.addEventListener('wheel', (e) => { e.preventDefault(); zoomAt(e.clientX, e.clientY, e.deltaY < 0 ? 1.1 : 1/1.1); }, {passive:false});
window.addEventListener('resize', () => { if (hasView) fitView(); });
connect();
</script>
</body>
</html>
"""


def make_qr_pixmap(text, module_px=6, quiet=4):
    """Render `text` to a crisp black-on-white QR `QPixmap`, or None if the
    `qrcode` package isn't available. Lazy-imported and matrix-rendered by hand
    so no Pillow dependency is pulled in (and a missing package degrades to a
    URL-only dialog rather than breaking)."""
    try:
        import qrcode
    except ImportError:
        log.warning("qrcode package not installed — showing URL without a QR code")
        return None
    qr = qrcode.QRCode(border=quiet, box_size=1,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(text)
    qr.make(fit=True)
    matrix = qr.get_matrix()              # square list-of-lists of bool (incl. quiet zone)
    n = len(matrix)
    img = QImage(n, n, QImage.Format_RGB32)
    img.fill(Qt.white)
    black = QColor(Qt.black)
    for y, row in enumerate(matrix):
        for x, on in enumerate(row):
            if on:
                img.setPixelColor(x, y, black)
    # Nearest-neighbour upscale → sharp modules (FastTransformation = no blur).
    return QPixmap.fromImage(img).scaled(n * module_px, n * module_px,
                                         Qt.KeepAspectRatio, Qt.FastTransformation)


def _first_free_port(tcp_server, start, span):
    """Probe [start, start+span) for a port QTcpServer can bind; return it (already
    listening) or None. A failed listen() leaves the server un-listening, so we can
    retry the next port on the same object."""
    for p in range(start, start + span):
        if tcp_server.listen(QHostAddress.Any, p):
            return p
    return None


def _first_free_ws_port(ws_server, start, span):
    """Same as _first_free_port for a QWebSocketServer."""
    for p in range(start, start + span):
        if ws_server.listen(QHostAddress.Any, p):
            return p
    return None


def _linux_firewall_command(http, ws):
    """Return the exact port-open command for whichever firewall is active on this
    Linux box (firewalld / ufw), or a generic hint. Used by the Firewall Help pane."""
    def _active(svc):
        try:
            r = subprocess.run(["systemctl", "is-active", svc],
                               capture_output=True, text=True, timeout=2)
            return r.stdout.strip() == "active"
        except Exception:
            return False
    if _active("firewalld"):
        return (f"sudo firewall-cmd --permanent --add-port={http}/tcp "
                f"--add-port={ws}/tcp && sudo firewall-cmd --reload")
    if _active("ufw"):
        return f"sudo ufw allow {http}:{ws}/tcp"
    return f"# Open TCP ports {http} and {ws} in your firewall (no ufw/firewalld detected)."


def _open_os_firewall_settings():
    """Best-effort: launch the OS firewall settings UI. Returns True on success."""
    try:
        if sys.platform.startswith("win"):
            subprocess.Popen("control firewall.cpl", shell=True)
        elif sys.platform == "darwin":
            subprocess.Popen(
                ["open", "x-apple.systempreferences:com.apple.preference.security?Firewall"])
        else:  # Linux — try known firewall GUIs; many systems have none.
            for tool in ("firewall-config", "gufw", "plasma-firewall"):
                try:
                    subprocess.Popen([tool])
                    return True
                except FileNotFoundError:
                    continue
            return False
        return True
    except Exception as e:
        log.warning("could not open firewall settings: %s", e)
        return False


def lan_ip():
    """Best-effort primary LAN IPv4. The UDP 'connect' just selects the outgoing
    interface (no packets sent), so it works offline as long as a route exists."""
    ip = ""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = ""
    finally:
        s.close()
    if ip and not ip.startswith("127."):
        return ip
    # Fallback: first non-loopback IPv4 on any interface.
    for addr in QNetworkInterface.allAddresses():
        if (addr.protocol() == QAbstractSocket.IPv4Protocol
                and not addr.isLoopback()):
            return addr.toString()
    return ip or "127.0.0.1"


class WebServer(QObject):
    """Owns the two listening servers + the frame timer. Construct with the
    MainWindow; call start()/stop(); read url()."""

    def __init__(self, window, http_port=DEFAULT_WEB_PORT):
        super().__init__(window)
        self.window = window
        self.http_port = http_port
        self.ws_port = http_port + 1
        self.running = False

        self.tcp = QTcpServer(self)
        self.tcp.newConnection.connect(self._on_http_conn)

        self.wss = QWebSocketServer("ArcaneAtlas",
                                    QWebSocketServer.NonSecureMode, self)
        self.wss.newConnection.connect(self._on_ws_conn)
        self.clients = []

        self.timer = QTimer(self)
        self.timer.setInterval(FRAME_INTERVAL_MS)
        self.timer.timeout.connect(self._tick)

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self):
        if self.running:
            return self.url()
        base = self.http_port
        # Auto-fallback: probe upward for a free HTTP port so a conflict on the
        # preferred port never hard-fails. The bound port is reflected in url().
        hp = _first_free_port(self.tcp, base, PORT_SCAN_SPAN)
        if hp is None:
            log.warning("web: no free HTTP port in %d..%d", base, base + PORT_SCAN_SPAN - 1)
            return None
        self.http_port = hp
        # WebSocket port: prefer http_port+1, else the next free one (the served
        # page injects whatever we pick, so they need not stay adjacent).
        wp = _first_free_ws_port(self.wss, hp + 1, PORT_SCAN_SPAN)
        if wp is None:
            log.warning("web: no free WS port near %d", hp + 1)
            self.tcp.close()
            return None
        self.ws_port = wp
        self.timer.start()
        self.running = True
        log.info("web sharing started at %s (ws %d)", self.url(), self.ws_port)
        return self.url()

    def stop(self):
        self.timer.stop()
        for c in list(self.clients):
            try:
                c.close()
            except RuntimeError:
                pass
        self.clients.clear()
        self.wss.close()
        self.tcp.close()
        self.running = False

    def url(self):
        return f"http://{lan_ip()}:{self.http_port}/"

    # ── HTTP (serve the single page) ───────────────────────────────────────
    def _on_http_conn(self):
        sock = self.tcp.nextPendingConnection()
        if sock is None:
            return
        sock.readyRead.connect(lambda s=sock: self._on_http_ready(s))
        sock.disconnected.connect(sock.deleteLater)

    def _on_http_ready(self, sock):
        _ = sock.readAll()                       # we serve the same page for any GET
        body = INDEX_HTML.replace("__WS_PORT__", str(self.ws_port)).encode("utf-8")
        header = ("HTTP/1.1 200 OK\r\n"
                  "Content-Type: text/html; charset=utf-8\r\n"
                  f"Content-Length: {len(body)}\r\n"
                  "Cache-Control: no-store\r\n"
                  "Connection: close\r\n\r\n").encode("ascii")
        sock.write(header + body)
        sock.flush()
        sock.disconnectFromHost()

    # ── WebSocket (frames out, moves in) ───────────────────────────────────
    def _on_ws_conn(self):
        c = self.wss.nextPendingConnection()
        if c is None:
            return
        c.textMessageReceived.connect(lambda m, cc=c: self._on_ws_msg(cc, m))
        c.disconnected.connect(lambda cc=c: self._drop(cc))
        self.clients.append(c)
        c.sendTextMessage(self._state_json())    # prime the client immediately

    def _drop(self, c):
        if c in self.clients:
            self.clients.remove(c)
        try:
            c.deleteLater()
        except RuntimeError:
            pass                                 # already torn down (shutdown race)

    def _on_ws_msg(self, c, msg):
        try:
            d = json.loads(msg)
        except ValueError:
            return
        if d.get("type") == "move":
            tid, nx, ny = d.get("id"), d.get("nx"), d.get("ny")
            if tid is not None and nx is not None and ny is not None:
                try:
                    self.window.apply_token_move(str(tid), float(nx), float(ny))
                except Exception:               # never let a bad client crash the GUI
                    log.exception("apply_token_move failed")

    # ── per-tick broadcast ─────────────────────────────────────────────────
    def _tick(self):
        if not self.clients:
            return
        frame = self._grab_jpeg()
        state = self._state_json()
        for c in self.clients:
            if frame is not None:
                c.sendBinaryMessage(frame)
            c.sendTextMessage(state)

    def _player_canvas(self):
        # Treat a hidden Player Window as "no view": disabling the player view
        # hides (not deletes) the window, so without the isVisible() check the
        # stream would keep grabbing its last frame and never clear on the client.
        pw = getattr(self.window, "player_window", None)
        if pw is None or not pw.isVisible():
            return None
        return getattr(pw, "canvas_view", None)

    def _grab_jpeg(self):
        cv = self._player_canvas()
        if cv is None:
            return None
        img = cv.viewport().grab().toImage()
        if img.width() > MAX_FRAME_WIDTH:
            img = img.scaledToWidth(MAX_FRAME_WIDTH, Qt.SmoothTransformation)
        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "JPEG", JPEG_QUALITY)
        buf.close()
        return ba

    def _state_json(self):
        """Controllable, player-visible tokens as normalised viewport rects.
        `view` is False when there's no Player Window to grab."""
        cv = self._player_canvas()
        tokens = []
        if cv is not None:
            vp = cv.viewport()
            vpw = vp.width() or 1
            vph = vp.height() or 1
            for it in self.window._map_items():
                if not (getattr(it, "is_token", False)
                        and getattr(it, "player_controllable", False)
                        and getattr(it, "visible_to_player", True)):
                    continue
                r = it.sceneBoundingRect()
                tl = cv.mapFromScene(r.topLeft())
                br = cv.mapFromScene(r.bottomRight())
                tokens.append({
                    "id": self.window._token_id(it),
                    "nx": tl.x() / vpw, "ny": tl.y() / vph,
                    "nw": (br.x() - tl.x()) / vpw, "nh": (br.y() - tl.y()) / vph,
                })
        return json.dumps({"type": "state", "view": cv is not None, "tokens": tokens})


class WebShareDialog(QDialog):
    """Settings dialog: start/stop LAN sharing and show the join URL + QR code."""

    def __init__(self, mw):
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("Web Sharing (LAN)")
        self.setModal(True)
        v = QVBoxLayout(self)

        self.status = QLabel(); self.status.setWordWrap(True)
        v.addWidget(self.status)

        self.qr = QLabel(); self.qr.setAlignment(Qt.AlignCenter)
        v.addWidget(self.qr, 0, Qt.AlignCenter)

        self.url = QLabel(); self.url.setAlignment(Qt.AlignCenter)
        self.url.setTextInteractionFlags(Qt.TextSelectableByMouse)
        f = self.url.font(); f.setBold(True); f.setPointSize(f.pointSize() + 1)
        self.url.setFont(f)
        v.addWidget(self.url)

        self.note = QLabel(
            "Players on the same Wi-Fi can scan the code or type the URL. Open the "
            "Player Window (enable Playerview) so they see the map.")
        self.note.setWordWrap(True); self.note.setStyleSheet("color: gray;")
        v.addWidget(self.note)

        # ── custom port (editable only while stopped) ─────────────────────────
        prow = QHBoxLayout()
        self.custom_chk = QCheckBox("Use a custom port")
        self.custom_chk.setChecked(bool(mw.web_port_custom))
        self.custom_chk.toggled.connect(self._on_custom_toggled)
        prow.addWidget(self.custom_chk)
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1024, 65534)      # leave room for port+1
        self.port_spin.setValue(int(mw.web_port))
        self.port_spin.valueChanged.connect(self._on_port_changed)
        prow.addWidget(self.port_spin)
        prow.addStretch(1)
        v.addLayout(prow)
        self.port_hint = QLabel("Sharing uses this port and the next one.")
        self.port_hint.setStyleSheet("color: gray;")
        v.addWidget(self.port_hint)

        # ── firewall notice + help ────────────────────────────────────────────
        line = QFrame(); line.setFrameShape(QFrame.HLine); line.setFrameShadow(QFrame.Sunken)
        v.addWidget(line)
        self.fw_note = QLabel(
            "⚠ Your computer's firewall must allow these ports, or players won't be "
            "able to connect. Windows/macOS usually pop up an “Allow” prompt "
            "the first time — click Allow.")
        self.fw_note.setWordWrap(True); self.fw_note.setStyleSheet("color: #b06a00;")
        v.addWidget(self.fw_note)
        self.fw_btn = QPushButton("Firewall Help…")
        self.fw_btn.clicked.connect(self._open_firewall_help)
        v.addWidget(self.fw_btn)

        self.toggle_btn = QPushButton(); self.toggle_btn.clicked.connect(self._toggle)
        v.addWidget(self.toggle_btn)
        close = QPushButton("Close"); close.clicked.connect(self.accept)
        v.addWidget(close)

        self._refresh()

    # ── port controls ────────────────────────────────────────────────────────
    def _on_custom_toggled(self, on):
        self.mw.web_port_custom = bool(on)
        self._refresh()

    def _on_port_changed(self, val):
        self.mw.web_port = int(val)

    def _pending_ports(self):
        """The (http, ws) ports sharing will *attempt* next — the running ones if
        active, else the configured base and base+1."""
        if self.mw._web_sharing_active():
            s = self.mw.web_server
            return s.http_port, s.ws_port
        base = int(self.mw.web_port) if self.mw.web_port_custom else DEFAULT_WEB_PORT
        return base, base + 1

    # ── start/stop ───────────────────────────────────────────────────────────
    def _toggle(self):
        if self.mw._web_sharing_active():
            self.mw._stop_web_sharing()
        elif not self.mw._start_web_sharing():
            self.status.setText("<b>Couldn't start the server</b> — no free port was "
                                "found. Try a different custom port.")
            return
        self._refresh()

    def _refresh(self):
        active = self.mw._web_sharing_active()
        # Port controls only editable while stopped.
        self.custom_chk.setEnabled(not active)
        self.port_spin.setEnabled(not active and self.custom_chk.isChecked())
        http, ws = self._pending_ports()
        self.fw_note.setText(
            f"⚠ Your computer's firewall must allow TCP ports <b>{http}</b> and "
            f"<b>{ws}</b>, or players won't be able to connect. Windows/macOS usually "
            "pop up an “Allow” prompt the first time — click Allow.")
        if active:
            url = self.mw.web_server.url()
            self.status.setText("Sharing is <b>ON</b>.")
            self.url.setText(url); self.url.setVisible(True)
            pm = make_qr_pixmap(url)
            self.qr.setVisible(pm is not None)
            if pm is not None:
                self.qr.setPixmap(pm)
            self.toggle_btn.setText("Stop Sharing")
        else:
            self.status.setText("Sharing is <b>off</b>.")
            self.url.setVisible(False); self.qr.setVisible(False)
            self.toggle_btn.setText("Start Sharing")

    def _open_firewall_help(self):
        http, ws = self._pending_ports()
        FirewallHelpDialog(self, http, ws).exec()


class FirewallHelpDialog(QDialog):
    """Novice-friendly, per-platform guidance for allowing the sharing ports
    through the OS firewall: a plain-English explanation, a button that opens the
    native firewall settings, and (where relevant) a copy-paste command."""

    def __init__(self, parent, http, ws):
        super().__init__(parent)
        self.setWindowTitle("Firewall Help")
        self.setModal(True)
        v = QVBoxLayout(self)

        head = QLabel("<b>Letting players reach this computer</b>")
        v.addWidget(head)

        cmd = None
        if sys.platform.startswith("win"):
            body = (
                f"Web sharing listens on TCP ports <b>{http}</b> and <b>{ws}</b>.<br><br>"
                "The first time you start sharing, Windows normally shows "
                "<i>“Allow Arcane Atlas to communicate on these networks?”</i> — "
                "tick <b>Private networks</b> and click <b>Allow access</b>.<br><br>"
                "If players still can't connect, open Windows Defender Firewall below, "
                "choose <i>Allow an app…</i>, and enable Arcane Atlas (Private).")
            open_label = "Open Windows Firewall Settings"
        elif sys.platform == "darwin":
            body = (
                f"Web sharing listens on TCP ports <b>{http}</b> and <b>{ws}</b>.<br><br>"
                "The first time you start sharing, macOS normally asks "
                "<i>“Do you want the application to accept incoming connections?”</i> — "
                "click <b>Allow</b>.<br><br>"
                "If players still can't connect, open the Firewall settings below and "
                "either allow Arcane Atlas or turn the firewall off on your home network.")
            open_label = "Open macOS Firewall Settings"
        else:
            cmd = _linux_firewall_command(http, ws)
            body = (
                f"Allow TCP ports <b>{http}</b> and <b>{ws}</b> through your firewall so "
                "players on the same network can connect. If you use a firewall, run "
                "this in a terminal:")
            open_label = "Open Firewall Tool"

        lbl = QLabel(body); lbl.setWordWrap(True)
        lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        v.addWidget(lbl)

        if cmd is not None:
            self.cmd_field = QLineEdit(cmd); self.cmd_field.setReadOnly(True)
            self.cmd_field.setCursorPosition(0)
            v.addWidget(self.cmd_field)
            copy_btn = QPushButton("Copy Command")
            copy_btn.clicked.connect(self._copy_cmd)
            v.addWidget(copy_btn)

        note = QLabel(
            "This is a local-network feature only — no internet access or router "
            "port-forwarding is needed.")
        note.setWordWrap(True); note.setStyleSheet("color: gray;")
        v.addWidget(note)

        open_btn = QPushButton(open_label)
        open_btn.clicked.connect(self._open_settings)
        v.addWidget(open_btn)
        close = QPushButton("Close"); close.clicked.connect(self.accept)
        v.addWidget(close)

    def _copy_cmd(self):
        QGuiApplication.clipboard().setText(self.cmd_field.text())

    def _open_settings(self):
        if not _open_os_firewall_settings():
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.information(
                self, "Firewall",
                "Couldn't find a firewall tool to open automatically. Please open your "
                "system's firewall settings manually and allow the ports shown above.")
