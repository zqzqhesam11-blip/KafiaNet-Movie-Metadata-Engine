from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read(path: str) -> str:
    target = ROOT / path
    if not target.is_file():
        raise AssertionError(f"Required source file is missing: {path}")
    return target.read_text(encoding="utf-8")

manifest = read("android-helper/app/src/main/AndroidManifest.xml")
main = read("android-helper/app/src/main/java/com/kafianet/shizukubridge/MainActivity.java")
efootball = read("android-helper/app/src/main/java/com/kafianet/shizukubridge/EFootballActivity.java")
home = read("android-helper/app/src/main/java/com/kafianet/shizukubridge/HomeActivity.java")
local_service = read("android-helper/app/src/main/java/com/kafianet/shizukubridge/PubgOfflineFinalizeService.java")
keep_alive = read("android-helper/app/src/main/java/com/kafianet/shizukubridge/BridgeKeepAliveService.java")
gradle = read("android-helper/app/build.gradle")

assert 'android:name=".BridgeKeepAliveService"' in manifest
assert 'android:exported="false"' in manifest
assert 'android:stopWithTask="false"' in manifest
assert 'android:foregroundServiceType="dataSync"' in manifest
assert 'android.permission.FOREGROUND_SERVICE_DATA_SYNC' in manifest
assert 'android.permission.WAKE_LOCK' in manifest

# Keep legacy SAF behavior for the user's Android/data finalize path.
assert "targetSdk 29" in gradle
assert 'applicationId \'com.kafianet.shizukubridge\'' in gradle

# The helper service must be foreground while its transfer Activity is backgrounded.
assert "START_STICKY" in keep_alive
assert "startForeground(NOTIFICATION_ID" in keep_alive
assert "setOngoing(true)" in keep_alive
assert "PowerManager.PARTIAL_WAKE_LOCK" in keep_alive
assert "PowerManager.SCREEN_DIM_WAKE_LOCK" in keep_alive
assert "acquire(WAKE_LOCK_TIMEOUT_MS)" in keep_alive
assert "releaseTransferWakeLocks()" in keep_alive
assert "stopWithTask" not in keep_alive

# Shizuku bridge lifecycle is connected to the real transfer endpoints.
assert "BridgeKeepAliveService.startSession(this);" in main
assert '"/stream/start"' in main and "BridgeKeepAliveService.transferStarted(this, jobId);" in main
assert '"/stream/finish"' in main and "BridgeKeepAliveService.transferFinished(this, jobId);" in main
assert '"/cancel"' in main and "BridgeKeepAliveService.transferFinished(this, jobId);" in main
assert "BridgeKeepAliveService.stopSession(this);" in main

# eFootball's separate SAF server has its own transfer start/finish lifecycle.
assert "BridgeKeepAliveService.startSession(this);" in efootball
assert '"/start"' in efootball and "BridgeKeepAliveService.transferStarted(this, activeJob);" in efootball
assert '"/finish"' in efootball and "BridgeKeepAliveService.transferFinished(this, jobId);" in efootball
assert "BridgeKeepAliveService.stopSession(this);" in efootball

# On-device root finalization keeps both CPU and display awake, then releases both.
assert "PowerManager.PARTIAL_WAKE_LOCK" in local_service
assert "PowerManager.SCREEN_DIM_WAKE_LOCK" in local_service
assert "12L * 60L * 60L * 1000L" in local_service
assert "screenWakeLock" in local_service
assert "releaseTransferWakeLock();" in local_service

# Visible progress UI prevents screen timeout while local copy is shown.
assert "FLAG_KEEP_SCREEN_ON" in home
assert '"preparing".equals(state) || "running".equals(state)' in home
assert home.index('String state = prefs.getString(PubgOfflineFinalizeService.KEY_STATE') < home.index('setLocalTransferScreenOn("preparing".equals(state)')
assert "الضغط على زر Home" in home

print("ANDROID TRANSFER BACKGROUND LIFECYCLE: PASS")
