# PyInstaller spec — build with:
#   pyinstaller packaging/akin-video-finder.spec --clean
# Produces dist/akin-video-finder.exe (console build — serves both the CLI and
# `akin-video-finder gui`). See docs/packaging.md for ffmpeg notes.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# The GUI can be launched from this exe too, so it carries the icon and
# unpacks it for QIcon the same way the GUI build does
_icon = Path("../resources/icons/akin-video-finder.ico")

a = Analysis(
    ["../src/akin_video_finder/__main__.py"],
    pathex=["../src"],
    binaries=[],
    datas=[(str(_icon), "resources/icons")],
    hiddenimports=collect_submodules("akin_video_finder"),
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="akin-video-finder",
    icon=str(_icon),
    console=True,
    upx=False,
)
