# remoteplay — WebRTC pipeline spike (Phase 2)

This directory holds the **standalone WebRTC sender harness** for the ArcaneAtlas
remote-play work. It lives on the **`remoteplay` fork** and is **not imported by
the app** — running the normal app (`python -m arcaneatlas`) never touches any of
this. Its only purpose is to prove the WebRTC send pipeline (aiortc + PyAV on
Python 3.13) end-to-end, against the local **ArcaneServer** signaling relay,
*before* it is wired into the app in Phase 3.

## What's here

- `webrtc_sender.py` — the GM-side sender. Produces a video track from a
  `FrameSource` (a synthetic animated pattern for now) and streams it over WebRTC
  to viewers, using ArcaneServer for signaling.

The **receiver** side (browser page + headless client) and the **signaling
server** live in the sibling `ArcaneServer` repo.

## Run the spike

```bash
# 0. one-time: add the WebRTC deps to the remoteplay venv (Python 3.13)
uv pip install -r requirements.txt -r requirements-remoteplay.txt

# 1. start the signaling server (in the ArcaneServer repo)
cd ../ArcaneServer && .venv/bin/python server.py --port 8080 &

# 2. start this sender
cd ../ArcaneAtlas && .venv/bin/python remoteplay/webrtc_sender.py \
    --server ws://localhost:8080/ws --room TEST

# 3. watch it: browser -> http://localhost:8080/?room=TEST
#    or headless -> (ArcaneServer) .venv/bin/python receiver.py --room TEST
```

## The path to Phase 3 (into the app)

Two seams make the jump mechanical:

1. **`FrameSource.render()`** → replace the synthetic pattern with a grab of the
   Player View: `player_window.canvas_view.viewport().grab().toImage()` →
   RGB888 ndarray with **even width/height**. Everything downstream is unchanged.
2. **`WebRtcSender`** → move onto a dedicated asyncio-loop **worker thread**
   (`RtcWorker`, mirroring `transcode.py`'s worker), marshalling to/from the Qt
   GUI thread with queued `QObject` signals. The aiortc code itself does not change.

Token control (`apply_token_move`) stays on the signaling/control channel, fully
decoupled from this video path.
