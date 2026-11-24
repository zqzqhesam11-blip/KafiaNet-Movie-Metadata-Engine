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
    "remote secure_mkdirs permission denial qualifies for Shizuku fallback",
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
    "remote open permission denial qualifies for Shizuku fallback",
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
    "Shizuku-confirmed missing Saved directory does not loop into the root dialog",
    not AppController._is_android_data_permission_denial(
        saved, "KAFIANET_SHIZUKU_DESTINATION_MISSING: Saved not found"
    ),
)
check(
    "ordinary non-protected missing destination does not request the root dialog",
    not AppController._is_android_data_permission_denial(
        saved, "KAFIANET_DESTINATION_MISSING: مجلد الوجهة غير موجود"
    ),
)
check(
    "no-space failure does not request the root dialog",
    not AppController._is_android_data_permission_denial(
        saved, "KAFIANET_NO_SPACE: No space left on device"
    ),
)
check(
    "generic permission denied without remote ADB marker does not request the root dialog",
    not AppController._is_android_data_permission_denial(
        saved, "Permission denied"
    ),
)
check(
    "unverified destination probe does not request the root dialog",
    not AppController._is_android_data_permission_denial(
        saved, "KAFIANET_ANDROID_DATA_ACCESS_UNVERIFIED: could not inspect path"
    ),
)
check(
    "read-only filesystem does not request the root dialog",
    not AppController._is_android_data_permission_denial(
        saved, "Read-only file system"
    ),
)
check(
    "offline/unauthorized ADB does not request the root dialog",
    not AppController._is_android_data_permission_denial(
        saved, "error: device offline"
    ),
)
check(
    "permission denial outside Android/data does not request the root dialog",
    not AppController._is_android_data_permission_denial(
        "/storage/emulated/0/Download/Saved", "remote open failed: Permission denied"
    ),
)
check(
    "read-only filesystem does not request the root dialog",
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
    "CN PlayHub helper 1.6.7 parses",
    ADBEngine._version_key("1.6.7") == (1, 6, 7),
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
    "direct stream avoids forced disk sync and destination re-read while keeping in-stream byte counting",
    ".kafianet-" in helper_service
    and "new FileOutputStream(temporary, false)" in helper_service
    and "output.getFD().sync()" not in helper_service
    and "temporary.length()" not in helper_service
    and "written != expectedSize" in helper_service
    and "Os.rename(" in helper_service
    and "CountDownLatch" in helper_service
    and "stream.completed.await(" in helper_service
    and "stream.completed.countDown()" in helper_service,
)
check(
    "destination root is inspected by the Shizuku UID before transfer",
    "inspectDestination(String destination)" in helper_service
    and '"/destination/check"' in helper_activity
    and '"/destination/check"' in engine_source
    and "destination_check.get(\"exists\") is not True" in engine_source
    and "destination_check.get(\"missing\") is True" in engine_source
    and "KAFIANET_SHIZUKU_DESTINATION_ACCESS_DENIED" in engine_source
    and "Os.stat(root.getAbsolutePath())" in helper_service
    and "error.errno == OsConstants.ENOENT" in helper_service
    and "destination_check.get(\"isDirectory\") is not True" in engine_source
    and 'destination = str(destination_check.get("path") or destination)' in engine_source,
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
push_source = (
    engine_source.split("def _push_thread", 1)[1]
    .split("    @Slot(str, str, str)\n    def push(", 1)[0]
)
non_saved_setup = (
    push_source.split("if not is_saved_destination:", 1)[1]
    .split("# Never create Saved or its parents.", 1)[0]
)
saved_preflight = (
    push_source.split("is_saved_destination =", 1)[1]
    .split("manifest, total_bytes = self._build_direct_stream_manifest(source_path)", 1)[0]
)
saved_copy_stage = saved_preflight.split("# Never create Saved or its parents.", 1)[1]
check(
    "ADB attempts the real push to Saved without a preliminary read/write probe",
    '"/android/data/" in normalized_check_path' in saved_path_guard
    and 'endswith("/saved")' in saved_path_guard
    and '"mkdir"' in non_saved_setup
    and '"-p"' in non_saved_setup
    and "self._remote_directory_exists(device, check_path)" not in saved_copy_stage
    and "KAFIANET_ANDROID_DATA_ACCESS_UNVERIFIED" not in saved_copy_stage
    and '"touch"' not in saved_copy_stage
    and '"rm"' not in saved_copy_stage
    and '"mkdir"' not in saved_copy_stage
    and '"push"' in push_source
    and "self._synced_push_command(" in push_source
    and "KAFIANET_ANDROID_DATA_PERMISSION_DENIED" in push_source
    and "KAFIANET_SHIZUKU_DESTINATION_MISSING" in engine_source,
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
    "ADB copy never changes destination permissions after transfer",
    "chmod -R a+rwX" not in engine_source
    and "_set_remote_game_permissions" not in engine_source
    and "KAFIANET_POST_COPY_PERMISSION_ERROR" not in app_source,
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
    "Shizuku manager is reused while the bundled helper upgrades only when older",
    "def package_is_installed(package_id):" in prepare_source
    and '"pm", "path", package_id' in prepare_source
    and "install_if_missing" in prepare_source
    and "def install_helper_if_outdated():" in prepare_source
    and '"dumpsys", "package", helper_package' in prepare_source
    and "installed_code >= 14" in prepare_source
    and "install_helper_if_outdated()" in prepare_source
    and "install_if_missing(shizuku_apk, shizuku_package, \"Shizuku\")" in prepare_source,
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
    "ADB uses fast timestamp sync and resumes the interrupted file before the remaining tree",
    '"push", "--sync"' in engine_source
    and "ADB RESUME MODE: retry only the interrupted file" in engine_source
    and "ADB RESUME CHECKPOINT:" in engine_source
    and "ADB SECURE_MKDIRS FULL RETRY" in engine_source,
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

check(
    "Shizuku fallback is called only after a confirmed ADB protected-write denial",
    "self._begin_shizuku_fallback(serial, operation, message)" in app_source
    and "self._request_android_data_root_fallback(serial, operation, message)" not in (
        app_source.split('if phase == "adb_direct":', 1)[1]
        .split('if phase == "shizuku_prepare":', 1)[0]
    ),
)
check(
    "root fallback after Shizuku requires the explicit protected-destination writer denial marker",
    "def _is_shizuku_destination_write_denial(" in app_source
    and "self._is_shizuku_destination_write_denial(" in app_source
    and "kafianet_shizuku_destination_write_denied:" in app_source,
)
check(
    "only explicit Shizuku writer EACCES/EPERM opens root fallback",
    AppController._is_shizuku_destination_write_denial(
        saved, "KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED: open failed: EACCES (Permission denied)"
    )
    and not AppController._is_shizuku_destination_write_denial(
        saved, "لم يمنح الهاتف تطبيق KafiaNet إذن Shizuku"
    )
    and not AppController._is_shizuku_destination_write_denial(
        saved, "KAFIANET_SHIZUKU_DESTINATION_MISSING: Saved not found"
    )
    and not AppController._is_shizuku_destination_write_denial(
        saved, "No space left on device"
    )
    and not AppController._is_shizuku_destination_write_denial(
        "/storage/emulated/0/Download/Saved",
        "KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED: EACCES"
    ),
)
aidl_source = (
    ROOT / "android-helper/app/src/main/aidl/com/kafianet/shizukubridge/ITransferUserService.aidl"
).read_text(encoding="utf-8")
check(
    "Shizuku bridge exposes batched sync checks and precise stream-start errors",
    "checkDirectStreamFiles(String jobId, String destination, String filesJson) = 17;" in aidl_source
    and "getLastStartError() = 18;" in aidl_source,
)
check(
    "Shizuku skips only regular files matching exact size and second-resolution mtime",
    "Os.lstat(target.getAbsolutePath())" in helper_service
    and "OsConstants.S_ISREG(stat.st_mode)" in helper_service
    and "stat.st_size == expectedSize" in helper_service
    and "stat.st_mtime == (modifiedTimeMs / 1000L)" in helper_service,
)
check(
    "Shizuku records skipped files and bytes in the same completion totals",
    "job.skippedFiles++" in helper_service
    and "job.skippedBytes += expectedSize" in helper_service
    and "job.completedPaths.add(relativePath)" in helper_service,
)
check(
    "Shizuku checks file metadata in bounded batches and sends only nonmatching files",
    'request_json(\n                        "/stream/check"' in engine_source
    and "sync_batch_size = 256" in engine_source
    and "files_to_send = [" in engine_source
    and "enumerate(files_to_send, start=1)" in engine_source,
)
check(
    "Shizuku writer emits a distinct marker only when an attempted write gets EACCES/EPERM",
    "KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED:" in helper_service
    and "describeWriteFailure(error)" in helper_service,
)

check(
    "remote read-only filesystem is not treated as a protected write permission denial",
    not AppController._is_android_data_permission_denial(
        saved, "adb: error: failed to copy 'x' to 'y': remote open failed: Read-only file system"
    ),
)
check(
    "Shizuku metadata EACCES/EPERM is not fatal and the actual writer decides",
    "error.errno != OsConstants.EACCES" in helper_service
    and "error.errno != OsConstants.EPERM" in helper_service
    and "Let the actual writer report a precise EACCES/EPERM" in helper_service,
)
check(
    "Shizuku transport loss waits for the same device and restarts from a fresh size+mtime sync pass",
    "def _resume_shizuku_bridge_after_reconnect(" in engine_source
    and "self._wait_for_device_reconnect(device)" in engine_source
    and "resume_attempts < 10" in engine_source
    and "secrets.token_hex(12)" in engine_source
    and "only incomplete/changed files" in engine_source,
)
check(
    "Shizuku resume handoff keeps the helper alive until the resumed worker owns cleanup",
    "resume_handoff = True" in engine_source
    and "if not resume_handoff:" in engine_source
    and "cleanup_state[\"local_port\"] = resumed_port" in engine_source,
)
check(
    "Shizuku reconnect parses the newly forwarded localhost TCP port",
    'port_match = re.search(r"\\b(\\d{4,5})\\b", forward_output)' in engine_source
    and 'replace("\\\\", "/")' not in (
        engine_source.split("def _resume_shizuku_bridge_after_reconnect(", 1)[1]
        .split("@Slot(str, str, str, str, str, str, str)", 1)[0]
    ),
)
print("SHIZUKU TRANSFER POLICY SIMULATION: PASS")
