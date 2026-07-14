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

"""Video probing and transcoding helpers (ffmpeg/ffprobe).

Pure module — no dependency on MainWindow, Canvas, or the graphics items — so it
can be imported anywhere without circular-import risk. MainWindow drives these
from a worker thread via its _run_transcode() (see main.py); the functions here
just locate the tools, probe the source, and run ffmpeg with progress/cancel.
"""
import sys, os, shutil, subprocess
from pathlib import Path
from PySide6.QtCore import QObject, Signal

# ── Video hardware-decode helpers ──────────────────────────────────────────
# Codecs modern GPUs decode in hardware. Anything else (notably VP8) falls back
# to CPU decoding in Qt's FFmpeg backend, which is expensive when several videos
# play at once — so on import we transcode those to H264.
HW_VIDEO_CODECS = {"h264", "hevc", "vp9", "av1"}

# Keep ffmpeg/ffprobe from flashing a console window in the windowed Windows build.
_SUBPROCESS_FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

def _ffmpeg_tool(name):
    """Locate a bundled or system ffmpeg/ffprobe binary; returns a path or None.

    Bundled tools live in resources/bin (shipped via the resources Tree in the
    PyInstaller spec); dev/Linux falls back to the system copy on PATH."""
    exe = name + (".exe" if sys.platform.startswith("win") else "")
    candidates = []
    if hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / "arcaneatlas" / "resources" / "bin" / exe)
        candidates.append(Path(sys._MEIPASS) / "bin" / exe)
    candidates.append(Path(__file__).resolve().parent / "resources" / "bin" / exe)
    for c in candidates:
        if c.exists():
            return str(c)
    return shutil.which(name)

def probe_video_codec(path):
    """Return the lowercase video codec name (e.g. 'vp8', 'h264'), or None."""
    ffprobe = _ffmpeg_tool("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30, creationflags=_SUBPROCESS_FLAGS)
        return (out.stdout.strip().lower() or None)
    except Exception:
        return None

def _video_duration_s(path):
    ffprobe = _ffmpeg_tool("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path], capture_output=True, text=True, timeout=30,
            creationflags=_SUBPROCESS_FLAGS)
        return float(out.stdout.strip() or 0.0)
    except Exception:
        return 0.0

def _run_ffmpeg(cmd, dst, dur, progress_cb=None, cancel_cb=None):
    """Run an ffmpeg `cmd` (which must include `-progress pipe:1 -nostats`),
    reporting progress and honoring cancellation. Shared by the transcoders so
    they differ only in their codec args. Returns True iff dst was written.

    Designed to run on a worker thread (the read on proc.stdout blocks, which is
    exactly why the GUI froze when this ran inline). progress_cb/cancel_cb are
    invoked from this thread, so callers marshal back to the GUI thread."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True,
                                creationflags=_SUBPROCESS_FLAGS)
        for line in proc.stdout:
            if cancel_cb and cancel_cb():
                proc.kill()
                break
            if progress_cb and dur > 0 and line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                    progress_cb(max(0.0, min(1.0, (us / 1e6) / dur)))
                except ValueError:
                    pass
        proc.wait()
        ok = (proc.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0)
        if not ok and os.path.exists(dst):
            os.remove(dst)
        return ok
    except Exception:
        if os.path.exists(dst):
            try:
                os.remove(dst)
            except OSError:
                pass
        return False

def transcode_to_h264(src, dst, progress_cb=None, cancel_cb=None):
    """Transcode src -> dst as H264/yuv420p mp4 (a hardware-decodable format).

    progress_cb(fraction 0..1) is called as it runs. cancel_cb() is polled while
    running — if it returns True, ffmpeg is killed and the partial file removed.
    Returns True on success (False on failure or cancellation)."""
    ffmpeg = _ffmpeg_tool("ffmpeg")
    if not ffmpeg:
        return False
    dur = _video_duration_s(src)
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", src,
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-preset", "veryfast", "-c:a", "aac", "-movflags", "+faststart",
           "-progress", "pipe:1", "-nostats", dst]
    return _run_ffmpeg(cmd, dst, dur, progress_cb, cancel_cb)

# ── Transparent animated objects (animated WebP) ───────────────────────────
# Qt's FFmpeg video backend (QMediaPlayer/QGraphicsVideoItem) DROPS the alpha
# channel of WebM (VP8/VP9) videos — it decodes to YUV420P/NV12 and the player
# renders an opaque rectangle. The only pipeline in this Qt build that composites
# per-pixel alpha is the *image* pipeline (QMovie/QImageReader). So transparent
# animated *objects* are converted to animated WebP on import and rendered by
# AnimatedItem via QMovie, never by the video player.
#
# Pixel formats that carry an alpha plane (used to decide if a source is
# "transparent" and therefore an animated-object candidate).
ALPHA_PIX_FMTS = {
    "yuva420p", "yuva422p", "yuva444p", "yuva420p10le", "yuva444p10le",
    "rgba", "bgra", "argb", "abgr", "ya8", "ya16", "gbrap", "pal8",
}

def video_has_alpha(path):
    """True if the video carries transparency. Checks the stream pixel format
    for an alpha plane AND the WebM `alpha_mode` tag — VP8/VP9 store alpha in a
    container side-channel, so the stream pix_fmt reads as plain yuv420p while
    `alpha_mode=1` flags the hidden alpha (see the import notes above)."""
    ffprobe = _ffmpeg_tool("ffprobe")
    if not ffprobe:
        return False
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=pix_fmt:stream_tags=alpha_mode",
             "-of", "default=noprint_wrappers=1", path],
            capture_output=True, text=True, timeout=30,
            creationflags=_SUBPROCESS_FLAGS).stdout.lower()
    except Exception:
        return False
    pix_fmt = None
    alpha_mode = None
    for line in out.splitlines():
        if line.startswith("pix_fmt="):
            pix_fmt = line.split("=", 1)[1].strip()
        elif line.startswith("tag:alpha_mode="):
            alpha_mode = line.split("=", 1)[1].strip()
    return (pix_fmt in ALPHA_PIX_FMTS) or (alpha_mode == "1")

def transcode_to_animated_webp(src, dst, src_codec=None, progress_cb=None, cancel_cb=None):
    """Transcode src -> dst as an animated WebP that preserves alpha.

    src_codec (from probe_video_codec) selects the decoder: ffmpeg's native VP8/
    VP9 decoders silently DROP the WebM alpha side-channel, so VP8/VP9 sources
    must be decoded with libvpx/libvpx-vp9 to recover it. Other codecs (qtrle,
    prores, png, …) carry alpha in the frame and decode fine by default.
    progress_cb(fraction 0..1) and cancel_cb() behave as in _run_ffmpeg.

    NOTE: the animated WebP muxer buffers the whole clip and reports progress
    only once, at the very end — so this path can't drive a real progress bar
    (MainWindow._run_transcode falls back to a pulse animation for it)."""
    ffmpeg = _ffmpeg_tool("ffmpeg")
    if not ffmpeg:
        return False
    dur = _video_duration_s(src)
    pre = []
    if src_codec == "vp8":
        pre = ["-c:v", "libvpx"]
    elif src_codec == "vp9":
        pre = ["-c:v", "libvpx-vp9"]
    # NOTE: use libwebp_anim, NOT libwebp. The plain libwebp encoder blends each
    # frame onto the previous canvas (transparent pixels don't clear it), so a
    # moving/fading animation leaves a trail of every prior frame. libwebp_anim
    # handles per-frame disposal so frames replace rather than accumulate.
    cmd = [ffmpeg, "-y", "-loglevel", "error", *pre, "-i", src,
           "-an", "-c:v", "libwebp_anim", "-pix_fmt", "yuva420p", "-loop", "0",
           "-lossless", "0", "-q:v", "70", "-compression_level", "4",
           "-progress", "pipe:1", "-nostats", dst]
    return _run_ffmpeg(cmd, dst, dur, progress_cb, cancel_cb)

class _TranscodeSignals(QObject):
    """Thread→GUI bridge for background transcodes. A worker thread emits these;
    Qt delivers them on the GUI thread (queued), so the progress dialog and the
    waiting event loop are only ever touched from the main thread."""
    progress = Signal(int)     # 0..100
    done     = Signal(bool)    # success
