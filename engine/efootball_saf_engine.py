from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import quote

from PySide6.QtCore import QObject, Signal, Slot


BASE_DIR = Path(__file__).resolve().parents[1]
HELPER_PACKAGE = "com.kafianet.shizukubridge"
HELPER_ACTIVITY = HELPER_PACKAGE + "/.EFootballActivity"
HELPER_APK_NAME = "CNPlayHubShizukuBridge.apk"
SERVER_PORT = 18766
EXPECTED_PROVIDER_AUTHORITY = "com.ziru.official.MTDataFilesProvider"
CHUNK_SIZE = 1024 * 1024
READY_TIMEOUT_SECONDS = 15 * 60


class EFootballSAFEngine(QObject):
    """Direct PC-to-Ziru_27 SAF writer; independent from ADB push and Shizuku."""

    progressChanged = Signal(int, str, str)
    statusChanged = Signal(str)
    finished = Signal(bool, str)
    logChanged = Signal(str)

    def __init__(self):
        super().__init__()
        bundled_adb = BASE_DIR / "platform-tools" / "adb.exe"
        path_adb = shutil.which("adb")
        self.adb = str(bundled_adb) if bundled_adb.is_file() else (path_adb or "adb")
        self._thread = None
        self._cancel_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._connection_lock = threading.Lock()
        self._active_connection = None
        self._forwarded_port = None

    @staticmethod
    def _hidden_creation_flags():
        return getattr(subprocess, "CREATE_NO_WINDOW", 0)

    def _run_adb(self, device, *args, timeout=30):
        command = [self.adb, "-s", str(device), *[str(item) for item in args]]
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            creationflags=self._hidden_creation_flags(),
            timeout=timeout,
        )

    @staticmethod
    def _result_text(result):
        return (
            str(result.stdout or "").strip()
            + "\n"
            + str(result.stderr or "").strip()
        ).strip()

    def _check_cancelled(self):
        if self._cancel_event.is_set():
            raise InterruptedError("تم إلغاء نقل eFootball بطلب المستخدم.")

    def _wait_while_paused(self):
        while not self._pause_event.wait(0.2):
            self._check_cancelled()
        self._check_cancelled()

    def _set_connection(self, connection):
        with self._connection_lock:
            self._active_connection = connection

    def _clear_connection(self, connection):
        with self._connection_lock:
            if self._active_connection is connection:
                self._active_connection = None

    def _ensure_helper(self, device):
        state = self._run_adb(device, "get-state", timeout=12)
        if state.returncode != 0 or state.stdout.strip().lower() != "device":
            raise RuntimeError(
                "اتصال ADB بالهاتف غير جاهز: "
                + (self._result_text(state) or "device is not authorized")
            )

        package_path = self._run_adb(
            device, "shell", "pm", "path", HELPER_PACKAGE, timeout=15
        )
        installed = (
            package_path.returncode == 0
            and "package:" in self._result_text(package_path)
        )
        if installed:
            version = self._run_adb(
                device, "shell", "dumpsys", "package", HELPER_PACKAGE, timeout=20
            )
            version_text = self._result_text(version)
            match = re.search(r"\bversionCode=(\d+)", version_text)
            installed_code = int(match.group(1)) if match else 0
            if installed_code >= 16:
                return

        apk = BASE_DIR / "android_support" / HELPER_APK_NAME
        if not apk.is_file():
            raise FileNotFoundError(
                "ملف مساعد KafiaNet الموحد غير موجود داخل حزمة البرنامج: " + str(apk)
            )
        self.statusChanged.emit("جاري تثبيت مساعد eFootball SAF على الهاتف...")
        installed_result = self._run_adb(
            device, "install", "-r", str(apk), timeout=180
        )
        install_text = self._result_text(installed_result)
        if installed_result.returncode != 0 or "success" not in install_text.lower():
            raise RuntimeError(
                "تعذر تثبيت مساعد eFootball SAF: "
                + (install_text or "adb install failed")
            )

    def _launch_helper(self, device, token):
        result = self._run_adb(
            device,
            "shell", "am", "start",
            "-n", HELPER_ACTIVITY,
            "--es", "token", token,
            timeout=30,
        )
        output = self._result_text(result)
        if result.returncode != 0 or "error:" in output.lower():
            raise RuntimeError(
                "تعذر فتح مساحة eFootball داخل مساعد KafiaNet الموحد: " + (output or "activity launch failed")
            )

    def _create_forward(self, device):
        result = self._run_adb(
            device, "forward", "tcp:0", f"tcp:{SERVER_PORT}", timeout=20
        )
        output = self._result_text(result)
        matches = re.findall(r"(?<!\d)(\d{4,5})(?!\d)", output)
        if result.returncode != 0 or not matches:
            raise RuntimeError(
                "تعذر إنشاء نفق ADB المحلي إلى مساعد eFootball: "
                + (output or "adb forward did not return a local port")
            )
        port = int(matches[-1])
        if not 1024 <= port <= 65535:
            raise RuntimeError("أعاد ADB رقم منفذ محليًا غير صالح: " + str(port))
        self._forwarded_port = port
        return port

    def _api_json(self, port, token, path, payload=None, timeout=10):
        body = None
        headers = {"X-Kafia-Token": token}
        method = "GET"
        if payload is not None:
            method = "POST"
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body))

        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        self._set_connection(connection)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            try:
                data = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise RuntimeError(
                    f"مساعد eFootball أعاد استجابة غير مفهومة ({response.status}): "
                    + repr(raw[:250])
                ) from exc
            if response.status != 200:
                raise RuntimeError(
                    str(data.get("error") or f"SAF helper HTTP {response.status}")
                )
            return data
        finally:
            self._clear_connection(connection)
            connection.close()

    def _wait_for_writable_tree(self, port, token):
        self.statusChanged.emit(
            "افتح مساعد KAFIA NET CONTROL PRO على الهاتف، واختر eFootball ثم مجلد data وامنح صلاحية الوصول..."
        )
        deadline = time.monotonic() + READY_TIMEOUT_SECONDS
        last_status = 0.0
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                health = self._api_json(port, token, "/health", timeout=5)
                authority = str(health.get("authority") or "")
                root_name = str(health.get("rootName") or "")
                if authority and authority != EXPECTED_PROVIDER_AUTHORITY:
                    raise RuntimeError(
                        "تم اختيار مزوّد تخزين غير متوقع. المطلوب مساحة Ziru_27: "
                        + EXPECTED_PROVIDER_AUTHORITY
                    )
                if bool(health.get("ready")):
                    if root_name.casefold() != "data":
                        raise RuntimeError(
                            "تم منح صلاحية لمجلد غير data. أعد اختيار eFootball / data."
                        )
                    if authority != EXPECTED_PROVIDER_AUTHORITY:
                        raise RuntimeError(
                            "صلاحية SAF ليست من مزوّد مساحة Ziru_27 الصحيح."
                        )
                    if health.get("writeProbe") is not True:
                        raise RuntimeError(
                            "لم ينجح اختبار الكتابة داخل مساحة eFootball؛ أوقفنا النقل."
                        )
                    self.statusChanged.emit(
                        "تم التحقق من مساحة eFootball وصلاحية الكتابة؛ جاري بدء النسخ المباشر..."
                    )
                    return
            except (OSError, http.client.HTTPException, json.JSONDecodeError):
                pass
            now = time.monotonic()
            if now - last_status >= 8:
                self.statusChanged.emit(
                    "بانتظار اختيار eFootball / data ومنح صلاحية الكتابة على الهاتف..."
                )
                last_status = now
            time.sleep(1.0)
        raise TimeoutError(
            "انتهت مهلة انتظار اختيار مساحة eFootball ومنح صلاحية الكتابة. "
            "افتح المساعد على الهاتف وأعد المحاولة."
        )

    def _collect_source_files(self, source):
        if not source.is_dir():
            raise FileNotFoundError("مصدر eFootball غير موجود: " + str(source))
        files = []
        for root, directories, names in os.walk(source, followlinks=False):
            root_path = Path(root)
            directories[:] = [
                name for name in directories
                if not (root_path / name).is_symlink()
            ]
            for name in names:
                candidate = root_path / name
                if candidate.is_file() and not candidate.is_symlink():
                    files.append(candidate)
        files.sort(key=lambda item: str(item.relative_to(source)).casefold())
        return files

    @staticmethod
    def _format_speed(bytes_per_second):
        value = max(0.0, float(bytes_per_second))
        units = ("B/s", "KB/s", "MB/s", "GB/s")
        index = 0
        while value >= 1024.0 and index < len(units) - 1:
            value /= 1024.0
            index += 1
        return f"{value:.1f} {units[index]}"

    def _stream_file(self, port, token, job_id, source_root, file_path,
                     index, total_files, bytes_before, started_at):
        relative = file_path.relative_to(source_root).as_posix()
        # Read source length once to frame the stream; never stat or re-read destination.
        content_length = file_path.stat().st_size
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=None)
        self._set_connection(connection)
        sent = 0
        remaining = content_length
        try:
            connection.putrequest("POST", "/file")
            connection.putheader("X-Kafia-Token", token)
            connection.putheader("X-Kafia-Job", job_id)
            connection.putheader("X-Kafia-Relative", quote(relative, safe=""))
            connection.putheader("Content-Type", "application/octet-stream")
            connection.putheader("Content-Length", str(content_length))
            connection.endheaders()

            with file_path.open("rb") as source_file:
                while remaining > 0:
                    self._wait_while_paused()
                    block = source_file.read(min(CHUNK_SIZE, remaining))
                    if not block:
                        raise EOFError(
                            "تغير ملف المصدر أو انتهى قبل اكتمال إرساله: " + relative
                        )
                    connection.send(block)
                    sent += len(block)
                    remaining -= len(block)
                    elapsed = max(0.001, time.monotonic() - started_at)
                    speed = self._format_speed((bytes_before + sent) / elapsed)
                    fraction = 1.0 if content_length == 0 else sent / content_length
                    percent = min(
                        99,
                        int(((index + fraction) / max(1, total_files)) * 100)
                    )
                    self.progressChanged.emit(percent, speed, "--")

            response = connection.getresponse()
            raw = response.read()
            try:
                result = json.loads(raw.decode("utf-8"))
            except Exception as exc:
                raise RuntimeError(
                    f"استجابة SAF غير صالحة للملف {relative}: {raw[:250]!r}"
                ) from exc
            if response.status != 200 or not result.get("accepted"):
                raise RuntimeError(
                    str(result.get("error") or f"فشل حفظ الملف داخل eFootball: {relative}")
                )
            received = result.get("receivedBytes")
            if received is not None and int(received) != sent:
                raise RuntimeError(
                    f"لم يستقبل مساعد SAF كل بايتات التدفق للملف: {relative}"
                )
            return sent
        finally:
            self._clear_connection(connection)
            connection.close()

    def _run_transfer(self, device, source_value, destination):
        job_started = False
        job_id = ""
        token = secrets.token_hex(24)
        port = None
        try:
            source = Path(source_value).resolve()
            if str(destination) != "SAF://eFootball/data":
                raise ValueError(
                    "وجهة eFootball غير معروفة. تم إيقاف النقل لحماية ملفات اللعبة."
                )
            files = self._collect_source_files(source)
            if not files:
                raise RuntimeError("مجلد data في مصدر eFootball لا يحتوي ملفات قابلة للنسخ.")

            self.statusChanged.emit("جاري تجهيز قناة SAF الخاصة بـ eFootball...")
            self._ensure_helper(device)
            self._launch_helper(device, token)
            port = self._create_forward(device)
            self._wait_for_writable_tree(port, token)
            self._check_cancelled()

            job_id = uuid.uuid4().hex
            started = self._api_json(
                port, token, "/start",
                {"jobId": job_id, "fileCount": len(files)},
                timeout=20,
            )
            if not started.get("accepted"):
                raise RuntimeError(
                    str(started.get("error") or "رفض مساعد eFootball بدء النقل.")
                )
            job_started = True

            self.statusChanged.emit(
                f"جاري نسخ eFootball عبر SAF مباشرة: {len(files)} ملف..."
            )
            start_time = time.monotonic()
            total_sent = 0
            for index, file_path in enumerate(files):
                self._wait_while_paused()
                sent = self._stream_file(
                    port, token, job_id, source, file_path,
                    index, len(files), total_sent, start_time
                )
                total_sent += sent
                percent = min(99, int((index + 1) * 100 / len(files)))
                elapsed = max(0.001, time.monotonic() - start_time)
                speed = self._format_speed(total_sent / elapsed)
                self.progressChanged.emit(percent, speed, "--")
                self.statusChanged.emit(
                    f"نسخ eFootball عبر SAF: {index + 1}/{len(files)} — "
                    + file_path.relative_to(source).as_posix()
                )

            finished = self._api_json(
                port, token, "/finish", {"jobId": job_id}, timeout=30
            )
            job_started = False
            if not finished.get("success"):
                raise RuntimeError(
                    str(finished.get("error") or "أبلغ مساعد SAF عن نقل غير مكتمل.")
                )
            copied = int(finished.get("copiedFiles") or 0)
            if copied != len(files):
                raise RuntimeError(
                    f"عدد الملفات المستلمة غير مكتمل: {copied}/{len(files)}"
                )
            elapsed = max(0.001, time.monotonic() - start_time)
            self.progressChanged.emit(100, self._format_speed(total_sent / elapsed), "00:00")
            self.statusChanged.emit("اكتمل نسخ eFootball إلى مساحة Ziru_27 عبر SAF.")
            self.finished.emit(
                True,
                f"اكتمل نسخ eFootball عبر SAF: {copied} ملف، "
                f"{total_sent:,} بايت. استُبدلت الملفات المتطابقة فقط، "
                "وبقيت الملفات الأخرى. لم تُنقل أو تُحذف ملفات المصدر."
            )
        except InterruptedError as exc:
            self.finished.emit(False, str(exc))
        except Exception as exc:
            self.logChanged.emit("EFOOTBALL SAF TRANSFER ERROR: " + repr(exc))
            self.finished.emit(False, "فشل نسخ eFootball عبر SAF: " + str(exc))
        finally:
            if job_started and job_id and port is not None:
                try:
                    self._api_json(port, token, "/finish", {"jobId": job_id}, timeout=10)
                except Exception:
                    pass
            with self._connection_lock:
                connection = self._active_connection
                self._active_connection = None
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            if port is not None:
                try:
                    self._run_adb(
                        device, "forward", "--remove", f"tcp:{port}", timeout=15
                    )
                except Exception as exc:
                    self.logChanged.emit("EFOOTBALL SAF FORWARD CLEANUP WARNING: " + str(exc))

    @Slot(str, str, str)
    def push(self, device, source, destination):
        if self._thread and self._thread.is_alive():
            self.statusChanged.emit("توجد عملية eFootball قيد التنفيذ على هذا الهاتف.")
            return
        self._cancel_event.clear()
        self._pause_event.set()
        self._thread = threading.Thread(
            target=self._run_transfer,
            args=(str(device), str(source), str(destination)),
            name="EFootball-Ziru27-SAF-" + str(device),
            daemon=True,
        )
        self._thread.start()

    @Slot(result=bool)
    def pause(self):
        if not (self._thread and self._thread.is_alive()):
            return False
        self._pause_event.clear()
        self.statusChanged.emit("تم إيقاف نقل eFootball مؤقتًا.")
        return True

    @Slot(result=bool)
    def resume(self):
        if not (self._thread and self._thread.is_alive()):
            return False
        self._pause_event.set()
        self.statusChanged.emit("استؤنف نقل eFootball.")
        return True

    @Slot()
    def cancel(self):
        self._cancel_event.set()
        self._pause_event.set()
        with self._connection_lock:
            connection = self._active_connection
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass
