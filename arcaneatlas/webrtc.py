"""arcaneatlas/webrtc.py — WebRTC send pipeline for Remote Play.

Streams the Player View to remote browser viewers over WebRTC, brokered by an
ArcaneServer-style signaling relay. This is the pipeline proven standalone in the
`remoteplay/` spike, moved into the app.

Threading model (mirrors transcode.py's worker pattern, but persistent):
  * aiortc needs a running asyncio loop, which must NOT be the Qt GUI thread.
    `RtcWorker` owns one dedicated thread running an asyncio loop for the
    connection's lifetime; all RTCPeerConnections + the signaling WebSocket live
    there.
  * The video source is produced on the GUI thread (grabbing the Player View has
    Qt affinity) and pushed into the loop via `push_frame()` → a single-slot
    `LatestFrameTrack`. One `MediaRelay` fans that single source out to every
    viewer, so N viewers cost one grab, not N.
  * State changes cross back to the GUI thread through `RtcSignals(QObject)`
    queued signals, so the Remote Play dialog updates without touching the worker
    thread.

Never imports `main` (one-way rule). Depends on aiortc / av / aiohttp / numpy and
PySide6.QtCore/QtGui only.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from fractions import Fraction
from urllib.parse import urlsplit

import numpy as np
from PySide6.QtCore import QObject, Qt, Signal

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from aiortc.contrib.media import MediaRelay
from av import VideoFrame

# ── quality knobs (trade framerate for resolution/sharpness) ─────────────────
# A battle map is mostly static and token motion is a browser overlay (smooth at
# any video fps), so a LOW framerate frees the encoder's bit budget for detail.
MAX_FRAME_WIDTH = 1600         # resolution cap (was 1280) — more map detail
VIDEO_FPS = 12                 # send framerate (aiortc's default track is fixed 30)
TARGET_BITRATE = 5_000_000     # lifts aiortc's artificial 1.5 Mbps VP8 ceiling

_VIDEO_CLOCK_RATE = 90000
_VIDEO_TIME_BASE = Fraction(1, _VIDEO_CLOCK_RATE)

# aiortc hard-caps VP8 at MAX_BITRATE (1.5 Mbps) and starts at DEFAULT_BITRATE
# (0.5 Mbps), which is grainy for a detailed map in motion. Raise the ceiling and
# the starting point so REMB can ramp higher on a capable link. Best-effort.
try:
    import aiortc.codecs.vpx as _vpx
    if TARGET_BITRATE > _vpx.MAX_BITRATE:
        _vpx.MAX_BITRATE = TARGET_BITRATE
    _vpx.DEFAULT_BITRATE = min(TARGET_BITRATE, max(_vpx.DEFAULT_BITRATE, 1_500_000))
except Exception:               # pragma: no cover - never fail import over a tweak
    pass


# ── address parsing ──────────────────────────────────────────────────────────
def parse_address(addr: str) -> tuple[str, str]:
    """Turn a user-typed address into (signaling_ws_url, viewer_http_url).

    Accepts 'host:port', 'ws://host:port', 'http://host:port', with or without a
    trailing path. The signaling URL always ends in '/ws'; the viewer URL is the
    page root. Examples:
        localhost:8080        -> ('ws://localhost:8080/ws',  'http://localhost:8080/')
        wss://relay.example   -> ('wss://relay.example/ws',  'https://relay.example/')
    """
    a = (addr or "").strip()
    if "://" not in a:
        a = "ws://" + a
    parts = urlsplit(a)
    scheme = parts.scheme.lower()
    ws_scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(scheme, "ws")
    http_scheme = {"ws": "http", "wss": "https"}.get(ws_scheme, "http")
    netloc = parts.netloc or parts.path        # tolerate 'host:port' with no scheme
    ws_url = f"{ws_scheme}://{netloc}/ws"
    http_url = f"{http_scheme}://{netloc}/"
    return ws_url, http_url


# ── QImage → ndarray (GUI thread) ────────────────────────────────────────────
def qimage_to_ndarray(img, max_width: int = MAX_FRAME_WIDTH) -> np.ndarray:
    """Convert a grabbed QImage to a contiguous RGB888 ndarray with EVEN W/H
    (VP8/H.264 require even dimensions), scaled down to max_width. GUI-thread only."""
    from PySide6.QtGui import QImage

    img = img.convertToFormat(QImage.Format.Format_RGB888)
    if img.width() > max_width:
        img = img.scaledToWidth(max_width, Qt.TransformationMode.SmoothTransformation)
    w, h, bpl = img.width(), img.height(), img.bytesPerLine()
    # Copy the pixels into Python-owned memory IMMEDIATELY. Do NOT keep a numpy
    # view onto the QImage's buffer — on the PySide6/shiboken stack that buffer's
    # lifetime isn't tied to the ndarray, which produced intermittent use-after-free
    # segfaults. `bytes(...)` makes an owned, immutable copy the ndarray backs onto.
    raw = bytes(memoryview(img.constBits()))
    arr = np.frombuffer(raw, np.uint8).reshape((h, bpl))[:, : w * 3].reshape((h, w, 3))
    # Crop to EVEN dims in numpy (VP8/H.264 require even W/H).
    return np.ascontiguousarray(arr[: h - (h % 2), : w - (w % 2)])


# ── the shared video source ──────────────────────────────────────────────────
class LatestFrameTrack(VideoStreamTrack):
    """Sends the most recently pushed frame, ~30fps, dropping stale frames.

    Pushing (GUI thread) never blocks: it overwrites a single slot. `recv()` (loop
    thread) always sends the newest frame — a static Player View just re-sends the
    same frame, which VP8 compresses to almost nothing."""

    def __init__(self, fps: int = VIDEO_FPS):
        super().__init__()
        self._fps = max(1, int(fps))
        self._latest: np.ndarray | None = None
        self._black = np.zeros((360, 640, 3), np.uint8)
        self._start: float | None = None
        self._ts = 0

    def set_frame(self, arr: np.ndarray) -> None:   # called on the loop thread
        self._latest = arr

    async def recv(self) -> VideoFrame:
        # Pace to self._fps — aiortc's base VideoStreamTrack is fixed at 30fps,
        # which spreads the bit budget too thin. Fewer frames → sharper frames.
        if self._start is None:
            self._start = time.monotonic()
            self._ts = 0
        else:
            self._ts += int(_VIDEO_CLOCK_RATE / self._fps)
            wait = self._start + self._ts / _VIDEO_CLOCK_RATE - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
        arr = self._latest if self._latest is not None else self._black
        frame = VideoFrame.from_ndarray(arr, format="rgb24")
        frame.pts, frame.time_base = self._ts, _VIDEO_TIME_BASE
        return frame


# ── GUI-thread-facing signals ────────────────────────────────────────────────
class RtcSignals(QObject):
    connected = Signal()          # signaling WS to the relay is open (GM registered)
    disconnected = Signal()       # worker stopped / WS closed
    error = Signal(str)           # a fatal problem (e.g. relay unreachable)
    viewers = Signal(int)         # current number of viewer peer connections
    move_received = Signal(str, float, float)   # a viewer's token drag: id, nx, ny (centre)


# ── the worker ───────────────────────────────────────────────────────────────
class RtcWorker:
    """Owns the asyncio-loop thread, the signaling client, and the peer set.

    Construct on the GUI thread (so `signals` has GUI-thread affinity). Reusable:
    start()/stop() may be called repeatedly; `signals` persists across restarts.
    """

    def __init__(self):
        self.signals = RtcSignals()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._source: LatestFrameTrack | None = None
        self._relay: MediaRelay | None = None
        self._pcs: dict[str, RTCPeerConnection] = {}
        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ws_url = ""
        self._room = ""
        self._running = False

    # -- public API (GUI thread) --
    def active(self) -> bool:
        return self._running

    def start(self, ws_url: str, room: str) -> None:
        if self._running:
            return
        self._ws_url, self._room = ws_url, room
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, name="RtcWorker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop = self._loop
        if self._running and loop is not None:
            loop.call_soon_threadsafe(self._request_stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._thread = None

    def push_frame(self, arr: np.ndarray) -> None:
        """Hand the newest Player-View frame (RGB ndarray) to the sender (non-blocking)."""
        loop, src = self._loop, self._source
        if self._running and loop is not None and src is not None:
            loop.call_soon_threadsafe(src.set_frame, arr)

    def send_json(self, obj: dict) -> None:
        """Send a JSON message over the signaling WS (call from the GUI thread).
        Used to broadcast token `state` to viewers via the relay."""
        loop = self._loop
        if self._running and loop is not None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._send(obj)))

    # -- worker thread --
    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._source = LatestFrameTrack()
        self._relay = MediaRelay()
        try:
            self._loop.run_until_complete(self._signal_client())
        except Exception as exc:                       # noqa: BLE001 — report anything
            self.signals.error.emit(str(exc))
        finally:
            try:
                self._loop.run_until_complete(self._cleanup())
            finally:
                self._loop.close()
                self._loop = None
                self._running = False
                self.signals.disconnected.emit()

    def _request_stop(self) -> None:                   # on loop thread
        if self._ws is not None and not self._ws.closed:
            asyncio.ensure_future(self._ws.close())

    async def _send(self, obj: dict) -> None:
        if self._ws is not None and not self._ws.closed:
            await self._ws.send_str(json.dumps(obj))

    def _emit_viewers(self) -> None:
        self.signals.viewers.emit(len(self._pcs))

    async def _offer_to(self, viewer_id: str) -> None:
        pc = RTCPeerConnection()                       # empty iceServers -> host candidates (LAN)
        self._pcs[viewer_id] = pc
        pc.addTrack(self._relay.subscribe(self._source))

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            if pc.connectionState in ("failed", "closed", "disconnected"):
                await pc.close()
                self._pcs.pop(viewer_id, None)
                self._emit_viewers()

        await pc.setLocalDescription(await pc.createOffer())   # gathers ICE fully
        await self._send({"type": "signal", "to": viewer_id,
                          "data": {"kind": "offer", "sdp": pc.localDescription.sdp}})
        self._emit_viewers()

    async def _handle(self, m: dict) -> None:
        t = m.get("type")
        if t == "welcome":
            for p in m.get("peers", []):
                if p.get("role") == "viewer":
                    await self._offer_to(p["id"])
        elif t == "peer-joined" and m.get("role") == "viewer":
            await self._offer_to(m["id"])
        elif t == "peer-left":
            pc = self._pcs.pop(m.get("id"), None)
            if pc is not None:
                await pc.close()
            self._emit_viewers()
        elif t == "signal" and (m.get("data") or {}).get("kind") == "answer":
            pc = self._pcs.get(m.get("from"))
            if pc is not None:
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=m["data"]["sdp"], type="answer"))
        elif t == "move":
            # a viewer dragged a token → marshal to the GUI thread (apply_token_move)
            try:
                self.signals.move_received.emit(str(m["id"]), float(m["nx"]), float(m["ny"]))
            except (KeyError, TypeError, ValueError):
                pass

    async def _signal_client(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(self._ws_url, heartbeat=30)
        except Exception as exc:                       # noqa: BLE001
            self.signals.error.emit(f"Could not reach relay: {exc}")
            return
        self.signals.connected.emit()
        await self._send({"type": "join", "room": self._room, "role": "gm"})
        async for msg in self._ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    await self._handle(json.loads(msg.data))
                except (json.JSONDecodeError, KeyError):
                    continue
            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                break

    async def _cleanup(self) -> None:
        for pc in list(self._pcs.values()):
            await pc.close()
        self._pcs.clear()
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._ws = self._session = None
