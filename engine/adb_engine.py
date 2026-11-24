import os
import re
import subprocess
import threading
import time
import ctypes
import json
import secrets
import urllib.request
import urllib.error
import http.client
from urllib.parse import quote
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Slot


BASE_DIR = Path(__file__).resolve().parent.parent



# ============================================================
# INTERNAL ADB I/O PROGRESS MONITOR
# ============================================================

def monitor_adb_io(
    pid,
    callback,
    stop_event,
    interval=0.20
):

    try:
        import psutil
    except Exception:
        return

    try:
        process = psutil.Process(pid)
    except Exception:
        return

    try:
        previous_bytes = (
            process.io_counters().read_bytes
        )
    except Exception:
        return

    previous_time = time.monotonic()

    try:

        while not stop_event.is_set():

            try:
                if not process.is_running():
                    break
            except Exception:
                break

            time.sleep(interval)

            try:
                current_bytes = (
                    process.io_counters().read_bytes
                )
            except Exception:
                break

            now = time.monotonic()

            delta_bytes = max(
                current_bytes - previous_bytes,
                0
            )

            elapsed = max(
                now - previous_time,
                0.001
            )

            speed = (
                delta_bytes / elapsed
            )

            previous_bytes = current_bytes
            previous_time = now

            try:
                callback(
                    delta_bytes,
                    speed
                )
            except Exception:
                pass

    except Exception:
        pass


class ADBEngine(QObject):

    progressChanged = Signal(int, str, str)
    statusChanged = Signal(str)
    finished = Signal(bool, str)
    devicesChanged = Signal(list)
    logChanged = Signal(str)

    def __init__(self):
        super().__init__()

        self.adb = self._get_adb_path()

        self._pause_event = threading.Event()
        self._pause_event.set()

        self._cancel_event = threading.Event()

        self._thread = None
        self._current_process = None
        self._process_lock = threading.Lock()
        self._process_paused = False
        self._reconnect_retry_count = 0
        self._progress_offset_bytes = 0
        self._progress_offset_percent = 0
        self._cancelled_by_user = False
        self._bridge_transfer_active = False
        self._current_http_connection = None
        self._shizuku_cleanup_state = {}

    def _get_adb_path(self):
        # Prefer the ADB binary bundled with this application. This keeps
        # packaged builds independent of a machine-specific D:\games path.
        local_adb = BASE_DIR / "platform-tools" / "adb.exe"
        if local_adb.is_file():
            return str(local_adb)

        # Development fallback for the shared workstation installation.
        external_adb = Path(
            r"D:\games\programe files\platform-tools\adb.exe"
        )
        if external_adb.is_file():
            return str(external_adb)

        # Final fallback: allow adb.exe from PATH.
        return "adb"

    def _run(self, args, timeout=None):

        command = [
            self.adb,
            *args
        ]

        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=startupinfo,
            creationflags=0x08000000,
            timeout=timeout
        )

    def _run_install_command(self, args, timeout):
        command = [
            self.adb,
            *args
        ]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="ignore",
            startupinfo=startupinfo,
            creationflags=0x08000000
        )
        with self._process_lock:
            self._current_process = process
            cancelled = self._cancel_event.is_set()

        if cancelled:
            process.terminate()

        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            stdout, stderr = process.communicate()
            process.returncode = process.returncode or 1
        finally:
            with self._process_lock:
                if self._current_process is process:
                    self._current_process = None

        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout,
            stderr
        )

    @Slot(result=list)
    def get_devices(self):
        try:
            result = self._run(["devices"], timeout=10)
        except Exception as exc:
            print("ADB DEVICE LIST FAILED:", exc)
            self.devicesChanged.emit([])
            return []

        devices = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line or line.startswith("List of devices"):
                continue

            parts = line.split()
            if len(parts) < 2 or parts[1] != "device":
                continue

            serial = parts[0]

            def read_device_property(args):
                try:
                    property_result = self._run(args, timeout=3)
                    return property_result.stdout.strip()
                except Exception as exc:
                    print("ADB DEVICE PROPERTY WARNING:", serial, exc)
                    return ""

            model = read_device_property([
                "-s", serial, "shell", "getprop", "ro.product.model"
            ]) or "Android"
            manufacturer = read_device_property([
                "-s", serial, "shell", "getprop", "ro.product.manufacturer"
            ]) or "Android"
            device_name = read_device_property([
                "-s", serial, "shell", "settings", "get", "global", "device_name"
            ])

            if not device_name or device_name.lower() in {"null", "unknown"}:
                device_name = f"{manufacturer} {model}".strip()

            devices.append({
                "serial": serial,
                "model": model,
                "manufacturer": manufacturer,
                "name": device_name
            })

        self.devicesChanged.emit(devices)
        return devices

    @Slot(str, result="QVariantMap")
    def inspect_device_compatibility(self, serial):
        """Read actual ADB/OEM facts only; this probe never writes to the phone."""
        serial = str(serial or "").strip()
        if not serial:
            return {"ok": False, "message": "معرّف الهاتف غير محدد."}

        try:
            device_result = self._run(["devices"], timeout=8)
        except Exception as exc:
            return {"ok": False, "message": "تعذر قراءة قائمة ADB: " + str(exc)}

        device_state = ""
        for line in str(device_result.stdout or "").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and parts[0] == serial:
                device_state = parts[1]
                break
        if device_state != "device":
            reason = {
                "unauthorized": "الهاتف لم يمنح تصريح ADB لهذا الكمبيوتر؛ وافق على نافذة RSA في الهاتف.",
                "offline": "الهاتف ظاهر في ADB لكنه غير متصل حالياً.",
            }.get(device_state, "الهاتف غير ظاهر كجهاز ADB مصرح.")
            return {
                "ok": False,
                "connected": False,
                "device_state": device_state,
                "message": reason,
            }

        def shell_text(*arguments):
            try:
                result = self._run(
                    ["-s", serial, "shell", *arguments],
                    timeout=5
                )
            except Exception:
                return ""
            if getattr(result, "returncode", 1) != 0:
                return ""
            return str(getattr(result, "stdout", "") or "").strip()

        manufacturer = shell_text("getprop", "ro.product.manufacturer")
        model = shell_text("getprop", "ro.product.model")
        android_release = shell_text("getprop", "ro.build.version.release")
        android_sdk = shell_text("getprop", "ro.build.version.sdk")
        adb_identity = shell_text("id")

        uid_match = re.search(r"\buid=(\d+)", adb_identity)
        adb_uid = int(uid_match.group(1)) if uid_match else None
        if adb_uid == 0:
            identity_label = "Root (uid=0)"
        elif adb_uid == 2000:
            identity_label = "ADB Shell (uid=2000)"
        elif adb_uid is not None:
            identity_label = "UID " + str(adb_uid)
        else:
            identity_label = "تعذر قراءة هوية shell"

        def package_is_installed(package_name):
            try:
                result = self._run(
                    ["-s", serial, "shell", "pm", "path", package_name],
                    timeout=5
                )
            except Exception:
                return None
            if getattr(result, "returncode", 1) != 0:
                return False
            return "package:" in str(getattr(result, "stdout", "") or "")

        shizuku_installed = package_is_installed("moe.shizuku.privileged.api")
        helper_installed = package_is_installed("com.kafianet.shizukubridge")
        try:
            pid_result = self._run(
                ["-s", serial, "shell", "pidof", "shizuku_server"],
                timeout=4
            )
            pid_text = (
                str(getattr(pid_result, "stdout", "") or "").strip()
                if getattr(pid_result, "returncode", 1) == 0 else ""
            )
            server_pid_visible = bool(pid_text)
        except Exception:
            pid_text = ""
            server_pid_visible = False

        brand = (manufacturer + " " + model).casefold()
        if any(name in brand for name in ("xiaomi", "redmi", "poco")):
            oem_setting_name = "USB debugging (Security options)"
            oem_setting_source = "توثيق Shizuku الرسمي لأجهزة MIUI (Xiaomi/POCO)."
        elif any(name in brand for name in ("oneplus", "oppo")):
            oem_setting_name = "Permission monitoring"
            oem_setting_source = "توثيق Shizuku الرسمي لأجهزة ColorOS (OPPO/OnePlus)."
        else:
            oem_setting_name = ""
            oem_setting_source = ""

        return {
            "ok": True,
            "connected": True,
            "device_state": device_state,
            "serial": serial,
            "manufacturer": manufacturer or "غير معروف",
            "model": model or "Android",
            "android_release": android_release or "غير معروف",
            "android_sdk": android_sdk or "غير معروف",
            "adb_shell_identity": adb_identity or "غير متاح",
            "adb_shell_uid": adb_uid,
            "adb_identity_label": identity_label,
            "shizuku_installed": shizuku_installed,
            "shizuku_server_running": True if server_pid_visible else None,
            "shizuku_server_pid": pid_text,
            "helper_installed": helper_installed,
            "oem_setting_name": oem_setting_name,
            "oem_setting_source": oem_setting_source,
            "oem_setting_value_readable": False,
            "oem_setting_note": (
                "لا يستطيع ADB قراءة حالة هذا المفتاح الداخلي بصورة موثوقة؛ "
                "يجب التحقق منه يدوياً داخل إعدادات الهاتف. لم نغيّر أي إعداد."
            ),
            "message": "اكتمل فحص ADB للقراءة فقط؛ لم تُجرَ كتابة أو محاكاة لقيود نظام آخر.",
        }

    def _format_size(self, size):

        units = [
            "B",
            "KB",
            "MB",
            "GB",
            "TB"
        ]

        size = float(size)

        for unit in units:

            if size < 1024:
                return f"{size:.1f} {unit}"

            size /= 1024

        return f"{size:.1f} PB"

    def _format_time(self, seconds):

        seconds = max(int(seconds), 0)

        hours = seconds // 3600

        minutes = (seconds % 3600) // 60

        seconds = seconds % 60

        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

        return f"{minutes:02d}:{seconds:02d}"

    def _get_remote_size(self, device, remote_path):

        try:

            result = self._run(
                [
                    '-s',
                    device,
                    'shell',
                    'stat',
                    '-c',
                    '%s',
                    remote_path
                ],
                timeout=5
            )

            value = result.stdout.strip()

            if value.isdigit():
                return int(value)

        except Exception:
            pass

        return 0

    def _emit_adb_live_progress(
        self,
        line,
        completed_bytes,
        current_file_size,
        source_total_bytes,
        start_time
    ):
        match = re.search(
            r'(\d+)%\s+(\d+(?:\.\d+)?)([KMGTP]?B)/s',
            line
        )

        if not match:
            return

        percent_in_file = int(match.group(1))

        speed_value = float(match.group(2))

        speed_unit = match.group(3)

        multipliers = {
            "B": 1,
            "KB": 1024,
            "MB": 1024 ** 2,
            "GB": 1024 ** 3,
            "TB": 1024 ** 4
        }

        speed = speed_value * multipliers.get(
            speed_unit,
            1
        )

        current_bytes = (
            completed_bytes
            + (
                current_file_size
                * percent_in_file
                / 100.0
            )
        )

        percent = (
            int(current_bytes * 100 / source_total_bytes)
            if source_total_bytes > 0
            else percent_in_file
        )

        if speed > 0 and source_total_bytes > 0:
            remaining_bytes = max(
                source_total_bytes - current_bytes,
                0
            )
            remaining_seconds = int(
                remaining_bytes / speed
            )
            eta = self._format_time(remaining_seconds)
        else:
            eta = "00:00"

        self.progressChanged.emit(
            max(0, min(percent, 100)),
            self._format_size(speed) + "/s",
            eta
        )

    @staticmethod
    def _combined_native_progress_percent(offset_percent, attempt_percent):
        """Keep native ADB progress monotonic when a disconnected copy retries."""
        offset = max(0, min(int(offset_percent), 99))
        attempt = max(0, min(int(attempt_percent), 100))
        combined = offset + ((100 - offset) * attempt / 100.0)
        return max(offset, min(int(combined), 99))

    @staticmethod
    def _parse_adb_progress_details(line):
        """Parse ADB's overall progress line and current local-file progress."""
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(line or "")).strip()
        overall = re.search(r"^\[\s*(\d{1,3})%\s*\]\s*(.*)$", text)
        if overall:
            percent = max(0, min(int(overall.group(1)), 100))
            remainder = overall.group(2).strip()
            file_match = re.match(r"^(.*):\s*(\d{1,3})%\s*$", remainder)
            if file_match:
                file_path = file_match.group(1).strip().strip('"')
                file_percent = max(0, min(int(file_match.group(2)), 100))
                return percent, file_path, file_percent
            file_path = remainder.strip('"')
            return percent, (file_path or None), (percent if file_path else None)

        # Compatibility with platform-tools that emit a per-file percent/rate.
        legacy = re.search(
            r"(?<!\d)(\d{1,3})%\s+(\d+(?:\.\d+)?)([KMGTP]?B)/s",
            text,
            re.IGNORECASE
        )
        if legacy:
            return max(0, min(int(legacy.group(1)), 100)), None, None
        return None, None, None

    @staticmethod
    def _parse_adb_transfer_speed(line):
        """Return measured ADB throughput in bytes/second, when present."""
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(line or ""))
        transfer = re.search(
            r"\(\s*(\d+)\s+bytes\s+in\s+(\d+(?:\.\d+)?)s\s*\)",
            text,
            re.IGNORECASE
        )
        if transfer:
            seconds = float(transfer.group(2))
            if seconds > 0:
                return float(transfer.group(1)) / seconds

        rate = re.search(
            r"(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB|B)/s\b",
            text,
            re.IGNORECASE
        )
        if not rate:
            return None
        multipliers = {
            "B": 1,
            "KB": 1024,
            "MB": 1024 ** 2,
            "GB": 1024 ** 3,
            "TB": 1024 ** 4,
        }
        return float(rate.group(1)) * multipliers[rate.group(2).upper()]

    def _parse_progress(self, line):

        match = re.search(
            r'(\d+)%\s+(\d+(?:\.\d+)?)([KMGTP]?B)/s',
            line
        )

        if not match:
            return

        percent = int(match.group(1))

        speed_value = float(match.group(2))

        speed_unit = match.group(3)

        multipliers = {
            "B": 1,
            "KB": 1024,
            "MB": 1024 ** 2,
            "GB": 1024 ** 3,
            "TB": 1024 ** 4
        }

        speed = speed_value * multipliers.get(
            speed_unit,
            1
        )

        self.progressChanged.emit(
            percent,
            self._format_size(speed) + "/s",
            "00:00"
        )

    def _device_is_connected(self, device):
        """Return whether ADB currently sees this serial as an online device."""
        try:
            result = self._run(["-s", device, "get-state"], timeout=5)
            return (
                result.returncode == 0
                and result.stdout.strip().lower() == "device"
            )
        except Exception as exc:
            print("ADB DEVICE STATE CHECK FAILED:", device, exc)
            return False

    def _wait_for_device_reconnect(self, device):
        self.statusChanged.emit(
            "انقطع اتصال الهاتف. الرجاء إعادة توصيله لاستمرار النسخ..."
        )

        print(
            "ADB DEVICE DISCONNECTED; WAITING:",
            device
        )

        while not self._cancel_event.is_set():
            try:
                result = subprocess.run(
                    [
                        self.adb,
                        "-s",
                        device,
                        "get-state",
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )

                if (
                    result.returncode == 0
                    and result.stdout.strip().lower() == "device"
                ):
                    self.statusChanged.emit(
                        "تمت إعادة توصيل الهاتف — استكمال النسخ..."
                    )

                    print(
                        "ADB DEVICE RECONNECTED:",
                        device
                    )

                    return True

            except Exception as e:
                print(
                    "ADB RECONNECT CHECK ERROR:",
                    e
                )

            time.sleep(2.0)

        return False

    def _remote_file_exists(self, device, remote_file):
        try:
            result = subprocess.run(
                [
                    self.adb,
                    "-s",
                    device,
                    "shell",
                    "test",
                    "-f",
                    remote_file,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="ignore",
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            return result.returncode == 0

        except Exception as e:
            print(
                "ADB REMOTE FILE CHECK ERROR:",
                e
            )
            return None

    @staticmethod
    def _adb_source_plan(source_path, destination):
        """Choose source args/ADB target without nesting a source root.

        When the local directory basename matches the remote directory name,
        pass the directory itself to its remote parent. This keeps very large
        folders to one source argument, including Windows case-only aliases
        such as local "saved" and remote "Saved". Otherwise pass each direct
        child into the exact destination root.
        """
        remote_destination = destination.rstrip("/")
        remote_leaf = remote_destination.rsplit("/", 1)[-1]
        remote_parent = remote_destination.rsplit("/", 1)[0] or "/"

        if (
            source_path.is_dir()
            and source_path.name.casefold() == remote_leaf.casefold()
        ):
            source_alias = source_path.with_name(remote_leaf)
            if source_alias.exists():
                return [str(source_alias)], remote_parent

        if source_path.is_dir():
            return (
                [
                    str(path)
                    for path in sorted(
                        source_path.iterdir(),
                        key=lambda item: item.name.casefold()
                    )
                ],
                remote_destination
            )
        return [str(source_path)], remote_destination

    @staticmethod
    def _shell_quote(value):
        """Quote one path for Android's POSIX shell without invoking a local shell."""
        return "'" + str(value).replace("'", "'\\''") + "'"

    @staticmethod
    def _normalize_android_remote_path(value):
        path = str(value or "").replace("\\\\", "/").replace("\\", "/").rstrip("/")
        if path == "/sdcard":
            return "/storage/emulated/0"
        if path.startswith("/sdcard/"):
            return "/storage/emulated/0/" + path[len("/sdcard/"):]
        return path

    @classmethod
    def _secure_mkdirs_retry_parent(cls, destination_root, adb_output):
        """Return the partially-created nested parent only for a secure_mkdirs denial."""
        match = re.search(
            r"failed to copy '([^']+)' to '([^']+)':\s*remote secure_mkdirs(?:\(\))?\s+failed:\s*([^\r\n]+)",
            str(adb_output or ""),
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        reason = match.group(3).casefold()
        if not any(marker in reason for marker in (
            "operation not permitted", "permission denied", "access denied",
            "eacces", "errno 13",
        )):
            return None

        root = cls._normalize_android_remote_path(destination_root)
        target = cls._normalize_android_remote_path(match.group(2))
        if not root or not target.startswith(root + "/"):
            return None

        parent = target.rsplit("/", 1)[0]
        # Saved itself is never a retry target and is never created by this helper.
        if parent == root or not parent.startswith(root + "/"):
            return None
        return parent

    def _remote_directory_exists(self, device, remote_path):
        """Inspect a remote directory without changing its contents or permissions."""
        command = "test -d " + self._shell_quote(remote_path)
        try:
            result = self._run(
                ["-s", device, "shell", "sh", "-c", command],
                timeout=10,
            )
        except Exception as exc:
            print("ADB REMOTE DIRECTORY CHECK WARNING:", remote_path, exc)
            return False
        return result.returncode == 0

    @staticmethod
    def _combined_progress_percent(total_bytes, offset_bytes, attempt_bytes):
        """Return monotonic overall progress across reconnect retries."""
        if total_bytes <= 0:
            return 0
        transferred = min(max(offset_bytes + attempt_bytes, 0), total_bytes)
        return max(0, min(int(transferred * 100 / total_bytes), 99))

    @staticmethod
    def _relative_source_file(source_root, file_path):
        """Return a validated source-relative path for one interrupted file."""
        root = Path(source_root)
        candidate = Path(file_path)
        try:
            root_resolved = root.resolve(strict=True)
            candidate_resolved = candidate.resolve(strict=True)
            if root.is_dir():
                relative = candidate_resolved.relative_to(root_resolved)
                if not relative.parts:
                    return None
            elif candidate_resolved == root_resolved:
                relative = Path(root.name)
            else:
                return None
            if relative.is_absolute() or not relative.parts or ".." in relative.parts:
                return None
            return relative.as_posix()
        except (OSError, RuntimeError, ValueError):
            return None

    @classmethod
    def _interrupted_source_file(cls, source_root, current_file_path, current_file_percent):
        """Select only the file that was still being written when ADB disconnected."""
        if not current_file_path or current_file_percent is None:
            return None
        try:
            if int(current_file_percent) >= 100:
                return None
        except (TypeError, ValueError):
            return None
        candidate = Path(current_file_path)
        if cls._relative_source_file(source_root, candidate) is None:
            return None
        return candidate if candidate.is_file() else None

    @classmethod
    def _remote_file_target_for_source_file(cls, source_root, file_path, destination):
        """Map one validated source file to its exact destination filename."""
        relative = cls._relative_source_file(source_root, file_path)
        if relative is None:
            raise ValueError("Interrupted source file is outside the transfer root.")
        base = str(destination or "").rstrip("/") or "/"
        return (base + "/" + relative) if base != "/" else ("/" + relative)

    @staticmethod
    def _synced_push_command(adb, device, sources, destination):
        """Use ADB sync to skip destination files that are not older than their source."""
        return [
            str(adb), "-s", str(device), "push", "--sync",
            *[str(source) for source in sources],
            str(destination),
        ]

    def _push_thread(
        self,
        device,
        source,
        destination,
        secure_mkdir_retry_paths=None,
        retry_file=None,
    ):

        process = None
        requested_source = str(source)
        requested_destination = destination
        if secure_mkdir_retry_paths is None:
            secure_mkdir_retry_paths = set()

        try:

            self.statusChanged.emit(
                "جاري نسخ الملفات..."
            )

            source_path = Path(source)

            if not source_path.exists():

                self.finished.emit(
                    False,
                    "المصدر غير موجود"
                )

                return

            # The destination is the exact root for the source contents.
            # Explicit child source arguments avoid ADB appending the source
            # directory name under an already-created destination.
            destination = destination.rstrip("/")

            # --------------------------------------------------------
            # PRE-FLIGHT DESTINATION DIRECTORY CHECK
            # PUBG Saved must already exist. Never create Saved or its parents.
            # --------------------------------------------------------
            check_path = destination.rstrip("/") or destination
            normalized_check_path = check_path.replace("\\", "/").lower()
            is_saved_destination = (
                "/android/data/" in normalized_check_path
                and normalized_check_path.rstrip("/").endswith("/saved")
            )
            permission_markers = (
                "permission denied",
                "access denied",
                "remote secure_mkdirs failed: permission denied",
                "remote open failed: permission denied",
                "remote write failed: permission denied",
                "remote mkdir failed: permission denied",
                "operation not permitted",
                "eacces",
                "errno 13",
            )
            no_space_markers = (
                "no space left on device",
                "enospc",
                "not enough space",
            )

            print("ADB DESTINATION DIRECTORY CHECK:", check_path)

            if not is_saved_destination:
                # Existing non-Saved game destinations retain their current setup behavior.
                mkdir_result = subprocess.run(
                    [
                        self.adb, "-s", device, "shell", "mkdir", "-p", check_path
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    encoding="utf-8",
                    errors="ignore",
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=10,
                )
                if mkdir_result.returncode != 0:
                    check_error = (
                        str(mkdir_result.stderr or "").strip()
                        or str(mkdir_result.stdout or "").strip()
                        or "mkdir failed"
                    )
                    lowered = check_error.lower()
                    if "/android/data/" in normalized_check_path and any(
                        marker in lowered for marker in permission_markers
                    ):
                        failure = "KAFIANET_ANDROID_DATA_PERMISSION_DENIED: " + check_error
                    elif any(marker in lowered for marker in no_space_markers):
                        failure = "KAFIANET_NO_SPACE: " + check_error
                    else:
                        failure = "KAFIANET_DESTINATION_PREPARE_FAILED: " + check_error
                    self.statusChanged.emit("تعذر تجهيز مجلد الوجهة")
                    self.finished.emit(False, failure)
                    return

            # Never create Saved or its parents. Do not preflight Saved with "test -d":
            # some Android builds deny shell inspection even when adb push can write.
            # Let the actual ADB push determine access; only a genuine remote write
            # denial below should activate the Shizuku fallback.
            manifest, total_bytes = self._build_direct_stream_manifest(source_path)

            progress_offset_bytes = min(
                self._progress_offset_bytes,
                total_bytes
            )
            progress_offset_percent = max(
                0,
                min(int(self._progress_offset_percent), 99)
            )

            if source_path.is_dir() and next(source_path.iterdir(), None) is None:
                self.progressChanged.emit(100, "0 B/s", "00:00")
                self.statusChanged.emit("اكتمل النسخ")
                self.finished.emit(True, "المصدر فارغ؛ تم تجهيز مجلد الوجهة")
                return

            adb_sources, adb_destination = self._adb_source_plan(
                source_path,
                destination
            )
            retry_file_path = None
            retry_file_size = 0
            retry_remote_file = ""
            if retry_file:
                retry_relative = self._relative_source_file(source_path, retry_file)
                if retry_relative:
                    retry_file_path = Path(retry_file)
                    retry_file_size = int(retry_file_path.stat().st_size)
                    retry_remote_file = self._remote_file_target_for_source_file(
                        source_path,
                        retry_file_path,
                        destination,
                    )
                    print(
                        "ADB RESUMING INTERRUPTED FILE:",
                        retry_relative,
                        "| bytes:",
                        retry_file_size,
                    )
                else:
                    print("ADB INTERRUPTED FILE RETRY SKIPPED: source path is outside the transfer root")
            if not adb_sources:
                self.progressChanged.emit(100, "0 B/s", "00:00")
                self.statusChanged.emit("اكتمل النسخ")
                self.finished.emit(True, "المصدر فارغ؛ تم تجهيز مجلد الوجهة")
                return
            print("ADB SOURCE ARGUMENT COUNT:", len(adb_sources))
            print("ADB DESTINATION ARGUMENT:", adb_destination)

            print()
            print("========================================")
            print("ADB FAST TRANSFER ENGINE")
            print("========================================")
            print("SOURCE:", source_path)
            print("DESTINATION:", destination)
            print("TOTAL SIZE:", self._format_size(total_bytes))
            print("========================================")

            self._pause_event.set()

            if retry_file_path is not None:
                # Force only the interrupted file. The normal --sync pass that
                # follows will skip files that were already completed.
                command = [
                    self.adb,
                    "-s",
                    device,
                    "push",
                    str(retry_file_path),
                    retry_remote_file,
                ]
            else:
                command = self._synced_push_command(
                    self.adb,
                    device,
                    adb_sources,
                    adb_destination,
                )

            print(
                "ADB FAST PUSH:",
                " ".join(command)
            )

            print(
                "STARTING DIRECT ADB TRANSFER"
            )

            # --------------------------------------------------------
            # DIRECT ADB PROCESS
            # No CMD wrapper.
            # process.pid is the real adb.exe PID.
            # --------------------------------------------------------

            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= (
                subprocess.STARTF_USESHOWWINDOW
            )
            startupinfo.wShowWindow = (
                subprocess.SW_HIDE
            )

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                startupinfo=startupinfo,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW
                )
            )

            # ADB writes its live transfer progress to stdout when stdout is
            # redirected (the normal case for this hidden GUI). Keep both streams
            # captured and parsed, so neither LAN nor local HDD loses progress.
            stdout_chunks = []
            stderr_chunks = []
            progress_stop = threading.Event()
            progress_lock = threading.Lock()
            start_time = time.monotonic()
            progress_state = {
                "bytes": 0,
                "speed": 0.0,
                "last_percent": -1,
                "speed_samples": [],
                "native_seen": False,
                "native_percent": 0,
                "native_display_percent": progress_offset_percent,
                "native_transferred_bytes": progress_offset_bytes,
                "native_speed_seen": False,
                "current_file_path": "",
                "current_file_percent": 0,
                "current_file_time": start_time,
                "file_size_cache": {},
            }

            def progress_snapshot_locked():
                if progress_state["native_seen"]:
                    # Native ADB percent describes the active full-copy attempt.
                    percent = progress_state["native_display_percent"]
                    transferred_bytes = min(
                        total_bytes,
                        max(
                            progress_offset_bytes,
                            int(progress_state["native_transferred_bytes"]),
                        ),
                    )
                else:
                    transferred_bytes = min(
                        progress_offset_bytes + progress_state["bytes"],
                        total_bytes
                    )
                    percent = self._combined_progress_percent(
                        total_bytes,
                        progress_offset_bytes,
                        progress_state["bytes"]
                    )

                percent = max(progress_state["last_percent"], percent)
                progress_state["last_percent"] = percent
                speed = progress_state["speed"]
                if progress_state["native_seen"]:
                    elapsed = max(time.monotonic() - start_time, 0.001)
                    remaining_seconds = (
                        elapsed * max(100 - percent, 0) / max(percent, 1)
                    )
                    eta = (
                        self._format_time(remaining_seconds)
                        if 0 < percent < 100
                        else "--"
                    )
                else:
                    remaining_bytes = max(total_bytes - transferred_bytes, 0)
                    eta = (
                        self._format_time(remaining_bytes / speed)
                        if speed > 0
                        else "--"
                    )
                speed_text = (
                    self._format_size(speed) + "/s"
                    if speed > 0
                    else "0 B/s"
                )
                return percent, speed_text, eta, transferred_bytes

            def emit_progress():
                with progress_lock:
                    percent, speed_text, eta, transferred = progress_snapshot_locked()
                self.progressChanged.emit(percent, speed_text, eta)
                print(
                    "ADB TRANSFER PROGRESS:",
                    f"{percent}%",
                    "|",
                    self._format_size(transferred),
                    "/",
                    self._format_size(total_bytes),
                    "|",
                    speed_text,
                    "| ETA:",
                    eta
                )

            def save_resume_progress(interrupted_file=None):
                with progress_lock:
                    _, _, _, observed_bytes = progress_snapshot_locked()

                if interrupted_file is not None:
                    if retry_file_path is not None:
                        # This is a forced single-file retry. None of its attempt
                        # bytes are committed to the resume checkpoint until success.
                        observed_bytes = progress_offset_bytes
                    else:
                        try:
                            interrupted_size = int(Path(interrupted_file).stat().st_size)
                            interrupted_percent = max(
                                0,
                                min(int(progress_state["current_file_percent"]), 99),
                            )
                            partial_bytes = int(
                                interrupted_size * interrupted_percent / 100
                            )
                            observed_bytes = max(
                                progress_offset_bytes,
                                observed_bytes - partial_bytes,
                            )
                        except (OSError, TypeError, ValueError):
                            observed_bytes = max(progress_offset_bytes, observed_bytes)

                checkpoint = min(
                    total_bytes,
                    max(progress_offset_bytes, int(observed_bytes)),
                )
                self._progress_offset_bytes = checkpoint
                self._progress_offset_percent = (
                    0 if total_bytes <= 0
                    else min(99, int(checkpoint * 100 / total_bytes))
                )
                print(
                    "ADB RESUME CHECKPOINT:",
                    self._progress_offset_percent,
                    "%",
                    "|",
                    self._format_size(checkpoint),
                    "/",
                    self._format_size(total_bytes),
                )
                return checkpoint

            def report_adb_output_line(line):
                overall_percent, file_path, file_percent = (
                    self._parse_adb_progress_details(line)
                )
                measured_speed = self._parse_adb_transfer_speed(line)
                if overall_percent is None and measured_speed is None:
                    return

                now = time.monotonic()
                with progress_lock:
                    if overall_percent is not None:
                        was_native_seen = progress_state["native_seen"]
                        progress_state["native_seen"] = True
                        progress_state["native_percent"] = max(
                            progress_state["native_percent"], overall_percent
                        )
                        if retry_file_path is not None:
                            retry_bytes = int(
                                retry_file_size * progress_state["native_percent"] / 100
                            )
                            native_bytes = min(
                                total_bytes,
                                progress_offset_bytes + retry_bytes,
                            )
                            combined_percent = (
                                0 if total_bytes <= 0
                                else min(99, int(native_bytes * 100 / total_bytes))
                            )
                            progress_state["native_transferred_bytes"] = native_bytes
                        else:
                            combined_percent = self._combined_native_progress_percent(
                                progress_offset_percent,
                                progress_state["native_percent"]
                            )
                            progress_state["native_transferred_bytes"] = min(
                                total_bytes,
                                int(total_bytes * combined_percent / 100),
                            )
                        if was_native_seen:
                            progress_state["native_display_percent"] = max(
                                progress_state["native_display_percent"],
                                combined_percent
                            )
                        else:
                            # Discard any early process-I/O estimate when ADB's
                            # authoritative overall percentage first arrives.
                            progress_state["native_display_percent"] = combined_percent
                            progress_state["last_percent"] = combined_percent

                    samples = progress_state["speed_samples"]
                    if measured_speed is not None and measured_speed > 0:
                        samples.append(measured_speed)
                        progress_state["native_speed_seen"] = True

                    if file_path and file_percent is not None:
                        file_key = os.path.normcase(os.path.normpath(file_path))
                        cache = progress_state["file_size_cache"]
                        if file_key not in cache:
                            try:
                                cache[file_key] = Path(file_path).stat().st_size
                            except (OSError, ValueError):
                                cache[file_key] = 0
                        file_size = cache[file_key]

                        is_new_file = (
                            file_key != progress_state["current_file_path"]
                        )
                        if is_new_file:
                            progress_state["current_file_path"] = file_key
                            progress_state["current_file_percent"] = 0
                            progress_state["current_file_time"] = now

                        previous_percent = progress_state["current_file_percent"]
                        previous_time = progress_state["current_file_time"]
                        if (
                            not is_new_file
                            and file_size > 0
                            and file_percent > previous_percent
                            and now > previous_time
                        ):
                            delta_bytes = (
                                file_size * (file_percent - previous_percent) / 100.0
                            )
                            elapsed = max(now - previous_time, 0.001)
                            file_speed = delta_bytes / elapsed
                            if file_speed > 0:
                                samples.append(file_speed)
                                progress_state["native_speed_seen"] = True

                        if file_percent >= previous_percent:
                            progress_state["current_file_percent"] = file_percent
                            progress_state["current_file_time"] = now

                    if len(samples) > 8:
                        del samples[:-8]
                    if samples:
                        progress_state["speed"] = sum(samples) / len(samples)

                emit_progress()
                print("ADB NATIVE PROGRESS LINE:", line.strip())

            def drain_output(stream, chunks, channel):
                if stream is None:
                    return
                pending = ""
                try:
                    while True:
                        chunk = stream.read1(4096)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        pending += chunk.decode("utf-8", errors="replace")
                        fragments = re.split(r"[\\r\\n]+", pending)
                        pending = fragments.pop()
                        for fragment in fragments:
                            report_adb_output_line(fragment)
                    if pending:
                        report_adb_output_line(pending)
                except Exception as exc:
                    print(f"ADB {channel.upper()} DRAIN WARNING:", exc)

            with self._process_lock:
                self._current_process = process
                cancelled = self._cancel_event.is_set()

            if cancelled:
                process.terminate()

            stdout_reader = threading.Thread(
                target=drain_output,
                args=(process.stdout, stdout_chunks, "stdout"),
                name=f"ADB-stdout-{device}",
                daemon=True
            )
            stderr_reader = threading.Thread(
                target=drain_output,
                args=(process.stderr, stderr_chunks, "stderr"),
                name=f"ADB-stderr-{device}",
                daemon=True
            )
            stdout_reader.start()
            stderr_reader.start()

            def io_progress_callback(io_bytes, io_speed):
                if progress_stop.is_set():
                    return

                delta_bytes = max(int(io_bytes), 0)
                raw_speed = max(float(io_speed), 0.0)
                with progress_lock:
                    progress_state["bytes"] += delta_bytes
                    # Process-I/O speed is only a fallback. ADB's file/summary
                    # measurements take precedence when the tool reports them.
                    if raw_speed > 0 and not progress_state["native_speed_seen"]:
                        samples = progress_state["speed_samples"]
                        samples.append(raw_speed)
                        if len(samples) > 8:
                            del samples[:-8]
                        progress_state["speed"] = sum(samples) / len(samples)
                emit_progress()

            progress_thread = threading.Thread(
                target=monitor_adb_io,
                args=(process.pid, io_progress_callback, progress_stop),
                daemon=True
            )
            progress_thread.start()

            # Drain both pipes while the real adb.exe process is running.
            return_code = process.wait()
            progress_stop.set()
            progress_thread.join(timeout=1.0)
            stdout_reader.join(timeout=2.0)
            stderr_reader.join(timeout=2.0)

            stdout_text = b"".join(stdout_chunks).decode(
                "utf-8", errors="ignore"
            ).strip()
            stderr_text = b"".join(stderr_chunks).decode(
                "utf-8", errors="ignore"
            ).strip()
            combined_output = "\\n".join(
                part for part in (stdout_text, stderr_text) if part
            )

            with self._process_lock:
                if self._current_process is process:
                    self._current_process = None

            # --------------------------------------------------------
            # SUCCESS
            # --------------------------------------------------------

            if self._cancel_event.is_set():
                self.statusChanged.emit(
                    "تم إلغاء النقل"
                )
                self.finished.emit(
                    False,
                    "تم إلغاء النسخ"
                )
                return

            if return_code == 0 and retry_file_path is not None:
                resumed_bytes = min(
                    total_bytes,
                    progress_offset_bytes + retry_file_size,
                )
                self._progress_offset_bytes = resumed_bytes
                self._progress_offset_percent = (
                    0 if total_bytes <= 0
                    else min(99, int(resumed_bytes * 100 / total_bytes))
                )
                self.statusChanged.emit(
                    "تم استكمال الملف الذي انقطع أثناء نقله؛ جارٍ متابعة بقية الملفات..."
                )
                self._push_thread(
                    device,
                    requested_source,
                    requested_destination,
                    secure_mkdir_retry_paths,
                    None,
                )
                return

            if return_code == 0:

                elapsed = max(
                    time.monotonic()
                    - start_time,
                    0.001
                )

                # Use the speed measured by ADB/native file progress. Do not
                # infer throughput from source size because every source file is sent.
                average_speed = progress_state["speed"]

                progress_state["bytes"] = (
                    total_bytes
                )

                self.progressChanged.emit(
                    100,
                    self._format_size(
                        average_speed
                    ) + "/s",
                    "00:00"
                )
                self._reconnect_retry_count = 0

                self.statusChanged.emit(
                    "اكتمل النسخ"
                )

                print()
                print(
                    "========================================"
                )
                print(
                    "ADB FAST TRANSFER COMPLETE"
                )
                print(
                    "TOTAL:",
                    self._format_size(
                        total_bytes
                    )
                )
                print(
                    "TIME:",
                    self._format_time(
                        elapsed
                    )
                )
                print(
                    "AVERAGE SPEED:",
                    self._format_size(
                        average_speed
                    ) + "/s"
                )
                print(
                    "========================================"
                )

                self.finished.emit(
                    True,
                    "تم نسخ اللعبة بنجاح"
                )

                return

            # --------------------------------------------------------
            # FAILURE
            # --------------------------------------------------------

            print(
                "ADB EXIT CODE:",
                return_code
            )

            # ADB/platform-tools versions use different wording for the same
            # remote Android/data denial. Match the remote-side operation and
            # denial separately instead of relying on a small set of exact strings.
            output_lower = (combined_output or "").lower()
            remote_write_operations = (
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
            remote_permission_denials = (
                "permission denied",
                "access denied",
                "operation not permitted",
                "eacces",
                "errno 13",
            )
            if (
                "/android/data/" in str(requested_destination).replace("\\", "/").lower()
                and any(marker in output_lower for marker in remote_write_operations)
                and any(marker in output_lower for marker in remote_permission_denials)
            ):
                combined_output = (
                    "KAFIANET_ANDROID_DATA_PERMISSION_DENIED: "
                    + str(combined_output or "")
                )

            if combined_output:

                print(
                    "ADB OUTPUT:",
                    combined_output
                )

            # secure_mkdirs may create a nested directory and then fail while
            # applying its metadata. Verify the Saved root and exact partial parent
            # read-only, then retry the incomplete file/tree. The following bulk
            # pass uses ADB --sync to avoid retransferring completed files.
            # Never create Saved or delete/rename phone files in this recovery path.
            retry_parent = self._secure_mkdirs_retry_parent(
                requested_destination,
                combined_output,
            )
            max_secure_mkdir_retries = 16
            if (
                retry_parent
                and retry_parent not in secure_mkdir_retry_paths
                and len(secure_mkdir_retry_paths) < max_secure_mkdir_retries
                and not self._cancel_event.is_set()
            ):
                root_exists = self._remote_directory_exists(
                    device, requested_destination
                )
                partial_parent_exists = self._remote_directory_exists(
                    device, retry_parent
                )
                connected = self._device_is_connected(device)
                if root_exists and partial_parent_exists and connected:
                    secure_mkdir_retry_paths.add(retry_parent)
                    secure_retry_file = retry_file_path
                    if secure_retry_file is None:
                        secure_retry_file = self._interrupted_source_file(
                            source_path,
                            progress_state["current_file_path"],
                            progress_state["current_file_percent"],
                        )
                    save_resume_progress(secure_retry_file)
                    self.statusChanged.emit(
                        "تم التحقق من وجود Saved والمجلد الجزئي؛ "
                        "جاري متابعة الملفات غير المكتملة مع تخطي الملفات المنسوخة."
                    )
                    print(
                        "ADB SECURE_MKDIRS FULL RETRY:",
                        retry_parent,
                        "| attempt:",
                        len(secure_mkdir_retry_paths),
                        "| mode: sync-resume",
                    )
                    retry_thread = threading.Thread(
                        target=self._push_thread,
                        args=(
                            device,
                            requested_source,
                            requested_destination,
                            secure_mkdir_retry_paths,
                            secure_retry_file,
                        ),
                        name=f"ADB-secure-mkdir-full-retry-{device}",
                        daemon=True,
                    )
                    self._thread = retry_thread
                    retry_thread.start()
                    return

                print(
                    "ADB SECURE_MKDIRS FULL RETRY SKIPPED:",
                    "saved_root_exists=", root_exists,
                    "partial_parent_exists=", partial_parent_exists,
                    "connected=", connected,
                )

            # A disconnected handset often makes adb push exit with a transport error.
            # Wait for the same serial to return. Re-send only the interrupted file,
            # then let ADB --sync skip files already completed on the device.
            stderr_lower = (combined_output or stderr_text or "").lower()
            disconnect_markers = (
                "device offline",
                "device not found",
                "no devices/emulators found",
                "device disconnected",
                "transport error",
                "connection reset",
                "error: closed",
            )
            looks_disconnected = any(
                marker in stderr_lower
                for marker in disconnect_markers
            )
            device_connected = self._device_is_connected(device)
            if not device_connected or looks_disconnected:
                interrupted_file = retry_file_path
                if interrupted_file is None:
                    interrupted_file = self._interrupted_source_file(
                        source_path,
                        progress_state["current_file_path"],
                        progress_state["current_file_percent"],
                    )
                save_resume_progress(interrupted_file)
                if interrupted_file is not None:
                    print(
                        "ADB RESUME MODE: retry only the interrupted file, "
                        "then continue remaining files with --sync:",
                        interrupted_file,
                    )
                else:
                    print(
                        "ADB RESUME MODE: no incomplete current file identified; "
                        "continue remaining files with --sync."
                    )

                self.statusChanged.emit(
                    "انقطع اتصال الهاتف. الرجاء إعادة توصيله لاستمرار النسخ..."
                )
                print("ADB PUSH INTERRUPTED; WAITING FOR RECONNECT:", device)
                if self._wait_for_device_reconnect(device):
                    if self._cancel_event.is_set():
                        self.finished.emit(
                            False,
                            "تم إلغاء النسخ أثناء انتظار إعادة توصيل الهاتف"
                        )
                        return

                    if self._reconnect_retry_count >= 10:
                        self.finished.emit(
                            False,
                            "تكرر فشل اتصال الهاتف بعد إعادة التوصيل؛ أعد المحاولة يدويًا."
                        )
                        return
                    self._reconnect_retry_count += 1
                    self._process_paused = False
                    self._pause_event.set()
                    retry_thread = threading.Thread(
                        target=self._push_thread,
                        args=(
                            device,
                            requested_source,
                            requested_destination,
                            secure_mkdir_retry_paths,
                            interrupted_file,
                        ),
                        name=f"ADB-push-resume-{device}",
                        daemon=True
                    )
                    self._thread = retry_thread
                    retry_thread.start()
                    return

                self.finished.emit(
                    False,
                    "تعذر استعادة اتصال الهاتف أو تم إلغاء النسخ"
                )
                return

            self.finished.emit(
                False,
                (
                    "فشل النسخ عبر ADB: "
                    + (combined_output or stderr_text)
                    if (combined_output or stderr_text)
                    else "فشل النسخ عبر ADB"
                )
            )

        except Exception as e:

            with self._process_lock:
                if self._current_process is process:
                    self._current_process = None

            print(
                "ADB FAST TRANSFER ERROR:",
                e
            )

            self.finished.emit(
                False,
                str(e)
            )


    @Slot(str, str, str)
    def push(self, device, source, destination):

        if self._thread and self._thread.is_alive():
            return

        self._pause_event.set()
        self._process_paused = False
        self._reconnect_retry_count = 0
        self._progress_offset_bytes = 0
        self._progress_offset_percent = 0

        self._cancel_event.clear()
        self._cancelled_by_user = False

        self._thread = threading.Thread(
            target=self._push_thread,
            args=(
                device,
                source,
                destination
            ),
            daemon=True
        )

        self._thread.start()

    @Slot(str, str, str)
    def pull(self, device, remote_source, local_destination):
        """Pull a game data directory from Android to the PC using ADB only."""
        if self._thread and self._thread.is_alive():
            return

        self._pause_event.set()
        self._process_paused = False
        self._cancel_event.clear()
        self._cancelled_by_user = False
        self._thread = threading.Thread(
            target=self._pull_thread,
            args=(device, remote_source, local_destination),
            name=f"KafiaNet-ADB-Pull-{device}",
            daemon=True
        )
        self._thread.start()

    def _pull_thread(self, device, remote_source, local_destination):
        process = None
        output_tail = []
        progress = 0
        try:
            destination = Path(local_destination)
            destination.parent.mkdir(parents=True, exist_ok=True)
            command = [
                self.adb, "-s", str(device), "pull", "-a",
                str(remote_source), str(destination)
            ]
            self.statusChanged.emit("جاري نسخ ملفات الهاتف إلى الكمبيوتر...")
            startupinfo = None
            if os.name == "nt":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = subprocess.SW_HIDE

            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                startupinfo=startupinfo,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            with self._process_lock:
                self._current_process = process

            started_at = time.monotonic()
            while True:
                if self._cancel_event.is_set():
                    if process.poll() is None:
                        process.terminate()
                    break

                self._pause_event.wait()
                if self._cancel_event.is_set():
                    if process.poll() is None:
                        process.terminate()
                    break

                line = process.stdout.readline() if process.stdout else ""
                if line:
                    cleaned = line.strip()
                    if cleaned:
                        output_tail.append(cleaned)
                        del output_tail[:-12]
                        self.logChanged.emit(cleaned)
                        self.statusChanged.emit(cleaned[:240])
                        match = re.search(r"(\d{1,3})\s*%", cleaned)
                        if match:
                            progress = max(progress, min(99, int(match.group(1))))
                            self.progressChanged.emit(progress, "--", "--")
                elif process.poll() is not None:
                    break
                elif time.monotonic() - started_at > 6 * 60 * 60:
                    process.terminate()
                    raise TimeoutError("تجاوز نسخ الملفات من الهاتف الحد الزمني المسموح.")
                else:
                    time.sleep(0.05)

            return_code = process.wait(timeout=30)
            if self._cancel_event.is_set():
                self.finished.emit(False, "تم إلغاء نسخ الملفات من الهاتف.")
            elif return_code == 0:
                self.progressChanged.emit(100, "--", "00:00")
                self.finished.emit(
                    True,
                    "اكتمل نسخ ملفات الهاتف إلى الكمبيوتر: " + str(destination)
                )
            else:
                details = "\n".join(output_tail[-8:])
                self.finished.emit(
                    False,
                    details or f"فشل ADB pull برمز خروج {return_code}."
                )
        except Exception as exc:
            self.finished.emit(False, f"تعذر نسخ الملفات من الهاتف: {exc}")
        finally:
            if process is not None and process.stdout is not None:
                try:
                    process.stdout.close()
                except Exception:
                    pass
            with self._process_lock:
                if self._current_process is process:
                    self._current_process = None
            self._process_paused = False

    @Slot(str, list)
    def install_batch(self, device, entries):
        """Install APK, APKS, split-APK and XAPK entries from a PC source folder."""
        self._cancel_event.clear()
        self._cancelled_by_user = False

        def worker():
            import re
            import shutil
            import tempfile
            import zipfile
            from pathlib import PurePosixPath

            successes = 0
            failures = []
            total = len(entries or [])
            if total == 0:
                self.finished.emit(False, "لا توجد حزم APK أو XAPK للتثبيت.")
                return

            package_pattern = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")

            for index, entry in enumerate(entries or [], start=1):
                if self._cancel_event.is_set():
                    break

                name = str(entry.get("name") or f"تطبيق {index}")
                entry_type = str(entry.get("type") or "apk")
                apk_paths = []
                obb_targets = []
                xapk_package = ""
                temporary_directory = None
                self.statusChanged.emit(
                    f"جاري تثبيت {name} ({index}/{total})..."
                )

                try:
                    if entry_type == "apks":
                        bundle_path = str(entry.get("path") or "")
                        if not os.path.isfile(bundle_path):
                            raise FileNotFoundError("ملف APKS غير موجود: " + bundle_path)

                        temporary_directory = tempfile.mkdtemp(
                            prefix="kafianet_install_"
                        )
                        with zipfile.ZipFile(bundle_path, "r") as archive:
                            members = [
                                item for item in archive.namelist()
                                if item.lower().endswith(".apk")
                                and not item.endswith("/")
                                and not item.startswith("/")
                                and ".." not in Path(item.replace("\\", "/")).parts
                            ]
                            for member_index, member in enumerate(members, start=1):
                                target_name = (
                                    f"{member_index:03d}_"
                                    + Path(member.replace("\\", "/")).name
                                )
                                target_path = os.path.join(
                                    temporary_directory, target_name
                                )
                                with archive.open(member, "r") as source_file:
                                    with open(target_path, "wb") as target_file:
                                        shutil.copyfileobj(source_file, target_file)
                                apk_paths.append(target_path)
                        if not apk_paths:
                            raise ValueError("لا توجد ملفات APK داخل حزمة APKS.")

                    elif entry_type == "xapk":
                        bundle_path = str(entry.get("path") or "")
                        if not os.path.isfile(bundle_path):
                            raise FileNotFoundError("ملف XAPK غير موجود: " + bundle_path)

                        temporary_directory = tempfile.mkdtemp(
                            prefix="kafianet_xapk_"
                        )
                        obb_package_candidates = set()
                        obb_names = set()
                        with zipfile.ZipFile(bundle_path, "r") as archive:
                            members = archive.infolist()
                            manifest_info = next(
                                (
                                    item for item in members
                                    if not item.is_dir()
                                    and PurePosixPath(
                                        item.filename.replace("\\", "/")
                                    ).name.casefold() == "manifest.json"
                                ),
                                None,
                            )
                            manifest_package = ""
                            if manifest_info is not None:
                                if manifest_info.file_size > 1024 * 1024:
                                    raise ValueError("ملف manifest.json داخل XAPK أكبر من الحجم المتوقع.")
                                with archive.open(manifest_info, "r") as manifest_stream:
                                    manifest_data = json.loads(
                                        manifest_stream.read(1024 * 1024 + 1).decode("utf-8-sig")
                                    )
                                if not isinstance(manifest_data, dict):
                                    raise ValueError("بيانات manifest.json داخل XAPK غير صالحة.")
                                application_data = manifest_data.get("application")
                                if not isinstance(application_data, dict):
                                    application_data = {}
                                manifest_package = str(
                                    manifest_data.get("package_name")
                                    or manifest_data.get("packageName")
                                    or manifest_data.get("package")
                                    or application_data.get("package_name")
                                    or application_data.get("packageName")
                                    or ""
                                ).strip()

                            apk_index = 0
                            obb_index = 0
                            for info in members:
                                normalized_name = str(info.filename or "").replace("\\", "/")
                                member_path = PurePosixPath(normalized_name)
                                if (
                                    info.is_dir()
                                    or member_path.is_absolute()
                                    or ".." in member_path.parts
                                    or (
                                        member_path.parts
                                        and ":" in member_path.parts[0]
                                    )
                                ):
                                    continue

                                suffix = member_path.suffix.casefold()
                                if suffix not in (".apk", ".obb") or not member_path.name:
                                    continue

                                if suffix == ".apk":
                                    apk_index += 1
                                    target_path = os.path.join(
                                        temporary_directory,
                                        f"apk_{apk_index:03d}_{member_path.name}",
                                    )
                                    with archive.open(info, "r") as source_file:
                                        with open(target_path, "wb") as target_file:
                                            shutil.copyfileobj(source_file, target_file)
                                    apk_paths.append(target_path)
                                else:
                                    obb_index += 1
                                    parts = list(member_path.parts)
                                    obb_folder_index = next(
                                        (
                                            part_index for part_index, part in enumerate(parts)
                                            if part.casefold() == "obb"
                                        ),
                                        -1,
                                    )
                                    if (
                                        obb_folder_index >= 0
                                        and len(parts) > obb_folder_index + 2
                                    ):
                                        obb_package_candidates.add(
                                            parts[obb_folder_index + 1]
                                        )
                                    if member_path.name in obb_names:
                                        raise ValueError(
                                            "توجد أسماء ملفات OBB مكررة داخل حزمة XAPK."
                                        )
                                    obb_names.add(member_path.name)
                                    target_path = os.path.join(
                                        temporary_directory,
                                        f"obb_{obb_index:03d}_{member_path.name}",
                                    )
                                    with archive.open(info, "r") as source_file:
                                        with open(target_path, "wb") as target_file:
                                            shutil.copyfileobj(source_file, target_file)
                                    obb_targets.append((target_path, member_path.name))

                        if not apk_paths:
                            raise ValueError("لا توجد ملفات APK صالحة داخل حزمة XAPK.")
                        if len(obb_package_candidates) > 1:
                            raise ValueError("تحتوي حزمة XAPK على ملفات OBB لأكثر من تطبيق.")
                        xapk_package = manifest_package
                        if not xapk_package and obb_package_candidates:
                            xapk_package = next(iter(obb_package_candidates))
                        if obb_targets:
                            if not package_pattern.fullmatch(xapk_package):
                                raise ValueError(
                                    "تعذر تحديد معرّف التطبيق لنسخ ملفات OBB داخل XAPK."
                                )
                            if (
                                obb_package_candidates
                                and xapk_package not in obb_package_candidates
                            ):
                                raise ValueError(
                                    "معرّف التطبيق في manifest لا يطابق مجلد OBB داخل XAPK."
                                )

                    elif entry_type == "apk_group":
                        apk_paths = [
                            str(path) for path in (entry.get("paths") or [])
                            if os.path.isfile(str(path))
                        ]
                        if len(apk_paths) < 2:
                            raise ValueError("حزمة APK المقسمة غير مكتملة.")

                    else:
                        apk_path = str(entry.get("path") or "")
                        if not os.path.isfile(apk_path):
                            raise FileNotFoundError("ملف APK غير موجود: " + apk_path)
                        apk_paths = [apk_path]

                    if self._cancel_event.is_set():
                        break

                    if len(apk_paths) > 1:
                        command = [
                            "-s", str(device), "install-multiple", "-r",
                            *apk_paths
                        ]
                    else:
                        command = [
                            "-s", str(device), "install", "-r", apk_paths[0]
                        ]

                    result = self._run_install_command(command, timeout=3600)
                    output = (
                        str(result.stdout or "").strip()
                        + "\n"
                        + str(result.stderr or "").strip()
                    ).strip()
                    install_success = (
                        result.returncode == 0
                        and "failure" not in output.casefold()
                    )

                    if install_success and obb_targets:
                        remote_obb_directory = (
                            "/sdcard/Android/obb/" + xapk_package
                        )
                        self.statusChanged.emit(
                            f"جاري نسخ ملفات بيانات {name} إلى الهاتف..."
                        )
                        mkdir_result = self._run_install_command(
                            [
                                "-s", str(device), "shell",
                                "mkdir", "-p", remote_obb_directory,
                            ],
                            timeout=120,
                        )
                        if mkdir_result.returncode != 0:
                            install_success = False
                            output = (
                                str(mkdir_result.stderr or mkdir_result.stdout or "").strip()
                                or "تعذر إنشاء مجلد بيانات اللعبة على الهاتف."
                            )
                        else:
                            for local_obb_path, obb_name in obb_targets:
                                if self._cancel_event.is_set():
                                    install_success = False
                                    output = "تم إلغاء نسخ ملفات بيانات XAPK."
                                    break
                                push_result = self._run_install_command(
                                    [
                                        "-s", str(device), "push",
                                        local_obb_path,
                                        remote_obb_directory + "/" + obb_name,
                                    ],
                                    timeout=3600,
                                )
                                if push_result.returncode != 0:
                                    install_success = False
                                    output = (
                                        str(push_result.stderr or push_result.stdout or "").strip()
                                        or "فشل نسخ ملف OBB إلى الهاتف: " + obb_name
                                    )
                                    break

                    if install_success:
                        successes += 1
                        self.logChanged.emit(f"تم تثبيت: {name}")
                    else:
                        failures.append(
                            name + ": " + (output or "فشل أمر التثبيت")
                        )
                        self.logChanged.emit(f"فشل تثبيت: {name}")

                except Exception as exc:
                    failures.append(name + ": " + str(exc))
                    self.logChanged.emit(f"فشل تثبيت {name}: {exc}")
                finally:
                    if temporary_directory:
                        shutil.rmtree(temporary_directory, ignore_errors=True)

                completed = index
                percent = int(completed * 100 / max(total, 1))
                self.progressChanged.emit(
                    percent, "--", f"{completed}/{total}"
                )
                if self._cancel_event.is_set():
                    break

            if self._cancel_event.is_set():
                self.finished.emit(
                    False,
                    f"تم إلغاء التثبيت بعد تثبيت {successes} من {total} تطبيقات."
                )
            elif failures:
                summary = (
                    f"تم تثبيت {successes} من {total} تطبيقات، "
                    f"وفشل {len(failures)}."
                )
                self.finished.emit(False, summary + "\n" + "\n".join(failures[:8]))
            else:
                self.progressChanged.emit(100, "--", "00:00")
                self.finished.emit(
                    True,
                    f"اكتمل تثبيت جميع التطبيقات: {successes}/{total}."
                )

        threading.Thread(
            target=worker,
            name=f"KafiaNet-ADB-Install-Batch-{device}",
            daemon=True
        ).start()

    @Slot(str, str)
    def install_apks(self, device, apks_file):

        self._cancel_event.clear()

        def worker():

            import tempfile
            import zipfile

            temp_dir = None

            try:
                if not os.path.isfile(apks_file):

                    self.finished.emit(
                        False,
                        "ملف APKS غير موجود"
                    )

                    return

                self.statusChanged.emit(
                    "جاري تجهيز حزمة APKS..."
                )

                temp_dir = tempfile.mkdtemp(
                    prefix="kafia_apks_"
                )

                with zipfile.ZipFile(
                    apks_file,
                    "r"
                ) as z:

                    apk_names = [
                        name
                        for name in z.namelist()
                        if name.lower().endswith(".apk")
                    ]

                    for name in apk_names:
                        z.extract(name, temp_dir)

                apk_files = [
                    str(Path(temp_dir) / name)
                    for name in apk_names
                ]

                if not apk_files:

                    self.finished.emit(
                        False,
                        "لم توجد ملفات APK داخل APKS"
                    )

                    return

                self.statusChanged.emit(
                    "جاري تثبيت حزمة Free Fire..."
                )

                result = self._run_install_command(
                    [
                        "-s",
                        device,
                        "install-multiple",
                        "-r",
                        *apk_files
                    ],
                    timeout=3600
                )

                output = (
                    result.stdout.strip()
                    + "\n"
                    + result.stderr.strip()
                ).strip()

                print(
                    "ADB INSTALL-MULTIPLE:",
                    output
                )

                if result.returncode == 0:

                    self.statusChanged.emit(
                        "اكتمل التثبيت"
                    )

                    self.finished.emit(
                        True,
                        "تم تثبيت التطبيق بنجاح"
                    )

                else:

                    self.finished.emit(
                        False,
                        output or "فشل تثبيت حزمة APKS"
                    )

            except Exception as e:

                print(
                    "APKS INSTALL ERROR:",
                    e
                )

                self.finished.emit(
                    False,
                    str(e)
                )

            finally:

                if temp_dir:

                    import shutil

                    try:
                        shutil.rmtree(
                            temp_dir,
                            ignore_errors=True
                        )
                    except Exception:
                        pass

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    @Slot(str, str, str)
    def install_if_missing(self, device, apk, package_id):
        """Install MT Manager only when it is not already present."""
        self._cancel_event.clear()

        def worker():
            try:
                check = self._run(
                    ["-s", device, "shell", "pm", "path", package_id],
                    timeout=30
                )
                check_output = (
                    str(check.stdout or "")
                    + "\n"
                    + str(check.stderr or "")
                ).strip()
                is_installed = (
                    check.returncode == 0
                    and any(
                        line.strip().startswith("package:")
                        for line in str(check.stdout or "").splitlines()
                    )
                )

                if is_installed:
                    self.statusChanged.emit("MT Manager مثبت مسبقًا")
                    self.finished.emit(True, "MT Manager مثبت مسبقًا")
                    return

                if not os.path.isfile(apk):
                    self.finished.emit(
                        False,
                        "ملف MT Manager غير موجود أو مسار الشبكة غير متاح: "
                        + str(apk)
                    )
                    return

                self.statusChanged.emit("جاري تثبيت MT Manager قبل النسخ...")
                result = self._run_install_command(
                    ["-s", device, "install", "-r", apk],
                    timeout=3600
                )
                output = (
                    str(result.stdout or "").strip()
                    + "\n"
                    + str(result.stderr or "").strip()
                ).strip()
                print("ADB INSTALL MT MANAGER:", output or check_output)

                if result.returncode == 0:
                    self.statusChanged.emit("تم تجهيز MT Manager")
                    self.finished.emit(True, "تم تثبيت MT Manager قبل النسخ")
                else:
                    self.finished.emit(
                        False,
                        "تعذر تثبيت MT Manager قبل النسخ: "
                        + (output or check_output or "فشل أمر ADB install")
                    )

            except Exception as e:
                print("MT MANAGER PRE-INSTALL ERROR:", e)
                self.finished.emit(
                    False,
                    f"خطأ أثناء التحقق من MT Manager أو تثبيته: {e}"
                )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    def force_stop_app(self, device, package_id):
        """Stop one installed game silently without opening Android Recents."""
        package_id = str(package_id or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+", package_id):
            return False, "معرّف حزمة اللعبة غير صالح."

        installed = self._run(
            ["-s", device, "shell", "pm", "path", package_id],
            timeout=20
        )
        if (
            installed.returncode != 0
            or "package:" not in str(installed.stdout or "")
        ):
            return False, "اللعبة غير مثبتة على الهاتف."

        result = self._run(
            ["-s", device, "shell", "am", "force-stop", package_id],
            timeout=20
        )
        output = (
            str(result.stdout or "").strip()
            + "\n"
            + str(result.stderr or "").strip()
        ).strip()
        if result.returncode != 0:
            return False, output or "رفض Android أمر إغلاق اللعبة."

        return True, "تم إرسال أمر إيقاف اللعبة بصمت."


    @staticmethod
    def _version_key(value):
        """Parse the numeric release prefix (for example 13.6.0.r1086...)."""
        match = re.match(r"^\s*v?(\d+(?:\.\d+){1,2})", str(value or ""), re.IGNORECASE)
        if not match:
            return None
        parts = tuple(int(part) for part in match.group(1).split("."))
        return (parts + (0, 0, 0))[:3]

    @staticmethod
    def _build_shizuku_start_command():
        """Start Shizuku if needed, supporting 13.6 native and 13.5 legacy starters."""
        return r"""
PKG='moe.shizuku.privileged.api'

is_shizuku_running() {
    if command -v pidof >/dev/null 2>&1 && pidof shizuku_server >/dev/null 2>&1; then
        return 0
    fi
    if ps -A 2>/dev/null | awk '$NF == "shizuku_server" { found=1 } END { exit !found }'; then
        return 0
    fi
    if ps 2>/dev/null | awk '$NF == "shizuku_server" { found=1 } END { exit !found }'; then
        return 0
    fi
    return 1
}

if is_shizuku_running; then
    echo 'KAFIA_SHIZUKU_ALREADY_RUNNING'
    exit 0
fi

pm_line=$(pm path "$PKG" 2>/dev/null | head -n 1)
apk_path="${pm_line#package:}"
if [ -z "$apk_path" ] || [ "$apk_path" = "$pm_line" ]; then
    echo 'Shizuku package is not installed or pm path returned no APK.'
    exit 2
fi
apk_dir="${apk_path%/*}"
starter_exit=1
native_found=0

# Shizuku 13.6.0+: use the official native-library path pattern.
# A shell glob is intentional: Package Manager's lib directory can contain
# symlinks, which a regular-file-only search can skip.
for shizuku_lib in "$apk_dir"/lib/*/libshizuku.so; do
    [ -x "$shizuku_lib" ] || continue
    native_found=1
    echo "KAFIA_SHIZUKU_NATIVE_STARTER:$shizuku_lib"
    "$shizuku_lib"
    starter_exit=$?
    if [ "$starter_exit" -eq 0 ]; then
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
            if is_shizuku_running; then
                echo 'KAFIA_SHIZUKU_STARTED'
                exit 0
            fi
            sleep 1
        done
    fi
done

# Shizuku 13.5.x and earlier: use the generated start.sh when present.
for start_script in \
    "/storage/emulated/0/Android/data/$PKG/start.sh" \
    "/sdcard/Android/data/$PKG/start.sh"; do
    [ -r "$start_script" ] || continue
    echo "KAFIA_SHIZUKU_LEGACY_STARTER:$start_script"
    sh "$start_script"
    starter_exit=$?
    if [ "$starter_exit" -eq 0 ]; then
        for attempt in 1 2 3 4 5 6 7 8 9 10; do
            if is_shizuku_running; then
                echo 'KAFIA_SHIZUKU_STARTED'
                exit 0
            fi
            sleep 1
        done
    fi
done

if [ "$native_found" -eq 0 ]; then
    echo 'No executable libshizuku.so or readable legacy start.sh was found.'
else
    echo "Shizuku starter exited with code $starter_exit but shizuku_server did not appear."
fi
exit 3
"""

    def _cleanup_shizuku_bridge(self, device, local_port=None, session_token=""):
        """Stop this transfer's helper session while keeping both apps installed."""
        state = self._shizuku_cleanup_state.pop(device, {})
        port = local_port or state.get("local_port")
        token = session_token or state.get("session_token") or ""

        if port and token:
            connection = None
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1", int(port), timeout=8
                )
                connection.request(
                    "POST",
                    "/shutdown",
                    body=b"{}",
                    headers={
                        "X-Kafia-Token": token,
                        "Content-Type": "application/json; charset=utf-8",
                    },
                )
                response = connection.getresponse()
                response.read()
            except Exception as exc:
                print("KAFIANET SHIZUKU SHUTDOWN REQUEST WARNING:", device, exc)
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

            # Wait for the helper to unbind its Shizuku UserService and close the
            # local server after this transfer session ends.
            deadline = time.monotonic() + 6.0
            while time.monotonic() < deadline:
                check_connection = None
                try:
                    check_connection = http.client.HTTPConnection(
                        "127.0.0.1", int(port), timeout=1
                    )
                    check_connection.request(
                        "GET",
                        "/health",
                        headers={"X-Kafia-Token": token},
                    )
                    probe = check_connection.getresponse()
                    probe.read()
                    time.sleep(0.15)
                except Exception:
                    break
                finally:
                    if check_connection is not None:
                        try:
                            check_connection.close()
                        except Exception:
                            pass

        if port:
            try:
                self._run(
                    ["-s", device, "forward", "--remove", "tcp:" + str(port)],
                    timeout=10
                )
            except Exception as exc:
                print("KAFIANET SHIZUKU FORWARD CLEANUP WARNING:", device, exc)

        self.logChanged.emit(
            "انتهت جلسة مساعد KafiaNet؛ تم الإبقاء على Shizuku ومساعد KafiaNet مثبتين للاستخدام القادم."
        )


    @Slot(str)
    def cleanup_shizuku_bridge(self, device):
        """Public cleanup hook for a setup that succeeded but could not start a copy."""
        self._cleanup_shizuku_bridge(device)

    @Slot(str, str, str, str, str, str)
    def prepare_shizuku_bridge(
        self, device, shizuku_apk, helper_apk, session_token, package_name, destination
    ):
        """Reuse installed Shizuku/helper packages; install only packages that are absent."""
        self._cancel_event.clear()

        def worker():
            local_port = None
            cleanup_state = {
                "session_token": str(session_token or ""),
                "local_port": None,
            }
            self._shizuku_cleanup_state[device] = cleanup_state

            try:
                def package_is_installed(package_id):
                    # Presence only; no version or signature inspection.
                    try:
                        result = self._run(
                            ["-s", device, "shell", "pm", "path", package_id],
                            timeout=25
                        )
                    except Exception as exc:
                        print("SHIZUKU PACKAGE PRESENCE CHECK WARNING:", package_id, exc)
                        return False
                    return (
                        result.returncode == 0
                        and any(
                            line.strip().startswith("package:")
                            for line in str(result.stdout or "").splitlines()
                        )
                    )

                # Keep the installed companion when it is already present. If
                # Android nevertheless rejects an attempted helper install because
                # its signing certificate conflicts with the bundled helper, remove
                # only that helper package and retry once with the bundled APK.
                helper_package = "com.kafianet.shizukubridge"

                def install_apk(apk_path, package_id, label):
                    if not os.path.isfile(apk_path):
                        raise RuntimeError(
                            "ملف " + label + " غير موجود داخل حزمة KafiaNet: " + str(apk_path)
                        )
                    self.logChanged.emit("جاري تثبيت " + label + " لأنه غير مثبت...")
                    install_command = ["-s", device, "install", "-r", apk_path]
                    result = self._run_install_command(
                        install_command,
                        timeout=3600
                    )
                    output = (
                        str(result.stdout or "").strip()
                        + "\\n"
                        + str(result.stderr or "").strip()
                    ).strip()
                    detail = output.casefold()
                    helper_signature_conflict = (
                        package_id == helper_package
                        and (
                            "install_failed_update_incompatible" in detail
                            or "signature mismatch" in detail
                            or "signatures do not match" in detail
                        )
                    )
                    if (
                        (result.returncode != 0 or "success" not in detail)
                        and helper_signature_conflict
                    ):
                        self.logChanged.emit(
                            "تعذر تحديث مساعد KafiaNet بسبب اختلاف التوقيع؛ "
                            "جاري إزالة نسخة المساعد المتعارضة وإعادة تثبيت المرفق."
                        )
                        uninstall_result = self._run(
                            ["-s", device, "uninstall", helper_package],
                            timeout=120
                        )
                        uninstall_output = (
                            str(uninstall_result.stdout or "").strip()
                            + "\\n"
                            + str(uninstall_result.stderr or "").strip()
                        ).strip()
                        if (
                            uninstall_result.returncode != 0
                            and "success" not in uninstall_output.casefold()
                        ):
                            raise RuntimeError(
                                "تعذر إزالة نسخة مساعد KafiaNet ذات التوقيع المتعارض: "
                                + (uninstall_output or "uninstall failed")
                            )
                        result = self._run_install_command(
                            install_command,
                            timeout=3600
                        )
                        output = (
                            str(result.stdout or "").strip()
                            + "\\n"
                            + str(result.stderr or "").strip()
                        ).strip()
                        detail = output.casefold()

                    if result.returncode != 0 or "success" not in detail:
                        raise RuntimeError(
                            "تعذر تثبيت " + label + " عبر ADB: "
                            + (output or "install failed")
                        )

                def install_if_missing(apk_path, package_id, label):
                    if package_is_installed(package_id):
                        self.logChanged.emit(
                            label + " مثبت مسبقًا؛ سنستخدم الموجود دون فحص الإصدار أو استبداله."
                        )
                        return
                    install_apk(apk_path, package_id, label)

                def install_helper_if_outdated():
                    # The companion contains the transfer protocol, so it must
                    # be upgraded when a newer bridge API is bundled.
                    if not package_is_installed(helper_package):
                        install_apk(helper_apk, helper_package, "مساعد CN PlayHub")
                        return
                    version_result = self._run(
                        ["-s", device, "shell", "dumpsys", "package", helper_package],
                        timeout=25,
                    )
                    version_text = (
                        str(version_result.stdout or "")
                        + "\\n"
                        + str(version_result.stderr or "")
                    )
                    match = re.search(r"\\bversionCode=(\\d+)", version_text)
                    installed_code = int(match.group(1)) if match else 0
                    if installed_code >= 14:
                        self.logChanged.emit(
                            "مساعد CN PlayHub محدث؛ سنستخدم النسخة المثبتة."
                        )
                        return
                    self.logChanged.emit(
                        "جاري تحديث مساعد CN PlayHub إلى محرك النقل الأسرع وفحص الوجهة عبر Shizuku..."
                    )
                    install_apk(helper_apk, helper_package, "مساعد CN PlayHub")

                # ---- Official Shizuku manager ----
                shizuku_package = "moe.shizuku.privileged.api"
                install_if_missing(shizuku_apk, shizuku_package, "Shizuku")

                # ---- KafiaNet companion ----
                install_helper_if_outdated()

                self.logChanged.emit(
                    "تم تجهيز Shizuku والمساعد؛ جاري تشغيل خادم Shizuku..."
                )
                start_result = self._run(
                    ["-s", device, "shell", self._build_shizuku_start_command()],
                    timeout=45
                )
                start_output = (
                    str(start_result.stdout or "").strip()
                    + "\n"
                    + str(start_result.stderr or "").strip()
                ).strip()
                if start_result.returncode != 0:
                    start_lower = start_output.lower()
                    if any(marker in start_lower for marker in (
                        "permission denied", "operation not permitted", "eacces", "errno 13"
                    )):
                        raise RuntimeError(
                            "KAFIANET_SHIZUKU_OEM_PERMISSION_LIMITED: "
                            "رفض Android تشغيل خادم Shizuku بواسطة ADB Shell. "
                            + (start_output or "permission denied")
                        )
                    raise RuntimeError(
                        "KAFIANET_SHIZUKU_SERVER_START_FAILED: "
                        + (start_output or "Shizuku starter failed")
                    )
                if "KAFIA_SHIZUKU_ALREADY_RUNNING" in start_output:
                    self.logChanged.emit(
                        "خادم Shizuku يعمل بالفعل؛ استخدمنا الجلسة الحالية دون إعادة تشغيله."
                    )
                else:
                    self.logChanged.emit("تم تشغيل خادم Shizuku والتحقق من ظهوره.")

                launch = self._run(
                    [
                        "-s", device, "shell", "am", "start",
                        "-n", "com.kafianet.shizukubridge/.MainActivity",
                        "--es", "token", session_token,
                        "--es", "package", package_name,
                        "--es", "destination", destination
                    ],
                    timeout=30
                )
                launch_output = (
                    str(launch.stdout or "").strip()
                    + "\n"
                    + str(launch.stderr or "").strip()
                ).strip()
                if launch.returncode != 0 or "Error" in launch_output:
                    raise RuntimeError(
                        "تعذر فتح مساعد KafiaNet: "
                        + (launch_output or "activity launch failed")
                    )

                forward = self._run(
                    ["-s", device, "forward", "tcp:0", "tcp:18765"],
                    timeout=20
                )
                port_match = re.search(r"\b(\d{4,5})\b", str(forward.stdout or ""))
                if forward.returncode != 0 or not port_match:
                    raise RuntimeError(
                        "تعذر إنشاء نفق الاتصال مع الهاتف: "
                        + str(forward.stderr or forward.stdout or "").strip()
                    )
                local_port = int(port_match.group(1))
                cleanup_state["local_port"] = local_port

                def request_health():
                    request = urllib.request.Request(
                        "http://127.0.0.1:" + str(local_port) + "/health",
                        headers={"X-Kafia-Token": session_token},
                        method="GET"
                    )
                    with urllib.request.urlopen(request, timeout=5) as response:
                        return json.loads(response.read().decode("utf-8", errors="replace"))

                self.logChanged.emit(
                    "جاري فحص الإذن السابق لتطبيق مساعد KafiaNet..."
                )
                deadline = time.monotonic() + 16 * 60
                state_started = time.monotonic()
                last_state = None
                while time.monotonic() < deadline:
                    if self._cancel_event.is_set():
                        raise RuntimeError("تم إلغاء تهيئة Shizuku.")
                    try:
                        health = request_health()
                        state = str(health.get("state") or "")
                        if state != last_state:
                            last_state = state
                            state_started = time.monotonic()
                            labels = {
                                "permission_required": "بانتظار موافقتك على صلاحية Shizuku على الهاتف...",
                                "waiting_shizuku": "خادم Shizuku غير متصل؛ جاري انتظار تشغيله...",
                                "binding_service": "تم التحقق من الإذن؛ جاري ربط خدمة النقل.",
                                "ready_direct": "تم منح صلاحية Shizuku؛ جاري تجهيز قناة النقل.",
                                "failed": "تعذر تجهيز خدمة Shizuku؛ جاري جمع سبب الفشل.",
                            }
                            self.logChanged.emit(
                                labels.get(state, "جاري التحقق من صلاحيات النقل...")
                            )

                        if state == "service_disconnected":
                            raise RuntimeError(
                                "KAFIANET_SHIZUKU_USER_SERVICE_DISCONNECTED: "
                                + str(health.get("error") or "انقطع اتصال خدمة Shizuku.")
                            )
                        if state == "permission_required" and time.monotonic() - state_started > 15 * 60:
                            raise RuntimeError(
                                "KAFIANET_SHIZUKU_PERMISSION_TIMEOUT: "
                                "لم تصل موافقة Shizuku خلال المهلة المحددة."
                            )

                        if state == "binding_service" and time.monotonic() - state_started > 90:
                            raise RuntimeError(
                                "KAFIANET_SHIZUKU_USER_SERVICE_BIND_TIMEOUT: "
                                "انتهت مهلة ربط خدمة Shizuku؛ راجع توافق الإصدار وقيود الشركة."
                            )
                        if state == "waiting_shizuku" and time.monotonic() - state_started > 60:
                            raise RuntimeError(
                                "KAFIANET_SHIZUKU_SERVER_NOT_RUNNING: "
                                "لم يظهر خادم Shizuku بعد التشغيل؛ افتح Shizuku وتأكد من تفعيل الخدمة."
                            )

                        if health.get("ready") is True:
                            identity = str(health.get("uid") or "")
                            if "uid=2000" not in identity and "uid=0" not in identity:
                                raise RuntimeError(
                                    "هوية خدمة Shizuku غير متوقعة؛ المتوقع UID 2000 أو UID 0: "
                                    + identity
                                )
                            self.finished.emit(
                                True,
                                json.dumps(
                                    {
                                        "port": local_port,
                                        "token": session_token,
                                        "uid": identity,
                                    },
                                    ensure_ascii=False
                                )
                            )
                            return

                        if state == "failed":
                            detail = str(health.get("error") or "Shizuku helper failed")
                            lowered = detail.lower()
                            if "found multiple conflicting per-domain rules" in lowered:
                                detail = (
                                    "KAFIANET_SHIZUKU_ANDROID16_USERSERVICE_INCOMPATIBLE: "
                                    + detail
                                )
                            elif "kafianet_destination_missing" in lowered:
                                detail = "KAFIANET_DESTINATION_MISSING: " + detail
                            elif "kafianet_shizuku_user_service" in lowered:
                                detail = "KAFIANET_SHIZUKU_USER_SERVICE_FAILED: " + detail
                            elif (
                                "cannot prepare/write the game destination" in lowered
                                or "permission denied" in lowered
                                or "eacces" in lowered
                                or "operation not permitted" in lowered
                            ):
                                detail = "KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED: " + detail
                            raise RuntimeError(detail)
                    except urllib.error.URLError:
                        pass
                    time.sleep(1.0)

                raise RuntimeError(
                    "انتهت مهلة انتظار Shizuku أو موافقة إذن المساعد على الهاتف."
                )

            except Exception as exc:
                print("KAFIANET SHIZUKU PREPARE FAILED:", device, exc)
                self._cleanup_shizuku_bridge(device, local_port, session_token)
                self.finished.emit(False, str(exc))

        threading.Thread(
            target=worker,
            name=f"KafiaNet-Shizuku-Prepare-{device}",
            daemon=True
        ).start()

    @staticmethod
    def _iter_file_chunks_exact(source, expected_size, chunk_size=4 * 1024 * 1024):
        """Yield exactly the scanned byte count and reject files changed mid-transfer."""
        remaining = int(expected_size)
        if remaining < 0 or chunk_size <= 0:
            raise ValueError("Invalid exact-stream size or chunk size")
        while remaining:
            block = source.read(min(int(chunk_size), remaining))
            if not block:
                raise EOFError(
                    "Source file was truncated during transfer; "
                    + str(remaining) + " bytes were still expected."
                )
            if len(block) > remaining:
                raise RuntimeError("Source reader returned more bytes than requested.")
            remaining -= len(block)
            yield block

        if source.read(1):
            raise RuntimeError(
                "Source file grew during transfer; refusing to commit a partial snapshot."
            )

    @staticmethod
    def _build_direct_stream_manifest(source_path):
        """Build a fast metadata-only file list for transfer progress and streaming."""
        source_root = Path(source_path).expanduser()
        if not source_root.exists():
            raise FileNotFoundError("مصدر النسخ غير موجود: " + str(source_root))
        files = []
        if source_root.is_dir():
            for candidate in source_root.rglob("*"):
                if candidate.is_symlink():
                    raise RuntimeError("لا يسمح بنقل رابط رمزي من المصدر: " + str(candidate))
                if candidate.is_file():
                    files.append((candidate, candidate.relative_to(source_root).as_posix()))
        elif source_root.is_file():
            files.append((source_root, source_root.name))
        else:
            raise RuntimeError("مصدر النسخ ليس ملفًا أو مجلدًا صالحًا.")

        files.sort(key=lambda item: item[1].casefold())
        manifest, total_bytes = [], 0
        for local_file, relative_path in files:
            before = local_file.stat()
            size = int(before.st_size)
            modified_ms = int(before.st_mtime * 1000)
            if (
                not relative_path or relative_path.startswith("/")
                or ".." in Path(relative_path).parts
                or "\n" in relative_path or "\r" in relative_path
            ):
                raise RuntimeError("مسار ملف غير آمن داخل مصدر النسخ: " + relative_path)
            manifest.append((local_file, relative_path, size, modified_ms))
            total_bytes += size
        return manifest, total_bytes

    def _resume_shizuku_bridge_after_reconnect(
        self, device, previous_port, session_token, destination
    ):
        """Re-establish Shizuku and its forwarded channel after the same device reconnects."""
        if not self._device_is_connected(device):
            if not self._wait_for_device_reconnect(device):
                raise RuntimeError("تعذر استعادة اتصال الهاتف لاستكمال النقل عبر Shizuku.")

        if self._cancel_event.is_set():
            raise RuntimeError("تم إلغاء النقل أثناء انتظار إعادة توصيل الهاتف.")

        start_result = self._run(
            ["-s", device, "shell", self._build_shizuku_start_command()],
            timeout=45,
        )
        start_output = (
            str(start_result.stdout or "").strip()
            + "\n"
            + str(start_result.stderr or "").strip()
        ).strip()
        if start_result.returncode != 0:
            raise RuntimeError(
                "KAFIANET_SHIZUKU_RECONNECT_SERVER_FAILED: "
                + (start_output or "تعذر إعادة تشغيل خادم Shizuku.")
            )

        package_match = re.search(
            r"/android/data/([^/]+)",
            str(destination or "").replace("\\", "/"),
            re.IGNORECASE,
        )
        if not package_match:
            raise RuntimeError(
                "KAFIANET_SHIZUKU_RECONNECT_PACKAGE_UNKNOWN: "
                "تعذر استخراج Package ID من مسار Android/data."
            )
        package_name = package_match.group(1)

        launch = self._run(
            [
                "-s", device, "shell", "am", "start",
                "-n", "com.kafianet.shizukubridge/.MainActivity",
                "--es", "token", session_token,
                "--es", "package", package_name,
                "--es", "destination", destination,
            ],
            timeout=30,
        )
        launch_output = (
            str(launch.stdout or "").strip()
            + "\n"
            + str(launch.stderr or "").strip()
        ).strip()
        if launch.returncode != 0 or "Error" in launch_output:
            raise RuntimeError(
                "KAFIANET_SHIZUKU_RECONNECT_HELPER_FAILED: "
                + (launch_output or "تعذر إعادة فتح مساعد KafiaNet.")
            )

        try:
            self._run(
                ["-s", device, "forward", "--remove", f"tcp:{int(previous_port)}"],
                timeout=15,
            )
        except Exception as exc:
            print("SHIZUKU OLD FORWARD REMOVE WARNING:", device, exc)

        forward = self._run(
            ["-s", device, "forward", "tcp:0", "tcp:18765"],
            timeout=20,
        )
        forward_output = str(forward.stdout or "").strip()
        port_match = re.search(r"\b(\d{4,5})\b", forward_output)
        if forward.returncode != 0 or not port_match:
            raise RuntimeError(
                "KAFIANET_SHIZUKU_RECONNECT_FORWARD_FAILED: "
                + str(forward.stderr or forward.stdout or "").strip()
            )
        local_port = int(port_match.group(1))
        health_url = f"http://127.0.0.1:{local_port}/health"
        deadline = time.monotonic() + 16 * 60
        last_state = ""
        while time.monotonic() < deadline:
            if self._cancel_event.is_set():
                raise RuntimeError("تم إلغاء النقل أثناء إعادة تشغيل مساعد Shizuku.")
            try:
                request = urllib.request.Request(
                    health_url,
                    headers={"X-Kafia-Token": session_token},
                    method="GET",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    health = json.loads(
                        response.read().decode("utf-8", errors="replace")
                    )
                state = str(health.get("state") or "")
                if state != last_state:
                    last_state = state
                    self.logChanged.emit(
                        "استعادة مساعد Shizuku: "
                        + {
                            "permission_required": "بانتظار موافقة Shizuku على الهاتف...",
                            "waiting_shizuku": "بانتظار خادم Shizuku...",
                            "binding_service": "إعادة ربط خدمة النقل...",
                            "ready_direct": "تمت استعادة خدمة النقل...",
                            "failed": "فشلت تهيئة خدمة النقل...",
                        }.get(state, state or "جاري التحقق...")
                    )
                if health.get("ready") is True:
                    identity = str(health.get("uid") or "")
                    if "uid=2000" not in identity and "uid=0" not in identity:
                        raise RuntimeError(
                            "هوية Shizuku بعد إعادة الاتصال غير متوقعة: " + identity
                        )
                    cleanup_state = self._shizuku_cleanup_state.get(device)
                    if cleanup_state is not None:
                        cleanup_state["local_port"] = local_port
                    return local_port
                if state == "failed":
                    raise RuntimeError(
                        "KAFIANET_SHIZUKU_RECONNECT_HELPER_FAILED: "
                        + str(health.get("error") or "فشل مساعد Shizuku بعد إعادة الاتصال.")
                    )
            except urllib.error.URLError:
                pass
            time.sleep(1.0)

        raise RuntimeError(
            "انتهت مهلة استعادة مساعد Shizuku أو انتظار موافقته بعد إعادة توصيل الهاتف."
        )

    @Slot(str, str, str, str, str, str, str)
    def copy_via_shizuku_bridge(
        self, device, local_port, session_token, job_id,
        source_path, destination, target_relative
    ):
        """Stream files once from the PC straight into the final Android/data folder."""
        if not self._bridge_transfer_active:
            self._cancel_event.clear()
        source_root = Path(source_path)

        def worker():
            self._bridge_transfer_active = True
            resume_handoff = False
            cleanup_done = False
            base_url = "http://127.0.0.1:" + str(local_port)
            forwarded_port = int(local_port)
            try:
                files, total_bytes = self._build_direct_stream_manifest(source_root)
                total_files = len(files)

                def request_json(path, method="GET", payload=None, timeout=30):
                    data = None
                    headers = {"X-Kafia-Token": session_token}
                    if payload is not None:
                        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                        headers["Content-Type"] = "application/json; charset=utf-8"
                    request = urllib.request.Request(
                        base_url + path,
                        data=data,
                        headers=headers,
                        method=method
                    )
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        raw = response.read().decode("utf-8", errors="replace")
                        return json.loads(raw) if raw else {}

                destination_check = request_json(
                    "/destination/check",
                    method="POST",
                    payload={"destination": destination},
                    timeout=30
                )
                if destination_check.get("exists") is not True:
                    detail = str(destination_check.get("error") or destination_check.get("path") or destination)
                    if destination_check.get("missing") is True:
                        raise RuntimeError(
                            "KAFIANET_SHIZUKU_DESTINATION_MISSING: "
                            + destination + " | " + detail
                        )
                    raise RuntimeError(
                        "KAFIANET_SHIZUKU_DESTINATION_ACCESS_DENIED: "
                        + destination + " | " + detail
                    )
                if destination_check.get("isDirectory") is not True:
                    detail = str(destination_check.get("path") or destination)
                    raise RuntimeError(
                        "KAFIANET_SHIZUKU_DESTINATION_NOT_DIRECTORY: " + detail
                    )
                # Use the canonical path returned by the Shizuku UID check,
                # not the ADB-visible alias which may be filtered by Android.
                destination = str(destination_check.get("path") or destination)
                print(
                    "SHIZUKU DESTINATION VERIFIED:",
                    destination_check.get("path") or destination,
                    "| canWrite hint:",
                    destination_check.get("canWrite"),
                )

                accepted = request_json(
                    "/stream/start",
                    method="POST",
                    payload={
                        "jobId": job_id,
                        "destination": destination,
                        "totalBytes": total_bytes,
                        "totalFiles": total_files
                    },
                    timeout=30
                )
                if accepted.get("accepted") is not True:
                    raise RuntimeError(
                        str(accepted.get("error") or "رفض Shizuku بدء النقل المباشر.")
                    )

                skipped_paths = set()
                skipped_bytes = 0
                skipped_files = 0
                sync_batch_size = 256
                for offset in range(0, total_files, sync_batch_size):
                    batch = files[offset:offset + sync_batch_size]
                    sync_result = request_json(
                        "/stream/check",
                        method="POST",
                        payload={
                            "jobId": job_id,
                            "files": [
                                {
                                    "path": relative_path,
                                    "size": file_size,
                                    "modifiedMs": modified_ms,
                                }
                                for _, relative_path, file_size, modified_ms in batch
                            ],
                        },
                        timeout=60,
                    )
                    if sync_result.get("ok") is not True:
                        raise RuntimeError(
                            "KAFIANET_SHIZUKU_FILE_SYNC_CHECK_FAILED: "
                            + str(sync_result.get("error") or "تعذر فحص الملفات الموجودة.")
                        )
                    for relative_path in (sync_result.get("skipped") or []):
                        skipped_paths.add(str(relative_path))
                    skipped_files += int(sync_result.get("skippedFiles") or 0)
                    skipped_bytes += int(sync_result.get("skippedBytes") or 0)

                known_paths = {relative_path for _, relative_path, _, _ in files}
                if not skipped_paths.issubset(known_paths):
                    raise RuntimeError("KAFIANET_SHIZUKU_FILE_SYNC_CHECK_FAILED: helper returned an unknown source path")
                if len(skipped_paths) != skipped_files:
                    raise RuntimeError("KAFIANET_SHIZUKU_FILE_SYNC_CHECK_FAILED: skipped-file count mismatch")
                expected_skipped_bytes = sum(
                    file_size
                    for _, relative_path, file_size, _ in files
                    if relative_path in skipped_paths
                )
                if expected_skipped_bytes != skipped_bytes:
                    raise RuntimeError("KAFIANET_SHIZUKU_FILE_SYNC_CHECK_FAILED: skipped-byte count mismatch")

                files_to_send = [item for item in files if item[1] not in skipped_paths]
                print(
                    "SHIZUKU DIRECT STREAM STARTED:",
                    device, total_files, total_bytes,
                    "| skipped:", skipped_files, "files /", skipped_bytes, "bytes",
                    "| pending:", len(files_to_send),
                )
                started = time.monotonic()
                confirmed_bytes = skipped_bytes
                sent_bytes = 0
                last_emit = 0.0
                last_percent = -1

                def emit_progress(active_bytes=0):
                    nonlocal last_emit, last_percent
                    now = time.monotonic()
                    if now - last_emit < 0.12 and active_bytes:
                        return
                    last_emit = now
                    completed = min(total_bytes, confirmed_bytes + max(active_bytes, 0))
                    percent = (
                        99 if total_bytes <= 0
                        else min(99, int(completed * 100 / total_bytes))
                    )
                    elapsed = max(now - started, 0.001)
                    speed = sent_bytes / elapsed
                    remaining = max(total_bytes - completed, 0)
                    eta = (
                        self._format_time(int(remaining / speed))
                        if speed > 0 and remaining > 0 else "--"
                    )
                    if percent != last_percent or active_bytes == 0:
                        self.progressChanged.emit(
                            percent,
                            self._format_size(speed) + "/s",
                            eta
                        )
                        last_percent = percent

                for index, (local_file, relative_path, file_size, modified_ms) in enumerate(files_to_send, start=1):
                    if self._cancel_event.is_set():
                        raise RuntimeError("تم إلغاء النسخ المباشر عبر Shizuku.")
                    self._pause_event.wait()

                    encoded_relative = quote(relative_path, safe="")
                    connection = http.client.HTTPConnection(
                        "127.0.0.1",
                        forwarded_port,
                        timeout=6 * 60 * 60
                    )
                    file_sent = 0
                    with self._process_lock:
                        self._current_http_connection = connection
                    try:
                        connection.putrequest("POST", "/stream/file")
                        connection.putheader("X-Kafia-Token", session_token)
                        connection.putheader("X-Kafia-Job", job_id)
                        connection.putheader("X-Kafia-Relative", encoded_relative)
                        connection.putheader("X-Kafia-Size", str(file_size))
                        connection.putheader("X-Kafia-Modified", str(modified_ms))
                        connection.putheader("Content-Type", "application/octet-stream")
                        connection.putheader("Content-Length", str(file_size))
                        connection.endheaders()

                        with local_file.open("rb") as source:
                            for block in self._iter_file_chunks_exact(
                                source, file_size, 4 * 1024 * 1024
                            ):
                                if self._cancel_event.is_set():
                                    raise RuntimeError(
                                        "تم إلغاء النسخ أثناء إرسال: " + relative_path
                                    )
                                self._pause_event.wait()
                                connection.send(block)
                                file_sent += len(block)
                                sent_bytes += len(block)
                                emit_progress(file_sent)
                        response = connection.getresponse()
                        raw = response.read().decode("utf-8", errors="replace")
                        payload = json.loads(raw) if raw else {}
                        if response.status < 200 or response.status >= 300:
                            raise RuntimeError(
                                str(payload.get("error") or response.reason or "HTTP stream failed")
                            )
                        if payload.get("accepted") is not True:
                            raise RuntimeError(
                                str(payload.get("error") or "رفض Shizuku كتابة الملف داخل مجلد اللعبة.")
                            )
                    finally:
                        connection.close()
                        with self._process_lock:
                            if self._current_http_connection is connection:
                                self._current_http_connection = None

                    confirmed_bytes += file_size
                    emit_progress(0)

                final_status = request_json(
                    "/stream/finish",
                    method="POST",
                    payload={"jobId": job_id},
                    timeout=30
                )
                if final_status.get("state") != "complete" or final_status.get("success") is not True:
                    raise RuntimeError(
                        str(final_status.get("error") or "لم يؤكد Shizuku اكتمال جميع ملفات الوجهة.")
                    )

                self.progressChanged.emit(100, self._format_size(sent_bytes / max(time.monotonic() - started, 0.001)) + "/s", "00:00")
                self._cleanup_shizuku_bridge(device, forwarded_port, session_token)
                self.finished.emit(
                    True,
                    "اكتمل النقل المباشر إلى مجلد اللعبة عبر Shizuku. "
                    + str(final_status.get("copiedFiles") or total_files)
                    + " ملفًا، وتم تخطي "
                    + str(final_status.get("skippedFiles") or skipped_files)
                    + " ملفًا مطابقًا للحجم وتاريخ التعديل، دون مجلد وسيط."
                )
            except Exception as exc:
                print("KAFIANET SHIZUKU DIRECT STREAM FAILED:", device, exc)
                try:
                    request_json(
                        "/cancel",
                        method="POST",
                        payload={"jobId": job_id},
                        timeout=15
                    )
                except Exception:
                    pass

                transport_text = str(exc).lower()
                transport_markers = (
                    "device offline",
                    "device not found",
                    "no devices/emulators found",
                    "device disconnected",
                    "transport error",
                    "connection reset",
                    "connection aborted",
                    "connection refused",
                    "broken pipe",
                    "remote end closed",
                    "forcibly closed",
                    "timed out",
                    "socket",
                )
                disconnected = False
                if not self._cancel_event.is_set():
                    disconnected = not self._device_is_connected(device)
                looks_like_transport_loss = (
                    disconnected
                    or any(marker in transport_text for marker in transport_markers)
                )

                cleanup_state = self._shizuku_cleanup_state.get(device)
                resume_attempts = int(
                    (cleanup_state or {}).get("resume_attempts", 0)
                )
                if (
                    looks_like_transport_loss
                    and not self._cancel_event.is_set()
                    and resume_attempts < 10
                ):
                    try:
                        if cleanup_state is not None:
                            cleanup_state["resume_attempts"] = resume_attempts + 1
                        self.statusChanged.emit(
                            "انقطع اتصال الهاتف أثناء Shizuku. "
                            "سيُعاد الاتصال ثم تُستأنف الملفات غير المكتملة..."
                        )
                        print(
                            "SHIZUKU RESUME MODE:",
                            device,
                            "| attempt:",
                            resume_attempts + 1,
                            "| previous job:",
                            job_id,
                        )
                        resumed_port = self._resume_shizuku_bridge_after_reconnect(
                            device,
                            forwarded_port,
                            session_token,
                            destination,
                        )
                        if cleanup_state is not None:
                            cleanup_state["local_port"] = resumed_port
                        # Reuse the same helper session but create a fresh job.
                        # Its next batched size+mtime pass will skip every file
                        # already committed and resend only incomplete/changed files.
                        # Transfer cleanup ownership before starting the new worker,
                        # so a very fast all-files-skipped resume cannot race this flag.
                        resume_handoff = True
                        try:
                            self.copy_via_shizuku_bridge(
                                device,
                                str(resumed_port),
                                session_token,
                                secrets.token_hex(12),
                                source_path,
                                destination,
                                target_relative,
                            )
                        except Exception:
                            resume_handoff = False
                            raise
                        return
                    except Exception as resume_error:
                        exc = RuntimeError(
                            str(exc)
                            + "\\nتعذرت استعادة نقل Shizuku تلقائيًا: "
                            + str(resume_error)
                        )

                if looks_like_transport_loss and resume_attempts >= 10:
                    exc = RuntimeError(
                        str(exc)
                        + "\\nتكرر انقطاع اتصال الهاتف 10 مرات؛ أعد المحاولة يدويًا."
                    )
                self._cleanup_shizuku_bridge(device, forwarded_port, session_token)
                cleanup_done = True
                self.finished.emit(False, str(exc))
            finally:
                if not resume_handoff:
                    if not cleanup_done:
                        self._cleanup_shizuku_bridge(device, forwarded_port, session_token)
                    self._bridge_transfer_active = False

        threading.Thread(
            target=worker,
            name=f"KafiaNet-Shizuku-DirectStream-{device}",
            daemon=True
        ).start()



    @Slot(str)
    def install(self, device, apk):

        self._cancel_event.clear()

        def worker():

            try:
                if not os.path.isfile(apk):

                    self.finished.emit(
                        False,
                        "ملف APK غير موجود"
                    )

                    return

                self.statusChanged.emit(
                    "جاري تثبيت التطبيق..."
                )

                result = self._run_install_command(
                    [
                        "-s",
                        device,
                        "install",
                        "-r",
                        apk
                    ],
                    timeout=3600
                )

                output = (
                    result.stdout.strip()
                    + "\n"
                    + result.stderr.strip()
                ).strip()

                print("ADB INSTALL:", output)

                if result.returncode == 0:

                    self.statusChanged.emit(
                        "اكتمل التثبيت"
                    )

                    self.finished.emit(
                        True,
                        "تم تثبيت التطبيق بنجاح"
                    )

                else:

                    self.finished.emit(
                        False,
                        output or "فشل تثبيت التطبيق"
                    )

            except Exception as e:

                self.finished.emit(
                    False,
                    str(e)
                )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()

    @Slot(str, str)
    def uninstall(self, device, package_id):
        """Remove a selected game's Android package without blocking the UI."""
        self._cancel_event.clear()

        def worker():
            try:
                check = self._run(
                    ["-s", device, "shell", "pm", "path", package_id],
                    timeout=30
                )
                if (
                    check.returncode != 0
                    or "package:" not in str(check.stdout or "")
                ):
                    self.finished.emit(False, "التطبيق غير مثبت على الهاتف.")
                    return

                self.statusChanged.emit("جاري إزالة تثبيت اللعبة...")
                result = self._run_install_command(
                    ["-s", device, "uninstall", package_id],
                    timeout=300
                )
                output = (
                    str(result.stdout or "").strip()
                    + "\n"
                    + str(result.stderr or "").strip()
                ).strip()
                print("ADB UNINSTALL:", device, package_id, output)

                if result.returncode == 0 and "failure" not in output.casefold():
                    self.statusChanged.emit("تمت إزالة تثبيت اللعبة")
                    self.finished.emit(True, "تمت إزالة تثبيت اللعبة بنجاح.")
                else:
                    self.finished.emit(
                        False,
                        output or "فشل أمر إزالة تثبيت اللعبة عبر ADB."
                    )
            except Exception as exc:
                self.finished.emit(False, f"خطأ أثناء إزالة تثبيت اللعبة: {exc}")

        threading.Thread(
            target=worker,
            name=f"KafiaNet-ADB-Uninstall-{device}",
            daemon=True
        ).start()

    def _suspend_current_process(self):

        with self._process_lock:
            process = self._current_process

        if process is None:
            return False

        if process.poll() is not None:
            return False

        try:

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")

            PROCESS_SUSPEND_RESUME = 0x0800

            OpenProcess = kernel32.OpenProcess
            OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32
            ]
            OpenProcess.restype = ctypes.c_void_p

            CloseHandle = kernel32.CloseHandle
            CloseHandle.argtypes = [ctypes.c_void_p]
            CloseHandle.restype = ctypes.c_int

            NtSuspendProcess = ntdll.NtSuspendProcess
            NtSuspendProcess.argtypes = [ctypes.c_void_p]
            NtSuspendProcess.restype = ctypes.c_long

            handle = OpenProcess(
                PROCESS_SUSPEND_RESUME,
                False,
                process.pid
            )

            if not handle:
                error = ctypes.get_last_error()
                print("PAUSE OPENPROCESS ERROR:", error)
                return False

            status = NtSuspendProcess(handle)

            CloseHandle(handle)

            if status == 0:

                self._process_paused = True

                print(
                    "ADB PROCESS PAUSED:",
                    process.pid
                )

                return True

            print(
                "NT SUSPEND ERROR:",
                status
            )

        except Exception as e:

            print(
                "PAUSE PROCESS ERROR:",
                e
            )

        return False

    def _resume_current_process(self):

        with self._process_lock:
            process = self._current_process

        if process is None:
            return False

        try:

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            ntdll = ctypes.WinDLL("ntdll")

            PROCESS_SUSPEND_RESUME = 0x0800

            OpenProcess = kernel32.OpenProcess
            OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32
            ]
            OpenProcess.restype = ctypes.c_void_p

            CloseHandle = kernel32.CloseHandle
            CloseHandle.argtypes = [ctypes.c_void_p]
            CloseHandle.restype = ctypes.c_int

            NtResumeProcess = ntdll.NtResumeProcess
            NtResumeProcess.argtypes = [ctypes.c_void_p]
            NtResumeProcess.restype = ctypes.c_long

            handle = OpenProcess(
                PROCESS_SUSPEND_RESUME,
                False,
                process.pid
            )

            if not handle:
                error = ctypes.get_last_error()
                print("RESUME OPENPROCESS ERROR:", error)
                return False

            status = NtResumeProcess(handle)

            CloseHandle(handle)

            if status == 0:

                self._process_paused = False

                print(
                    "ADB PROCESS RESUMED:",
                    process.pid
                )

                return True

            print(
                "NT RESUME ERROR:",
                status
            )

        except Exception as e:

            print(
                "RESUME PROCESS ERROR:",
                e
            )

        return False

    @Slot(result=bool)
    def pause(self):

        if self._process_paused:
            return True

        with self._process_lock:
            process = self._current_process

        if process is None or process.poll() is not None:
            if self._bridge_transfer_active:
                self._pause_event.clear()
                self.statusChanged.emit("تم إيقاف النقل عبر Shizuku مؤقتًا")
                return True

            self.statusChanged.emit(
                "لا توجد عملية نقل يمكن إيقافها مؤقتًا الآن"
            )
            return False

        if self._suspend_current_process():

            self._pause_event.clear()

            self.statusChanged.emit(
                "تم إيقاف النقل مؤقتًا"
            )
            return True

        else:

            self.statusChanged.emit(
                "تعذر إيقاف النقل مؤقتًا"
            )
            return False

    @Slot(result=bool)
    def resume(self):

        process = self._current_process

        if process is None or process.poll() is not None:
            if self._bridge_transfer_active:
                self._pause_event.set()
                self.statusChanged.emit("تم استئناف النقل عبر Shizuku")
                return True

            self._pause_event.set()
            self.statusChanged.emit(
                "لا توجد عملية نقل جارية"
            )
            return False

        if self._process_paused:

            if self._resume_current_process():

                self._pause_event.set()

                self.statusChanged.emit(
                    "تم استئناف النقل..."
                )
                return True

            else:

                self.statusChanged.emit(
                    "تعذر استئناف النقل"
                )
                return False

        else:

            self._pause_event.set()

            self.statusChanged.emit(
                "تم استئناف النقل..."
            )
            return True

    @Slot()
    def cancel(self):

        print("ADB CANCEL REQUESTED")

        self._cancelled_by_user = True

        self._cancel_event.set()

        self._pause_event.set()

        with self._process_lock:
            process = self._current_process
            http_connection = self._current_http_connection

        if http_connection is not None:
            try:
                http_connection.close()
                print("SHIZUKU DIRECT STREAM CONNECTION CLOSED")
            except Exception as e:
                print("SHIZUKU STREAM CANCEL WARNING:", e)

        if process is not None:

            try:

                if self._process_paused:

                    self._resume_current_process()

                if process.poll() is None:

                    process.terminate()

                    print(
                        "ADB PROCESS TERMINATED:",
                        process.pid
                    )

            except Exception as e:

                print(
                    "CANCEL ERROR:",
                    e
                )

        self._process_paused = False
