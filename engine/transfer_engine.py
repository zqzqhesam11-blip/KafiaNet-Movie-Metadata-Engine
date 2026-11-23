import ctypes
import os
import shutil
import threading
import time

from PySide6.QtCore import QObject, Signal, Slot


class TransferEngine(QObject):
    """Ordinary local file copy engine used for removable USB storage."""

    progressChanged = Signal(int, str, str)
    statusChanged = Signal(str)
    finished = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._cancel_event = threading.Event()
        self._thread = None
        self.copy_stats = {"copied_files": 0, "skipped_files": 0}

    @staticmethod
    def _format_size(size):
        size = float(max(int(size or 0), 0))
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if size < 1024 or unit == "TB":
                return f"{size:.1f} {unit}"
            size /= 1024
        return "0 B"

    @staticmethod
    def _format_time(seconds):
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        seconds = seconds % 60
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    def _preserve_timestamps(source_stat, destination):
        """Keep the source file's access and last-modified times on the copy."""
        os.utime(
            destination,
            ns=(source_stat.st_atime_ns, source_stat.st_mtime_ns)
        )

    def _folder_copy_plan(self, source, destination):
        """Walk the source once and cache each file's size and metadata."""
        files = []
        total = 0
        skipped = 0
        for root, _, names in os.walk(source):
            for name in names:
                source_file = os.path.join(root, name)
                relative = os.path.relpath(source_file, source)
                destination_file = os.path.join(destination, relative)
                if os.path.exists(destination_file):
                    skipped += 1
                    continue
                try:
                    source_stat = os.stat(source_file)
                    size = int(source_stat.st_size)
                except OSError:
                    source_stat = None
                    size = 0
                files.append((source_file, destination_file, source_stat))
                total += size
        return files, total, skipped

    def _copy_file_windows(self, source, destination, copied, total, start_time, source_stat):
        """Use the native Windows copy API, retaining progress and cancellation."""
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.DWORD,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.c_longlong,
            ctypes.c_longlong,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.c_void_p,
        )
        kernel32.CopyFileExW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            callback_type,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.BOOL),
            wintypes.DWORD,
        ]
        kernel32.CopyFileExW.restype = wintypes.BOOL

        cancel_flag = wintypes.BOOL(False)
        last_emit = [0.0]

        def on_progress(
            total_file_size,
            total_bytes_transferred,
            stream_size,
            stream_bytes_transferred,
            stream_number,
            callback_reason,
            source_handle,
            destination_handle,
            context,
        ):
            try:
                self._pause_event.wait()
                if self._cancel_event.is_set():
                    cancel_flag.value = True
                    return 1  # PROGRESS_CANCEL

                current_copied = copied + int(total_bytes_transferred)
                now = time.monotonic()
                if now - last_emit[0] >= 0.15 or (
                    source_stat and current_copied >= total
                ):
                    elapsed = max(now - start_time, 0.001)
                    speed = current_copied / elapsed
                    remaining = (
                        max(int((total - current_copied) / speed), 0)
                        if speed > 0 else 0
                    )
                    percent = (
                        min(int((current_copied / total) * 100), 99)
                        if total else 99
                    )
                    self.progressChanged.emit(
                        percent,
                        self._format_size(speed) + "/s",
                        self._format_time(remaining),
                    )
                    last_emit[0] = now
                return 0  # PROGRESS_CONTINUE
            except Exception as exc:
                print("WINDOWS COPY CALLBACK ERROR:", exc)
                cancel_flag.value = True
                return 1

        callback = callback_type(on_progress)
        copy_file_fail_if_exists = 0x00000001
        copied_ok = kernel32.CopyFileExW(
            str(source),
            str(destination),
            callback,
            None,
            ctypes.byref(cancel_flag),
            copy_file_fail_if_exists,
        )
        if not copied_ok:
            error_code = ctypes.get_last_error()
            if error_code in (80, 183):
                return True, copied, False
            if self._cancel_event.is_set() or error_code == 1235:
                try:
                    os.remove(destination)
                except OSError:
                    pass
                return False, copied, False
            try:
                os.remove(destination)
            except OSError:
                pass
            raise ctypes.WinError(error_code)

        self._preserve_timestamps(source_stat, destination)
        return True, copied + int(source_stat.st_size), True

    def _copy_file(self, source, destination, copied, total, start_time, source_stat=None):
        """Create destination files exclusively; never overwrite existing files."""
        if os.path.exists(destination):
            return True, copied, False

        parent = os.path.dirname(destination) or "."
        os.makedirs(parent, exist_ok=True)
        if source_stat is None:
            source_stat = os.stat(source)

        if os.name == "nt":
            return self._copy_file_windows(
                source, destination, copied, total, start_time, source_stat
            )

        chunk_size = 8 * 1024 * 1024
        created_destination = False
        cancelled = False
        try:
            with open(source, "rb") as src:
                try:
                    dst = open(destination, "xb")
                    created_destination = True
                except FileExistsError:
                    return True, copied, False

                with dst:
                    while True:
                        if self._cancel_event.is_set():
                            cancelled = True
                            break

                        self._pause_event.wait()
                        if self._cancel_event.is_set():
                            cancelled = True
                            break

                        chunk = src.read(chunk_size)
                        if not chunk:
                            break

                        dst.write(chunk)
                        copied += len(chunk)

                        elapsed = max(time.monotonic() - start_time, 0.001)
                        speed = copied / elapsed
                        remaining = (
                            max(int((total - copied) / speed), 0)
                            if speed > 0 else 0
                        )
                        percent = (
                            min(int((copied / total) * 100), 99)
                            if total else 99
                        )
                        self.progressChanged.emit(
                            percent,
                            self._format_size(speed) + "/s",
                            self._format_time(remaining),
                        )

            if cancelled:
                if created_destination:
                    try:
                        os.remove(destination)
                    except OSError:
                        pass
                return False, copied, False

            self._preserve_timestamps(source_stat, destination)
            return True, copied, created_destination

        except Exception:
            if created_destination:
                try:
                    os.remove(destination)
                except OSError:
                    pass
            raise

    def _copy_folder(self, files, total, initially_skipped):
        self.copy_stats = {
            "copied_files": 0,
            "skipped_files": initially_skipped,
        }
        copied_bytes = 0
        start_time = time.monotonic()

        for source_file, destination_file, source_stat in files:
            if self._cancel_event.is_set():
                return False

            ok, copied_bytes, created = self._copy_file(
                source_file,
                destination_file,
                copied_bytes,
                total,
                start_time,
                source_stat,
            )
            if not ok:
                return False

            if created:
                self.copy_stats["copied_files"] += 1
            else:
                self.copy_stats["skipped_files"] += 1

        return True

    def _run(self, source, destination):
        try:
            self.copy_stats = {"copied_files": 0, "skipped_files": 0}
            self.statusChanged.emit("جاري النسخ إلى الفلاشة...")

            if os.path.isdir(source):
                os.makedirs(destination, exist_ok=True)
                files, total, initially_skipped = self._folder_copy_plan(
                    source, destination
                )
                usage = shutil.disk_usage(destination)
                if total > usage.free:
                    raise OSError(
                        28,
                        "Not enough free space for new files. Required: "
                        + self._format_size(total)
                        + ", available: "
                        + self._format_size(usage.free),
                        destination,
                    )
                success = self._copy_folder(files, total, initially_skipped)

            else:
                destination_parent = os.path.dirname(destination) or "."
                os.makedirs(destination_parent, exist_ok=True)
                if os.path.exists(destination):
                    self.copy_stats["skipped_files"] = 1
                    success = True
                else:
                    source_stat = os.stat(source)
                    total = int(source_stat.st_size)
                    usage = shutil.disk_usage(destination_parent)
                    if total > usage.free:
                        raise OSError(
                            28,
                            "Not enough free space for new file. Required: "
                            + self._format_size(total)
                            + ", available: "
                            + self._format_size(usage.free),
                            destination,
                        )
                    start_time = time.monotonic()
                    success, _, created = self._copy_file(
                        source, destination, 0, total, start_time, source_stat
                    )
                    if created:
                        self.copy_stats["copied_files"] = 1
                    elif success:
                        self.copy_stats["skipped_files"] = 1

            if self._cancel_event.is_set() or not success:
                self.statusChanged.emit("تم إلغاء النسخ")
                self.finished.emit(False, "تم إلغاء النسخ")
                return

            copied_files = int(self.copy_stats.get("copied_files", 0))
            skipped_files = int(self.copy_stats.get("skipped_files", 0))
            self.progressChanged.emit(100, "0 B/s", "00:00")
            message = (
                f"اكتمل النسخ: {copied_files} ملف جديد، "
                f"تم تخطي {skipped_files} ملف موجود مسبقاً"
            )
            self.statusChanged.emit(message)
            self.finished.emit(True, message)

        except Exception as exc:
            self.statusChanged.emit("حدث خطأ أثناء النسخ")
            self.finished.emit(False, str(exc))

    @Slot(str, str)
    def start(self, source, destination):
        if self._thread and self._thread.is_alive():
            return
        self._pause_event.set()
        self._cancel_event.clear()
        self.copy_stats = {"copied_files": 0, "skipped_files": 0}
        self._thread = threading.Thread(
            target=self._run,
            args=(source, destination),
            daemon=True,
        )
        self._thread.start()

    @Slot()
    def pause(self):
        self._pause_event.clear()
        self.statusChanged.emit("متوقف مؤقتًا")

    @Slot()
    def resume(self):
        self._pause_event.set()
        self.statusChanged.emit("جاري النسخ...")

    @Slot()
    def cancel(self):
        self._cancel_event.set()
        self._pause_event.set()
