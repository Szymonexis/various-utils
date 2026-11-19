# PyInstaller build for Windows and macOS (see README.md, "Building apps").
#   pip install -r requirements.txt -r requirements-build.txt
#   pyinstaller --noconfirm --clean audio-rec.spec
# Output: dist/audio-rec/ (Windows: audio-rec.exe inside), macOS: dist/Audio Recorder.app
# Build on each OS (and each Mac architecture) separately: PyInstaller cannot
# cross-compile and numpy/cffi wheels are single-architecture.
import sys

from PyInstaller.utils.hooks import collect_data_files

APP = "audio-rec"

a = Analysis(
    ["audio_rec.py"],
    pathex=["."],
    binaries=[],
    # the bundled ffmpeg of imageio-ffmpeg (hooks-contrib collects it too; belt and braces)
    datas=collect_data_files("imageio_ffmpeg"),
    hiddenimports=[
        "audiorec.pipewire", "audiorec.portable", "audiorec.ui", "audiorec.daemon",
        "audiorec.finalize", "imageio_ffmpeg.binaries",
    ],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,  # onedir: a detached recorder can outlive the window
    name=APP,
    console=False,  # no console window; CLI output goes to a log
    disable_windowed_traceback=True,  # a crashing background worker must exit, not show a dialog
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,  # ad-hoc signing (macOS); set a Developer ID to distribute
    entitlements_file=None,
    upx=False,
    icon=None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=APP)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Audio Recorder.app",
        icon=None,
        bundle_identifier="local.audio-rec",  # keep stable: macOS keys the mic permission on it
        version="1.0.0",
        info_plist={
            "NSMicrophoneUsageDescription":
                "Audio Recorder records your microphone together with your system audio.",
            "CFBundleShortVersionString": "1.0.0",
            "CFBundleVersion": "1.0.0",
            "LSMinimumSystemVersion": "13.0",
            "NSHighResolutionCapable": True,
        },
    )
