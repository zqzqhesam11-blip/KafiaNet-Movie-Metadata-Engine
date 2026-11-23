import threading
import time
from dataclasses import dataclass, field

DEVICES = [
    "SERIAL_A",
    "SERIAL_B",
    "SERIAL_C",
    "SERIAL_D",
    "SERIAL_E",
]

@dataclass
class Device:
    serial: str
    progress: int = 0
    status: str = "جاهز"
    speed: str = "0 B/s"
    eta: str = "--"
    error: str = ""
    cancelled: bool = False
    finished: bool = False
    started_at: float = 0.0
    finished_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

devices = {serial: Device(serial) for serial in DEVICES}

# سيناريو الاختبار:
# A = نجاح
# B = نجاح
# C = فشل عند 60%
# D = إلغاء عند 40%
# E = نجاح
FAIL_SERIAL = "SERIAL_C"
CANCEL_SERIAL = "SERIAL_D"

barrier = threading.Barrier(len(DEVICES))

def update(device, **kwargs):
    with device.lock:
        for key, value in kwargs.items():
            setattr(device, key, value)

def fake_copy(serial):
    device = devices[serial]

    barrier.wait()

    update(
        device,
        status="جاري النسخ",
        started_at=time.monotonic(),
    )

    for percent in (10, 20, 30, 40, 50, 60, 70, 80, 90):
        time.sleep(0.08)

        if device.cancelled:
            update(
                device,
                status="ملغي",
                error="تم الإلغاء بواسطة المستخدم",
                finished=True,
                finished_at=time.monotonic(),
            )
            return

        if serial == FAIL_SERIAL and percent == 60:
            update(
                device,
                progress=percent,
                status="فشل",
                error="محاكاة فشل ADB",
                finished=True,
                finished_at=time.monotonic(),
            )
            return

        update(
            device,
            progress=percent,
            speed=f"{20 + percent} MB/s",
            eta=f"00:{max(1, (100 - percent) // 10):02d}",
        )

    update(
        device,
        progress=100,
        status="اكتمل",
        speed="0 B/s",
        eta="00:00",
        finished=True,
        finished_at=time.monotonic(),
    )

threads = [
    threading.Thread(target=fake_copy, args=(serial,))
    for serial in DEVICES
]

t0 = time.monotonic()

for thread in threads:
    thread.start()

# ننتظر قليلًا ثم نلغي D وهو يعمل.
time.sleep(0.16)
devices[CANCEL_SERIAL].cancelled = True

for thread in threads:
    thread.join()

elapsed = time.monotonic() - t0

print("\n=== FIVE DEVICE LIFECYCLE ===")

for serial in DEVICES:
    d = devices[serial]
    print(
        f"{serial}: "
        f"status={d.status}, "
        f"progress={d.progress}%, "
        f"speed={d.speed}, "
        f"error={d.error or '-'}"
    )

print("\n=== ASSERTIONS ===")

checks = {
    "all_started": all(d.started_at > 0 for d in devices.values()),
    "A_success": (
        devices["SERIAL_A"].status == "اكتمل"
        and devices["SERIAL_A"].progress == 100
    ),
    "B_success": (
        devices["SERIAL_B"].status == "اكتمل"
        and devices["SERIAL_B"].progress == 100
    ),
    "C_failed": (
        devices["SERIAL_C"].status == "فشل"
        and devices["SERIAL_C"].progress == 60
        and bool(devices["SERIAL_C"].error)
    ),
    "D_cancelled": (
        devices["SERIAL_D"].status == "ملغي"
        and devices["SERIAL_D"].cancelled
    ),
    "E_success": (
        devices["SERIAL_E"].status == "اكتمل"
        and devices["SERIAL_E"].progress == 100
    ),
    "independent_progress": len(
        {
            (
                d.serial,
                d.progress,
                d.status,
            )
            for d in devices.values()
        }
    ) == 5,
}

for name, passed in checks.items():
    print(f"{name}: {'PASS' if passed else 'FAIL'}")

starts = [d.started_at for d in devices.values()]
spread = max(starts) - min(starts)

print("\nSTART SPREAD:", round(spread, 3), "seconds")
print("TOTAL:", round(elapsed, 3), "seconds")

all_pass = all(checks.values()) and spread < 0.15

print(
    "\nRESULT:",
    "PASS — 5 هواتف مستقلة، تقدم مستقل، فشل/إلغاء مستقل"
    if all_pass
    else "FAIL"
)

if not all_pass:
    raise SystemExit(1)
