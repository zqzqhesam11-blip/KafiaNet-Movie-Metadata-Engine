"""Runtime bootstrap for the compiled KAFIA NET CONTROL PRO package."""
from __future__ import annotations

import importlib
import os
import struct
import sys
from pathlib import Path


def main() -> int:
    runtime_root = Path(r"D:\games\py")
    if os.environ.get("KAFIA_NET_CI_RUNTIME_SMOKE") == "1":
        runtime_root = Path(sys.prefix).resolve()

    if sys.version_info[:2] != (3, 14) or struct.calcsize("P") != 8:
        print(
            "[KAFIA NET CONTROL PRO] This protected build requires 64-bit Python 3.14.",
            file=sys.stderr,
        )
        return 20

    python_exe = runtime_root / "python.exe"
    if not python_exe.is_file():
        print(
            "[KAFIA NET CONTROL PRO] External runtime not found: "
            f"{python_exe}",
            file=sys.stderr,
        )
        return 21

    app_root = Path(__file__).resolve().parent
    site_packages = runtime_root / "Lib" / "site-packages"
    pyside_root = site_packages / "PySide6"

    dll_directories = (
        runtime_root,
        runtime_root / "DLLs",
        pyside_root,
        pyside_root / "Qt" / "bin",
        pyside_root / "Qt" / "lib",
        site_packages / "shiboken6",
    )
    dll_directory_handles = []
    path_entries = []
    for directory in dll_directories:
        if not directory.is_dir():
            continue
        path_entries.append(str(directory))
        if hasattr(os, "add_dll_directory"):
            try:
                dll_directory_handles.append(os.add_dll_directory(str(directory)))
            except OSError as exc:
                print("DLL DIRECTORY WARNING:", directory, exc, file=sys.stderr)

    existing_path = os.environ.get("PATH", "")
    os.environ["PATH"] = os.pathsep.join(
        path_entries + ([existing_path] if existing_path else [])
    )

    for import_path in (app_root, site_packages):
        value = str(import_path)
        if import_path.is_dir() and value not in sys.path:
            sys.path.insert(0, value)

    plugin_roots = (
        pyside_root / "plugins",
        pyside_root / "Qt" / "plugins",
    )
    for plugin_root in plugin_roots:
        platform_plugin = plugin_root / "platforms" / "qwindows.dll"
        if platform_plugin.is_file():
            os.environ["QT_PLUGIN_PATH"] = str(plugin_root)
            os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = str(
                plugin_root / "platforms"
            )
            break

    qml_roots = [
        path
        for path in (
            pyside_root / "qml",
            pyside_root / "Qt" / "qml",
        )
        if path.is_dir()
    ]
    if qml_roots:
        qml_path = os.pathsep.join(str(path) for path in qml_roots)
        os.environ["QML_IMPORT_PATH"] = qml_path
        os.environ["QML2_IMPORT_PATH"] = qml_path

    if not any(app_root.glob("app_qml*.pyd")):
        print(
            "[KAFIA NET CONTROL PRO] Compiled app_qml native module is missing.",
            file=sys.stderr,
        )
        return 22

    sys.argv = ["app_qml", *sys.argv[1:]]
    module = importlib.import_module("app_qml")
    entry_point = getattr(module, "main", None)
    if not callable(entry_point):
        print(
            "[KAFIA NET CONTROL PRO] Compiled app entry point is missing.",
            file=sys.stderr,
        )
        return 23
    return int(entry_point() or 0)


if __name__ == "__main__":
    raise SystemExit(main())
