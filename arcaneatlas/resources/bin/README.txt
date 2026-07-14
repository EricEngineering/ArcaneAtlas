FFmpeg binaries for bundled video conversion
============================================

ArcaneAtlas transcodes videos that the GPU can't hardware-decode (e.g. VP8)
to H264 on import, and via Settings > "Optimize Library for Hardware…".
That needs ffmpeg + ffprobe at runtime.

Resolution order (see _ffmpeg_tool in main.py):
  1. resources/bin/ffmpeg(.exe) and ffprobe(.exe)  <-- this folder
  2. the system PATH

For a Windows release build, place these two files in THIS folder before
running build_exe.bat:
  - ffmpeg.exe
  - ffprobe.exe

Use a STATIC, self-contained LGPL build (no external DLLs needed) — e.g. the
"release-lgpl" static build from https://www.gyan.dev/ffmpeg/builds/ . LGPL
(not GPL) keeps the bundled-binary licensing clean.

The PyInstaller spec ships the whole resources/ tree, so files placed here are
automatically included in dist/ArcaneAtlas/.

On Linux/dev these are not committed — the system ffmpeg/ffprobe on PATH is used.
The .exe files are git-ignored (too large to commit); add them per build machine.
