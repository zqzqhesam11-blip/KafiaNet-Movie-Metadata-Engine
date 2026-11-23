from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_qml import AppController
from engine.adb_engine import ADBEngine


def check(label, condition):
    if not condition:
        raise AssertionError("FAILED: " + label)
    print("PASS:", label)


saved = (
    "/storage/emulated/0/Android/data/com.tencent.ig/"
    "files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved"
)

check(
    "remote secure_mkdirs permission denial activates Shizuku",
    AppController._is_android_data_permission_denial(
        saved,
        "KAFIANET_ANDROID_DATA_PERMISSION_DENIED: remote secure_mkdirs failed: Permission denied",
    ),
)
check(
    "real ADB secure_mkdirs() Operation not permitted activates Shizuku",
    AppController._is_android_data_permission_denial(
        saved,
        "adb: error: failed to copy 'x' to 'y': remote secure_mkdirs() failed: Operation not permitted",
    ),
)
check(
    "remote open permission denial activates Shizuku",
    AppController._is_android_data_permission_denial(
        saved,
        "adb: error: failed to copy 'x' to 'y': remote open failed: Permission denied",
    ),
)
check(
    "remote open EACCES activates Shizuku",
    AppController._is_android_data_permission_denial(
        saved, "adb: remote open failed: EACCES (Permission denied)"
    ),
)
check(
    "missing Saved directory does not activate Shizuku",
    not AppController._is_android_data_permission_denial(
        saved, "KAFIANET_DESTINATION_MISSING: مجلد الوجهة غير موجود"
    ),
)
check(
    "no-space failure does not activate Shizuku",
    not AppController._is_android_data_permission_denial(
        saved, "KAFIANET_NO_SPACE: No space left on device"
    ),
)
check(
    "generic permission denied without remote ADB marker does not activate Shizuku",
    not AppController._is_android_data_permission_denial(
        saved, "Permission denied"
    ),
)
check(
    "read-only filesystem does not activate Shizuku",
    not AppController._is_android_data_permission_denial(
        saved, "Read-only file system"
    ),
)
check(
    "offline/unauthorized ADB does not activate Shizuku",
    not AppController._is_android_data_permission_denial(
        saved, "error: device offline"
    ),
)
check(
    "permission denial outside Android/data does not activate Shizuku",
    not AppController._is_android_data_permission_denial(
        "/storage/emulated/0/Download/Saved", "remote open failed: Permission denied"
    ),
)
check(
    "read-only filesystem does not activate Shizuku",
    not AppController._is_android_data_permission_denial(
        saved, "Read-only file system"
    ),
)
check(
    "official Shizuku version with revision suffix parses",
    ADBEngine._version_key("13.6.0.r1086.2650830c") == (13, 6, 0),
)
check(
    "Shizuku 13.5.4 remains a supported version",
    ADBEngine._version_key("13.5.4") >= (13, 5, 4),
)
check(
    "newer Shizuku version compares numerically",
    ADBEngine._version_key("14.0.1") > ADBEngine._version_key("13.6.0"),
)
check(
    "KafiaNet helper 1.6.0 parses",
    ADBEngine._version_key("1.6.0") == (1, 6, 0),
)

helper_service = (
    ROOT / "android-helper/app/src/main/java/com/kafianet/shizukubridge/TransferUserService.java"
).read_text(encoding="utf-8")
helper_activity = (
    ROOT / "android-helper/app/src/main/java/com/kafianet/shizukubridge/MainActivity.java"
).read_text(encoding="utf-8")
engine_source = (ROOT / "engine/adb_engine.py").read_text(encoding="utf-8")
app_source = (ROOT / "app_qml.py").read_text(encoding="utf-8")

check(
    "Shizuku direct-stream helper contains no chmod calls",
    "Os.chmod(" not in helper_service,
)
check(
    "Shizuku destination write-probe API is removed",
    "canWriteDestination(" not in helper_service
    and "probe.createNewFile()" not in helper_service,
)
check(
    "Shizuku stream does not apply canonical folder containment checks",
    all(marker not in helper_service for marker in (
        "ensureWritable(",
        "Writable target escaped",
        "Writable path escaped",
        "Could not locate authorized game folder",
    )),
)
check(
    "selected destination is absolute but not restricted to Android/data",
    "Destination path must be absolute" in helper_service
    and 'String base = "/storage/emulated/0/Android/data/";' not in helper_service,
)
check(
    "direct stream retains temporary-file write, sync, size verification, and atomic rename",
    ".kafianet-" in helper_service
    and "new FileOutputStream(temporary, false)" in helper_service
    and "output.getFD().sync()" in helper_service
    and "written != expectedSize" in helper_service
    and "Os.rename(" in helper_service,
)
check(
    "Shizuku helper does not create an app-local staging copy",
    "getExternalFilesDir(\"incoming\")" not in helper_service
    and "File.createTempFile(\"kafianet-stage-\"" not in helper_service,
)
check(
    "Shizuku direct-stream start does not mkdir the Saved root",
    "if (!root.exists() && !root.mkdirs())" not in helper_service,
)

saved_path_guard = (
    engine_source.split("is_saved_destination = (", 1)[1]
    .split("permission_markers =", 1)[0]
)
non_saved_setup = (
    engine_source.split("if not is_saved_destination:", 1)[1]
    .split("write_result = subprocess.run(", 1)[0]
)
saved_write_probe = (
    engine_source.split("write_result = subprocess.run(", 1)[1]
    .split("if write_result.returncode != 0:", 1)[0]
)
check(
    "ADB Saved preflight never creates the Saved destination",
    '"/android/data/" in normalized_check_path' in saved_path_guard
    and 'endswith("/saved")' in saved_path_guard
    and '"mkdir"' in non_saved_setup
    and '"-p"' in non_saved_setup
    and '"touch"' in saved_write_probe
    and '"mkdir"' not in saved_write_probe
    and "KAFIANET_DESTINATION_MISSING" in engine_source,
)
check(
    "first Shizuku permission grant is polled and resumes setup",
    "permissionGrantMonitor" in helper_activity
    and "Shizuku.checkSelfPermission()" in helper_activity
    and "main.postDelayed(permissionGrantMonitor, 500L)" in helper_activity,
)
check(
    "initial permission wait allows a manual grant",
    "SHIZUKU_PERMISSION_WAIT_TIMEOUT_MS = 15L * 60L * 1000L" in helper_activity
    and "deadline = time.monotonic() + 16 * 60" in engine_source,
)
prepare_source = (
    engine_source.split("def prepare_shizuku_bridge", 1)[1]
    .split("start_result = self._run(", 1)[0]
)
check(
    "Shizuku preparation does not overwrite the transfer status box",
    "self.statusChanged.emit(" not in prepare_source,
)
direct_copy_source = (
    engine_source.split("def copy_via_shizuku_bridge", 1)[1]
    .split("@Slot(str)\n    def install", 1)[0]
)
check(
    "Shizuku copy loop does not emit per-file UI statuses",
    "self.statusChanged.emit(" not in direct_copy_source,
)
check(
    "fallback uses one fixed Shizuku comment",
    app_source.count('"جاري نسخ الملفات عبر شيزوكو"') >= 3,
)
check(
    "Shizuku stream uses 4 MiB chunks and preserves manifest order",
    "chunk_size=4 * 1024 * 1024" in engine_source
    and "source, file_size, 4 * 1024 * 1024" in engine_source
    and "files.sort(key=lambda item: item[1].casefold())" in engine_source,
)
check(
    "helper exposes token-protected controlled shutdown endpoint",
    '"/shutdown".equals(path)' in helper_activity
    and "scheduleShutdownAfterResponse()" in helper_activity,
)
check(
    "post-copy chmod failure no longer activates Shizuku fallback",
    "KAFIANET_POST_COPY_PERMISSION_ERROR:" not in app_source[
        app_source.index('if phase == "adb_direct":'):
        app_source.index('if phase == "shizuku_prepare":')
    ],
)
check(
    "Shizuku service failures are distinct from destination denial",
    "KAFIANET_SHIZUKU_USER_SERVICE_BIND_FAILED:" in (
        ROOT / "android-helper/app/src/main/java/com/kafianet/shizukubridge/MainActivity.java"
    ).read_text(encoding="utf-8")
    and "KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED:" in (
        ROOT / "android-helper/app/src/main/java/com/kafianet/shizukubridge/MainActivity.java"
    ).read_text(encoding="utf-8"),
)
prepare_source = (
    engine_source.split("def prepare_shizuku_bridge", 1)[1]
    .split("start_result = self._run(", 1)[0]
)
install_apk_source = (
    prepare_source.split("def install_apk", 1)[1]
    .split("def install_if_missing", 1)[0]
)
install_if_missing_source = (
    prepare_source.split("def install_if_missing", 1)[1]
    .split("# ---- Official Shizuku manager ----", 1)[0]
)
check(
    "Shizuku and helper are reused by package presence only",
    "def package_is_installed(package_id):" in prepare_source
    and '"pm", "path", package_id' in prepare_source
    and "dumpsys" not in prepare_source
    and "versionCode=" not in prepare_source
    and "versionName=" not in prepare_source
    and "verify_version" not in prepare_source
    and "install_if_missing" in prepare_source
    and "if package_is_installed(package_id):" in install_if_missing_source
    and "install_apk(apk_path, package_id, label)" in install_if_missing_source,
)
signature_guard_position = install_apk_source.find("helper_signature_conflict")
signature_uninstall_position = install_apk_source.find(
    '["-s", device, "uninstall", helper_package]'
)
signature_retry_position = (
    install_apk_source.find(
        "result = self._run_install_command(",
        signature_uninstall_position
    )
    if signature_uninstall_position >= 0 else -1
)
check(
    "only a helper signature conflict permits uninstall and one bundled-APK retry",
    '"install_failed_update_incompatible"' in install_apk_source
    and '"signature mismatch"' in install_apk_source
    and '"signatures do not match"' in install_apk_source
    and 'package_id == helper_package' in install_apk_source
    and signature_guard_position >= 0
    and signature_guard_position < signature_uninstall_position < signature_retry_position
    and install_apk_source.count('["-s", device, "uninstall", helper_package]') == 1,
)
ready_response_source = (
    engine_source.split('if health.get("ready") is True:', 1)[1]
    .split('if state == "failed":', 1)[0]
)
check(
    "ready Shizuku response uses only live session values after package-version checks were removed",
    '"port": local_port' in ready_response_source
    and '"token": session_token' in ready_response_source
    and '"uid": identity' in ready_response_source
    and "shizuku_info" not in ready_response_source
    and "helper_target_name" not in ready_response_source,
)
secure_mkdir_error = (
    "adb: error: failed to copy 'D:\\games\\Saved\\Avatar\\51668192071\\50018001.png' to "
    "'/sdcard/Android/data/com.tencent.ig/files/UE4Game/"
    "ShadowTrackerExtra/ShadowTrackerExtra/Saved/Avatar/51668192071/50018001.png': "
    "remote secure_mkdirs() failed: Operation not permitted"
)
check(
    "secure_mkdirs() denial resolves only the partially-created nested parent",
    ADBEngine._secure_mkdirs_retry_parent(saved, secure_mkdir_error)
    == saved + "/Avatar/51668192071",
)
check(
    "secure_mkdirs without parentheses is also recognized",
    ADBEngine._secure_mkdirs_retry_parent(
        saved,
        secure_mkdir_error.replace("secure_mkdirs()", "secure_mkdirs"),
    ) == saved + "/Avatar/51668192071",
)
check(
    "secure_mkdirs retry rejects Saved root and paths outside the destination",
    ADBEngine._secure_mkdirs_retry_parent(
        saved,
        "failed to copy 'x' to '" + saved + "/file.png': "
        "remote secure_mkdirs failed: Operation not permitted",
    ) is None
    and ADBEngine._secure_mkdirs_retry_parent(
        saved,
        "failed to copy 'x' to '/sdcard/Download/other/file.png': "
        "remote secure_mkdirs failed: Operation not permitted",
    ) is None,
)
retry_source = engine_source.split(
    "# secure_mkdirs may create a nested directory", 1
)[1].split("# A disconnected handset often makes adb push exit", 1)[0]
# Normalize whitespace so harmless line wrapping does not break source-policy checks.
retry_source_normalized = " ".join(retry_source.split())
check(
    "secure_mkdirs retry is bounded and verifies Saved plus partial parent without creating them",
    "max_secure_mkdir_retries = 16" in retry_source
    and "_remote_directory_exists( device, requested_destination )" in retry_source_normalized
    and "_remote_directory_exists( device, retry_parent )" in retry_source_normalized
    and "secure_mkdir_retry_paths.add(retry_parent)" in retry_source
    and "target=self._push_thread" in retry_source
    and '"mkdir"' not in retry_source
    and '"rm"' not in retry_source,
)
check(
    "ADB --sync is enabled only after a verified secure_mkdirs partial-directory retry",
    'if secure_mkdir_retry_paths:' in engine_source
    and 'command.append("--sync")' in engine_source
)

cleanup_source = (
    engine_source.split("def _cleanup_shizuku_bridge", 1)[1]
    .split("@Slot(str)\n    def cleanup_shizuku_bridge", 1)[0]
)
check(
    "Shizuku manager and KafiaNet helper stay installed after success or failure",
    '"/shutdown"' in cleanup_source
    and "uninstall" not in cleanup_source.lower()
    and "force-stop" not in cleanup_source.lower()
    and "shizuku_server" not in cleanup_source
    and "remove_helper" not in engine_source
    and "shizuku_installed_by_kafianet" not in engine_source,
)
pubg_start = app_source.find('elif game_name == "PUBG Mobile":')
flash_start = app_source.find('elif game_name == "PUBG Mobile Flash":', pubg_start + 1)
cod_start = app_source.find('elif game_name == "Call of Duty Mobile":', flash_start + 1)
pubg_source_block = app_source[pubg_start:flash_start] if pubg_start >= 0 and flash_start > pubg_start else ""
pubg_flash_source_block = app_source[flash_start:cod_start] if flash_start >= 0 and cod_start > flash_start else ""
saved_source_guards = (
    "size_folder = self._pubg_source_size",
    'self._find_case_insensitive_child_directory(',
    'if size_folder in ("Medium", "Low"):',
    'selected_source, "Saved"',
    "if nested_saved is None:",
    "selected_source = nested_saved",
    'if selected_source.name.casefold() != "saved":',
    "source = selected_source",
)
check(
    "PUBG and PUBG FLASH sources are constrained to Saved",
    pubg_start >= 0
    and flash_start > pubg_start
    and cod_start > flash_start
    and all(marker in pubg_source_block for marker in saved_source_guards)
    and all(marker in pubg_flash_source_block for marker in saved_source_guards),
)

print("SHIZUKU TRANSFER POLICY SIMULATION: PASS")
