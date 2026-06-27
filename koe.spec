# Koe.spec — PyInstaller build for the local voice-dictation app.
#
#   Build (inside the .venv):
#       .\.venv\Scripts\pyinstaller.exe koe.spec --noconfirm
#
# Produces dist\Koe\  — a self-contained folder (Koe.exe + DLLs, ~1.5 GB with
# the CUDA libraries). Zip that folder for distribution. The Whisper model is
# NOT bundled; it downloads once on first run and is then fully offline.

import os
import sys
import glob
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# Packages that ship native DLLs / data PyInstaller won't find on its own.
#  - ctranslate2 / faster_whisper : the Whisper inference backend
#  - onnxruntime                  : Silero VAD inside faster-whisper
#  - sounddevice                  : bundles the PortAudio DLL via CFFI
#  - pystray / PIL                : tray icon
#  - uiautomation / comtypes      : context grounding (focused-window read)
#  - keyboard / pyperclip         : hotkeys + clipboard injection
for pkg in (
    "ctranslate2", "faster_whisper", "onnxruntime", "sounddevice",
    "pystray", "PIL", "uiautomation", "comtypes", "keyboard", "pyperclip",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:  # a missing optional package shouldn't kill the build
        print(f"[koe.spec] collect_all({pkg}) skipped: {e}")

# NVIDIA CUDA runtime DLLs (cuBLAS + cuDNN) shipped as pip wheels under
# <venv>/Lib/site-packages/nvidia/**/bin/*.dll. CTranslate2 needs these at run
# time for the GPU path; place them next to Koe.exe so they're on the DLL path.
_nvidia_root = os.path.join(sys.prefix, "Lib", "site-packages", "nvidia")
_cuda_dlls = glob.glob(os.path.join(_nvidia_root, "**", "bin", "*.dll"), recursive=True)
for _dll in _cuda_dlls:
    binaries.append((_dll, "."))
print(f"[koe.spec] bundling {len(_cuda_dlls)} CUDA DLL(s) from {_nvidia_root}")

# Ship a starter dictionary example next to the app (the real dictionary.txt is
# created on first run; we never bundle personal data).
if os.path.exists("dictionary.txt.example"):
    datas.append(("dictionary.txt.example", "."))

block_cipher = None

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter.test", "test", "pytest"],
    cipher=block_cipher,
    noarchive=False,
)

# De-duplicate the CUDA DLLs. PyInstaller's nvidia hooks ALSO collect cuBLAS /
# cuDNN into _internal\nvidia\**\bin\, but CTranslate2's loader only searches its
# own package dir + the bundle root (sys._MEIPASS) — never those subdirs. So our
# root-level copies (added to `binaries` above) are the load-bearing ones; the
# nvidia\ copies are dead weight. Dropping them saves ~0.7 GB.
_before = len(a.binaries)
a.binaries = [b for b in a.binaries
              if not b[0].lower().replace("\\", "/").startswith("nvidia/")]
print(f"[koe.spec] dropped {_before - len(a.binaries)} duplicate nvidia DLL(s)")

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Koe",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                # UPX can corrupt CUDA DLLs — leave off
    console=True,             # v1: keep the console so model-load progress is visible
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="Koe",
)
