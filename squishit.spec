# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

project_root = Path.cwd()
ffmpeg_dir = project_root / "vendor" / "ffmpeg"
icon_path = project_root / "assets" / "squishit.ico"

binaries = []
for binary_name in ("ffmpeg.exe", "ffprobe.exe", "cjxl.exe"):
    binary_path = ffmpeg_dir / binary_name
    if binary_path.exists():
        binaries.append((str(binary_path), "vendor/ffmpeg"))

# Qt plugins come from the PySide6 hook once the shell is imported.
# CustomTkinter is not on the default import path.
datas = []
if icon_path.exists():
    datas.append((str(icon_path), "assets"))

block_cipher = None

a = Analysis(
    ["__main__.py"],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=[
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
        "pystray._win32",
        "PIL.Image",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["customtkinter", "tkinterdnd2"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="SquishIt",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    icon="assets/squishit.ico",
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SquishIt",
)
