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
check("no destination preflight API remains", "canWriteDestination(" not in service)
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
check("direct stream retains temp file, fsync, size check and atomic rename", all(
    token in service for token in (
        '".kafianet-"',
        "new FileOutputStream(temporary, false)",
        "output.getFD().sync()",
        "written != expectedSize",
        "Os.rename(",
    )
))
check("first-run Shizuku permission is actively polled", (
    "permissionGrantMonitor" in activity
    and "Shizuku.checkSelfPermission()" in activity
    and "main.postDelayed(permissionGrantMonitor, 500L)" in activity
))
check("permission wait lasts fifteen minutes", (
    "SHIZUKU_PERMISSION_WAIT_TIMEOUT_MS = 15L * 60L * 1000L" in activity
    and "permissionRequestStartedAt" in activity
))
check("Shizuku UserService version advances", ".version(11)" in activity)
check("HTTP receive buffer is 4 MiB", "new byte[4 * 1024 * 1024]" in activity)
check("Shizuku writer buffer is 4 MiB", "new byte[4 * 1024 * 1024]" in service)
check("helper package is 1.6.5 / code 12", (
    "versionCode 12" in gradle and "versionName '1.6.5'" in gradle
))
print("SHIZUKU PERMISSION / STREAM POLICY: PASS")
