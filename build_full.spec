# Self-contained PyInstaller spec. Python, PySide6 and Qt runtime files are collected by PyInstaller.
# UI/QML/images and game metadata are compiled into ui_resources_rc before this spec runs.
# Do not copy source folders (engine/, ui/, qml/, assets/) as loose data files.

import os

resource_build_dir = os.path.abspath("build")

datas = [
    ("platform-tools", "platform-tools"),
    ("build/android_support", "android_support"),
]

hiddenimports = [
    "PySide6",
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "ui_resources_rc",
]

a = Analysis(
    ["app_qml.py"],
    pathex=[".", resource_build_dir],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)


# Keep all Qt/PySide6 QML modules and translations. The released app uses
# Qt Quick Controls and we explicitly favor UI completeness over a smaller
# installer; missing QML modules can leave a window technically loaded but
# with controls/pages absent at runtime.

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
)

bundle = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="KAFIA_NET_CONTROL_PRO",
)
