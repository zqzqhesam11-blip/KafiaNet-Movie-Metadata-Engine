import sys
import os
import json
import time
import hmac
import hashlib
import io
import re
import struct
import threading
import secrets
import zipfile
import logging
import tempfile
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path

# Keep Arabic diagnostics from crashing when launched with a legacy Windows
# console code page. Packaged GUI builds may have no console streams at all.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass

os.environ["QT_QUICK_CONTROLS_STYLE"] = "Fusion"

from PySide6.QtCore import (
    QObject, Signal, Slot, Property, QTimer, QUrl,
    QEvent, Qt, QPointF, QtMsgType, qInstallMessageHandler,
)
from PySide6.QtGui import QDesktopServices, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QFileDialog
from PySide6.QtQml import QQmlApplicationEngine


# ---------------------------------------------------------------------------
# Persistent diagnostics for packaged GUI builds.
# stdout/stderr are often unavailable for PyInstaller windowed applications,
# so keep the existing print-based diagnostics and Qt/QML messages in a
# rotating UTF-8 file under the user's LocalAppData directory.
# ---------------------------------------------------------------------------

_DIAGNOSTIC_LOGGER = logging.getLogger("cn_playhub")
_DIAGNOSTIC_LOG_PATH = None
_DIAGNOSTIC_HANDLER = None


class _LineLoggingStream:
    """Mirror a text stream while writing complete lines to the app log."""

    def __init__(self, logger, level, prefix, original=None):
        self._logger = logger
        self._level = level
        self._prefix = prefix
        self._original = original
        self._buffer = ""
        self._lock = threading.RLock()
        self.encoding = "utf-8"
        self.errors = "replace"

    def write(self, text):
        if text is None:
            return 0
        text = str(text)
        if not text:
            return 0

        with self._lock:
            if self._original is not None:
                try:
                    self._original.write(text)
                except (OSError, ValueError):
                    pass

            self._buffer += text
            while "\n" in self._buffer:
                line, self._buffer = self._buffer.split("\n", 1)
                line = line.rstrip("\r")
                if line:
                    self._logger.log(self._level, "%s%s", self._prefix, line)
        return len(text)

    def flush(self):
        with self._lock:
            pending = self._buffer.rstrip("\r")
            self._buffer = ""
            if pending:
                self._logger.log(
                    self._level, "%s%s", self._prefix, pending
                )
            if self._original is not None:
                try:
                    self._original.flush()
                except (OSError, ValueError):
                    pass

    def isatty(self):
        return False

    def fileno(self):
        if self._original is None:
            raise OSError("No console file descriptor is available")
        return self._original.fileno()

    def writable(self):
        return True


def _qt_diagnostic_message_handler(mode, context, message):
    """Persist Qt, Qt Quick, and QML console messages with source location."""
    level = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }.get(mode, logging.INFO)

    category = "default"
    source = ""
    try:
        if context is not None:
            category = getattr(context, "category", None) or "default"
            source_file = getattr(context, "file", None)
            source_line = getattr(context, "line", 0)
            source_function = getattr(context, "function", None)
            if source_file:
                source = f" [{source_file}:{source_line}"
                if source_function:
                    source += f" {source_function}"
                source += "]"
    except Exception:
        source = ""

    _DIAGNOSTIC_LOGGER.log(
        level, "QT[%s]%s %s", category, source, str(message)
    )


def _configure_diagnostics():
    """Enable persistent diagnostics without requiring a console window."""
    global _DIAGNOSTIC_LOG_PATH, _DIAGNOSTIC_HANDLER

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        log_root = Path(local_app_data) / "CN PlayHub" / "logs"
    else:
        log_root = Path.home() / "AppData" / "Local" / "CN PlayHub" / "logs"

    try:
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / "cn-playhub-diagnostic.log"
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
            delay=False,
        )
    except OSError:
        log_root = Path(tempfile.gettempdir()) / "CN PlayHub" / "logs"
        log_root.mkdir(parents=True, exist_ok=True)
        log_path = log_root / "cn-playhub-diagnostic.log"
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5 * 1024 * 1024,
            backupCount=4,
            encoding="utf-8",
            delay=False,
        )

    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s "
        "[%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(handler)

    _DIAGNOSTIC_LOGGER.setLevel(logging.DEBUG)
    _DIAGNOSTIC_LOGGER.propagate = True
    _DIAGNOSTIC_LOG_PATH = log_path
    _DIAGNOSTIC_HANDLER = handler

    original_stdout = sys.stdout
    original_stderr = sys.stderr
    sys.stdout = _LineLoggingStream(
        _DIAGNOSTIC_LOGGER, logging.INFO, "PYTHON | ", original_stdout
    )
    sys.stderr = _LineLoggingStream(
        _DIAGNOSTIC_LOGGER, logging.ERROR, "PYTHON STDERR | ", original_stderr
    )

    qInstallMessageHandler(_qt_diagnostic_message_handler)

    def _uncaught_exception(exc_type, exc_value, exc_traceback):
        _DIAGNOSTIC_LOGGER.critical(
            "UNCAUGHT PYTHON EXCEPTION",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    sys.excepthook = _uncaught_exception

    if hasattr(threading, "excepthook"):
        def _uncaught_thread_exception(args):
            _DIAGNOSTIC_LOGGER.critical(
                "UNCAUGHT THREAD EXCEPTION in %s",
                getattr(args.thread, "name", "unknown"),
                exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
            )
        threading.excepthook = _uncaught_thread_exception

    _DIAGNOSTIC_LOGGER.info(
        "DIAGNOSTICS STARTED | log_path=%s | capture=Python stdout/stderr, "
        "Qt/QML messages, uncaught exceptions, UI mouse clicks",
        log_path,
    )
    return log_path


class _UiInteractionLogger(QObject):
    """Log clicked QML control identity without recording typed field values."""

    _SENSITIVE_TEXT_TYPES = (
        "TextField", "TextInput", "TextArea", "Password", "LineEdit"
    )

    def _deepest_item_at(self, root_item, x, y):
        current = root_item
        point = QPointF(float(x), float(y))
        for _ in range(16):
            try:
                child = current.childAt(point.x(), point.y())
            except (AttributeError, RuntimeError, TypeError):
                break
            if child is None or child is current:
                break
            try:
                point = child.mapFromItem(current, point)
            except (AttributeError, RuntimeError, TypeError):
                pass
            current = child
        return current

    def _describe_target(self, item):
        parts = []
        current = item
        for _ in range(8):
            if current is None:
                break
            try:
                class_name = current.metaObject().className()
            except (AttributeError, RuntimeError):
                class_name = type(current).__name__

            details = [str(class_name)]
            try:
                object_name = current.objectName()
                if object_name:
                    details.append(f"name={object_name!r}")
            except (AttributeError, RuntimeError):
                pass

            is_sensitive_control = any(
                token.casefold() in str(class_name).casefold()
                for token in self._SENSITIVE_TEXT_TYPES
            )
            if not is_sensitive_control:
                for property_name in ("text", "title", "accessibleName"):
                    try:
                        value = current.property(property_name)
                    except (AttributeError, RuntimeError):
                        value = None
                    if isinstance(value, str) and value.strip():
                        safe_value = value.replace("\r", " ").replace("\n", " ")
                        details.append(f"{property_name}={safe_value[:100]!r}")

            parts.append(" ".join(details))
            try:
                current = current.parentItem()
            except (AttributeError, RuntimeError):
                current = None

        return " <- ".join(parts) if parts else "target=unresolved"

    def eventFilter(self, watched, event):
        try:
            if event.type() == QEvent.Type.MouseButtonRelease:
                button = event.button()
                position = event.position()
                target_description = "window target unavailable"

                content_item_getter = getattr(watched, "contentItem", None)
                if callable(content_item_getter):
                    content_item = content_item_getter()
                    if content_item is not None:
                        target = self._deepest_item_at(
                            content_item, position.x(), position.y()
                        )
                        target_description = self._describe_target(target)

                _DIAGNOSTIC_LOGGER.info(
                    "UI_CLICK | button=%s | x=%.1f y=%.1f | target=%s",
                    button, position.x(), position.y(), target_description,
                )
        except Exception:
            _DIAGNOSTIC_LOGGER.exception("UI interaction event logging failed")

        return False


# CI/runtime smoke test: importing QtWidgets alone is insufficient because
# Qt's platform plugins are loaded when QApplication is constructed.
# Build verification invokes this mode against the frozen executable.
if "--qt-import-smoke-test" in sys.argv:
    pyside_version = getattr(__import__("PySide6"), "__version__", "0.0.0")
    print("PYTHON_RUNTIME:", sys.executable)
    print("PYSIDE6_VERSION:", pyside_version)
    try:
        pyside_parts = tuple(int(part) for part in pyside_version.split(".")[:3])
    except (TypeError, ValueError):
        pyside_parts = (0, 0, 0)
    if pyside_parts < (6, 10, 1):
        raise RuntimeError(
            "Python 3.14 requires PySide6 6.10.1 or newer; "
            f"found {pyside_version}."
        )
    app = QApplication([])
    print("QTWIDGETS_QAPPLICATION_SMOKE_TEST: PASS")
    sys.exit(0)

from engine.transfer_engine import TransferEngine
from engine.adb_engine import ADBEngine
from engine.efootball_saf_engine import EFootballSAFEngine
from engine.path_manager import PathManager
from engine.game_config import (
    get_game,
    find_game_folder,
    discover_dashboard_games,
)
from engine.windows_fullscreen import WindowsFullscreenProtection
from engine.windows_storage import enumerate_removable_storage_devices


BASE_DIR = Path(__file__).resolve().parent
VERSIONED_GAME_NAMES = ("eFootball", "Grand Theft Auto")


# ?????? Android ???????? ???????
ADB_PACKAGE_IDS = {
    "Free Fire": "com.dts.freefireth",
    "Fortnite": "com.epicgames.fortnite",
    "Genshin Impact": "com.miHoYo.GenshinImpact",
    "eFootball": "com.ziru.official",
    "PUBG Mobile": "com.tencent.ig",
    "PUBG Mobile Flash": "com.tencent.igfit",
    "Call of Duty Mobile": "com.activision.callofduty.shooter",
}

ADB_GAME_DESTINATIONS = {
    "Fortnite": "/sdcard/Android/data/com.epicgames.fortnite/",
    "Genshin Impact": "/sdcard/Android/data/com.miHoYo.GenshinImpact/",
    "eFootball": "/sdcard/Android/data/com.konami.pesam/",
    "Free Fire": "/sdcard/Android/data/com.dts.freefireth/",
    "PUBG Mobile": "/sdcard/Android/data/com.tencent.ig/files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved/",
    "PUBG Mobile Flash": "/storage/emulated/0/Android/data/com.tencent.igfit/files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved",
    "Call of Duty Mobile": "/sdcard/Android/data/com.activision.callofduty.shooter/",
}

# ????? ?????? ??????? ??? ?????? ???? Python ??? ?? ?????
# ?????? PowerShell.
def _arabic_name(codepoints):
    return "".join(chr(x) for x in codepoints)


SERVER_GAMES_FOLDER = _arabic_name([
    0x627, 0x644, 0x639, 0x627, 0x628, 0x64a
])


def build_server_source_root():
    return Path(r"\\SERVER06\c$") / SERVER_GAMES_FOLDER


def build_usb_source_root():
    return Path(r"D:\games")


PRE_COPY_APPS_ROOT = Path(r"D:\games\تطبيقات للنسخ")


def _normalize_apk_filename(value):
    return "".join(character for character in str(value).casefold() if character.isalnum())


def _find_pre_copy_apk(app_key, default_filename):
    """Resolve an app APK from the dedicated pre-copy apps folder."""
    root = PRE_COPY_APPS_ROOT
    preferred = root / default_filename
    if preferred.is_file():
        return preferred
    if not root.is_dir():
        return preferred

    matches = []
    for candidate in root.rglob("*.apk"):
        if not candidate.is_file():
            continue
        normalized = _normalize_apk_filename(candidate.stem)
        if app_key not in normalized:
            continue
        if "kafianetshizukubridge" in normalized:
            continue
        try:
            modified = candidate.stat().st_mtime_ns
        except OSError:
            modified = 0
        matches.append((modified, str(candidate).casefold(), candidate))

    if not matches:
        return preferred
    matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return matches[0][2]


def build_mt_manager_apk_path():
    return _find_pre_copy_apk("mtmanager", "MT Manager.apk")


def build_shizuku_apk_path():
    return BASE_DIR / "android_support" / "Shizuku.apk"


def build_shizuku_helper_apk_path():
    return BASE_DIR / "android_support" / "CNPlayHubShizukuBridge.apk"


def build_efootball_saf_helper_apk_path():
    # Backward-compatible alias: eFootball now uses the same unified helper APK.
    return build_shizuku_helper_apk_path()


def _read_axml_package_name(manifest_bytes):
    """Read the package attribute from a binary AndroidManifest.xml (AXML)."""
    if not manifest_bytes:
        return ""

    # Keep the text-XML branch useful for test fixtures and unusual APK tooling.
    text_head = manifest_bytes[:4096].decode("utf-8", errors="ignore")
    text_match = re.search(
        r"<manifest\b[^>]*\bpackage\s*=\s*['\"]([^'\"]+)['\"]",
        text_head,
        flags=re.IGNORECASE | re.DOTALL
    )
    if text_match:
        return text_match.group(1).strip()

    strings = []

    def read_u8_length(data, position):
        first = data[position]
        position += 1
        if first & 0x80:
            if position >= len(data):
                raise ValueError("Truncated UTF-8 AXML length")
            length = ((first & 0x7F) << 8) | data[position]
            position += 1
            return length, position
        return first, position

    def read_u16_length(data, position):
        first = struct.unpack_from("<H", data, position)[0]
        position += 2
        if first & 0x8000:
            second = struct.unpack_from("<H", data, position)[0]
            position += 2
            return ((first & 0x7FFF) << 16) | second, position
        return first, position

    def parse_string_pool(offset, header_size, chunk_size):
        count = struct.unpack_from("<I", manifest_bytes, offset + 8)[0]
        flags = struct.unpack_from("<I", manifest_bytes, offset + 16)[0]
        strings_start = struct.unpack_from("<I", manifest_bytes, offset + 20)[0]
        if count > 100000 or header_size < 28 or chunk_size < header_size:
            raise ValueError("Invalid AXML string pool")
        offsets_start = offset + header_size
        string_data_start = offset + strings_start
        utf8 = bool(flags & 0x100)
        parsed = []
        for index in range(count):
            relative = struct.unpack_from("<I", manifest_bytes, offsets_start + index * 4)[0]
            position = string_data_start + relative
            if position >= offset + chunk_size:
                raise ValueError("AXML string offset outside its chunk")
            if utf8:
                _, position = read_u8_length(manifest_bytes, position)
                byte_length, position = read_u8_length(manifest_bytes, position)
                value = manifest_bytes[position:position + byte_length].decode(
                    "utf-8", errors="replace"
                )
            else:
                char_length, position = read_u16_length(manifest_bytes, position)
                byte_length = char_length * 2
                value = manifest_bytes[position:position + byte_length].decode(
                    "utf-16le", errors="replace"
                )
            parsed.append(value)
        return parsed

    def string_at(index):
        if index in (0xFFFFFFFF, -1) or index < 0 or index >= len(strings):
            return ""
        return strings[index]

    try:
        if len(manifest_bytes) < 8:
            return ""
        declared_size = struct.unpack_from("<I", manifest_bytes, 4)[0]
        limit = min(len(manifest_bytes), declared_size or len(manifest_bytes))
        offset = struct.unpack_from("<H", manifest_bytes, 2)[0]
        while offset + 8 <= limit:
            chunk_type, header_size, chunk_size = struct.unpack_from(
                "<HHI", manifest_bytes, offset
            )
            if header_size < 8 or chunk_size < header_size or offset + chunk_size > limit:
                break

            if chunk_type == 0x0001:
                strings = parse_string_pool(offset, header_size, chunk_size)

            elif chunk_type == 0x0102 and strings:
                # ResXMLTree_node is 16 bytes; the element extension follows it.
                extension = offset + 16
                if extension + 20 > offset + chunk_size:
                    offset += chunk_size
                    continue
                element_name = string_at(
                    struct.unpack_from("<I", manifest_bytes, extension + 4)[0]
                )
                if element_name == "manifest":
                    attribute_start = struct.unpack_from(
                        "<H", manifest_bytes, extension + 8
                    )[0]
                    attribute_size = struct.unpack_from(
                        "<H", manifest_bytes, extension + 10
                    )[0]
                    attribute_count = struct.unpack_from(
                        "<H", manifest_bytes, extension + 12
                    )[0]
                    if attribute_size < 20 or attribute_count > 4096:
                        return ""
                    attributes = extension + attribute_start
                    for index in range(attribute_count):
                        item = attributes + index * attribute_size
                        if item + 20 > offset + chunk_size:
                            break
                        name_index = struct.unpack_from("<I", manifest_bytes, item + 4)[0]
                        if string_at(name_index) != "package":
                            continue
                        raw_value = struct.unpack_from("<I", manifest_bytes, item + 8)[0]
                        if raw_value != 0xFFFFFFFF:
                            value = string_at(raw_value)
                        else:
                            value_type = manifest_bytes[item + 15]
                            value_data = struct.unpack_from(
                                "<I", manifest_bytes, item + 16
                            )[0]
                            value = string_at(value_data) if value_type == 0x03 else ""
                        if value and re.fullmatch(
                            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+",
                            value
                        ):
                            return value
            offset += chunk_size
    except (IndexError, struct.error, UnicodeError, ValueError):
        return ""
    return ""


def _extract_apk_package_name(apk_path):
    """Read the package id from APK/XAPK/APKS metadata without installing it."""
    path = Path(apk_path)
    if not path.is_file():
        return ""

    def package_from_apk_bytes(payload):
        try:
            with zipfile.ZipFile(io.BytesIO(payload), "r") as nested:
                manifest = nested.read("AndroidManifest.xml")
        except (OSError, KeyError, zipfile.BadZipFile):
            return ""
        return _read_axml_package_name(manifest)

    try:
        if path.suffix.casefold() == ".apk":
            with zipfile.ZipFile(path, "r") as archive:
                return _read_axml_package_name(archive.read("AndroidManifest.xml"))

        with zipfile.ZipFile(path, "r") as archive:
            if path.suffix.casefold() == ".xapk":
                try:
                    metadata = json.loads(archive.read("manifest.json").decode("utf-8-sig"))
                    package_name = str(metadata.get("package_name") or "").strip()
                    if package_name:
                        return package_name
                except (KeyError, ValueError, UnicodeError, AttributeError):
                    pass

            nested_apks = sorted(
                name for name in archive.namelist()
                if name.casefold().endswith(".apk")
                and not name.replace("\\", "/").startswith("../")
            )
            preferred = sorted(
                nested_apks,
                key=lambda name: (
                    0 if Path(name).name.casefold() in ("base.apk", "base-master.apk") else 1,
                    len(name),
                    name.casefold()
                )
            )
            for name in preferred:
                package_name = package_from_apk_bytes(archive.read(name))
                if package_name:
                    return package_name
    except (OSError, KeyError, zipfile.BadZipFile, ValueError):
        return ""
    return ""


class AppController(QObject):

    messageChanged = Signal(str)
    transferPercentChanged = Signal(int)
    transferSpeedChanged = Signal(str)
    transferEtaChanged = Signal(str)
    transferFinished = Signal(bool, str)
    operationStateChanged = Signal(str)
    transferPausedChanged = Signal(bool)
    operationRejected = Signal(str)
    transferBatchChanged = Signal(list)
    deviceProgressChanged = Signal(str, int, str, str, str, str, str)
    operationFinished = Signal(str)
    androidDataFallbackRequested = Signal(str, str, str, str)
    busyChanged = Signal(bool)
    usbChanged = Signal(str)
    destinationChanged = Signal(str)
    adbDevicesChanged = Signal(list)
    adbDiscoveryCompleted = Signal(list)
    transferHistoryChanged = Signal(list)
    transferSummaryChanged = Signal(dict)
    pubgSourceSizeChanged = Signal(str)
    selectedGameVersionChanged = Signal(str, str)

    def __init__(self):
        super().__init__()

        self.paths = PathManager()

        # ?????? ??????: ????? ??????
        self.engine = TransferEngine()

        # ???? ADB: ????? ???????? ??? ??????
        self.adb_engine = ADBEngine()

        # Native taskbar/shortcut protection is active only during the
        # password-protected fullscreen lock.
        self._windows_fullscreen_protection = WindowsFullscreenProtection()
        self._fullscreen_taskbar_timer = QTimer(self)
        self._fullscreen_taskbar_timer.setInterval(100)
        self._fullscreen_taskbar_timer.timeout.connect(
            self._windows_fullscreen_protection.enforce_locked_desktop
        )

        self.adb_engine.progressChanged.connect(
            self._progress
        )

        self.adb_engine.statusChanged.connect(
            self._transfer_status
        )

        self.adb_engine.logChanged.connect(
            self._transfer_log
        )

        self.adb_engine.statusChanged.connect(
            self._status
        )

        self.adb_engine.finished.connect(
            self._finished
        )

        self.engine.progressChanged.connect(
            self._progress
        )

        self.engine.statusChanged.connect(
            self._status
        )

        self.engine.finished.connect(
            self._finished
        )

        self.source_root = build_server_source_root()

        self.destination_root = Path(
            r"D:\games"
        )

        self._current_game = ""

        # ???? ADB ??????
        self.selected_device = ""
        self.selected_devices = []
        self._active_operations = {}
        self._operation_state = "IDLE"
        self._operation_busy = False
        self._transfer_paused = False
        self._device_info = {}

        self._last_adb_devices = []
        self._last_usb_devices = []
        self._usb_device_signature = ()
        self._usb_discovery_timer = QTimer(self)
        self._usb_discovery_timer.setInterval(1800)
        self._usb_discovery_timer.timeout.connect(self.refreshUsbStorageDevices)
        self._last_usb_devices = enumerate_removable_storage_devices()
        self._usb_device_signature = None
        self._usb_discovery_timer.start()

        self.current_source = "WIRELESS"
        self.custom_source_path = ""
        self.install_source_path = str(Path(r"D:\games") / "تثبيت")
        self._pubg_source_size = "Saved"
        self._selected_game_versions = {}
        self._oem_compatibility_checks_enabled = False
        self._app_data_dir = (
            Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
            / "KafiaNetControlPro"
        )
        self._settings_file = self._app_data_dir / "settings.json"
        self._history_file = self._app_data_dir / "transfer_history.json"
        self._transfer_history = []
        self._device_first_seen = {}
        self._load_persistent_state()
        self._adb_discovery_lock = threading.Lock()
        self._adb_discovery_thread = None
        self.adbDiscoveryCompleted.connect(self._apply_adb_devices)
        print("CONTROLLER READY")
        print("SOURCE:", self.source_root)
        print("DEST:", self.destination_root)

    def _load_persistent_state(self):
        """Load source selection, first-seen device timestamps and transfer history."""
        try:
            if self._settings_file.is_file():
                settings = json.loads(self._settings_file.read_text(encoding="utf-8"))
                self.custom_source_path = str(settings.get("custom_source_path") or "")
                self.install_source_path = str(
                    settings.get("install_source_path") or Path(r"D:\games") / "تثبيت"
                )
                saved_pubg_size = str(settings.get("pubg_source_size") or "Saved")
                if saved_pubg_size in ("Saved", "Medium", "Low"):
                    self._pubg_source_size = saved_pubg_size
                saved_versions = settings.get("selected_game_versions") or {}
                if isinstance(saved_versions, dict):
                    self._selected_game_versions = {
                        str(name): str(version)
                        for name, version in saved_versions.items()
                        if name in VERSIONED_GAME_NAMES and str(version).strip()
                    }
                self._oem_compatibility_checks_enabled = bool(
                    settings.get("oem_compatibility_checks_enabled", False)
                )
                saved_seen = settings.get("device_first_seen") or {}
                self._device_first_seen = {
                    str(serial): float(timestamp)
                    for serial, timestamp in saved_seen.items()
                    if str(serial) and isinstance(timestamp, (int, float))
                }
                source = str(settings.get("source") or "WIRELESS").upper()
                if source == "USB":
                    self.current_source = "USB"
                    self.source_root = build_usb_source_root()
                elif source == "CUSTOM" and self.custom_source_path:
                    self.current_source = "CUSTOM"
                    self.source_root = Path(self.custom_source_path)
                else:
                    self.current_source = "WIRELESS"
                    self.source_root = build_server_source_root()
        except (OSError, ValueError, TypeError) as exc:
            print("SETTINGS LOAD WARNING:", exc)

        try:
            if self._history_file.is_file():
                payload = json.loads(self._history_file.read_text(encoding="utf-8"))
                if isinstance(payload, list):
                    self._transfer_history = [
                        item for item in payload if isinstance(item, dict)
                    ]
        except (OSError, ValueError, TypeError) as exc:
            print("TRANSFER HISTORY LOAD WARNING:", exc)

        if self._migrate_transfer_history():
            self._save_transfer_history()

    def _migrate_transfer_history(self):
        """Normalize legacy history so installs and duplicate copies are not counted as new successes."""
        changed = False
        completed_keys = set()
        pending_failures = {}

        for entry in self._transfer_history:
            kind = str(entry.get("kind") or "")
            status = str(entry.get("status") or "")

            if kind == "تثبيت":
                if status == "اكتمل":
                    entry["status"] = "تم التثبيت"
                    changed = True
                elif status == "غير مكتمل":
                    entry["status"] = "فشل التثبيت"
                    changed = True
                continue

            if kind != "نسخ":
                continue

            # USB history has its own file counters; never merge its rows
            # into the one-success-per-phone/game migration logic.
            if str(entry.get("target_type") or "android") == "usb_storage":
                continue

            serial = str(entry.get("serial") or "")
            game = str(entry.get("game") or "")
            if not serial or not game:
                # Never merge entries when a stable device identity is unavailable.
                key = None
            else:
                key = (serial, game)

            message = str(entry.get("raw_message") or entry.get("message") or "")
            if status == "غير مكتمل" and any(
                marker in message.casefold()
                for marker in ("cancelled", "canceled", "تم إلغاء", "أُلغي", "ملغي")
            ):
                entry["status"] = "ملغي"
                changed = True
                continue

            if status in ("فشل", "غير مكتمل", "مساحة غير كافية"):
                if self._is_insufficient_storage_failure(message) and status != "مساحة غير كافية":
                    entry["status"] = "مساحة غير كافية"
                    changed = True
                if key is not None:
                    pending_failures.setdefault(key, []).append(entry)
                continue

            if status in ("اكتمل", "مراجعة"):
                if key is None:
                    continue

                already_completed = key in completed_keys
                if status == "اكتمل" and already_completed:
                    entry["status"] = "مراجعة"
                    changed = True
                completed_keys.add(key)

                # A later successful copy resolves all earlier failed attempts
                # for this same phone/game, but does not erase their history rows.
                unresolved = pending_failures.pop(key, [])
                for failed_entry in unresolved:
                    failed_entry["status"] = "تمت المعالجة"
                    failed_entry["resolved"] = True
                    failed_entry["resolved_at"] = entry.get("date", "")
                    failed_entry["resolved_by"] = entry.get("id", "")
                    changed = True

        return changed

    def _save_settings(self):
        try:
            self._app_data_dir.mkdir(parents=True, exist_ok=True)
            payload = {
                "source": self.current_source,
                "custom_source_path": self.custom_source_path,
                "install_source_path": self.install_source_path,
                "pubg_source_size": self._pubg_source_size,
                "selected_game_versions": self._selected_game_versions,
                "oem_compatibility_checks_enabled": self._oem_compatibility_checks_enabled,
                "device_first_seen": self._device_first_seen,
            }
            temporary = self._settings_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            temporary.replace(self._settings_file)
        except OSError as exc:
            print("SETTINGS SAVE WARNING:", exc)

    def _save_transfer_history(self):
        try:
            self._app_data_dir.mkdir(parents=True, exist_ok=True)
            temporary = self._history_file.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(self._transfer_history, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            temporary.replace(self._history_file)
        except OSError as exc:
            print("TRANSFER HISTORY SAVE WARNING:", exc)
            self.messageChanged.emit("تعذر حفظ سجل النقل على هذا الكمبيوتر.")

    @Slot(result=str)
    def getPubgSourceSize(self):
        return self._pubg_source_size

    @Slot(str, result=bool)
    def setPubgSourceSize(self, value):
        selected = str(value or "").strip()
        if selected not in ("Saved", "Medium", "Low"):
            self.messageChanged.emit("حجم PUBG المحدد غير معروف.")
            return False
        if selected != self._pubg_source_size:
            self._pubg_source_size = selected
            self._save_settings()
            self.pubgSourceSizeChanged.emit(selected)
            labels = {"Saved": "كامل", "Medium": "متوسط", "Low": "منخفض"}
            self.messageChanged.emit("تم اختيار حجم PUBG: " + labels[selected])
        return True


    def _find_version_apk(self, version_root):
        if not version_root or not Path(version_root).is_dir():
            return None
        try:
            candidates = [
                item for item in Path(version_root).iterdir()
                if item.is_file() and item.suffix.casefold() in (".apk", ".apks", ".xapk")
            ]
        except OSError:
            return None
        priority = {".apk": 0, ".xapk": 1, ".apks": 2}
        candidates.sort(
            key=lambda item: (
                priority.get(item.suffix.casefold(), 9),
                item.name.casefold()
            )
        )
        return candidates[0] if candidates else None

    def _package_id_for_version(self, game_name, version_root):
        if game_name == "eFootball":
            return ADB_PACKAGE_IDS.get("eFootball", "")
        apk = self._find_version_apk(version_root)
        return _extract_apk_package_name(apk) if apk else ""

    def _resolve_selected_game_version(self, game_name, game_root, notify=True):
        if game_name not in VERSIONED_GAME_NAMES:
            return game_root
        selected_name = str(self._selected_game_versions.get(game_name) or "").strip()
        if not selected_name:
            if notify:
                self.messageChanged.emit(
                    "افتح بطاقة " + game_name + " واختر الإصدار قبل تنفيذ العملية."
                )
            return None
        selected_folder = self._find_case_insensitive_child_directory(
            Path(game_root), selected_name
        ) if game_root else None
        if selected_folder is None:
            if notify:
                self.messageChanged.emit(
                    "الإصدار المحدد لم يعد موجودًا في مجلد اللعبة: " + selected_name
                )
            return None
        return selected_folder

    def _discovered_folder_package_id(self, game_name):
        """Resolve an unconfigured card's app ID from APK metadata in its own folder."""
        try:
            source_root = Path(self.source_root).resolve()
            game_root = find_game_folder(source_root, str(game_name))
            if game_root is None:
                self.messageChanged.emit("لم يتم العثور على مجلد اللعبة: " + str(game_name))
                return ""
            game_root = Path(game_root).resolve(strict=True)
            game_root.relative_to(source_root)
            if game_root == source_root or not game_root.is_dir():
                raise ValueError("Invalid discovered game folder.")
        except (OSError, RuntimeError, ValueError, TypeError):
            self.messageChanged.emit(
                "مجلد اللعبة غير صالح أو خارج مصدر الألعاب الحالي: " + str(game_name)
            )
            return ""

        try:
            apk_files = sorted(
                (
                    item for item in game_root.rglob("*")
                    if item.is_file()
                    and item.suffix.casefold() in (".apk", ".xapk", ".apks")
                ),
                key=lambda item: (
                    0 if item.suffix.casefold() == ".apk" else
                    1 if item.suffix.casefold() == ".xapk" else 2,
                    str(item.relative_to(game_root)).casefold(),
                ),
            )
        except OSError:
            apk_files = []

        package_ids = []
        for apk_file in apk_files:
            package_id = _extract_apk_package_name(apk_file)
            if package_id and package_id not in package_ids:
                package_ids.append(package_id)
                if len(package_ids) > 1:
                    break

        if len(package_ids) == 1:
            return package_ids[0]
        if len(package_ids) > 1:
            self.messageChanged.emit(
                "يحتوي المجلد " + str(game_name)
                + " على أكثر من Package ID مختلف؛ لن أخمّن أي تطبيق للتشغيل أو الإغلاق أو الإزالة."
            )
            return ""

        self.messageChanged.emit(
            "تعذر تحديد Package ID للمجلد " + str(game_name)
            + ": لم يتم العثور على APK صالح بداخله."
        )
        return ""

    def _selected_game_package_id(self, game_name):
        if game_name not in VERSIONED_GAME_NAMES:
            configured_package_id = ADB_PACKAGE_IDS.get(game_name, "")
            if configured_package_id:
                return configured_package_id
            return self._discovered_folder_package_id(game_name)

        try:
            game_root = find_game_folder(self.source_root, game_name)
        except (OSError, RuntimeError):
            game_root = None
        if game_root is None:
            self.messageChanged.emit("لم يتم العثور على مجلد اللعبة: " + game_name)
            return ""
        version_root = self._resolve_selected_game_version(game_name, game_root)
        if version_root is None:
            return ""
        package_id = self._package_id_for_version(game_name, version_root)
        if not package_id:
            self.messageChanged.emit(
                "تعذر تحديد Package ID الحقيقي للإصدار "
                + version_root.name
                + " من ملف APK؛ لن أنفذ عملية على مسار تخميني."
            )
        return package_id

    @Slot(str, result=list)
    def getGameVersions(self, game_name):
        game_name = str(game_name or "")
        if game_name not in VERSIONED_GAME_NAMES:
            return []
        try:
            game_root = find_game_folder(self.source_root, game_name)
            if game_root is None:
                return []
            folders = [item for item in game_root.iterdir() if item.is_dir()]
        except OSError as exc:
            print("GAME VERSION DISCOVERY FAILED:", game_name, exc)
            return []

        def version_sort_key(folder):
            years = re.findall(r"(?:19|20)\d{2}", folder.name)
            return (int(years[-1]) if years else -1, folder.name.casefold())

        result = []
        for folder in sorted(folders, key=version_sort_key, reverse=True):
            try:
                children = list(folder.iterdir())
            except OSError:
                children = []
            apk = self._find_version_apk(folder)
            image_candidates = sorted(
                (
                    item for item in children
                    if item.is_file()
                    and item.suffix.casefold() in (".jpg", ".jpeg", ".png", ".webp")
                ),
                key=lambda item: (
                    0 if item.name.casefold() in ("folder.jpg", "folder.jpeg", "folder.png") else 1,
                    item.name.casefold()
                )
            )
            image_path = image_candidates[0] if image_candidates else None
            data_folder = self._find_case_insensitive_child_directory(folder, "data")
            obb_folder = self._find_case_insensitive_child_directory(folder, "obb")
            package_id = self._package_id_for_version(game_name, folder)
            result.append({
                "name": folder.name,
                "path": str(folder),
                "image": QUrl.fromLocalFile(str(image_path)).toString() if image_path else "",
                "apkName": apk.name if apk else "",
                "hasApk": bool(apk),
                "hasData": data_folder is not None,
                "hasObb": obb_folder is not None,
                "packageId": package_id,
            })
        return result

    @Slot(str, result=str)
    def getSelectedGameVersion(self, game_name):
        return str(self._selected_game_versions.get(str(game_name or "")) or "")

    @Slot(str, str, result=bool)
    def selectGameVersion(self, game_name, version_name):
        game_name = str(game_name or "")
        version_name = str(version_name or "").strip()
        if game_name not in VERSIONED_GAME_NAMES or not version_name:
            self.messageChanged.emit("اختيار اللعبة أو الإصدار غير صالح.")
            return False
        game_root = find_game_folder(self.source_root, game_name)
        version_root = self._find_case_insensitive_child_directory(
            game_root, version_name
        ) if game_root else None
        if version_root is None:
            self.messageChanged.emit(
                "لم يتم العثور على الإصدار المطلوب داخل مكتبة اللعبة: " + version_name
            )
            return False
        selected_name = version_root.name
        self._selected_game_versions[game_name] = selected_name
        self._save_settings()
        self.selectedGameVersionChanged.emit(game_name, selected_name)
        self.messageChanged.emit(
            "تم تحديد " + game_name + " — الإصدار " + selected_name
        )
        return True

    @Slot(result=bool)
    def getOemCompatibilityChecksEnabled(self):
        return bool(self._oem_compatibility_checks_enabled)

    @Slot(bool, result=bool)
    def setOemCompatibilityChecksEnabled(self, enabled):
        selected = bool(enabled)
        if selected != self._oem_compatibility_checks_enabled:
            self._oem_compatibility_checks_enabled = selected
            self._save_settings()
            self.messageChanged.emit(
                "تم تفعيل فحص توافق الشركات الحقيقي."
                if selected
                else "تم تعطيل فحص توافق الشركات."
            )
        return True

    @Slot(str, result="QVariantMap")
    def inspectDeviceCompatibility(self, serial):
        if not self._oem_compatibility_checks_enabled:
            return {
                "ok": False,
                "message": "فحص توافق الشركات معطل من الإعدادات.",
            }

        serial = str(serial or "").strip()
        if not serial:
            return {
                "ok": False,
                "message": "حدد هاتف Android متصلاً أولاً.",
            }

        inspect = getattr(self.adb_engine, "inspect_device_compatibility", None)
        if not callable(inspect):
            return {
                "ok": False,
                "message": "أداة فحص ADB الفعلية غير متاحة في هذا الإصدار.",
            }

        try:
            result = dict(inspect(serial) or {})
        except Exception as exc:
            return {
                "ok": False,
                "message": "تعذر إكمال فحص ADB: " + str(exc),
            }

        if result.get("ok"):
            info = dict(self._device_info.get(serial, {}))
            info.update({
                "serial": serial,
                "manufacturer": str(result.get("manufacturer") or info.get("manufacturer") or ""),
                "model": str(result.get("model") or info.get("model") or ""),
                "name": str(result.get("model") or info.get("name") or serial),
            })
            self._device_info[serial] = info
        return result

    def _source_label(self):
        return {
            "WIRELESS": "LAN",
            "USB": "HDD",
            "CUSTOM": "مخصص"
        }.get(self.current_source, self.current_source)

    @Slot(result="QVariantMap")
    def getSourceState(self):
        return {
            "source": self.current_source,
            "path": str(self.source_root),
            "exists": bool(str(self.source_root)),
            "customPath": self.custom_source_path,
        }

    @Slot(result=list)
    def getDashboardGames(self):
        """Return the fixed cards and all currently discovered source folders."""
        return discover_dashboard_games(
            self.source_root,
            flash_source_root=build_usb_source_root(),
        )

    @Slot(str, result=bool)
    def openDashboardFolder(self, source_path):
        """Open only a real folder that belongs to the active source root."""
        try:
            root = Path(self.source_root).resolve()
            source = Path(str(source_path or "")).resolve(strict=True)
            source.relative_to(root)
            if source == root or not source.is_dir():
                raise ValueError("Not a child directory.")
        except (OSError, ValueError, TypeError):
            self.messageChanged.emit("مسار المجلد غير صالح أو خارج مصدر الألعاب المحدد.")
            return False
        return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(source))))

    @Slot(str, str, str, result=bool)
    def copyDiscoveredFolder(self, source_path, game_name, android_destination):
        """Copy a discovered folder to selected USB devices or an explicit Android path."""
        name = str(game_name or "").strip()
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            self.messageChanged.emit("اسم المجلد غير صالح.")
            return False

        try:
            source_root = Path(self.source_root).resolve()
            source = Path(str(source_path or "")).resolve(strict=True)
            source.relative_to(source_root)
            if source == source_root or not source.is_dir() or source.name != name:
                raise ValueError("Source/name mismatch.")
        except (OSError, ValueError, TypeError):
            self.messageChanged.emit("مجلد اللعبة غير موجود داخل مصدر الألعاب الحالي.")
            return False

        target_serials = list(self.selected_devices)
        if not target_serials and self.selected_device:
            target_serials = [self.selected_device]
        if not target_serials:
            self._reject_operation("حدد هاتفًا أو فلاشة قبل نسخ المجلد المكتشف.")
            return False

        remote_destination = str(android_destination or "").strip().replace("\\", "/")
        android_targets = [
            serial for serial in target_serials
            if self._device_info.get(serial, {}).get("type") != "usb_storage"
        ]
        if android_targets:
            unsafe_characters = any(
                ord(character) < 32
                or character in {chr(34), chr(39), chr(96), "$", ";", "&", "|", "<", ">", "*", "?"}
                for character in remote_destination
            )
            remote_parts = remote_destination.rstrip("/").split("/")
            if (
                not remote_destination.startswith("/sdcard/")
                or len(remote_parts) < 3
                or any(part in ("", ".", "..") for part in remote_parts[1:])
                or unsafe_characters
            ):
                self.messageChanged.emit(
                    "أدخل وجهة Android صريحة تبدأ بـ /sdcard/ ولا تحتوي على . أو .. "
                    "أو رموز غير مسموحة."
                )
                return False
            remote_destination = remote_destination.rstrip("/") + "/"

        live_usb = {}
        if any(
            self._device_info.get(serial, {}).get("type") == "usb_storage"
            for serial in target_serials
        ):
            try:
                live_usb = {
                    str(item.get("serial") or ""): item
                    for item in enumerate_removable_storage_devices()
                }
            except Exception as exc:
                print("DISCOVERED FOLDER USB VERIFY FAILED:", exc)

        started = 0
        for serial in target_serials:
            device_info = self._device_info.get(serial, {})
            target_type = str(device_info.get("type") or "android")
            device_name = str(
                device_info.get("display_name")
                or device_info.get("name")
                or device_info.get("model")
                or serial
            )
            existing = self._active_operations.get(serial)
            if existing and not existing.get("completed", False):
                self.messageChanged.emit("الجهاز لديه عملية نقل قيد التنفيذ: " + serial)
                continue

            if target_type == "usb_storage":
                live_device = live_usb.get(serial)
                if live_device is None:
                    self.messageChanged.emit("الفلاشة غير متصلة أو تغيرت وحدتها: " + device_name)
                    continue
                target_root = Path(str(live_device.get("root") or ""))
                if not target_root.is_dir():
                    self.messageChanged.emit("مسار الفلاشة غير متاح: " + str(target_root))
                    continue
                source_drive = os.path.splitdrive(os.path.abspath(str(source)))[0].casefold()
                target_drive = os.path.splitdrive(os.path.abspath(str(target_root)))[0].casefold()
                if source_drive and source_drive == target_drive:
                    self.messageChanged.emit(
                        "المصدر والفلاشة على نفس وحدة التخزين؛ اختر مصدرًا من قرص آخر."
                    )
                    continue
                destination = target_root / "CN PlayHub" / name
            else:
                destination = remote_destination

            operation = {
                "serial": serial,
                "model": device_name,
                "device_name": device_name,
                "target_type": target_type,
                "units": 1,
                "unit_price": 500,
                "billing_value": 500,
                "game": name,
                "game_version": "",
                "kind": "copy",
                "source": str(source),
                "destination": str(destination),
                "status": "جاري التجهيز",
                "percent": 0,
                "operation": "جاري تجهيز المجلد المكتشف للنسخ",
                "speed": "--",
                "eta": "--",
                "error": "",
                "completed": False,
                "cancelled": False,
                "history_recorded": False,
                "source_label": self._source_label(),
                "source_root": str(source_root),
                "target_package": "",
                "shizuku_fallback_attempted": False,
                "phase": "local_usb_copy" if target_type == "usb_storage" else "adb_direct",
            }
            self._start_operation(operation)
            started += 1

        if not started:
            self._reject_operation("لم يبدأ النسخ؛ تحقق من اتصال الجهاز أو انشغال العملية.")
            return False

        self.messageChanged.emit("بدأ نسخ المجلد المكتشف: " + name)
        return True

    @Slot(result=bool)
    def chooseCustomSource(self):
        preferred_directory = (
            Path(self.custom_source_path)
            if self.custom_source_path
            else build_usb_source_root()
        )
        start_directory = (
            str(preferred_directory)
            if preferred_directory.is_dir()
            else str(Path.home())
        )
        selected = QFileDialog.getExistingDirectory(
            None,
            "اختيار مجلد الألعاب",
            start_directory
        )
        if not selected:
            return False

        source = Path(selected)
        if not source.is_dir():
            self.messageChanged.emit("المجلد المحدد غير متاح.")
            return False

        self.current_source = "CUSTOM"
        self.custom_source_path = str(source)
        self.source_root = source
        self._save_settings()
        self.messageChanged.emit(f"تم اختيار مصدر الألعاب المخصص: {source}")
        print("SOURCE SELECTED: CUSTOM", self.source_root)
        return True

    @Slot(result=str)
    def getInstallSourcePath(self):
        return str(self.install_source_path)

    def _collect_install_batch(self):
        """Recursively collect APK/APKS/XAPK packages, grouping nested paths by game folder."""
        source_root = Path(self.install_source_path)
        if not source_root.is_dir():
            return []

        entries = []
        wrapper_names = {
            "apk", "apks", "install", "installer", "packages", "package",
            "files", "build", "release", "releases", "android", "data",
            "obb", "base", "universal", "arm64-v8a", "armeabi-v7a",
            "armeabi", "x86", "x86_64", "تثبيت", "ملفات", "ملفات التثبيت",
            "الحزم", "نسخة", "النسخة", "الاصدار", "الإصدار",
        }

        def add_group(label, apk_files, game_key):
            apk_files = sorted(
                {
                    Path(item)
                    for item in apk_files
                    if Path(item).is_file()
                    and Path(item).suffix.lower() in (".apk", ".apks", ".xapk")
                },
                key=lambda item: str(item).casefold()
            )
            if not apk_files:
                return

            xapk_bundles = [item for item in apk_files if item.suffix.lower() == ".xapk"]
            if xapk_bundles:
                for bundle in xapk_bundles:
                    entries.append({
                        "name": label if len(xapk_bundles) == 1 else label + " - " + bundle.stem,
                        "type": "xapk",
                        "path": str(bundle),
                        "fileCount": 1,
                        "gameKey": game_key,
                        "gameName": label,
                    })
                return

            bundles = [item for item in apk_files if item.suffix.lower() == ".apks"]
            if bundles:
                for bundle in bundles:
                    entries.append({
                        "name": label if len(bundles) == 1 else label + " - " + bundle.stem,
                        "type": "apks",
                        "path": str(bundle),
                        "fileCount": 1,
                        "gameKey": game_key,
                        "gameName": label,
                    })
                return

            base_apks = [
                item for item in apk_files
                if item.name.casefold() == "base.apk"
            ]
            split_apks = [
                item for item in apk_files
                if item.name.casefold().startswith("split_")
                or item.name.casefold().startswith("config.")
            ]
            if base_apks and len(apk_files) > 1 and split_apks:
                entries.append({
                    "name": label,
                    "type": "apk_group",
                    "paths": [str(item) for item in apk_files],
                    "fileCount": len(apk_files),
                    "gameKey": game_key,
                    "gameName": label,
                })
            else:
                for apk in apk_files:
                    entries.append({
                        "name": label if len(apk_files) == 1 else label + " - " + apk.stem,
                        "type": "apk",
                        "path": str(apk),
                        "fileCount": 1,
                        "gameKey": game_key,
                        "gameName": label,
                    })

        try:
            directories = [source_root]
            directories.extend(
                item for item in source_root.rglob("*")
                if item.is_dir()
            )
            directories.sort(
                key=lambda item: str(item.relative_to(source_root)).casefold()
                if item != source_root else ""
            )

            root_packages = [
                item for item in source_root.iterdir()
                if item.is_file() and item.suffix.lower() in (".apk", ".apks", ".xapk")
            ]
            if root_packages:
                base_apks = [
                    item for item in root_packages
                    if item.name.casefold() == "base.apk"
                ]
                split_apks = [
                    item for item in root_packages
                    if item.name.casefold().startswith("split_")
                    or item.name.casefold().startswith("config.")
                ]
                if base_apks and split_apks:
                    add_group(
                        "ملفات جذر المصدر",
                        root_packages,
                        "root:" + str(source_root.resolve()),
                    )
                else:
                    for package in root_packages:
                        add_group(
                            package.stem,
                            [package],
                            "root:" + str(package.resolve()),
                        )

            for directory in directories:
                if directory == source_root:
                    continue
                direct_packages = [
                    item for item in directory.iterdir()
                    if item.is_file() and item.suffix.lower() in (".apk", ".apks", ".xapk")
                ]
                if not direct_packages:
                    continue

                relative_parts = list(directory.relative_to(source_root).parts)
                game_parts = list(relative_parts)
                while len(game_parts) > 1 and game_parts[-1].casefold() in wrapper_names:
                    game_parts.pop()

                if not game_parts:
                    game_parts = relative_parts
                game_root = source_root.joinpath(*game_parts)
                game_key = str(game_root.resolve())
                label = " / ".join(game_parts[-2:]) if game_parts else directory.name
                add_group(label, direct_packages, game_key)

        except OSError as exc:
            print("INSTALL SOURCE SCAN WARNING:", exc)
            return []

        entries.sort(key=lambda item: str(item.get("name") or "").casefold())
        return entries

    @Slot(result=list)
    def previewInstallBatch(self):
        return [
            {
                "name": str(item.get("name") or ""),
                "fileCount": int(item.get("fileCount") or 1),
                "type": str(item.get("type") or "apk"),
                "gameKey": str(item.get("gameKey") or ""),
                "gameName": str(item.get("gameName") or item.get("name") or ""),
            }
            for item in self._collect_install_batch()
        ]

    @staticmethod
    def _normalize_install_amount(amount_text):
        """Parse Western or Arabic-Indic whole-number amounts."""
        normalized = str(amount_text or "").translate(str.maketrans(
            "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹",
            "01234567890123456789"
        ))
        normalized = normalized.replace(",", "").replace("٬", "").strip()
        return int(normalized) if normalized.isdecimal() else None

    @staticmethod
    def _game_count_for_amount(amount):
        """Convert currency to the configured game entitlement, never 1:1."""
        if amount is None or amount < 100:
            return 0
        return 10 + ((int(amount) - 100) * 15 + 50) // 100

    @Slot(str, result=int)
    def getGameCountForAmount(self, amount_text):
        """Return an approximate game count for any whole amount >= 100."""
        amount = self._normalize_install_amount(amount_text)
        return self._game_count_for_amount(amount)

    @Slot(result=bool)
    def chooseInstallSource(self):
        current = Path(self.install_source_path)
        start_directory = str(current) if current.is_dir() else str(build_usb_source_root())
        selected = QFileDialog.getExistingDirectory(
            None,
            "اختيار مجلد تثبيت الألعاب والتطبيقات",
            start_directory
        )
        if not selected:
            return False
        source = Path(selected)
        if not source.is_dir():
            self.messageChanged.emit("مجلد التثبيت المحدد غير متاح.")
            return False
        self.install_source_path = str(source)
        self._save_settings()
        self.messageChanged.emit("تم اختيار مصدر التثبيت: " + str(source))
        return True

    @Slot(result=list)
    def getTransferHistory(self):
        return list(reversed(self._transfer_history))

    @staticmethod
    def _is_insufficient_storage_failure(raw_message):
        text = str(raw_message or "").casefold()
        markers = (
            "no space left on device",
            "not enough space",
            "not enough storage",
            "insufficient storage",
            "insufficient space",
            "not enough space",
            "no space left",
            "storage is full",
            "storage full",
            "disk full",
            "out of space",
            "enospc",
            "errno 28",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _copy_entry_units(item):
        try:
            return max(1, min(int(item.get("units", 1) or 1), 100))
        except (TypeError, ValueError, OverflowError):
            return 1

    def _build_transfer_summary(self):
        completed = 0
        completed_today = 0
        failed = 0
        review = 0
        insufficient_space = 0
        completed_keys = set()
        today = datetime.now().astimezone().date()
        copy_entries = [
            item for item in self._transfer_history
            if item.get("kind") == "نسخ"
            and str(item.get("target_type") or "android") != "usb_storage"
        ]

        for item in copy_entries:
            status = str(item.get("status") or "")
            raw_message = item.get("raw_message") or item.get("message") or ""
            if status == "اكتمل":
                serial = str(item.get("serial") or "")
                game = str(item.get("game") or "")
                key = (serial, game) if serial and game else ("legacy-entry", str(item.get("id") or id(item)))
                if key not in completed_keys:
                    units = self._copy_entry_units(item)
                    completed += units
                    completed_keys.add(key)

                    # Persisted ISO timestamps include the original local UTC offset.
                    # Convert to this computer's local timezone before comparing dates.
                    recorded_at = str(item.get("date") or "").strip()
                    try:
                        recorded_datetime = datetime.fromisoformat(
                            recorded_at.replace("Z", "+00:00")
                        )
                        if recorded_datetime.tzinfo is None:
                            recorded_datetime = recorded_datetime.astimezone()
                        if recorded_datetime.astimezone().date() == today:
                            completed_today += units
                    except (TypeError, ValueError, OverflowError):
                        # Legacy or malformed timestamps stay in lifetime totals,
                        # but are not assigned to today's count without reliable dates.
                        pass
            elif status == "مراجعة":
                review += self._copy_entry_units(item)
            elif status == "مساحة غير كافية" or (
                status == "غير مكتمل"
                and self._is_insufficient_storage_failure(raw_message)
            ):
                insufficient_space += self._copy_entry_units(item)
            elif status in ("فشل", "غير مكتمل"):
                failed += self._copy_entry_units(item)

        return {
            "completed": completed,
            "completedToday": completed_today,
            "failed": failed,
            "review": review,
            "insufficientSpace": insufficient_space,
            "totalCopies": sum(self._copy_entry_units(item) for item in copy_entries),
        }

    @Slot(result="QVariantMap")
    def getTransferSummary(self):
        return self._build_transfer_summary()

    def _emit_transfer_summary(self):
        self.transferSummaryChanged.emit(self._build_transfer_summary())

    def _record_transfer_history(self, operation, success, message):
        if operation.get("history_recorded", False):
            return None
        operation["history_recorded"] = True

        serial = str(operation.get("serial") or "")
        game = str(operation.get("game") or "")
        operation_kind = str(operation.get("kind") or "copy")
        cancelled = bool(operation.get("cancelled", False))
        completed = bool(success) and not cancelled
        kind = {
            "install": "تثبيت",
            "uninstall": "إزالة تثبيت",
            "copy": "نسخ",
            "pull": "نسخ من الهاتف",
        }.get(operation_kind, "نسخ")
        entry_id = str(time.time_ns())
        raw_message = str(operation.get("raw_error") or message or "")

        if kind == "تثبيت":
            status = "ملغي" if cancelled else "تم التثبيت" if completed else "فشل التثبيت"
        elif kind == "إزالة تثبيت":
            status = "ملغي" if cancelled else "تمت إزالة التثبيت" if completed else "فشل إزالة التثبيت"
        elif kind == "نسخ من الهاتف":
            status = "ملغي" if cancelled else "اكتمل" if completed else "فشل"
        elif cancelled:
            status = "ملغي"
        elif completed and str(operation.get("target_type") or "") == "usb_storage":
            copied_files = max(0, int(operation.get("copied_files") or 0))
            skipped_files = max(0, int(operation.get("skipped_files") or 0))
            status = (
                "تم التخطي" if copied_files == 0 and skipped_files > 0
                else "اكتمل مع تخطي" if skipped_files > 0
                else "اكتمل"
            )
        elif completed:
            previously_completed = any(
                item.get("kind") == "نسخ"
                and str(item.get("serial") or "") == serial
                and str(item.get("game") or "") == game
                and item.get("status") in ("اكتمل", "مراجعة")
                for item in self._transfer_history
            )

            # A successful retry resolves earlier failures for this same phone/game.
            resolved_at = datetime.now().astimezone().isoformat(timespec="seconds")
            for item in self._transfer_history:
                same_copy = (
                    item.get("kind") == "نسخ"
                    and str(item.get("serial") or "") == serial
                    and str(item.get("game") or "") == game
                )
                if same_copy and item.get("status") in (
                    "فشل", "مساحة غير كافية", "غير مكتمل"
                ):
                    item["status"] = "تمت المعالجة"
                    item["resolved"] = True
                    item["resolved_at"] = resolved_at
                    item["resolved_by"] = entry_id

            status = "مراجعة" if previously_completed else "اكتمل"
        else:
            status = (
                "مساحة غير كافية"
                if self._is_insufficient_storage_failure(raw_message)
                else "فشل"
            )

        size_labels = {"Saved": "كامل", "Medium": "متوسط", "Low": "منخفض"}
        device_name = str(
            operation.get("device_name")
            or operation.get("model")
            or serial
            or "Android"
        )
        entry = {
            "id": entry_id,
            "date": datetime.now().astimezone().isoformat(timespec="seconds"),
            "serial": serial,
            "device": device_name,
            "device_name": device_name,
            "game": game,
            "kind": kind,
            "status": status,
            "source": str(operation.get("source_label") or self._source_label()),
            "source_path": str(operation.get("source") or operation.get("source_root") or ""),
            "source_root": str(operation.get("source_root") or ""),
            "destination": str(operation.get("destination") or ""),
            "target_type": str(operation.get("target_type") or "android"),
            "units": self._copy_entry_units(operation),
            "unit_price": max(0, int(operation.get("unit_price", 500) or 0)),
            "billing_value": max(
                0,
                int(operation.get(
                    "billing_value",
                    self._copy_entry_units(operation) * int(operation.get("unit_price", 500) or 0)
                ) or 0)
            ),
            "game_size": size_labels.get(str(operation.get("game_size") or ""), ""),
            "copied_files": max(0, int(operation.get("copied_files") or 0)),
            "skipped_files": max(0, int(operation.get("skipped_files") or 0)),
            "message": str(message or ""),
            "raw_message": raw_message,
        }
        self._transfer_history.append(entry)
        self._save_transfer_history()
        self.transferHistoryChanged.emit(list(reversed(self._transfer_history)))
        self._emit_transfer_summary()
        return status

    @Slot(str, result=bool)
    def verifyWindowPassword(self, password):
        """Verify the local window-lock password without embedding it in QML."""
        candidate = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            bytes.fromhex("5c1698fab0d3d99a4fd63f40427556c5"),
            300000
        ).hex()
        expected = "4604842565cfb13a9e2a70a7c2c6c053af2d08fb1c6d1d23f231c5b8544a28d5"
        return hmac.compare_digest(candidate, expected)


    @staticmethod
    def _find_main_window_handle():
        """Resolve the QML main window's native HWND without changing its flags."""
        try:
            windows = QGuiApplication.topLevelWindows()
        except Exception:
            return 0

        for window in windows:
            try:
                if (
                    window.objectName() == "mainWindow"
                    or window.title() == "CN PlayHub"
                ):
                    handle = int(window.winId())
                    if handle > 0:
                        return handle
            except Exception:
                continue
        return 0

    @Slot(bool, result=bool)
    def setFullscreenProtection(self, enabled):
        """Enable or release the Windows fullscreen desktop barrier."""
        if enabled:
            window_handle = self._find_main_window_handle()
            if (
                not window_handle
                or not self._windows_fullscreen_protection.set_window_handle(
                    window_handle
                )
            ):
                self._windows_fullscreen_protection.last_error = (
                    "Could not resolve the CN PlayHub native window handle."
                )
                detail = self._windows_fullscreen_protection.last_error
                self.messageChanged.emit(
                    "تعذر تفعيل حاجز سطح المكتب. سيبقى قفل كلمة المرور داخل التطبيق فقط، "
                    "ويمكن الخروج إلى سطح المكتب أو إظهار شريط المهام. التفاصيل: "
                    + detail
                )
                return False

            if self._windows_fullscreen_protection.enabled:
                if not self._windows_fullscreen_protection.enforce_locked_desktop():
                    return False
                if not self._fullscreen_taskbar_timer.isActive():
                    self._fullscreen_taskbar_timer.start()
                return True

            if not self._windows_fullscreen_protection.enable():
                detail = str(
                    getattr(self._windows_fullscreen_protection, "last_error", "") or ""
                ).strip()
                suffix = (" التفاصيل: " + detail) if detail else ""
                self.messageChanged.emit(
                    "تعذر تفعيل حاجز سطح المكتب. سيبقى قفل كلمة المرور داخل التطبيق فقط، "
                    "ويمكن الخروج إلى سطح المكتب أو إظهار شريط المهام. التفاصيل: "
                    + detail
                )
                return False

            self._fullscreen_taskbar_timer.start()
            print("WINDOWS FULLSCREEN PROTECTION: ENABLED")
            self.messageChanged.emit(
                "تم تفعيل الحاجز الإضافي: حظر اختصارات التنقل الشائعة، "
                "وتثبيت نافذة التطبيق فوق النوافذ، وإخفاء شريط المهام. "
                "هذا ليس وضع Kiosk على مستوى Windows."
            )
            return True

        self._fullscreen_taskbar_timer.stop()
        self._windows_fullscreen_protection.disable()
        print("WINDOWS FULLSCREEN PROTECTION: RELEASED")
        return True

    @Slot()
    def releaseFullscreenProtection(self):
        """Best-effort cleanup on a normal application shutdown."""
        try:
            self.setFullscreenProtection(False)
        except Exception as exc:
            print("WINDOWS FULLSCREEN PROTECTION CLEANUP WARNING:", exc)

    @Slot(str, result=bool)
    def dismissDeviceOperation(self, serial):
        operation = self._active_operations.get(serial)
        if operation is None:
            return False
        if not operation.get("completed", False):
            self.messageChanged.emit("لا يمكن إغلاق بطاقة عملية ما زالت قيد التنفيذ.")
            return False
        self._active_operations.pop(serial, None)
        self._emit_operation_state()
        return True

    @Slot(result=bool)
    def useWirelessSource(self):
        source = build_server_source_root()
        if not source.is_dir():
            self.messageChanged.emit(
                f"تعذر الوصول إلى مصدر الشبكة: {source}"
            )
            return False

        self.current_source = "WIRELESS"
        self.source_root = source
        self._save_settings()
        self.messageChanged.emit(f"تم اختيار مصدر الشبكة: {source}")
        print("SOURCE SELECTED: WIRELESS", self.source_root)
        return True

    @Slot(result=bool)
    def useUsbSource(self):
        source = build_usb_source_root()
        if not source.is_dir():
            self.messageChanged.emit(
                f"مجلد USB غير موجود أو غير متاح: {source}"
            )
            return False

        self.current_source = "USB"
        self.source_root = source
        self._save_settings()
        self.messageChanged.emit(f"تم اختيار مصدر USB: {source}")
        print("SOURCE SELECTED: USB", self.source_root)
        return True



    def _transfer_progress(self, percent, speed, eta):
        self._progress(percent, speed, eta)


    def _transfer_status(self, text):
        self._status(text)


    def _transfer_log(self, text):
        print("TRANSFER LOG:", text)

    def _get_current_game(self):
        return self._current_game

    def _set_current_game(self, value):
        if self._current_game != value:
            self._current_game = value
            self.currentGameChanged.emit()

    currentGameChanged = Signal()
    currentGame = Property(
        str,
        _get_current_game,
        _set_current_game,
        notify=currentGameChanged
    )

    def _get_operation_busy(self):
        return self._operation_busy

    operationBusy = Property(
        bool,
        _get_operation_busy,
        notify=busyChanged
    )

    def _progress(self, percent, speed, eta):
        print(
            f"PROGRESS: {percent}% | {speed} | ETA {eta}"
        )

        self.transferPercentChanged.emit(percent)
        self.transferSpeedChanged.emit(speed)
        self.transferEtaChanged.emit(eta)

        for serial, operation in self._active_operations.items():
            if operation.get("engine") is self.adb_engine:
                self._update_operation(
                    serial,
                    percent=percent,
                    speed=speed,
                    eta=eta
                )

    def _status(self, message):
        print("STATUS:", message)
        self.messageChanged.emit(message)

        for serial, operation in self._active_operations.items():
            if operation.get("engine") is self.adb_engine:
                self._update_operation(
                    serial,
                    status=message,
                    operation_text=message
                )

    def _set_operation_state(self, state):
        self._operation_state = state
        busy = any(
            not operation.get("completed", False)
            for operation in self._active_operations.values()
        )

        if self._operation_busy != busy:
            self._operation_busy = busy
            self.busyChanged.emit(busy)

        if not busy and self._transfer_paused:
            self._transfer_paused = False
            self.transferPausedChanged.emit(False)

        self.operationStateChanged.emit(state)

    def _reject_operation(self, message):
        self.messageChanged.emit(message)
        self.operationRejected.emit(message)

    def _operation_rows(self):
        return [
            {
                "serial": operation["serial"],
                "model": operation.get("model", "Android"),
                "target_type": operation.get("target_type", "android"),
                "units": self._copy_entry_units(operation),
                "game": operation.get("game", ""),
                "kind": operation.get("kind", ""),
                "status": operation.get("status", "انتظار"),
                "percent": operation.get("percent", 0),
                "operation": operation.get("operation", ""),
                "speed": operation.get("speed", "--"),
                "eta": operation.get("eta", "--"),
                "error": operation.get("error", ""),
                "raw_error": operation.get("raw_error", ""),
                "cancelled": operation.get("cancelled", False),
                "completed": operation.get("completed", False),
                "awaitingFallback": operation.get("awaiting_root_fallback", False),
                "copied_files": operation.get("copied_files", 0),
                "skipped_files": operation.get("skipped_files", 0),
            }
            for operation in self._active_operations.values()
        ]

    def _update_operation(
        self,
        serial,
        percent=None,
        status=None,
        operation_text=None,
        speed=None,
        eta=None,
        error=None
    ):
        operation = self._active_operations.get(serial)
        if operation is None:
            return

        if percent is not None:
            operation["percent"] = percent
        if status is not None:
            operation["status"] = status
        if operation_text is not None:
            operation["operation"] = operation_text
        if speed is not None:
            operation["speed"] = speed
        if eta is not None:
            operation["eta"] = eta
        if error is not None:
            operation["error"] = error

        self.deviceProgressChanged.emit(
            serial,
            operation["percent"],
            operation["status"],
            operation["operation"],
            operation["speed"],
            operation["eta"],
            operation["error"]
        )
        self.transferBatchChanged.emit(self._operation_rows())

    def _emit_operation_state(self):
        self.transferBatchChanged.emit(self._operation_rows())
        active = any(
            not operation.get("completed", False)
            for operation in self._active_operations.values()
        )
        self._set_operation_state("ACTIVE" if active else "IDLE")

    def _operation_status_changed(self, serial, message):
        self._update_operation(
            serial,
            status=message,
            operation_text=message
        )
        self.messageChanged.emit(message)

    def _start_operation(self, operation):
        serial = operation["serial"]
        is_usb_target = operation.get("target_type") == "usb_storage"
        is_efootball_saf = operation.get("transfer_backend") == "efootball_saf"
        engine = (
            TransferEngine()
            if is_usb_target
            else EFootballSAFEngine()
            if is_efootball_saf
            else ADBEngine()
        )
        operation["engine"] = engine
        operation.setdefault(
            "phase",
            "local_usb_copy"
            if is_usb_target
            else "adb_direct" if operation.get("kind") == "copy"
            else "normal"
        )
        operation.setdefault("target_type", "usb_storage" if is_usb_target else "android")
        operation.setdefault("units", 1)
        # Every transfer backend emits a consistent initial row, including SAF
        # operations built by callers that do not pre-populate UI progress fields.
        operation.setdefault("percent", 0)
        operation.setdefault("status", "")
        operation.setdefault("operation", "")
        operation.setdefault("speed", "0 B/s")
        operation.setdefault("eta", "--")
        operation.setdefault("error", "")
        self._active_operations[serial] = operation

        engine.progressChanged.connect(
            lambda percent, speed, eta, s=serial:
            self._operation_progress(s, percent, speed, eta)
        )
        engine.statusChanged.connect(
            lambda message, s=serial:
            self._operation_status_changed(s, message)
        )
        engine.finished.connect(
            lambda success, message, s=serial:
            self._engine_finished_for_device(s, success, message)
        )

        is_install = operation.get("kind") == "install"
        is_batch_install = is_install and operation.get("install_type") == "batch"
        is_uninstall = operation.get("kind") == "uninstall"
        is_pull = operation.get("kind") == "pull"
        self._emit_operation_state()
        self._update_operation(
            serial,
            status=(
                "جاري تثبيت الألعاب المختارة عشوائيًا"
                if is_batch_install and operation.get("install_amount") is not None
                else "جاري تثبيت كل الألعاب والتطبيقات"
                if is_batch_install
                else "جاري التثبيت" if is_install
                else "جاري إزالة التثبيت" if is_uninstall
                else "جاري نسخ الملفات من الهاتف" if is_pull
                else "جاري النسخ إلى الفلاشة" if is_usb_target
                else "جاري النسخ المباشر إلى eFootball عبر SAF" if is_efootball_saf
                else "جاري النسخ المباشر عبر ADB"
            ),
            operation_text=(
                "تثبيت الألعاب المختارة عشوائيًا حسب المبلغ"
                if is_batch_install and operation.get("install_amount") is not None
                else "تثبيت حزم APK/APKS/XAPK بالتتابع دون نقل ملفات اللعبة"
                if is_batch_install
                else "تثبيت التطبيق"
                if is_install
                else "إزالة التطبيق من الهاتف"
                if is_uninstall
                else "سحب ملفات اللعبة من مسارها على الهاتف إلى الكمبيوتر"
                if is_pull
                else "نسخ ملفات جديدة إلى الفلاشة وتخطي الموجود"
                if is_usb_target
                else "نسخ مباشر إلى مساحة eFootball الخاصة بـ Ziru_27 عبر SAF"
                if is_efootball_saf
                else "محاولة النسخ مباشرة إلى مجلد اللعبة"
            ),
            speed="--" if is_install or is_uninstall or is_pull else "0 B/s",
            eta="--"
        )

        if is_install:
            if operation.get("install_type") == "batch":
                engine.install_batch(serial, operation.get("batch_entries") or [])
            elif operation["install_type"] == "apks":
                engine.install_apks(serial, operation["apk"])
            elif operation["install_type"] == "xapk":
                engine.install_batch(serial, [{
                    "name": str(operation.get("game") or "التطبيق"),
                    "type": "xapk",
                    "path": operation["apk"],
                }])
            else:
                engine.install(serial, operation["apk"])
        elif is_uninstall:
            engine.uninstall(serial, operation["package_id"])
        elif is_usb_target:
            # USB remains an ordinary local filesystem copy: no ADB or Shizuku.
            engine.start(operation["source"], operation["destination"])
        elif is_pull:
            engine.pull(
                serial,
                operation["source"],
                operation["destination"],
                operation.get("comparison_destination", ""),
            )
        else:
            engine.push(serial, operation["source"], operation["destination"])

    def _operation_progress(self, serial, percent, speed, eta):
        operation = self._active_operations.get(serial, {})
        parts = operation.get("copy_parts") or []
        if parts:
            index = int(operation.get("copy_part_index", 0))
            part_percent = max(0, min(100, int(percent)))
            percent = int((index + part_percent / 100.0) * 100 / len(parts))
        self._update_operation(
            serial,
            percent=int(percent),
            speed=speed,
            eta=eta
        )


    def _advance_copy_part(self, serial, operation):
        """Continue the next data/obb leg as one logical per-phone operation."""
        parts = operation.get("copy_parts") or []
        current_index = int(operation.get("copy_part_index", 0))
        next_index = current_index + 1
        if next_index >= len(parts):
            return False

        part = parts[next_index]
        operation["copy_part_index"] = next_index
        operation["source"] = str(part.get("source") or "")
        operation["destination"] = str(part.get("destination") or "")
        operation["phase"] = "adb_direct"
        operation["percent"] = int(next_index * 100 / len(parts))
        label = str(part.get("label") or "ملفات اللعبة")

        engine = ADBEngine()
        operation["engine"] = engine
        engine.progressChanged.connect(
            lambda percent, speed, eta, s=serial:
            self._operation_progress(s, percent, speed, eta)
        )
        engine.statusChanged.connect(
            lambda message, s=serial:
            self._operation_status_changed(s, message)
        )
        engine.finished.connect(
            lambda success, message, s=serial:
            self._engine_finished_for_device(s, success, message)
        )

        self._update_operation(
            serial,
            percent=operation["percent"],
            status="جاري نسخ " + label,
            operation_text="الجزء " + str(next_index + 1) + " من " + str(len(parts))
                           + " — " + label,
            speed="0 B/s",
            eta="--",
            error=""
        )
        engine.push(serial, operation["source"], operation["destination"])
        return True

    @staticmethod
    def _root_fallback_destination(game_name):
        folder = str(game_name or "GameData").strip()
        for character in ('\\', '/', ':', '*', '?', '"', '<', '>', '|'):
            folder = folder.replace(character, "_")
        folder = folder.replace(" ", "_").strip("._")
        if not folder:
            folder = "GameData"
        return f"/sdcard/{folder}/"

# Preserve the legacy user-confirmed retry to the internal-storage root after an actual ADB write denial.
    def _request_android_data_root_fallback(self, serial, operation, raw_message):
        raw_error = str(raw_message or "")
        operation["raw_error"] = raw_error
        message = self._device_copy_failure_summary(serial, operation, raw_error)
        destination = self._root_fallback_destination(operation.get("game", ""))
        operation.update({
            "awaiting_root_fallback": True,
            "completed": True,
            "phase": "awaiting_root_fallback",
            "status": "بانتظار اختيار وجهة بديلة",
            "operation": "بانتظار اختيار وجهة بديلة",
            "error": message,
            "result": {
                "serial": serial,
                "success": False,
                "message": message,
            },
        })
        self._emit_operation_state()
        self.operationFinished.emit(f"{serial}: FAILED")
        self.transferFinished.emit(False, message)
        self.messageChanged.emit(message)
        operation["engine"] = None

        reason = (
            str(message)
            + "\n\nرسالة ADB الأصلية:\n"
            + raw_error
        )
        self.androidDataFallbackRequested.emit(
            serial,
            str(operation.get("game", "")),
            destination,
            reason,
        )

    def _engine_finished_for_device(self, serial, success, message):
        operation = self._active_operations.get(serial)
        if operation is None or operation.get("completed"):
            return

        phase = operation.get("phase", "normal")
        engine = operation.get("engine")

        if phase == "efootball_saf":
            operation["phase"] = "final"
            self._finished_for_device(serial, success, message)
            return

        if phase == "root_helper_launch":
            operation["phase"] = "final"
            operation["local_finalize_pending"] = True
            if not success:
                message = (
                    "اكتمل النسخ إلى الجذر وبقيت الملفات محفوظة، لكن تعذر فتح مساعد الهاتف تلقائياً. "
                    "افتح KafiaNet Assistant يدوياً ثم تابع طلب إذن المجلد.\n"
                    + str(message or "")
                )
            self._finished_for_device(serial, True, message)
            return

        if phase == "adb_direct":
            if (
                not success
                and operation.get("kind") == "copy"
                and not operation.get("cancelled", False)
                and not operation.get("root_fallback_attempted", False)
                and self._is_android_data_permission_denial(
                    operation.get("destination", ""), message
                )
            ):
                # A confirmed ADB write denial must try Shizuku first. The
                # independent-storage-root option is offered only if Shizuku
                # also reaches the destination writer and receives EACCES/EPERM.
                self._begin_shizuku_fallback(serial, operation, message)
                return
            if success and operation.get("root_fallback_attempted", False):
                self._launch_phone_local_finalize(serial, operation)
                return
            if success and self._advance_copy_part(serial, operation):
                return
            operation["phase"] = "final"
            self._finished_for_device(serial, success, message)
            return

        if phase == "shizuku_prepare":
            if not success:
                combined = (
                    "تعذر تجهيز Shizuku قبل بدء إرسال الملفات؛ لم يتم تأكيد رفض الكتابة "
                    "إلى Android/data، لذلك لم يُفتح النسخ إلى الجذر.\n"
                    + "خطأ تجهيز Shizuku: " + str(message or "سبب غير معروف")
                )
                operation["phase"] = "final"
                self._finished_for_device(serial, False, combined)
                return
            try:
                bridge = json.loads(str(message or "{}"))
                operation["shizuku_port"] = int(bridge["port"])
                operation["shizuku_token"] = str(bridge["token"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                if engine is not None:
                    engine.cleanup_shizuku_bridge(serial)
                operation["phase"] = "final"
                self._finished_for_device(
                    serial,
                    False,
                    "تعذر قراءة بيانات جلسة Shizuku قبل بدء النقل؛ لم يتم فتح النسخ إلى الجذر. "
                    + str(exc),
                )
                return

            if engine is None:
                operation["phase"] = "final"
                self._finished_for_device(
                    serial,
                    False,
                    "محرك النقل غير متاح لاستكمال Shizuku؛ لم يتم فتح النسخ إلى الجذر.",
                )
                return
            operation["phase"] = "shizuku_copy"
            operation["status"] = "جاري نسخ الملفات عبر شيزوكو"
            operation["operation"] = "جاري نسخ الملفات عبر شيزوكو"
            operation["percent"] = 0
            operation["speed"] = "0 B/s"
            operation["eta"] = "--"
            self._update_operation(
                serial,
                percent=0,
                status=operation["status"],
                operation_text=operation["operation"],
                speed=operation["speed"],
                eta=operation["eta"],
                error=""
            )
            engine.copy_via_shizuku_bridge(
                serial,
                str(operation["shizuku_port"]),
                operation["shizuku_token"],
                operation["shizuku_job_id"],
                operation["source"],
                operation["destination"],
                operation["target_relative"]
            )
            return

        if phase == "shizuku_copy":
            if (
                not success
                and operation.get("kind") == "copy"
                and not operation.get("cancelled", False)
                and not operation.get("root_fallback_attempted", False)
            ):
                shizuku_error = str(message or "سبب غير معروف")
                if self._is_shizuku_destination_write_denial(
                    operation.get("destination"), shizuku_error
                ):
                    adb_error = str(operation.get("shizuku_original_error") or "")
                    combined = (
                        "ADB copy failed: " + adb_error
                        + "\nShizuku copy failed: " + shizuku_error
                    )
                    self._request_android_data_root_fallback(serial, operation, combined)
                    return

                # A generic Shizuku error is not proof that Android denied a
                # protected write. Keep the original destination untouched and
                # report the failure instead of opening the root-copy fallback.
                operation["phase"] = "final"
                self._finished_for_device(
                    serial,
                    False,
                    "تعذر إكمال النسخ عبر Shizuku دون تأكيد رفض الكتابة إلى المجلد المحمي. "
                    "لم يتم فتح النسخ إلى الجذر تلقائيًا.\n"
                    + shizuku_error,
                )
                return
            if success and self._advance_copy_part(serial, operation):
                return
            operation["phase"] = "final"
            self._finished_for_device(serial, success, message)
            return

        operation["phase"] = "final"
        self._finished_for_device(serial, success, message)

    def _launch_phone_local_finalize(self, serial, operation):
        """After the root copy completes, ask the unified phone helper to finish into Android/data."""
        package_name = str(operation.get("target_package") or "").strip()
        original_destination = str(
            operation.get("original_destination")
            or operation.get("destination")
            or ""
        ).replace("\\", "/").rstrip("/")
        normalized = original_destination
        if normalized.lower().startswith("/sdcard/"):
            normalized = "/storage/emulated/0/" + normalized[len("/sdcard/"):]
        package_root = "/storage/emulated/0/Android/data/" + package_name
        if not package_name:
            operation["phase"] = "final"
            self._finished_for_device(
                serial, True,
                "اكتمل النسخ إلى الجذر، لكن لم يُحدد Package ID صالح لإكمال النقل المحلي."
            )
            return
        normalized_lower = normalized.lower()
        package_root_lower = package_root.lower()
        if normalized_lower == package_root_lower:
            target_relative = ""
        elif normalized_lower.startswith(package_root_lower + "/"):
            target_relative = normalized[len(package_root):].lstrip("/")
        else:
            operation["phase"] = "final"
            self._finished_for_device(
                serial,
                True,
                "اكتمل النسخ إلى مجلد الجذر وبقيت الملفات محفوظة. "
                "لم يمكن تحديد مسار Android/data الأصلي بدقة لإكمال النقل المحلي."
            )
            return

        engine = operation.get("engine")
        if engine is None or not hasattr(engine, "launch_phone_local_finalize"):
            operation["phase"] = "final"
            self._finished_for_device(
                serial, True,
                "اكتمل النسخ إلى الجذر. تعذر تشغيل طلب الإكمال المحلي من نسخة المحرك الحالية؛ "
                "الملفات محفوظة ويمكن فتح مساعد KafiaNet يدوياً."
            )
            return

        source_root = str(operation.get("destination") or "")
        if not source_root:
            operation["phase"] = "final"
            self._finished_for_device(serial, True, "اكتمل النسخ إلى الجذر، لكن مسار نسخة الجذر غير متاح.")
            return

        operation["phase"] = "root_helper_launch"
        operation["status"] = "تم النسخ إلى الجذر — بانتظار إذن المساعد"
        operation["operation"] = "فتح مساعد الهاتف لنقل الملفات إلى المجلد المحمي"
        operation["percent"] = 100
        self._update_operation(
            serial,
            percent=100,
            status=operation["status"],
            operation_text=operation["operation"],
            speed="--",
            eta="--",
            error=""
        )
        self.messageChanged.emit(
            "اكتمل النسخ إلى الجذر. يجري فتح تطبيق KafiaNet Assistant على الهاتف "
            "لطلب إذن مجلد " + package_name + ". بعد ظهور نسبة النقل داخل المساعد يمكنك فصل USB."
        )
        engine.launch_phone_local_finalize(
            serial,
            source_root,
            package_name,
            target_relative,
            str(operation.get("game") or package_name)
        )

    @Slot(str, result=bool)
    def copyToDeviceRoot(self, serial):
        operation = self._active_operations.get(serial)
        if operation is None or not operation.get("awaiting_root_fallback", False):
            self.messageChanged.emit("لا يوجد طلب نسخ بديل معلّق لهذا الهاتف.")
            return False

        original_destination = operation.get(
            "original_destination", operation.get("destination", "")
        )
        destination = self._root_fallback_destination(operation.get("game", ""))
        operation.update({
            "original_destination": original_destination,
            "destination": destination,
            "root_fallback_attempted": True,
            "awaiting_root_fallback": False,
            "completed": False,
            "phase": "adb_direct",
            "status": "جاري النسخ إلى الجذر",
            "percent": 0,
            "operation": "نسخ إلى مجلد مستقل في جذر الذاكرة",
            "speed": "0 B/s",
            "eta": "--",
            "error": "",
            "raw_error": "",
            "history_recorded": False,
        })
        operation.pop("result", None)
        self.messageChanged.emit(
            f"إعادة نسخ {operation.get('game', 'البيانات')} إلى {destination}"
        )
        self._start_operation(operation)
        return True

    @Slot(str, result=bool)
    def declineRootFallback(self, serial):
        operation = self._active_operations.get(serial)
        if operation is None or not operation.get("awaiting_root_fallback", False):
            return False

        operation["awaiting_root_fallback"] = False
        operation["completed"] = True
        operation["phase"] = "final"
        operation["status"] = "فشل"
        operation["operation"] = "تم إلغاء النسخ إلى الجذر"
        message = "تم إلغاء النسخ إلى الجذر؛ لم يُجرَ نقل بديل."
        self._record_transfer_history(operation, False, message)
        self._emit_operation_state()
        self.messageChanged.emit(message)
        return True

    def _begin_shizuku_fallback(self, serial, operation, original_error):
        package_name = str(operation.get("target_package") or "")
        destination = str(operation.get("destination") or "")
        normalized = destination.replace("\\", "/").rstrip("/")
        if normalized.lower().startswith("/sdcard/"):
            normalized = "/storage/emulated/0/" + normalized[len("/sdcard/"):]
        package_root = "/storage/emulated/0/Android/data/" + package_name
        if not package_name or not normalized.lower().startswith((package_root + "/").lower()) and normalized.lower() != package_root.lower():
            operation["phase"] = "final"
            self._finished_for_device(
                serial, False,
                "رفض ADB الكتابة إلى Android/data، ولم يمكن تحديد مجلد اللعبة بدقة. "
                + str(original_error or "")
            )
            return

        relative = normalized[len(package_root):].lstrip("/")
        helper_apk = build_shizuku_helper_apk_path()
        shizuku_apk = build_shizuku_apk_path()
        # The engine first reuses an installed helper; the bundled APK is needed only when absent.

        job_id = secrets.token_hex(12)
        session_token = secrets.token_hex(24)
        operation.update({
            "phase": "shizuku_prepare",
            "shizuku_fallback_attempted": True,
            "shizuku_original_error": str(original_error or ""),
            "shizuku_job_id": job_id,
            "shizuku_token": session_token,
            "shizuku_port": 0,
            "target_relative": relative,
            "status": "جاري نسخ الملفات عبر شيزوكو",
            "percent": 0,
            "operation": "جاري نسخ الملفات عبر شيزوكو",
            "speed": "--",
            "eta": "--",
            "error": ""
        })
        self._update_operation(
            serial,
            percent=0,
            status=operation["status"],
            operation_text=operation["operation"],
            speed="--",
            eta="--",
            error=""
        )
        self.messageChanged.emit("جاري نسخ الملفات عبر شيزوكو")
        engine = operation.get("engine")
        if engine is None:
            operation["phase"] = "final"
            self._finished_for_device(serial, False, "محرك ADB غير متاح لتثبيت Shizuku.")
            return
        engine.prepare_shizuku_bridge(
            serial,
            str(shizuku_apk),
            str(helper_apk),
            session_token,
            package_name,
            destination
        )

    def _finished(self, success, message):
        self.messageChanged.emit(message)

    def _device_copy_failure_summary(self, serial, operation, raw_message):
        """Map common OEM/Android storage denials to a useful short message."""
        if operation.get("kind") != "copy":
            return raw_message

        text = str(raw_message or "")
        lowered = text.lower()
        if "kafianet_shizuku_destination_write_denied" in lowered:
            return "رفض ADB الكتابة إلى Android/data، ثم رفض Shizuku الكتابة إلى الوجهة المحمية أيضًا. يمكنك الآن اختيار النسخ إلى مجلد مستقل في جذر الذاكرة."
        if "kafianet_shizuku_destination_access_denied" in lowered:
            return "استطاع Shizuku بدء الفحص لكنه لم يحصل على صلاحية قراءة مجلد Saved. راجع صلاحيات Shizuku وحماية الشركة المصنّعة؛ لم نعتبر المجلد مفقودًا."
        if "kafianet_shizuku_destination_missing" in lowered:
            return "تحقق Shizuku بصلاحيات النقل وأكد أن مجلد Saved غير موجود فعلًا في المسار المحدد؛ شغّل PUBG FLASH على الهاتف حتى تنشئه ثم أعد المحاولة."
        if "kafianet_shizuku_destination_not_directory" in lowered:
            return "تحقق Shizuku أكد أن مسار Saved موجود لكنه ليس مجلدًا صالحًا؛ راجع مسار PUBG FLASH على الهاتف."
        if "kafianet_destination_missing" in lowered:
            return "مجلد الوجهة غير موجود. شغّل اللعبة على الهاتف أولًا حتى تنشئ مجلد Saved ثم أعد المحاولة."
        if "kafianet_destination_not_directory" in lowered:
            return "مسار الوجهة موجود لكنه ليس مجلد Saved صالحًا."
        if "read-only file system" in lowered:
            return "الوجهة أو وحدة التخزين للقراءة فقط؛ لم يتم تشغيل Shizuku لأن هذا ليس رفض كتابة Android/data."
        if "kafianet_no_space" in lowered or self._is_insufficient_storage_failure(text):
            return "مساحة التخزين المتاحة في الهاتف غير كافية لإكمال النسخ."
        if any(marker in lowered for marker in (
            "device offline", "device not found", "unauthorized",
            "no devices/emulators found", "transport error"
        )):
            return "اتصال ADB بالهاتف غير جاهز. افحص كابل USB وموافقة تصحيح USB ثم أعد المحاولة."
        if "kafianet_shizuku_oem_permission_limited" in lowered:
            return (
                "إعدادات حماية الشركة المصنّعة منعت ADB من تشغيل خادم Shizuku. "
                "افتح إرشادات الإصلاح الخاصة بالشركة؛ لم يبدأ نقل الملفات عبر المساعد."
            )
        if "kafianet_shizuku_user_service" in lowered:
            return (
                "تعذر ربط أو تشغيل خدمة Shizuku داخل الهاتف. هذا عطل خدمة/توافق وليس رفض كتابة Saved."
            )
        if "kafianet_shizuku_android16_userservice_incompatible" in lowered:
            return (
                "بدأ Shizuku لكن خدمة النقل تعثرت بتعارض توافق معروف في بعض أجهزة Android 16/HyperOS. "
                "لم نخفض إصدار Shizuku تلقائيًا أو نحذف نسختك الشخصية."
            )
        if "kafianet_shizuku_destination_or_oem_permission_denied" in lowered:
            return (
                "تم تشغيل Shizuku، لكن صلاحية ADB Shell أو الوصول إلى مجلد Saved ما زالت مقيّدة. "
                "افتح إرشادات الإصلاح الخاصة بالشركة المصنّعة."
            )
        permission_markers = (
            "permission denied",
            "remote secure_mkdirs failed",
            "read-only file system",
            "operation not permitted",
            "eacces",
            "errno 13",
        )
        if not any(marker in lowered for marker in permission_markers):
            return raw_message

        destination = str(operation.get("destination", ""))
        if "/android/data/" in destination.lower() or destination.lower().endswith("/android/data"):
            return (
                "الهاتف منع الكتابة داخل Android/data. "
                "افتح «إرشادات الإصلاح» لمعرفة السبب والخطوات المناسبة."
            )

        return (
            "الهاتف رفض الكتابة إلى وجهة النسخ. "
            "افتح «إرشادات الإصلاح» لمعرفة خطوات التشخيص."
        )

    @Slot(str, result=str)
    def deviceCopyHelp(self, serial):
        operation = self._active_operations.get(serial, {})
        info = self._device_info.get(serial, {})
        model = str(info.get("model") or info.get("name") or "Android")
        manufacturer = str(info.get("manufacturer") or "")
        brand_text = (manufacturer + " " + model).strip()
        brand_lower = (manufacturer + " " + model).lower()
        destination = str(operation.get("destination", ""))
        raw_error = str(operation.get("raw_error") or operation.get("error") or "")

        lines = [
            "الجهاز: " + brand_text,
            "",
            "1) افتح قفل الهاتف وأبقِ الشاشة مفتوحة. إذا ظهر طلب «السماح بتصحيح USB» لهذا الكمبيوتر، وافق عليه بعد التأكد أنك تثق بهذا الكمبيوتر.",
            "2) افتح إعدادات المطوّر وتأكد من تفعيل USB debugging. افصل الكابل وأعد توصيله، ثم جرّب منفذًا وكابل بيانات آخر عند الحاجة.",
            "3) Shizuku بدون Root ليس Root؛ يعمل عادة بهوية ADB Shell (uid=2000)، لذلك لا يتجاوز كل قيود Android أو قيود الشركة المصنّعة.",
        ]

        if any(name in brand_lower for name in ("xiaomi", "redmi", "poco", "شاومي", "ريدمي")):
            lines.extend([
                "",
                "شاومي / Redmi / POCO (MIUI أو HyperOS):",
                "• الإعدادات > الإعدادات الإضافية > خيارات المطوّر > USB debugging.",
                "• إذا كان الخيار موجودًا وكان إصدار النظام يطلبه، راجع USB debugging (Security settings) واقبل التحذير بعد قراءته. قد يتطلب النظام موافقة حساب Xiaomi أو شروطًا إضافية.",
                "• خيار Install via USB يتعلق أساسًا بتثبيت APK؛ تفعيله وحده لا يمنح حق الكتابة إلى Android/data.",
                "• خيار USB debugging (Security settings) منفصل عن USB debugging العادي، ويُراجع فقط إذا أبلغ Shizuku أن صلاحيات ADB محدودة.",
            ])
        elif any(name in brand_lower for name in ("oneplus", "oppo", "ون بلس")):
            lines.extend([
                "",
                "OPPO / OnePlus (ColorOS أو OxygenOS حسب الإصدار):",
                "• تأكد أولًا من USB debugging وقبول مفتاح RSA لهذا الكمبيوتر.",
                "• إذا أبلغ Shizuku أن صلاحيات ADB محدودة، راجع Permission monitoring في خيارات المطوّر؛ لا تغيّره بسبب خطأ مسار أو مساحة أو اتصال.",
                "• اختر File transfer فقط إذا كان اتصال USB نفسه غير مستقر.",
            ])
        elif "realme" in brand_lower or "ريلمي" in brand_lower:
            lines.extend([
                "",
                "realme:",
                "• ابدأ بتصحيح USB وقبول مفتاح RSA لهذا الكمبيوتر.",
                "• في بعض إصدارات Realme UI قد يظهر خيار Disable permission monitoring؛ راجعه يدويًا فقط إذا أبلغ Shizuku عن تقييد صلاحيات ADB.",
            ])
        elif "meizu" in brand_lower or "ميزو" in brand_lower:
            lines.extend([
                "",
                "Meizu / Flyme:",
                "• إذا أبلغ Shizuku عن تقييد صلاحيات ADB، راجع Flyme payment protection في خيارات المطوّر.",
                "• لا تغيّر هذا الإعداد إذا كانت المشكلة مجلد Saved مفقودًا أو انقطاع USB.",
            ])
        else:
            lines.extend([
                "",
                "الإعدادات العامة:",
                "• خيارات المطوّر > USB debugging، ثم اقبل مفتاح RSA لهذا الكمبيوتر على شاشة الهاتف.",
            ])

        if "/android/data/" in destination.lower() or destination.lower().endswith("/android/data"):
            lines.extend([
                "",
                "مهم بخصوص Android/data:",
                "يبدأ CN PlayHub بمحاولة ADB مباشرة، ولا يثبت MT Manager أو Shizuku قبل النسخ.",
                "إذا رفض الهاتف فعليًا الكتابة داخل Android/data، يعرض CN PlayHub خيار إعادة النسخ إلى مجلد مستقل في جذر الذاكرة الداخلية.",
                "عند اختيار «نسخ إلى الجذر» يعيد التطبيق إرسال مصدر النسخ إلى /sdcard/<اسم اللعبة>/؛ ولا يبدأ تحويلًا تلقائيًا إلى Shizuku في هذا المسار.",
                "لا يغيّر CN PlayHub صلاحيات الملفات أو يحذف بيانات المصدر عند إعادة النسخ إلى الجذر.",
            ])

        if "kafianet_shizuku_android16_userservice_incompatible" in raw_error.lower():
            lines.extend([
                "",
                "توافق خدمة Shizuku:",
                "• بعض تقارير المطورين وصفت فشل UserService مع Shizuku 13.6.0 على Android 16/HyperOS. هذا لا يعني أن كل أجهزة Xiaomi متأثرة.",
                "• لم يغيّر CN PlayHub إصدار Shizuku الموجود ولم يحذفه تلقائيًا؛ افحص إصدار Shizuku وسجل الخطأ قبل أي تحديث أو تخفيض.",
            ])
        if any(marker in raw_error.lower() for marker in (
            "kafianet_shizuku_destination_write_denied",
            "kafianet_shizuku_oem_permission_limited",
        )):
            lines.extend([
                "",
                "صلاحية Shizuku / حماية الشركة:",
                "• تحقق من تشغيل خادم Shizuku ومنح المساعد الإذن. موافقة الإذن لا تضمن وحدها أن UID 2000 يستطيع الكتابة.",
                "• لا تعطّل إعدادات الحماية إلا إذا ظهر تشخيص صريح بأن صلاحيات ADB محدودة.",
            ])
        if "kafianet_shizuku_user_service" in raw_error.lower():
            lines.extend([
                "",
                "خدمة Shizuku:",
                "• هذه مشكلة ربط أو توافق في خدمة النقل، وليست رفض كتابة Saved. تحقق من إصدار Shizuku، وحالة الخادم، وسجل الخطأ قبل تغيير إعدادات الحماية.",
            ])

        if raw_error:
            lines.extend([
                "",
                "رسالة ADB الأصلية:",
                raw_error,
            ])

        return "\n".join(lines)

    @staticmethod
    def _is_android_data_permission_denial(destination, raw_message):
        target = str(destination or "").replace("\\", "/").lower()
        if "/android/data/" not in target and not target.endswith("/android/data"):
            return False

        lowered = str(raw_message or "").lower()
        # An incomplete transport response is not proof that Android denied a write.
        # Retry the interrupted ADB file before considering the Shizuku fallback.
        transport_failure_markers = (
            "incompleteread",
            "incomplete read",
            "unexpected eof",
            "unexpected end of stream",
            "connection reset",
            "connection aborted",
            "broken pipe",
            "device offline",
            "device disconnected",
        )
        if any(marker in lowered for marker in transport_failure_markers):
            return False

        # ADB may print either "secure_mkdirs failed" or "secure_mkdirs() failed".
        # Normalize the empty call suffix so both forms activate the Shizuku fallback.
        lowered = lowered.replace("secure_mkdirs()", "secure_mkdirs")
        if "kafianet_android_data_permission_denied:" in lowered:
            return True

        # ADB/platform-tools versions vary in how they describe a remote
        # create/write denial. Require both a remote-side operation and an
        # access-denied reason so local source errors do not open the root dialog.
        remote_write_markers = (
            "remote secure_mkdirs failed",
            "remote open failed",
            "remote write failed",
            "remote mkdir failed",
            "remote could not create",
            "remote couldn't create",
            "remote failed to create",
            "failed to create remote",
            "remote create failed",
            "remote file creation failed",
        )
        permission_markers = (
            "permission denied",
            "access denied",
            "operation not permitted",
            "eacces",
            "errno 13",
        )
        return (
            any(marker in lowered for marker in remote_write_markers)
            and any(marker in lowered for marker in permission_markers)
        )

    @staticmethod
    def _is_shizuku_destination_write_denial(destination, raw_message):
        target = str(destination or "").replace("\\", "/").lower()
        if target.startswith("/sdcard/"):
            target = "/storage/emulated/0/" + target[len("/sdcard/"):]
        if "/android/data/" not in target:
            return False
        # This marker is emitted only by the Shizuku-side mkdir/open/rename
        # writer after explicit EACCES or EPERM. Service startup failures,
        # a declined Shizuku prompt, missing paths, storage errors, and
        # transport failures must never offer the independent-root fallback.
        return "kafianet_shizuku_destination_write_denied:" in str(raw_message or "").lower()

    def _finished_for_device(self, serial, success, message):
        print("FINISHED:", serial, success, message)
        operation = self._active_operations.get(serial)
        if operation is None or operation.get("completed"):
            return

        if operation.get("target_type") == "usb_storage" or operation.get("kind") == "pull":
            engine = operation.get("engine")
            stats_name = "copy_stats" if operation.get("target_type") == "usb_storage" else "pull_stats"
            stats = getattr(engine, stats_name, {}) if engine is not None else {}
            operation["copied_files"] = max(0, int(stats.get("copied_files", 0)))
            operation["skipped_files"] = max(0, int(stats.get("skipped_files", 0)))

        if not success and operation.get("kind") == "copy":
            operation["raw_error"] = str(message or "")
            if operation.get("root_fallback_attempted"):
                original = str(operation.get("shizuku_original_error") or "")
                message = (
                    "فشل النسخ إلى جذر الذاكرة حتى بعد رفض ADB وShizuku.\\n"
                    + str(message or "")
                    + (("\\nخطأ ADB الأصلي: " + original) if original else "")
                )
            elif operation.get("shizuku_fallback_attempted"):
                original = str(operation.get("shizuku_original_error") or "")
                message = (
                    "فشل النسخ بعد رفض ADB وتجربة Shizuku.\\n"
                    + str(message or "")
                    + (("\\nخطأ ADB الأصلي: " + original) if original else "")
                )
            elif operation.get("target_type") == "usb_storage":
                message = (
                    "فشل النسخ إلى الفلاشة "
                    + str(operation.get("device_name") or serial)
                    + ": " + str(message or "سبب غير معروف")
                )
            else:
                message = self._device_copy_failure_summary(serial, operation, message)

        operation["completed"] = True
        operation["phase"] = "final"
        operation["status"] = (
            "ملغي" if operation.get("cancelled")
            else "اكتمل" if success
            else "فشل"
        )
        operation["operation"] = (
            "تم إلغاء العملية" if operation.get("cancelled")
            else "اكتمل تثبيت جميع الحزم" if operation.get("install_type") == "batch" and success
            else "اكتمل التثبيت" if operation["kind"] == "install" and success
            else "فشل التثبيت" if operation["kind"] == "install"
            else "اكتمل إلغاء التثبيت" if operation["kind"] == "uninstall" and success
            else "فشل إزالة التثبيت" if operation["kind"] == "uninstall"
            else "اكتمل النسخ من الهاتف" if operation["kind"] == "pull" and success
            else "فشل النسخ من الهاتف" if operation["kind"] == "pull"
            else "اكتمل النسخ" if success
            else "فشل النسخ"
        )
        operation["percent"] = (
            100 if success and operation["kind"] in ("copy", "pull")
            else operation["percent"]
        )
        operation["error"] = "" if success else message
        operation["result"] = {
            "serial": serial,
            "success": success and not operation.get("cancelled"),
            "message": message
        }

        history_status = self._record_transfer_history(
            operation,
            operation["result"]["success"],
            message
        )
        if operation.get("target_type") == "usb_storage" and history_status in (
            "اكتمل مع تخطي", "تم التخطي"
        ):
            operation["status"] = history_status
            operation["operation"] = (
                f"نسخ {operation.get('copied_files', 0)} ملف جديد، "
                f"وتخطي {operation.get('skipped_files', 0)} ملف موجود"
            )
            self._update_operation(
                serial,
                status=operation["status"],
                operation_text=operation["operation"]
            )
        elif operation.get("kind") == "copy" and history_status == "مراجعة":
            operation["status"] = "مراجعة"
            operation["operation"] = "نسخ إضافي — يُحتسب ضمن المراجعة"
        elif operation.get("kind") == "copy" and history_status == "مساحة غير كافية":
            operation["status"] = "مساحة غير كافية"
            operation["operation"] = "مساحة التخزين غير كافية"
        if operation.get("kind") == "copy" and history_status in (
            "مراجعة", "مساحة غير كافية"
        ):
            self._update_operation(
                serial,
                status=operation["status"],
                operation_text=operation["operation"]
            )

        self._emit_operation_state()
        self.operationFinished.emit(
            f"{serial}: {'SUCCESS' if success else 'FAILED'}"
        )
        self.transferFinished.emit(
            operation["result"]["success"],
            operation["result"]["message"]
        )
        self.messageChanged.emit(message)
        if operation.get("engine") is not None:
            operation["engine"] = None

    @Slot(result=list)
    def getADBDevices(self):
        # Synchronous compatibility entry point for tests/non-UI callers.
        return self._apply_adb_devices(self.adb_engine.get_devices())

    @staticmethod
    def _usb_signature(devices):
        return tuple(sorted(
            (
                str(item.get("serial") or item.get("id") or ""),
                str(item.get("root") or ""),
                str(item.get("name") or ""),
                int(item.get("total_bytes") or 0),
            )
            for item in (devices or [])
        ))

    @Slot()
    def refreshUsbStorageDevices(self):
        """Poll removable volumes so USB flash hot-plug appears without reopening the popup."""
        try:
            devices = enumerate_removable_storage_devices()
        except Exception as exc:
            print("USB STORAGE DISCOVERY FAILED:", exc)
            devices = []

        signature = self._usb_signature(devices)
        if signature == self._usb_device_signature:
            return

        self._usb_device_signature = signature
        self._last_usb_devices = devices
        print("USB STORAGE DEVICES:", devices)
        self._apply_all_devices()

    @Slot()
    def refreshADBDevices(self):
        # ADB commands can stall on disconnected devices; never run discovery
        # directly on the Qt UI thread. USB volumes are polled independently.
        with self._adb_discovery_lock:
            if (
                self._adb_discovery_thread is not None
                and self._adb_discovery_thread.is_alive()
            ):
                return

            self._adb_discovery_thread = threading.Thread(
                target=self._discover_adb_devices,
                name="KafiaNet-ADB-Discovery",
                daemon=True
            )
            worker = self._adb_discovery_thread

        worker.start()

    def _discover_adb_devices(self):
        try:
            devices = self.adb_engine.get_devices()
        except Exception as exc:
            print("ADB DISCOVERY FAILED:", exc)
            devices = []
        self.adbDiscoveryCompleted.emit(devices)

    @Slot(list)
    def _apply_adb_devices(self, devices):
        self._last_adb_devices = [
            dict(item)
            for item in (devices or [])
            if str(item.get("type") or "android") != "usb_storage"
        ]
        return self._apply_all_devices()

    def _apply_all_devices(self):
        devices = [
            dict(item, type="android", units=1)
            for item in self._last_adb_devices
        ] + [
            dict(item, type="usb_storage", units=1)
            for item in self._last_usb_devices
        ]
        print("AVAILABLE COPY TARGETS:", devices)

        now = time.time()
        available_serials = {
            str(item.get("serial") or item.get("id") or "")
            for item in devices
            if item.get("serial") or item.get("id")
        }
        for item in devices:
            serial = str(item.get("serial") or item.get("id") or "")
            item["serial"] = serial
            if serial and serial not in self._device_first_seen:
                self._device_first_seen[serial] = now

        for serial in list(self._device_first_seen):
            if serial not in available_serials:
                self._device_first_seen.pop(serial, None)

        devices.sort(
            key=lambda item: self._device_first_seen.get(
                str(item.get("serial") or ""), 0.0
            ),
            reverse=True
        )
        self._save_settings()

        self.selected_devices = [
            serial for serial in self.selected_devices
            if serial in available_serials
        ]
        self._device_info = {}
        android_serials = set()
        for item in devices:
            serial = str(item.get("serial") or "")
            item_type = str(item.get("type") or "android")
            display_name = str(
                item.get("name") or item.get("model") or serial or "Android"
            ).strip()
            item["display_name"] = display_name
            item["units"] = 1
            if item_type != "usb_storage":
                android_serials.add(serial)
            self._device_info[serial] = item

        selected_android = [
            serial for serial in self.selected_devices
            if serial in android_serials
        ]
        if self.selected_device not in android_serials:
            self.selected_device = (
                selected_android[0] if selected_android else ""
            )

        qml_devices = []
        for item in devices:
            serial = str(item.get("serial") or "")
            qml_devices.append({
                "serial": serial,
                "model": item.get("display_name", serial),
                "name": item.get("display_name", serial),
                "type": item.get("type", "android"),
                "units": int(item.get("units") or 1),
                "root": str(item.get("root") or ""),
                "drive": str(item.get("drive") or ""),
                "capacityText": str(item.get("capacity_text") or ""),
                "freeText": str(item.get("free_text") or ""),
                "unitPrice": int(item.get("unit_price") or 500),
                "billingValue": int(item.get("billing_value") or 500),
                "firstSeen": self._device_first_seen.get(serial, 0.0),
            })

        self.adbDevicesChanged.emit(qml_devices)
        return devices

    def _selected_targets_include_usb_storage(self):
        return any(
            self._device_info.get(serial, {}).get("type") == "usb_storage"
            for serial in self.selected_devices
        )

    def _reject_usb_for_android_only_action(self, action_name):
        if not self._selected_targets_include_usb_storage():
            return False
        self._reject_operation(
            "تم تحديد فلاش USB. الفلاشة مخصصة لنسخ ملفات الألعاب فقط؛ "
            "أزل تحديدها لتنفيذ " + str(action_name) + " على هواتف Android."
        )
        return True

    @Slot(str)
    def selectADBDevice(self, device):
        info = self._device_info.get(device, {})
        if info.get("type") == "usb_storage":
            self.setADBDeviceSelection([device])
            return

        self.selected_device = device
        self.selected_devices = [device] if device else []
        print("ADB DEVICE SELECTED:", device)
        self.messageChanged.emit(f"تم تحديد الجهاز: {device}")

    @Slot(list)
    def setADBDeviceSelection(self, devices):
        known = set(self._device_info)
        selected = []
        for device in devices:
            if isinstance(device, str) and device in known and device not in selected:
                selected.append(device)
        self.selected_devices = selected
        self.selected_device = next(
            (
                serial for serial in selected
                if self._device_info.get(serial, {}).get("type") != "usb_storage"
            ),
            ""
        )
        print("COPY TARGETS SELECTED:", self.selected_devices)

    @Slot(result=int)
    def getSelectedTargetUnits(self):
        return sum(
            int(self._device_info.get(serial, {}).get("units") or 1)
            for serial in self.selected_devices
        )

    @staticmethod
    def _find_case_insensitive_child_directory(parent, name):
        parent = Path(parent)
        try:
            # Return the real child entry so its original casing is preserved
            # on case-insensitive Windows filesystems (for example "saved").
            matches = sorted(
                (
                    child for child in parent.iterdir()
                    if child.is_dir() and child.name.casefold() == str(name).casefold()
                ),
                key=lambda child: child.name.casefold()
            )
        except OSError:
            matches = []
        if matches:
            return matches[0]
        direct = parent / name
        return direct if direct.is_dir() else None

    @Slot(str)
    def copyGame(self, game_name):

        print()
        print("==============================")
        print("ADB COPY REQUEST:", game_name)
        print("==============================")

        # ==========================================
        # EXPLICITLY SELECTED ADB DEVICES
        # ==========================================

        target_devices = list(self.selected_devices)

        if not target_devices and self.selected_device:
            target_devices = [self.selected_device]

        if not target_devices:

            self._reject_operation(
                "يرجى تحديد هاتف واحد على الأقل قبل بدء النسخ."
            )
            return

        # ==========================================
        # CHECK SOURCE
        # ==========================================

        selected_source_root = (
            build_usb_source_root()
            if game_name == "PUBG Mobile Flash"
            else self.source_root
        )
        if not selected_source_root.is_dir():

            print(
                "SOURCE NOT FOUND:",
                selected_source_root
            )

            self.messageChanged.emit(
                f"مجلد مصدر اللعبة غير موجود: {selected_source_root}"
            )

            return

        # ==========================================
        # FIND GAME
        # ==========================================

        source = find_game_folder(
            selected_source_root,
            game_name
        )

        if source is None:

            print(
                "GAME NOT FOUND:",
                game_name
            )

            self.messageChanged.emit(
                f"لم يتم العثور على اللعبة: {game_name}"
            )

            return

        selected_version_root = None
        if game_name in VERSIONED_GAME_NAMES:
            selected_version_root = self._resolve_selected_game_version(
                game_name, source
            )
            if selected_version_root is None:
                return
            source = selected_version_root
        selected_game_version = selected_version_root.name if selected_version_root else ""

        game = get_game(game_name)

        if not game:

            print(
                "GAME CONFIG NOT FOUND:",
                game_name
            )

            self.messageChanged.emit(
                "إعدادات اللعبة غير موجودة"
            )

            return

        # ==========================================
        # ADB SOURCE / DESTINATION
        # ==========================================

        copy_parts = []
        target_package = ADB_PACKAGE_IDS.get(game_name, "")

        if game_name == "Free Fire":

            source = source / "com.dts.freefireth"

            print("FREE FIRE DATA SOURCE:", source)

            if not source.exists() or not source.is_dir():
                print("FREE FIRE DATA NOT FOUND:", source)
                self.messageChanged.emit(
                    "مجلد Free Fire com.dts.freefireth غير موجود"
                )
                return

            adb_destination = (
                "/sdcard/Android/data/com.dts.freefireth/"
            )

        elif game_name == "eFootball":
            package_id = ADB_PACKAGE_IDS["eFootball"]
            data_folder = self._find_case_insensitive_child_directory(source, "data")
            if data_folder is None or not data_folder.is_dir():
                self.messageChanged.emit(
                    "مجلد data لمصدر eFootball غير موجود داخل الإصدار المحدد: "
                    + str(source / "data")
                )
                return
            target_package = package_id
            source = data_folder
            copy_parts = [{
                "source": str(data_folder),
                "destination": "SAF://eFootball/data",
                "label": "بيانات eFootball (Ziru_27)",
            }]
            adb_destination = copy_parts[0]["destination"]
            print("EFOOTBALL ZIRU_27 PACKAGE:", package_id)
            print("EFOOTBALL SAF SOURCE:", data_folder)
            print("EFOOTBALL SAF DESTINATION: provider data tree")

        elif game_name in VERSIONED_GAME_NAMES:
            package_id = self._package_id_for_version(game_name, source)
            if not package_id:
                self.messageChanged.emit(
                    "لم أستطع استخراج Package ID من إصدار " + source.name
                    + "؛ أوقفنا النسخ حتى لا نكتب إلى مجلد Android غير صحيح."
                )
                return
            target_package = package_id
            for child_name, destination_root, label in (
                ("data", "/sdcard/Android/data/", "بيانات اللعبة"),
                ("obb", "/sdcard/Android/obb/", "ملفات OBB"),
            ):
                child_folder = self._find_case_insensitive_child_directory(
                    source, child_name
                )
                if child_folder is not None:
                    package_child = self._find_case_insensitive_child_directory(
                        child_folder, package_id
                    )
                    if package_child is not None:
                        child_folder = package_child
                    copy_parts.append({
                        "source": str(child_folder),
                        "destination": destination_root + package_id + "/",
                        "label": label,
                    })
            if not copy_parts:
                self.messageChanged.emit(
                    "الإصدار المحدد لا يحتوي مجلدي data أو obb قابلين للنسخ: "
                    + str(source)
                )
                return
            adb_destination = copy_parts[0]["destination"]
            print("SELECTED GAME VERSION:", source.name)
            print("VERSION PACKAGE ID:", package_id)
            print("VERSION COPY PARTS:", copy_parts)

        elif game_name == "PUBG Mobile":

            # Select one of the sibling folders under the PUBG game directory.
            # Saved is the full package; Medium and Low are user-prepared variants.
            size_folder = self._pubg_source_size
            selected_source = self._find_case_insensitive_child_directory(
                source, size_folder
            )
            if selected_source is None:
                labels = {"Saved": "كامل", "Medium": "متوسط", "Low": "منخفض"}
                self.messageChanged.emit(
                    "مجلد حجم PUBG المحدد غير موجود: "
                    + str(source / size_folder)
                    + " (الحجم: "
                    + labels.get(size_folder, size_folder)
                    + ")"
                )
                return

            if size_folder in ("Medium", "Low"):
                nested_saved = self._find_case_insensitive_child_directory(
                    selected_source, "Saved"
                )
                if nested_saved is None:
                    self.messageChanged.emit(
                        "مجلد Saved داخل نسخة PUBG المحددة غير موجود: "
                        + str(selected_source / "Saved")
                    )
                    return
                selected_source = nested_saved
                print("PUBG VARIANT CONTAINS SAVED; COPYING ITS CONTENTS:", nested_saved)

            if selected_source.name.casefold() != "saved":
                self.messageChanged.emit(
                    "مصدر PUBG المحدد ليس مجلد Saved؛ أوقفنا النقل لمنع نسخ ملفات خارج Saved."
                )
                return

            source = selected_source
            adb_destination = (
                "/sdcard/Android/data/com.tencent.ig/"
                "files/UE4Game/ShadowTrackerExtra/"
                "ShadowTrackerExtra/Saved/"
            )

            print("PUBG SELECTED SIZE:", size_folder)
            print("PUBG SOURCE:", source)

        elif game_name == "PUBG Mobile Flash":

            if not source.is_dir():
                self.messageChanged.emit(
                    "مجلد مصدر PUBG FLASH غير موجود: " + str(source)
                )
                return

            size_folder = self._pubg_source_size
            selected_source = self._find_case_insensitive_child_directory(
                source, size_folder
            )
            if selected_source is None:
                labels = {"Saved": "كامل", "Medium": "متوسط", "Low": "منخفض"}
                self.messageChanged.emit(
                    "مجلد حجم PUBG FLASH المحدد غير موجود: "
                    + str(source / size_folder)
                    + " (الحجم: "
                    + labels.get(size_folder, size_folder)
                    + ")"
                )
                return

            if size_folder in ("Medium", "Low"):
                nested_saved = self._find_case_insensitive_child_directory(
                    selected_source, "Saved"
                )
                if nested_saved is None:
                    self.messageChanged.emit(
                        "مجلد Saved داخل نسخة PUBG FLASH المحددة غير موجود: "
                        + str(selected_source / "Saved")
                    )
                    return
                selected_source = nested_saved

            if selected_source.name.casefold() != "saved":
                self.messageChanged.emit(
                    "مصدر PUBG FLASH المحدد ليس مجلد Saved؛ أوقفنا النقل لمنع نسخ ملفات خارج Saved."
                )
                return

            source = selected_source
            adb_destination = ADB_GAME_DESTINATIONS[game_name]
            print("PUBG FLASH SELECTED SIZE:", size_folder)
            print("PUBG FLASH SOURCE:", source)
            print("PUBG FLASH DESTINATION:", adb_destination)

        elif game_name == "Call of Duty Mobile":

            # COD data is stored inside the package-named directory.
            source = source / "com.activision.callofduty.shooter"

            adb_destination = (
                "/sdcard/Android/data/com.activision.callofduty.shooter/"
            )

            print("COD PACKAGE SOURCE:", source)

            if not source.exists() or not source.is_dir():

                print(
                    "COD PACKAGE SOURCE NOT FOUND:",
                    source
                )

                self.messageChanged.emit(
                    "مجلد COD package غير موجود"
                )

                return

        else:

            adb_destination = ADB_GAME_DESTINATIONS.get(
                game_name
            )

            if not adb_destination:
                # USB storage can receive ordinary game folders even when a
                # game has no Android package destination.
                print(
                    "ADB DESTINATION NOT CONFIGURED; USB COPY REMAINS AVAILABLE:",
                    game_name
                )

        # ==========================================
        # DEBUG
        # ==========================================

        print("SOURCE:", source)
        print("SOURCE EXISTS:", source.exists())
        print("SOURCE IS DIR:", source.is_dir())
        print("ADB DEVICES:", target_devices)
        print("ADB DESTINATION:", adb_destination)

        # Avoid a duplicate source-tree walk; only the transfer engine enumerates the source for progress.

        self.currentGame = game_name

        self.messageChanged.emit(
            f"جاري تجهيز {game_name} للنسخ..."
        )

        # ==========================================
        # START ADB TRANSFER
        # ==========================================

        # Pass the source root unchanged. ADBEngine either transfers this
        # directory to the exact remote parent (when names match) or passes
        # direct children to the target root, avoiding nested Saved/saved.
        push_source = str(source)

        started = 0
        for serial in target_devices:
            existing = self._active_operations.get(serial)
            if existing and not existing.get("completed", False):
                self.messageChanged.emit(
                    f"الجهاز المحدد لديه عملية نقل قيد التنفيذ: {serial}"
                )
                continue

            device_info = self._device_info.get(serial, {})
            device_name = str(
                device_info.get("display_name")
                or device_info.get("name")
                or device_info.get("model")
                or serial
            )
            target_type = str(device_info.get("type") or "android")
            target_units = 1

            if target_type == "usb_storage":
                # Re-check the volume serial before writing. A removed drive
                # must never be replaced by a different device using the same
                # Windows drive letter.
                live_usb = {
                    str(item.get("serial") or ""): item
                    for item in enumerate_removable_storage_devices()
                }
                live_device = live_usb.get(serial)
                if live_device is None:
                    self.messageChanged.emit(
                        "تم فصل الفلاشة أو تغيرت وحدتها: " + device_name
                    )
                    continue

                target_root = Path(str(live_device.get("root") or ""))
                if not target_root.is_dir():
                    self.messageChanged.emit(
                        "مسار الفلاشة غير متاح: " + str(target_root)
                    )
                    continue

                source_drive = os.path.splitdrive(os.path.abspath(str(source)))[0].casefold()
                target_drive = os.path.splitdrive(os.path.abspath(str(target_root)))[0].casefold()
                if source_drive and source_drive == target_drive:
                    self.messageChanged.emit(
                        "المصدر والفلاشة على نفس وحدة التخزين. اختر مصدرًا من قرص آخر لتجنب النسخ داخل نفسه."
                    )
                    continue

                local_destination = target_root / "CN PlayHub" / game_name
                if selected_game_version:
                    local_destination = local_destination / selected_game_version
                if game_name == "eFootball":
                    local_destination = local_destination / "data"
                operation = {
                    "serial": serial,
                    "model": device_name,
                    "device_name": device_name,
                    "target_type": "usb_storage",
                    "units": 1,
                    "unit_price": 500,
                    "billing_value": 500,
                    "game": game_name,
                    "game_version": selected_game_version,
                    "kind": "copy",
                    "source": push_source,
                    "destination": str(local_destination),
                    "status": "جاري التجهيز",
                    "percent": 0,
                    "operation": "جاري تجهيز الفلاشة للنسخ",
                    "speed": "--",
                    "eta": "--",
                    "error": "",
                    "completed": False,
                    "cancelled": False,
                    "history_recorded": False,
                    "source_label": "HDD" if game_name == "PUBG Mobile Flash" else self._source_label(),
                    "source_root": str(selected_source_root),
                    "game_size": self._pubg_source_size if game_name in ("PUBG Mobile", "PUBG Mobile Flash") else "",
                    "phase": "local_usb_copy",
                }
                self._start_operation(operation)
                started += 1
                continue

            if not adb_destination:
                self.messageChanged.emit(
                    "لا توجد وجهة Android مهيأة لهذه اللعبة؛ يمكن نسخها إلى الفلاشة فقط."
                )
                continue

            operation = {
                "serial": serial,
                "model": device_name,
                "device_name": device_name,
                "target_type": "android",
                "units": 1,
                "unit_price": 500,
                "billing_value": 500,
                "game": game_name,
                "game_version": selected_game_version,
                "kind": "copy",
                "source": str(copy_parts[0]["source"]) if copy_parts else push_source,
                "destination": str(copy_parts[0]["destination"]) if copy_parts else adb_destination,
                "copy_parts": copy_parts,
                "copy_part_index": 0,
                "status": "جاري التجهيز",
                "percent": 0,
                "operation": "جاري بدء العملية",
                "speed": "--",
                "eta": "--",
                "error": "",
                "completed": False,
                "cancelled": False,
                "history_recorded": False,
                "source_label": "HDD" if game_name == "PUBG Mobile Flash" else self._source_label(),
                "source_root": str(selected_source_root),
                "game_size": self._pubg_source_size if game_name in ("PUBG Mobile", "PUBG Mobile Flash") else "",
                "target_package": target_package,
                "transfer_backend": "efootball_saf" if game_name == "eFootball" else "adb",
                "shizuku_fallback_attempted": False,
                "phase": "efootball_saf" if game_name == "eFootball" else "adb_direct"
            }
            self._start_operation(operation)
            started += 1

        if not started:
            self._reject_operation(
                "كل الهواتف المحددة مشغولة بعمليات نقل حالية."
            )

    def _pull_comparison_for_game(self, game_name):
        """Return the original PC copy to compare against during a phone pull."""
        if str(game_name) != "PUBG Mobile":
            return None

        # The original PUBG data stays on D:\\games; pulled files are saved
        # separately under D:\\games\\من الهاتف and never written back here.
        game_root = Path(self.destination_root) / "ببجي (PUBG)"
        size_folder = str(self._pubg_source_size or "Saved")
        selected_source = self._find_case_insensitive_child_directory(
            game_root, size_folder
        )
        if selected_source is None:
            return None

        if size_folder.casefold() in ("medium", "low"):
            nested_saved = self._find_case_insensitive_child_directory(
                selected_source, "Saved"
            )
            if nested_saved is not None:
                selected_source = nested_saved

        return selected_source

    def _pull_destination_for_game(self, game_name):
        if str(game_name) == "eFootball":
            # Ziru_27 exposes this tree through SAF, not a normal ADB path.
            return None
        game_config = get_game(str(game_name))
        if not game_config:
            return None

        source_root = (
            build_usb_source_root()
            if game_name == "PUBG Mobile Flash"
            else self.source_root
        )
        source_folder = find_game_folder(source_root, game_name)

        if game_name in VERSIONED_GAME_NAMES:
            if source_folder is None:
                return None
            selected_version = self._resolve_selected_game_version(
                game_name, source_folder, notify=False
            )
            if selected_version is None:
                return None
            try:
                relative = selected_version.relative_to(source_root) / "data"
            except ValueError:
                return None
        elif game_name == "PUBG Mobile":
            # Mirror the exact source selected by copyGame(): Saved, Medium,
            # or Low. Medium/Low may contain a nested Saved directory; when
            # present, pushGame transfers that nested directory, so pull must
            # target the same relative path rather than always using Saved.
            if source_folder is None:
                source_folder = source_root / str(
                    game_config.get("source") or game_name
                )
            size_folder = str(self._pubg_source_size or "Saved")
            selected_source = self._find_case_insensitive_child_directory(
                source_folder, size_folder
            )
            if selected_source is None:
                return None

            if size_folder.casefold() in ("medium", "low"):
                nested_saved = self._find_case_insensitive_child_directory(
                    selected_source, "Saved"
                )
                if nested_saved is not None:
                    selected_source = nested_saved

            try:
                relative = selected_source.relative_to(source_root)
            except ValueError:
                return None
        elif source_folder is not None:
            try:
                relative = source_folder.relative_to(source_root)
            except ValueError:
                return None
        else:
            relative = Path(str(game_config.get("source") or game_name))

        if relative.is_absolute() or ".." in relative.parts:
            return None

        if game_name == "Free Fire":
            relative = relative / "com.dts.freefireth"
        elif game_name == "Call of Duty Mobile":
            relative = relative / "com.activision.callofduty.shooter"

        return self.destination_root / "من الهاتف" / relative

    @Slot(str, result=str)
    def getPhoneGameDestination(self, game_name):
        game_name = str(game_name or "")
        if game_name == "eFootball":
            return ""
        if game_name in VERSIONED_GAME_NAMES:
            package_id = self._selected_game_package_id(game_name)
            return "/sdcard/Android/data/" + package_id + "/" if package_id else ""
        return str(ADB_GAME_DESTINATIONS.get(game_name) or "")

    @Slot(str, result=str)
    def getPullLocalDestination(self, game_name):
        destination = self._pull_destination_for_game(str(game_name))
        return str(destination) if destination is not None else ""

    @Slot(str)
    def pullGame(self, game_name):
        """Copy the configured Android game directory back to D:\\games\\من الهاتف."""
        if self._reject_usb_for_android_only_action("نسخ الملفات من الهاتف"):
            return False

        target_devices = list(self.selected_devices)
        if not target_devices and self.selected_device:
            target_devices = [self.selected_device]
        if not target_devices:
            self._reject_operation("حدد هاتفًا واحدًا على الأقل لنسخ ملفاته.")
            return False
        if len(target_devices) != 1:
            self._reject_operation(
                "اختر هاتفًا واحدًا فقط في كل عملية استرجاع؛ "
                "حتى لا تختلط ملفات هاتفين في مجلد اللعبة نفسه."
            )
            return False

        remote_source = self.getPhoneGameDestination(str(game_name))
        destination = self._pull_destination_for_game(str(game_name))
        comparison_destination = self._pull_comparison_for_game(str(game_name))
        if not remote_source or destination is None:
            self._reject_operation(
                "لا توجد وجهة هاتف أو مسار كمبيوتر آمن لهذه اللعبة: " + str(game_name)
            )
            return False
        if str(game_name) == "PUBG Mobile" and (
            comparison_destination is None or not comparison_destination.is_dir()
        ):
            expected_comparison = (
                Path(self.destination_root) / "ببجي (PUBG)"
                / str(self._pubg_source_size or "Saved")
            )
            self._reject_operation(
                "لم يتم العثور على مجلد PUBG الأصلي للمقارنة: "
                + str(expected_comparison)
                + "\nلم يبدأ النقل حتى لا يعيد نسخ ملفات اللعبة الموجودة."
            )
            return False
        started = 0
        for serial in target_devices:
            existing = self._active_operations.get(serial)
            if existing and not existing.get("completed", False):
                self.messageChanged.emit(
                    "الهاتف لديه عملية أخرى قيد التنفيذ: " + str(serial)
                )
                continue

            info = self._device_info.get(serial, {})
            device_name = str(
                info.get("display_name") or info.get("name")
                or info.get("model") or serial
            )
            operation = {
                "serial": serial,
                "model": device_name,
                "device_name": device_name,
                "target_type": "android",
                "units": 1,
                "game": str(game_name),
                "kind": "pull",
                "source": str(remote_source),
                "destination": str(destination),
                "source_label": "الهاتف",
                "source_root": str(remote_source),
                "comparison_destination": (
                    str(comparison_destination) if comparison_destination is not None else ""
                ),
                "status": "جاري مقارنة ملفات الهاتف وتخطي الموجود",
                "percent": 0,
                "operation": "ADB pull إلى الكمبيوتر",
                "speed": "--",
                "eta": "--",
                "error": "",
                "completed": False,
                "cancelled": False,
                "history_recorded": False,
                "phase": "adb_pull",
            }
            self._start_operation(operation)
            started += 1

        if not started:
            self._reject_operation("لم يبدأ النسخ؛ تحقق من انشغال الهواتف المحددة.")
            return False

        self.messageChanged.emit(
            f"بدأ نسخ {game_name} من الهاتف إلى {destination}"
        )
        return True

    @Slot(str)
    def startGameTransfer(self, game_name):
        self.copyGame(game_name)

    @Slot(str)
    def launchGame(self, game_name):
        print()
        print("==============================")
        print("ADB LAUNCH REQUEST:", game_name)
        print("==============================")

        if self._reject_usb_for_android_only_action("launchGame"):
            return

        target_devices = list(self.selected_devices)
        if not target_devices and self.selected_device:
            target_devices = [self.selected_device]

        if not target_devices:
            self._reject_operation(
                "يرجى تحديد هاتف واحد على الأقل قبل تشغيل اللعبة."
            )
            return

        # Launching supports one explicitly selected target. For multi-select,
        # use the first selected serial consistently with selected_device.
        self.selected_device = target_devices[0]
        device = self.selected_device
        package_id = self._selected_game_package_id(game_name)

        if not package_id:
            self.messageChanged.emit(
                f"لا يوجد Package ID للعبة {game_name}"
            )
            return

        def output_text(result):
            return "\n".join(
                str(getattr(result, key, "") or "").strip()
                for key in ("stdout", "stderr")
                if str(getattr(result, key, "") or "").strip()
            ).strip()

        try:
            print("DEVICE:", device)
            print("PACKAGE:", package_id)

            # Verify the exact installed package before resolving its launcher.
            package_result = self.adb_engine._run(
                ["-s", device, "shell", "pm", "path", package_id],
                timeout=20,
            )
            package_output = output_text(package_result)
            package_paths = [
                line.strip()
                for line in package_output.splitlines()
                if line.strip().startswith("package:")
            ]
            if package_result.returncode != 0 or not package_paths:
                detail = package_output or "لم يُرجع Android مسار APK للحزمة."
                message = (
                    f"{game_name} غير مثبتة على الهاتف المحدد أو غير متاحة لهذا المستخدم. "
                    f"Package ID: {package_id}. التفاصيل: {detail}"
                )
                print("ADB LAUNCH PACKAGE CHECK FAILED:", message)
                self.messageChanged.emit(message)
                return

            # Resolve the current launcher activity from the installed APK.
            resolve_result = self.adb_engine._run(
                [
                    "-s", device, "shell", "cmd", "package",
                    "resolve-activity", "--brief",
                    "-a", "android.intent.action.MAIN",
                    "-c", "android.intent.category.LAUNCHER",
                    "-p", package_id,
                ],
                timeout=20,
            )
            resolve_output = output_text(resolve_result)
            component = ""
            for line in resolve_output.splitlines():
                candidate = line.strip()
                if (
                    "/" in candidate
                    and candidate.startswith(package_id + "/")
                    and not candidate.startswith("Warning:")
                ):
                    component = candidate
                    break

            if resolve_result.returncode != 0 or not component:
                detail = resolve_output or "لم يجد Android نشاط تشغيل رئيسيًا للحزمة."
                message = (
                    f"اللعبة مثبتة، لكن Android لم يجد شاشة تشغيل لها. "
                    f"Package ID: {package_id}. التفاصيل: {detail}"
                )
                print("ADB LAUNCH ACTIVITY RESOLUTION FAILED:", message)
                self.messageChanged.emit(message)
                return

            print("ADB LAUNCHER ACTIVITY:", component)
            launch_result = self.adb_engine._run(
                [
                    "-s", device, "shell", "am", "start", "-W",
                    "-a", "android.intent.action.MAIN",
                    "-c", "android.intent.category.LAUNCHER",
                    "-n", component,
                ],
                timeout=45,
            )
            launch_output = output_text(launch_result)
            status_ok = bool(
                re.search(r"(?im)^\s*Status:\s*ok\s*$", launch_output)
            )
            has_error = bool(
                re.search(
                    r"(?im)^\s*(?:Error:|Error type \d+|Exception|"
                    r"Activity class .* does not exist|Permission Denial)",
                    launch_output,
                )
            )
            if launch_result.returncode == 0 and status_ok and not has_error:
                print("ADB LAUNCH RESULT: PASS")
                self.messageChanged.emit(f"تم فتح {game_name}")
                return

            detail = launch_output or (
                f"am start returned exit code {launch_result.returncode} "
                "without Android confirming Status: ok."
            )
            message = (
                f"تعذر تشغيل {game_name} رغم وجود الحزمة. "
                f"النشاط: {component}. التفاصيل: {detail}"
            )
            print("ADB LAUNCH FAILED:", message)
            self.messageChanged.emit(message)

        except Exception as exc:
            print("ADB LAUNCH ERROR:", device, package_id, exc)
            self.messageChanged.emit(
                f"خطأ أثناء تشغيل {game_name} على الهاتف {device}: {exc}"
            )

    @Slot(str)
    def closeGame(self, game_name):
        if self._reject_usb_for_android_only_action("closeGame"):
            return

        target_devices = list(self.selected_devices)
        if not target_devices and self.selected_device:
            target_devices = [self.selected_device]
        if not target_devices:
            self._reject_operation(
                "يرجى تحديد هاتف واحد على الأقل لإغلاق اللعبة."
            )
            return

        package_id = self._selected_game_package_id(game_name)
        if not package_id:
            self.messageChanged.emit(
                f"لا يوجد Package ID للعبة {game_name}"
            )
            return

        for serial in target_devices:
            threading.Thread(
                target=self._close_game_worker,
                args=(serial, game_name, package_id),
                name=f"KafiaNet-CloseGame-{serial}",
                daemon=True
            ).start()

    def _close_game_worker(self, serial, game_name, package_id):
        try:
            success, detail = self.adb_engine.force_stop_app(serial, package_id)
            if success:
                self.messageChanged.emit(
                    f"تم إغلاق {game_name} بصمت على الهاتف {serial}."
                )
            else:
                self.messageChanged.emit(
                    f"لم يتم إغلاق {game_name} على الهاتف {serial}: {detail}"
                )
        except Exception as e:
            self.messageChanged.emit(
                f"تعذر إغلاق {game_name} بصمت على الهاتف {serial}: {e}"
            )


    @Slot(str)
    def uninstallGame(self, game_name):
        if self._reject_usb_for_android_only_action("uninstallGame"):
            return

        target_devices = list(self.selected_devices)
        if not target_devices and self.selected_device:
            target_devices = [self.selected_device]
        if not target_devices:
            self._reject_operation(
                "يرجى تحديد هاتف واحد على الأقل لإزالة تثبيت اللعبة."
            )
            return

        package_id = self._selected_game_package_id(game_name)
        if not package_id:
            self.messageChanged.emit(
                f"لا يوجد Package ID للعبة {game_name}"
            )
            return

        started = 0
        for serial in target_devices:
            existing = self._active_operations.get(serial)
            if existing and not existing.get("completed", False):
                self.messageChanged.emit(
                    f"الهاتف لديه عملية أخرى قيد التنفيذ: {serial}"
                )
                continue

            device = self._device_info.get(serial, {})
            self._start_operation({
                "serial": serial,
                "model": device.get("display_name")
                         or device.get("name")
                         or device.get("model")
                         or serial,
                "game": game_name,
                "kind": "uninstall",
                "package_id": package_id,
                "status": "جاري إزالة التثبيت",
                "percent": -1,
                "operation": "إزالة التطبيق من الهاتف",
                "speed": "--",
                "eta": "--",
                "error": "",
                "completed": False,
                "cancelled": False,
                "history_recorded": False,
                "source_label": self._source_label(),
                "source_root": str(self.source_root),
            })
            started += 1

        if not started:
            self._reject_operation(
                "كل الهواتف المحددة مشغولة بعمليات أخرى."
            )

    @Slot()
    def installAllGames(self):
        """Install every APK/APKS package found under the selected install source."""
        if self._reject_usb_for_android_only_action("تثبيت الألعاب"):
            return False

        target_devices = [
            serial for serial in self.selected_devices
            if self._device_info.get(serial, {}).get("type") != "usb_storage"
        ]
        if not target_devices and self.selected_device:
            target_devices = [self.selected_device]
        if not target_devices:
            self._reject_operation("حدد هاتفًا واحدًا على الأقل من قائمة الأجهزة أولاً.")
            return False

        source_root = Path(self.install_source_path)
        if not source_root.is_dir():
            self._reject_operation("مجلد التثبيت غير موجود: " + str(source_root))
            return False

        entries = self._collect_install_batch()
        if not entries:
            self._reject_operation(
                "لم أعثر على أي ملفات APK أو APKS داخل: " + str(source_root)
            )
            return False

        started = 0
        for serial in target_devices:
            existing = self._active_operations.get(serial)
            if existing and not existing.get("completed", False):
                self.messageChanged.emit(
                    "الهاتف لديه عملية أخرى قيد التنفيذ: " + str(serial)
                )
                continue

            info = self._device_info.get(serial, {})
            device_name = str(
                info.get("display_name") or info.get("name")
                or info.get("model") or serial
            )
            operation = {
                "serial": serial,
                "model": device_name,
                "device_name": device_name,
                "target_type": "android",
                "units": 1,
                "game": "تثبيت الكل",
                "kind": "install",
                "install_type": "batch",
                "batch_entries": entries,
                "apk_count": len(entries),
                "status": "جاري التجهيز",
                "percent": 0,
                "operation": "تثبيت ملفات APK/APKS فقط",
                "speed": "--",
                "eta": "--",
                "error": "",
                "completed": False,
                "cancelled": False,
                "history_recorded": False,
                "source_label": "الكمبيوتر",
                "source": str(source_root),
                "source_root": str(source_root),
                "destination": "Android Package Manager",
            }
            self._start_operation(operation)
            started += 1

        if not started:
            self._reject_operation("كل الهواتف المحددة مشغولة بعمليات حالية.")
            return False

        self.messageChanged.emit(
            f"بدأ تثبيت {len(entries)} حزمة على {started} هاتف/هواتف. "
            "لن يتم نقل ملفات اللعبة."
        )
        return True

    def _install_games_by_amount(self, amount_text, selected_only=False):
        """Install a random game sample on all selected phones or only the primary selected phone."""
        amount = self._normalize_install_amount(amount_text)
        if amount is None or amount < 100:
            self._reject_operation("أدخل مبلغًا صحيحًا لا يقل عن 100.")
            return False
        # The currency amount is mapped through the tariff; it is never used as a game quantity.
        requested_games = self._game_count_for_amount(amount)

        if selected_only:
            selected_phone = str(self.selected_device or "").strip()
            selected_info = self._device_info.get(selected_phone, {})
            if not selected_phone or selected_info.get("type") == "usb_storage":
                self._reject_operation(
                    "حدد هاتف Android واحدًا ليتم التثبيت عليه فقط."
                )
                return False
            target_devices = [selected_phone]
        else:
            if self._reject_usb_for_android_only_action("تثبيت الألعاب حسب المبلغ"):
                return False
            target_devices = [
                serial for serial in self.selected_devices
                if self._device_info.get(serial, {}).get("type") != "usb_storage"
            ]
            if not target_devices and self.selected_device:
                if self._device_info.get(self.selected_device, {}).get("type") != "usb_storage":
                    target_devices = [self.selected_device]
            if not target_devices:
                self._reject_operation("حدد هاتفًا واحدًا على الأقل من قائمة الأجهزة أولاً.")
                return False

        source_root = Path(self.install_source_path)
        if not source_root.is_dir():
            self._reject_operation("مجلد التثبيت غير موجود: " + str(source_root))
            return False

        entries = self._collect_install_batch()
        grouped_entries = {}
        for index, entry in enumerate(entries):
            game_key = str(entry.get("gameKey") or ("package:" + str(index)))
            grouped_entries.setdefault(game_key, []).append(entry)

        if not grouped_entries:
            self._reject_operation(
                "لم أعثر على أي مجلد ألعاب يحتوي ملفات APK أو APKS داخل: "
                + str(source_root)
            )
            return False

        all_game_keys = list(grouped_entries)
        selected_game_keys = secrets.SystemRandom().sample(
            all_game_keys,
            min(requested_games, len(all_game_keys))
        )
        selected_entries = []
        selected_games = []
        for game_key in selected_game_keys:
            game_entries = grouped_entries[game_key]
            selected_entries.extend(game_entries)
            selected_games.append(
                str(game_entries[0].get("gameName") or game_entries[0].get("name") or game_key)
            )

        started = 0
        for serial in target_devices:
            existing = self._active_operations.get(serial)
            if existing and not existing.get("completed", False):
                self.messageChanged.emit(
                    "الهاتف لديه عملية أخرى قيد التنفيذ: " + str(serial)
                )
                continue

            info = self._device_info.get(serial, {})
            device_name = str(
                info.get("display_name") or info.get("name")
                or info.get("model") or serial
            )
            operation = {
                "serial": serial,
                "model": device_name,
                "device_name": device_name,
                "target_type": "android",
                "units": 1,
                "game": f"تثبيت حسب المبلغ {amount}",
                "install_target_scope": "selected_device" if selected_only else "selected_devices",
                "kind": "install",
                "install_type": "batch",
                "batch_entries": selected_entries,
                "apk_count": len(selected_entries),
                "game_count": len(selected_game_keys),
                "requested_game_count": requested_games,
                "available_game_count": len(all_game_keys),
                "selected_games": selected_games,
                "install_amount": amount,
                "status": "جاري التجهيز",
                "percent": 0,
                "operation": f"تثبيت {len(selected_game_keys)} لعبة عشوائية",
                "speed": "--",
                "eta": "--",
                "error": "",
                "completed": False,
                "cancelled": False,
                "history_recorded": False,
                "source_label": "الكمبيوتر",
                "source": str(source_root),
                "source_root": str(source_root),
                "destination": "Android Package Manager",
            }
            self._start_operation(operation)
            started += 1

        if not started:
            self._reject_operation("كل الهواتف المحددة مشغولة بعمليات حالية.")
            return False

        target_message = (
            "على الهاتف المحدد فقط."
            if selected_only
            else f"على {started} هاتف/هواتف محددة."
        )
        message = (
            f"السعر {amount} يقابل {requested_games} لعبة وفق جدول التسعير؛ "
            f"المتاح {len(all_game_keys)} لعبة، وتم اختيار {len(selected_game_keys)} لعبة للتثبيت "
            + target_message
        )
        if len(selected_game_keys) < requested_games:
            message += (
                f" العدد المطلوب {requested_games} لكن المتاح "
                f"{len(selected_game_keys)} فقط."
            )
        self.messageChanged.emit(message)
        return True

    @Slot(str, result=bool)
    def installGamesByAmount(self, amount_text):
        """Install a random sample on all selected Android phones."""
        return self._install_games_by_amount(amount_text, selected_only=False)

    @Slot(str, result=bool)
    def installGamesByAmountOnSelectedPhone(self, amount_text):
        """Install a random sample on the one primary selected Android phone."""
        return self._install_games_by_amount(amount_text, selected_only=True)

    @Slot(str)
    def installGame(self, game_name):

        print()
        print("==============================")
        print("ADB INSTALL REQUEST:", game_name)
        print("==============================")

        if self._reject_usb_for_android_only_action("installGame"):
            return

        target_devices = list(self.selected_devices)
        if not target_devices:
            self._reject_operation(
                "يرجى تحديد هاتف واحد على الأقل قبل بدء التثبيت."
            )
            return

        selected_source_root = (
            build_usb_source_root()
            if game_name == "PUBG Mobile Flash"
            else self.source_root
        )
        if not selected_source_root.exists():

            print(
                "SOURCE NOT FOUND:",
                selected_source_root
            )

            self.messageChanged.emit(
                f"المصدر غير موجود: {selected_source_root}"
            )

            return

        source = find_game_folder(
            selected_source_root,
            game_name
        )

        if source is None:

            print(
                "GAME FOLDER NOT FOUND:",
                game_name
            )

            self.messageChanged.emit(
                f"لم يتم العثور على مجلد اللعبة: {game_name}"
            )

            return

        selected_version_root = None
        if game_name in VERSIONED_GAME_NAMES:
            selected_version_root = self._resolve_selected_game_version(
                game_name, source
            )
            if selected_version_root is None:
                return
            source = selected_version_root
        selected_game_version = selected_version_root.name if selected_version_root else ""

        # ==========================================
        # FIND APK INSIDE GAME FOLDER ONLY
        # ==========================================

        # Never search source.parent.
        # This prevents installing unrelated APK files
        # found outside the selected game's own folder.

        apk_files = [
            p
            for p in source.rglob("*")
            if p.is_file() and p.suffix.lower() in [".apk", ".apks", ".apk+", ".xapk"]
        ]

        if not apk_files:

            print(
                "GAME APK NOT FOUND:",
                game_name
            )

            self.messageChanged.emit(
                f"لم يتم العثور على APK الخاص بـ {game_name}"
            )

            return

        if len(apk_files) > 1:

            print(
                "MULTIPLE GAME APK FILES:",
                game_name
            )

            for candidate in apk_files:
                print(
                    "APK CANDIDATE:",
                    candidate
                )

        # Prefer a real APK. If only an APKS bundle exists,
        # let ADBEngine install all split APKs together.

        xapk_bundles = [
            p for p in apk_files
            if p.suffix.lower() == ".xapk"
        ]

        normal_apks = [
            p for p in apk_files
            if p.suffix.lower() == ".apk"
        ]

        apks_bundles = [
            p for p in apk_files
            if p.suffix.lower() == ".apks"
        ]

        if xapk_bundles:
            apk = xapk_bundles[0]
            install_type = "xapk"
        elif normal_apks:
            apk = normal_apks[0]
            install_type = "apk"
        elif apks_bundles:
            apk = apks_bundles[0]
            install_type = "apks"
        else:
            self.messageChanged.emit(
                f"لم يتم العثور على APK صالح لـ {game_name}"
            )
            return

        print(
            "GAME APK SELECTED:",
            apk
        )

        print("DEVICES:", target_devices)
        print("APK:", apk)
        print("INSTALL TYPE:", install_type)

        self.currentGame = game_name

        self.messageChanged.emit(
            f"جاري تثبيت {game_name} على {len(target_devices)} جهاز..."
        )

        started = 0
        for serial in target_devices:
            existing = self._active_operations.get(serial)
            if existing and not existing.get("completed", False):
                self.messageChanged.emit(
                    f"الهاتف المحدد لديه عملية نقل قيد التنفيذ: {serial}"
                )
                continue

            self._start_operation(
                {
                    "serial": serial,
                    "model": self._device_info.get(
                        serial,
                        {}
                    ).get("display_name")
                    or self._device_info.get(serial, {}).get("name")
                    or self._device_info.get(serial, {}).get("model")
                    or serial,
                    "game": game_name,
                    "game_version": selected_game_version,
                    "kind": "install",
                    "apk": str(apk),
                    "install_type": install_type,
                    "status": "جاري التجهيز",
                    "percent": -1,
                    "operation": "جاري بدء العملية",
                    "speed": "--",
                    "eta": "--",
                    "error": "",
                    "completed": False,
                    "cancelled": False,
                    "history_recorded": False,
                    "source_label": "HDD" if game_name == "PUBG Mobile Flash" else self._source_label(),
                    "source_root": str(selected_source_root)
                }
            )
            started += 1

        if not started:
            self._reject_operation(
                "كل الهواتف المحددة مشغولة بعمليات تثبيت حالية."
            )

    @Slot()
    def pauseTransfer(self):
        paused_any = False

        for serial, operation in list(self._active_operations.items()):
            if operation.get("completed", False):
                continue

            engine = operation.get("engine")
            if engine is None:
                continue

            try:
                paused_any = bool(engine.pause()) or paused_any
            except Exception as e:
                print("PAUSE ERROR:", serial, e)

        if paused_any != self._transfer_paused:
            self._transfer_paused = paused_any
            self.transferPausedChanged.emit(paused_any)

        if not paused_any:
            self.messageChanged.emit(
                "لا توجد عملية نقل نشطة يمكن إيقافها مؤقتًا الآن."
            )

    @Slot()
    def resumeTransfer(self):
        resumed_any = False

        for serial, operation in list(self._active_operations.items()):
            if operation.get("completed", False):
                continue

            engine = operation.get("engine")
            if engine is None:
                continue

            try:
                resumed_any = bool(engine.resume()) or resumed_any
            except Exception as e:
                print("RESUME ERROR:", serial, e)

        if resumed_any and self._transfer_paused:
            self._transfer_paused = False
            self.transferPausedChanged.emit(False)

        if not resumed_any:
            self.messageChanged.emit(
                "لا توجد عملية نقل متوقفة مؤقتًا لاستئنافها."
            )

    @Slot()
    def cancelTransfer(self):
        if not self._operation_busy:
            return

        self.messageChanged.emit(
            "جاري إلغاء العمليات المحددة..."
        )
        for serial in list(self._active_operations):
            self.cancelDevice(serial)

    @Slot(str)
    def cancelDevice(self, serial):
        operation = self._active_operations.get(serial)
        if operation is None or operation.get("completed"):
            return

        operation["cancelled"] = True
        operation["status"] = "ملغي"
        operation["operation"] = "جاري إلغاء العملية"
        self._update_operation(serial)
        engine = operation.get("engine")
        if engine is not None:
            engine.cancel()


def main():
    diagnostic_log_path = _configure_diagnostics()
    print("DIAGNOSTIC LOG PATH:", diagnostic_log_path)
    print("STARTING CN PLAYHUB")

    app = QApplication(sys.argv)
    app.setApplicationName("CN PlayHub")
    app.setApplicationDisplayName("CN PlayHub")
    app.setOrganizationName("CN PlayHub")
    engine = QQmlApplicationEngine()

    try:
        import ui_resources_rc
        app_icon = QIcon(":/assets/app_icon.png")
        if not app_icon.isNull():
            app.setWindowIcon(app_icon)
        qml_entry = QUrl("qrc:/qml/main.qml")
        print("LOADING EMBEDDED QML:", qml_entry.toString())
    except ImportError:
        app_icon_path = BASE_DIR / "assets" / "app_icon.png"
        if app_icon_path.is_file():
            app.setWindowIcon(QIcon(str(app_icon_path)))
        qml_entry = QUrl.fromLocalFile(str(BASE_DIR / "qml" / "main.qml"))
        print("LOADING SOURCE QML:", qml_entry.toString())

    def report_qml_warnings(warnings):
        for warning in warnings or []:
            try:
                detail = warning.toString()
            except Exception:
                detail = str(warning)
            print("QML LOAD WARNING:", detail, file=sys.stderr)

    engine.warnings.connect(report_qml_warnings)
    controller = AppController()
    app.aboutToQuit.connect(controller.releaseFullscreenProtection)
    engine.rootContext().setContextProperty("appController", controller)
    engine.rootContext().setContextProperty("baseDirectory", str(BASE_DIR))
    engine.load(qml_entry)

    root_count = len(engine.rootObjects())
    print("ROOT OBJECTS:", root_count)
    if not root_count:
        print("QML FAILED TO LOAD")
        return 2

    ui_interaction_logger = _UiInteractionLogger(app)
    interaction_windows = 0
    for root_object in engine.rootObjects():
        try:
            root_object.installEventFilter(ui_interaction_logger)
            interaction_windows += 1
        except (AttributeError, RuntimeError) as exc:
            print("UI INTERACTION LOGGER INSTALL WARNING:", exc)
    print("UI INTERACTION LOGGER INSTALLED:", interaction_windows)

    print("QML LOADED SUCCESSFULLY")
    if "--protected-build-smoke-test" in sys.argv:
        root = engine.rootObjects()[0]
        required_objects = (
            "mainSidebar",
            "dashboardPage",
            "transferPanel",
            "fullscreenLockButton",
            "windowUnlockDialog",
            "transferCompletedCountToggle",
        )
        found_objects = {
            name: root.findChild(QObject, name)
            for name in required_objects
        }
        missing_objects = [
            name for name, obj in found_objects.items() if obj is None
        ]
        if missing_objects:
            print(
                "UI_SMOKE_TEST_FAILED: missing QML objects: "
                + ", ".join(missing_objects),
                file=sys.stderr,
            )
            return 3

        expected_game_count = len(controller.getDashboardGames())
        if int(root.property("totalGames") or 0) != expected_game_count:
            print(
                "UI_SMOKE_TEST_FAILED: sidebar game count does not match "
                "the discovered game folders.",
                file=sys.stderr,
            )
            return 7

        root.setProperty("sidebarCollapsed", True)
        if int(root.property("sidebarWidth") or 0) != 78:
            print(
                "UI_SMOKE_TEST_FAILED: collapsed sidebar width is incorrect.",
                file=sys.stderr,
            )
            return 8
        root.setProperty("sidebarCollapsed", False)
        if int(root.property("sidebarWidth") or 0) != 240:
            print(
                "UI_SMOKE_TEST_FAILED: expanded sidebar width is incorrect.",
                file=sys.stderr,
            )
            return 9
        print("SIDEBAR_COLLAPSE_UI_SMOKE_TEST: PASS")
        print("DASHBOARD_GAME_COUNT_SMOKE_TEST: PASS")

        # Simulate the controller's real transfer-list signal. The panel is
        # hidden while idle, so this verifies the signal that reveals it during
        # a genuine copy/install operation.
        controller.transferBatchChanged.emit([
            {
                "serial": "CI-SMOKE-DEVICE",
                "model": "CI Smoke Device",
                "game": "PUBG Mobile",
                "kind": "copy",
                "status": "جاري النسخ",
                "percent": 25,
                "operation": "اختبار ظهور لوحة النقل",
                "speed": "1 MB/s",
                "eta": "00:01",
                "error": "",
                "cancelled": False,
                "raw_error": "",
            }
        ])
        for _ in range(3):
            app.processEvents()

        panel = found_objects["transferPanel"]
        if not bool(panel.property("visible")):
            print(
                "UI_SMOKE_TEST_FAILED: transfer panel did not become visible "
                "after the transfer-list signal.",
                file=sys.stderr,
            )
            return 4

        root.setProperty("activePage", "سجل النقل")
        for _ in range(5):
            app.processEvents()
        history_status_list = root.findChild(QObject, "transferHistoryStatusList")
        success_devices_list = root.findChild(QObject, "successDevicesVerticalList")
        if history_status_list is None or success_devices_list is None:
            print(
                "UI_SMOKE_TEST_FAILED: split transfer-history columns did not load.",
                file=sys.stderr,
            )
            return 5

        print("DASHBOARD_UI_SMOKE_TEST: PASS")
        print("TRANSFER_PANEL_SIGNAL_SMOKE_TEST: PASS")
        print("HISTORY_SPLIT_UI_SMOKE_TEST: PASS")
        root.setProperty("activePage", "الإعدادات")
        for _ in range(5):
            app.processEvents()
        settings_switch = root.findChild(QObject, "oemCompatibilitySwitch")
        if settings_switch is None:
            print(
                "UI_SMOKE_TEST_FAILED: real OEM diagnostics settings page did not load.",
                file=sys.stderr,
            )
            return 6

        print("WINDOW_LOCK_UI_SMOKE_TEST: PASS")
        print("OEM_DIAGNOSTICS_SETTINGS_UI_SMOKE_TEST: PASS")
        print("PROTECTED_BUILD_SMOKE_TEST: PASS")
        return 0

    print("STARTING QT EVENT LOOP")
    exit_code = app.exec()
    print("QT EVENT LOOP ENDED:", exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
