import threading
import time

DEVICES = ["SERIAL_A", "SERIAL_B", "SERIAL_C", "SERIAL_D", "SERIAL_E"]

started = {}
finished = {}
lock = threading.Lock()
barrier = threading.Barrier(len(DEVICES))

def fake_adb_push(serial):
    barrier.wait()

    start = time.monotonic()
    with lock:
        started[serial] = start

    # محاكاة adb push طويل
    time.sleep(1.5)

    end = time.monotonic()
    with lock:
        finished[serial] = end

threads = [
    threading.Thread(target=fake_adb_push, args=(serial,), daemon=False)
    for serial in DEVICES
]

t0 = time.monotonic()

for t in threads:
    t.start()

for t in threads:
    t.join()

total = time.monotonic() - t0

print("\n=== CONCURRENCY RESULT ===")
for serial in DEVICES:
    print(
        serial,
        "START=", round(started[serial] - t0, 3),
        "END=", round(finished[serial] - t0, 3),
    )

print("\nTOTAL:", round(total, 3), "seconds")

starts = list(started.values())
spread = max(starts) - min(starts)

print("START SPREAD:", round(spread, 3), "seconds")

if total < 2.2 and spread < 0.2:
    print("PASS: العمليات الخمسة بدأت بالتوازي")
else:
    print("FAIL: التنفيذ ليس متوازيًا")
