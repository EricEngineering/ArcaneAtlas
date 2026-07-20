"""Standalone WebRTC sender harness — remote-play spike (Phase 2).

Sends a live video track to browser (or headless) viewers via an ArcaneServer
signaling relay. This is a **standalone harness**: it is NOT imported by the
ArcaneAtlas app and does not touch the local player-control path. Its job is to
prove the WebRTC send pipeline (aiortc + PyAV on Python 3.13) end-to-end before it
is wired into the app.

Design seams that carry forward to Phase 3 (`arcaneatlas/webrtc.py`):

  * `FrameSource` isolates *what* is sent. Here it is a synthetic animated pattern;
    in the app it becomes the Player-View grab (`canvas_view.viewport().grab()` ->
    even-W/H RGB ndarray). The rest of the pipeline is unchanged.
  * `WebRtcSender` owns the signaling-client + one `RTCPeerConnection` per viewer
    and the GM-offers handshake. In the app this logic lives on a dedicated
    asyncio-loop worker thread (an `RtcWorker`, mirroring `transcode.py`'s worker),
    marshalling to/from the Qt GUI thread via queued signals — but the aiortc code
    itself is identical to what is here.

Signaling uses ArcaneServer's room protocol (see ArcaneServer/README.md). The GM
offers; each viewer answers. Vanilla ICE (aiortc gathers inside setLocalDescription),
so no trickle-candidate messages are needed on a LAN.

Run (with ArcaneServer running):
    python remoteplay/webrtc_sender.py --server ws://localhost:8080/ws --room TEST
Then open  http://localhost:8080/?room=TEST  (browser) or run ArcaneServer's
receiver.py in the same room.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import time

import aiohttp
import numpy as np
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
from av import VideoFrame

WIDTH, HEIGHT = 640, 360   # even dimensions (VP8/H.264 requirement)


class FrameSource:
    """Produces RGB frames. Swap `render()` for a Player-View grab in the app.

    Must return a contiguous uint8 ndarray of shape (H, W, 3) with even H/W.
    """

    def __init__(self, width: int = WIDTH, height: int = HEIGHT):
        self.width = width
        self.height = height
        self._x = np.linspace(0, 255, width, dtype=np.float32)
        self._y = np.linspace(0, 255, height, dtype=np.float32)[:, None]

    def render(self, t: float) -> np.ndarray:
        # Scrolling gradient (unmistakably *live*) + a white block orbiting the
        # centre + a small "seconds" bar, so motion is obvious in a browser.
        shift = (t * 60.0) % 256.0
        r = ((self._x[None, :] + shift) % 256).astype(np.uint8)
        r = np.broadcast_to(r, (self.height, self.width))
        g = ((self._y + shift * 0.5) % 256).astype(np.uint8)
        g = np.broadcast_to(g, (self.height, self.width))
        b = np.full((self.height, self.width),
                    int((math.sin(t) * 0.5 + 0.5) * 255), np.uint8)
        frame = np.dstack([r, g, b]).copy()   # contiguous

        cx = int(self.width / 2 + math.cos(t * 1.5) * self.width * 0.35)
        cy = int(self.height / 2 + math.sin(t * 1.5) * self.height * 0.35)
        s = 36
        frame[max(0, cy - s):cy + s, max(0, cx - s):cx + s] = (255, 255, 255)
        return frame


class SyntheticVideoTrack(VideoStreamTrack):
    """Wraps a FrameSource as an aiortc video track (paces itself ~30 fps)."""

    def __init__(self, source: FrameSource):
        super().__init__()
        self.source = source
        self._start = time.monotonic()

    async def recv(self) -> VideoFrame:
        pts, time_base = await self.next_timestamp()   # aiortc paces to ~30fps
        arr = self.source.render(time.monotonic() - self._start)
        frame = VideoFrame.from_ndarray(arr, format="rgb24")
        frame.pts, frame.time_base = pts, time_base
        return frame


class WebRtcSender:
    """Signaling client + one RTCPeerConnection per viewer; GM-offers handshake."""

    def __init__(self, server_url: str, room: str, source: FrameSource):
        self.server_url = server_url
        self.room = room
        self.source = source
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.pcs: dict[str, RTCPeerConnection] = {}

    async def _send(self, obj: dict) -> None:
        assert self.ws is not None
        await self.ws.send_str(json.dumps(obj))

    async def _offer_to(self, viewer_id: str) -> None:
        pc = RTCPeerConnection()               # iceServers default -> host candidates on LAN
        self.pcs[viewer_id] = pc
        pc.addTrack(SyntheticVideoTrack(self.source))

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            print(f"[sender] pc[{viewer_id}] {pc.connectionState}")
            if pc.connectionState in ("failed", "closed"):
                await pc.close()
                self.pcs.pop(viewer_id, None)

        await pc.setLocalDescription(await pc.createOffer())   # gathers ICE fully
        await self._send({"type": "signal", "to": viewer_id,
                          "data": {"kind": "offer", "sdp": pc.localDescription.sdp}})
        print(f"[sender] offered to viewer {viewer_id}")

    async def _on_answer(self, viewer_id: str, sdp: str) -> None:
        pc = self.pcs.get(viewer_id)
        if pc:
            await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="answer"))

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self.server_url) as ws:
                self.ws = ws
                await self._send({"type": "join", "room": self.room, "role": "gm"})
                print(f"[sender] joined room '{self.room}' on {self.server_url} as GM")
                try:
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            continue
                        m = json.loads(msg.data)
                        t = m.get("type")
                        if t == "welcome":
                            for p in m.get("peers", []):
                                if p["role"] == "viewer":
                                    await self._offer_to(p["id"])
                        elif t == "peer-joined" and m.get("role") == "viewer":
                            await self._offer_to(m["id"])
                        elif t == "peer-left":
                            pc = self.pcs.pop(m["id"], None)
                            if pc:
                                await pc.close()
                        elif t == "signal" and (m.get("data") or {}).get("kind") == "answer":
                            await self._on_answer(m["from"], m["data"]["sdp"])
                finally:
                    # copy: pc.close() fires connectionstatechange -> pops self.pcs
                    for pc in list(self.pcs.values()):
                        await pc.close()
                    self.pcs.clear()


def main() -> None:
    ap = argparse.ArgumentParser(description="Remote-play WebRTC sender harness (spike)")
    ap.add_argument("--server", default="ws://localhost:8080/ws")
    ap.add_argument("--room", default="TEST")
    args = ap.parse_args()
    sender = WebRtcSender(args.server, args.room, FrameSource())
    try:
        asyncio.run(sender.run())
    except KeyboardInterrupt:
        print("\n[sender] stopped")


if __name__ == "__main__":
    main()
