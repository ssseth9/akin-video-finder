# PyInstaller spec for the double-clickable GUI build:
#   pyinstaller packaging/akin-video-finder-gui.spec --clean
# Produces dist/akin-video-finder-gui.exe — a windowed app that needs no
# console and no --config flag. See docs/packaging.md.

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

# Bundled ffmpeg/ffprobe — drop the binaries into packaging/bin/ before
# building; the app prefers them over PATH (see loader._resolve_tool)
_bin = Path("bin")
binaries = [
    (str(_bin / f"{t}.exe"), "bin")
    for t in ("ffmpeg", "ffprobe")
    if (_bin / f"{t}.exe").exists()
]

# The window/taskbar icon. Embedded in the exe (so Explorer draws it) and
# also unpacked as data, so QIcon can pick the size the title bar asks for.
_icon = Path("../resources/icons/akin-video-finder.ico")
# The scan-completion chime plays through winsound, so it ships beside the
# exe rather than being generated at runtime on every launch.
_sound = Path("../resources/sounds/scan-complete.wav")

a = Analysis(
    ["../src/akin_video_finder/gui_entry.py"],
    pathex=["../src"],
    binaries=binaries,
    datas=[(str(_icon), "resources/icons"), (str(_sound), "resources/sounds")],
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
    name="akin-video-finder-gui",
    icon=str(_icon),
    console=False,
    upx=False,
)
