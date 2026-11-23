import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app_qml import AppController


def _stamp(days_ago=0):
    return (
        datetime.now().astimezone() - timedelta(days=days_ago)
    ).isoformat(timespec="seconds")


def main():
    history = [
        {
            "id": "today-a",
            "kind": "نسخ",
            "status": "اكتمل",
            "target_type": "android",
            "serial": "DEVICE-A",
            "game": "Fortnite",
            "units": 2,
            "date": _stamp(0),
        },
        {
            "id": "yesterday-a",
            "kind": "نسخ",
            "status": "اكتمل",
            "target_type": "android",
            "serial": "DEVICE-B",
            "game": "PUBG Mobile",
            "units": 3,
            "date": _stamp(1),
        },
        {
            "id": "today-duplicate",
            "kind": "نسخ",
            "status": "اكتمل",
            "target_type": "android",
            "serial": "DEVICE-A",
            "game": "Fortnite",
            "units": 2,
            "date": _stamp(0),
        },
        {
            "id": "today-review",
            "kind": "نسخ",
            "status": "مراجعة",
            "target_type": "android",
            "serial": "DEVICE-C",
            "game": "Genshin Impact",
            "units": 1,
            "date": _stamp(0),
        },
        {
            "id": "today-usb",
            "kind": "نسخ",
            "status": "اكتمل",
            "target_type": "usb_storage",
            "serial": "USB-1",
            "game": "USB folder",
            "units": 1,
            "date": _stamp(0),
        },
        {
            "id": "today-failed",
            "kind": "نسخ",
            "status": "فشل",
            "target_type": "android",
            "serial": "DEVICE-D",
            "game": "Free Fire",
            "units": 1,
            "date": _stamp(0),
        },
    ]
    fake_controller = SimpleNamespace(
        _transfer_history=history,
        _copy_entry_units=AppController._copy_entry_units,
        _is_insufficient_storage_failure=AppController._is_insufficient_storage_failure,
    )
    summary = AppController._build_transfer_summary(fake_controller)

    assert summary["completed"] == 5, summary
    assert summary["completedToday"] == 2, summary
    assert summary["review"] == 1, summary
    assert summary["failed"] == 1, summary
    assert summary["totalCopies"] == 9, summary

    print("TODAY SUCCESSFUL-COPY SUMMARY SIMULATION: OK")


if __name__ == "__main__":
    main()
