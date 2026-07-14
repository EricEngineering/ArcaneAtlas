# arcaneatlas.spec
import os, sys
from pathlib import Path
from PyInstaller.building.build_main import Analysis, PYZ, EXE, COLLECT
from PyInstaller.building.datastruct import Tree
from PyInstaller.utils.hooks import collect_submodules

APP_NAME = "ArcaneAtlas"
ENTRY = "arcaneatlas/main.py"   # runs main() via if __name__ == "__main__"

hiddenimports = collect_submodules("arcaneatlas")

# Optional icon (.ico) for the EXE (keep .png in resources for runtime)
ico_path = Path("arcaneatlas/resources/icon.ico")
icon_arg = str(ico_path) if ico_path.exists() else None

# macOS code signing (single source of truth = the CI secrets). When
# MACOS_SIGN_IDENTITY is set (only in the signed CI path), PyInstaller signs
# every collected binary with a hardened runtime + our entitlements during the
# build. Empty/unset (local dev, unsigned CI) → codesign_identity=None → an
# ordinary unsigned build. Non-darwin always None (codesign is macOS-only).
_is_mac = sys.platform == "darwin"
codesign_identity = (os.environ.get("MACOS_SIGN_IDENTITY") or None) if _is_mac else None
entitlements_file = "packaging/entitlements.mac.plist" if codesign_identity else None
# Build a universal2 (arm64 + x86_64) macOS binary so the app runs on both Apple
# Silicon and Intel Macs. Requires a universal2 Python + universal2 wheels + a fat
# ffmpeg (CI provides all three). None elsewhere (Windows/Linux are single-arch).
target_arch = "universal2" if _is_mac else None

a = Analysis(
    [ENTRY],
    pathex=[],
    binaries=[],
    datas=[],                       # <-- IMPORTANT: leave datas empty here
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                   # flip False for release
    icon=icon_arg,                  # remove this line if you don't have an .ico
    codesign_identity=codesign_identity,   # macOS signing; None elsewhere
    entitlements_file=entitlements_file,
    target_arch=target_arch,               # 'universal2' on macOS, None elsewhere
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    Tree("arcaneatlas/resources", prefix="arcaneatlas/resources"),  # <-- Tree goes here
    strip=False,
    upx=True,
    name=APP_NAME,
)

# macOS: also wrap the collected app in a .app bundle so it can be shipped in a
# .dmg (double-clickable). Platform-guarded, so Windows/Linux builds are
# unaffected. See CLAUDE.md → "Release automation (GitHub Actions)".
if _is_mac:
    from PyInstaller.building.osx import BUNDLE
    icns_path = Path("arcaneatlas/resources/icon.icns")
    app = BUNDLE(
        coll,
        name=f"{APP_NAME}.app",
        icon=str(icns_path) if icns_path.exists() else None,
        bundle_identifier="org.arcanetools.arcaneatlas",
    )
