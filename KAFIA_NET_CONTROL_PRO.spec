# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules

datas = [
    ("qml", "qml"),
    ("assets", "assets"),
    ("ui", "ui"),
    ("engine", "engine"),
    ("games.json", "."),
]

binaries = [
    ("platform-tools/adb.exe", "platform-tools"),
    ("platform-tools/AdbWinApi.dll", "platform-tools"),
    ("platform-tools/AdbWinUsbApi.dll", "platform-tools"),
]

hiddenimports = [
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
]

# Keep Python-side PySide6 submodules available without manually
# collecting a second copy of the Qt binary tree.
hiddenimports += collect_submodules("PySide6")

a = Analysis(
    ["app_qml.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="KAFIA_NET_CONTROL_PRO",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
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
    name="KAFIA_NET_CONTROL_PRO",
)
