from pathlib import Path

service = Path(
    "android-helper/app/src/main/java/com/kafianet/shizukubridge/TransferUserService.java"
).read_text(encoding="utf-8")
activity = Path(
    "android-helper/app/src/main/java/com/kafianet/shizukubridge/MainActivity.java"
).read_text(encoding="utf-8")
gradle = Path("android-helper/app/build.gradle").read_text(encoding="utf-8")


def check(label, condition):
    if not condition:
        raise SystemExit("FAIL: " + label)
    print("PASS:", label)


check("no chmod calls in Shizuku transfer service", "Os.chmod(" not in service)
check("old destination write-probe API is absent", "canWriteDestination(" not in service)
check("read-only Shizuku destination inspection uses stat", (
    "inspectDestination(String destination)" in service
    and "Os.stat(root.getAbsolutePath())" in service
    and "OsConstants.ENOENT" in service
))
check("no canonical authorized-root containment checks remain", all(
    token not in service for token in (
        "ensureWritable(",
        "Writable target escaped",
        "Writable path escaped",
        "Could not locate authorized game folder",
    )
))
check("selected root is absolute without Android/data pinning", (
    "Destination path must be absolute" in service
    and 'String base = "/storage/emulated/0/Android/data/";' not in service
))
check("unsafe relative paths remain rejected", all(
    token in service for token in (
        'relative.startsWith("/")',
        'relative.contains(":")',
        '".."',
        '"."',
    )
))
check("direct stream avoids fsync and destination re-read while keeping in-stream byte counting", all(
    token in service for token in (
        '".kafianet-"',
        "new FileOutputStream(temporary, false)",
        "written != expectedSize",
        "Os.rename(",
        "stream.completed.await(",
        "stream.completed.countDown()",
    )
) and "output.getFD().sync()" not in service and "temporary.length()" not in service)
check("per-file 100ms status polling is removed", "Thread.sleep(100L)" not in activity)
check("first-run Shizuku permission is actively polled", (
    "permissionGrantMonitor" in activity
    and "Shizuku.checkSelfPermission()" in activity
    and "main.postDelayed(permissionGrantMonitor, 500L)" in activity
))
check("permission wait lasts fifteen minutes", (
    "SHIZUKU_PERMISSION_WAIT_TIMEOUT_MS = 15L * 60L * 1000L" in activity
    and "permissionRequestStartedAt" in activity
))
check("Shizuku UserService version advances", ".version(12)" in activity)
check("HTTP receive buffer is 4 MiB", "new byte[4 * 1024 * 1024]" in activity)
check("Shizuku writer buffer is 4 MiB", "new byte[4 * 1024 * 1024]" in service)
check("helper package is 1.6.6 / code 13", (
    "versionCode 13" in gradle and "versionName '1.6.6'" in gradle
))
print("SHIZUKU PERMISSION / STREAM POLICY: PASS")
