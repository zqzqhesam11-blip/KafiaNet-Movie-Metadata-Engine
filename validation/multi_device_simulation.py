"""Deterministic simulation of independent per-device operations."""

import os
import hashlib
import subprocess
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import ModuleType, SimpleNamespace
import json

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adb_engine import ADBEngine


def load_controller():
    module = ModuleType("app_qml")
    module.__file__ = str(ROOT / "app_qml.py")
    sys.modules["app_qml"] = module
    source = Path(module.__file__).read_text(encoding="utf-8")
    marker = "\ndef main():"
    if marker not in source:
        raise AssertionError("Could not locate app_qml main entry marker.")
    source = source.split(marker, 1)[0]
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module


app_qml = load_controller()

DEVICES = [
    {"serial": "SERIAL_A", "model": "Samsung A", "name": "Samsung A"},
    {"serial": "SERIAL_B", "model": "Samsung B", "name": "Samsung B"},
    {"serial": "SERIAL_C", "model": "Samsung C", "name": "Samsung C"},
]


class FakeOperationEngine(QObject):
    progressChanged = Signal(int, str, str)
    statusChanged = Signal(str)
    finished = Signal(bool, str)
    logChanged = Signal(str)

    instances = []

    def __init__(self):
        super().__init__()
        self.serial = None
        self.calls = []
        self.cancelled = False
        self.paused = False
        FakeOperationEngine.instances.append(self)

    def push(self, serial, source, destination):
        self.serial = serial
        self.calls.append(("push", serial, source, destination))

    def install_if_missing(self, serial, apk, package_id):
        self.serial = serial
        self.calls.append(("install_if_missing", serial, apk, package_id))

    def prepare_shizuku_bridge(self, serial, shizuku_apk, helper_apk, token, package, destination):
        self.serial = serial
        self.calls.append((
            "prepare_shizuku_bridge", serial, shizuku_apk, helper_apk,
            token, package, destination
        ))

    def copy_via_shizuku_bridge(self, serial, port, token, job_id, staging, destination, relative):
        self.serial = serial
        self.calls.append((
            "copy_via_shizuku_bridge", serial, port, token,
            job_id, staging, destination, relative
        ))

    def launch_phone_local_finalize(self, serial, source_root, package, relative, game):
        self.serial = serial
        self.calls.append((
            "launch_phone_local_finalize", serial, source_root, package, relative, game
        ))
        self.finished.emit(
            True,
            "تم فتح المساعد لطلب الإذن؛ يستمر النقل محلياً بعد الموافقة."
        )

    def prepare_game_permissions_via_shizuku(self, serial, port, token, destination):
        self.serial = serial
        self.calls.append((
            "prepare_game_permissions_via_shizuku", serial, port, token, destination
        ))

    def install(self, serial, apk):
        self.serial = serial
        self.calls.append(("install", serial, apk))

    def install_batch(self, serial, entries):
        self.serial = serial
        self.calls.append(("install_batch", serial, [dict(item) for item in entries]))

    def install_apks(self, serial, apk):
        self.serial = serial
        self.calls.append(("install_apks", serial, apk))

    def uninstall(self, serial, package_id):
        self.serial = serial
        self.calls.append(("uninstall", serial, package_id))

    def cancel(self):
        self.cancelled = True

    def pause(self):
        self.paused = True
        return True

    def resume(self):
        self.paused = False
        return True

    def progress(self, percent, speed="--", eta="--"):
        self.progressChanged.emit(percent, speed, eta)

    def finish(self, success, message):
        self.finished.emit(success, message)


class FakeEFootballSAFEngine(FakeOperationEngine):
    """Test double distinct from the ordinary ADB transfer engine."""


class DiscoveryEngine:
    _thread = None

    def __init__(self):
        self.discovery = list(DEVICES)

    def get_devices(self):
        return list(self.discovery)


def rows(serials):
    return [
        {
            "serial": serial,
            "model": serial,
            "game": "PUBG Mobile",
            "kind": "copy",
            "status": "انتظار",
            "percent": 0,
            "operation": "في قائمة الانتظار",
            "speed": "--",
            "eta": "--",
            "error": "",
            "completed": False,
            "cancelled": False,
        }
        for serial in serials
    ]


def make_controller():
    app_qml.ADBEngine = FakeOperationEngine
    app_qml.EFootballSAFEngine = FakeEFootballSAFEngine
    controller = app_qml.AppController()
    controller.adb_engine = DiscoveryEngine()
    # Isolate validation from persistent user settings and history files.
    controller._save_transfer_history = lambda: None
    controller._save_settings = lambda: None
    FakeOperationEngine.instances = []
    return controller


def engine_for(serial):
    return next(
        engine for engine in FakeOperationEngine.instances
        if engine.serial == serial
    )


def test_discovery_and_selection(controller):
    models = []
    controller.adbDevicesChanged.connect(models.append)
    controller.getADBDevices()
    assert [item["serial"] for item in models[-1]] == [
        "SERIAL_A", "SERIAL_B", "SERIAL_C"
    ]
    assert controller.selected_devices == []
    assert controller.selected_device == ""


def test_usb_storage_is_merged_and_counted_as_one_unit():
    controller = make_controller()
    usb_serial = "USBVOL:AB12CD34:E:"
    controller._last_usb_devices = [{
        "serial": usb_serial,
        "type": "usb_storage",
        "name": "KINGSTON (E:)",
        "model": "KINGSTON (E:)",
        "root": "E:\\",
        "drive": "E:",
        "total_bytes": 64 * 1024**3,
        "free_bytes": 40 * 1024**3,
        "units": 1,
        "unit_price": 500,
        "billing_value": 500,
    }]
    emitted = []
    controller.adbDevicesChanged.connect(emitted.append)
    controller._apply_adb_devices(DEVICES[:2])

    targets = emitted[-1]
    assert [item["serial"] for item in targets] == [
        "SERIAL_A", "SERIAL_B", usb_serial
    ]
    flash = next(item for item in targets if item["serial"] == usb_serial)
    assert flash["type"] == "usb_storage"
    assert flash["units"] == 1

    controller.setADBDeviceSelection(["SERIAL_A", usb_serial])
    assert controller.getSelectedTargetUnits() == 2
    assert controller.selected_device == "SERIAL_A"
    assert controller.selected_devices == ["SERIAL_A", usb_serial]


def test_usb_copy_target_routes_to_local_drive_with_one_unit():
    controller = make_controller()
    usb_serial = "USBVOL:11223344:E:"
    with TemporaryDirectory() as directory:
        root = Path(directory)
        game = root / "ببجي (PUBG)"
        payload = game / "Saved"
        payload.mkdir(parents=True)
        (payload / "asset.pak").write_bytes(b"pubg-data")
        usb_root = root / "USB"
        usb_root.mkdir()

        controller.source_root = root
        controller.selected_devices = [usb_serial]
        controller.selected_device = ""
        controller._device_info[usb_serial] = {
            "serial": usb_serial,
            "type": "usb_storage",
            "name": "USB Flash (E:)",
            "display_name": "USB Flash (E:)",
            "root": str(usb_root),
            "units": 1,
        }

        original_find_game = app_qml.find_game_folder
        original_get_game = app_qml.get_game
        original_enumerate = app_qml.enumerate_removable_storage_devices
        original_splitdrive = app_qml.os.path.splitdrive
        captured = []
        app_qml.find_game_folder = lambda _root, _name: game
        app_qml.get_game = lambda _name: {"name": "PUBG Mobile"}
        app_qml.enumerate_removable_storage_devices = lambda: [{
            "serial": usb_serial,
            "root": str(usb_root),
            "name": "USB Flash (E:)",
        }]
        def fake_splitdrive(path):
            text_path = str(path).casefold()
            if text_path.startswith(str(usb_root).casefold()):
                return "E:", text_path[2:]
            return "C:", text_path
        app_qml.os.path.splitdrive = fake_splitdrive
        controller._start_operation = lambda operation: captured.append(operation)
        try:
            controller.copyGame("PUBG Mobile")
        finally:
            app_qml.find_game_folder = original_find_game
            app_qml.get_game = original_get_game
            app_qml.enumerate_removable_storage_devices = original_enumerate
            app_qml.os.path.splitdrive = original_splitdrive

        assert len(captured) == 1
        operation = captured[0]
        assert operation["target_type"] == "usb_storage"
        assert operation["units"] == 1
        assert Path(operation["source"]) == payload
        assert Path(operation["destination"]) == (
            usb_root / "CN PlayHub" / "PUBG Mobile"
        )
        assert operation["phase"] == "local_usb_copy"


def test_local_usb_transfer_engine_copies_files_and_checks_free_space():
    import engine.transfer_engine as transfer_module
    from types import SimpleNamespace

    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "source"
        (source / "Paks").mkdir(parents=True)
        (source / "Paks" / "data.pak").write_bytes(b"usb-copy-payload")
        destination = root / "USB" / "CN PlayHub" / "PUBG Mobile"

        engine = app_qml.TransferEngine()
        completed = []
        engine.finished.connect(
            lambda success, message: completed.append((success, message))
        )
        engine._run(str(source), str(destination))
        assert completed and completed[-1][0] is True
        assert (
            destination / "Paks" / "data.pak"
        ).read_bytes() == b"usb-copy-payload"

        original_disk_usage = transfer_module.shutil.disk_usage
        transfer_module.shutil.disk_usage = lambda _path: SimpleNamespace(
            total=1024, used=1024, free=0
        )
        try:
            blocked_engine = app_qml.TransferEngine()
            blocked = []
            blocked_engine.finished.connect(
                lambda success, message: blocked.append((success, message))
            )
            blocked_engine._run(str(source), str(root / "USB-FULL" / "game"))
            assert blocked and blocked[-1][0] is False
            assert "Not enough free space for new files" in blocked[-1][1]
            assert app_qml.AppController._is_insufficient_storage_failure(
                blocked[-1][1]
            )
        finally:
            transfer_module.shutil.disk_usage = original_disk_usage


def test_usb_insufficient_space_error_is_classified_correctly():
    controller = make_controller()
    assert controller._is_insufficient_storage_failure(
        "Not enough space on USB storage. Required: 20 GB, available: 0 B"
    )
    assert controller._is_insufficient_storage_failure(
        "OSError: [Errno 28] No space left on device"
    )


def test_transfer_summary_excludes_usb_flash_and_counts_phone_copy():
    controller = make_controller()
    controller._transfer_history = [
        {
            "id": "usb-copy", "kind": "نسخ", "status": "اكتمل",
            "serial": "USBVOL:00112233:E:", "target_type": "usb_storage",
            "game": "PUBG Mobile", "units": 1,
        },
        {
            "id": "phone-copy", "kind": "نسخ", "status": "اكتمل",
            "serial": "SERIAL_A", "target_type": "android",
            "game": "PUBG Mobile", "units": 1,
        },
    ]
    summary = controller.getTransferSummary()
    assert summary["completed"] == 1
    assert summary["totalCopies"] == 1


def test_usb_storage_cannot_receive_android_install_commands():
    controller = make_controller()
    usb_serial = "USBVOL:00112233:E:"
    controller._device_info[usb_serial] = {
        "serial": usb_serial,
        "type": "usb_storage",
        "name": "USB Flash",
    }
    controller.selected_devices = [usb_serial]
    rejected = []
    controller.operationRejected.connect(rejected.append)
    controller.installGame("PUBG Mobile")
    assert rejected
    assert "فلاش USB" in rejected[-1]
    assert not controller._active_operations


def test_device_sorting_uses_latest_first_seen(controller):
    controller._device_first_seen = {
        "SERIAL_A": 10.0,
        "SERIAL_B": 30.0,
        "SERIAL_C": 20.0,
    }
    emitted = []
    controller.adbDevicesChanged.connect(emitted.append)
    controller.getADBDevices()
    assert [item["serial"] for item in emitted[-1]] == [
        "SERIAL_B", "SERIAL_C", "SERIAL_A"
    ]
    assert emitted[-1][0]["model"] == controller._device_info["SERIAL_B"]["display_name"]


def test_transfer_history_and_completed_card_dismissal():
    controller = make_controller()
    controller._start_operation(
        dict(
            rows(["SERIAL_A"])[0],
            model="Samsung A",
            device_name="هاتف هشام",
            source="source-folder",
            destination="/sdcard/GameData/",
            source_label="مخصص",
            source_root=r"D:\CustomGames",
        )
    )
    engine_for("SERIAL_A").finish(True, "copy complete")
    history = controller.getTransferHistory()
    assert len(history) == 1
    assert history[0]["device"] == "هاتف هشام"
    assert history[0]["device_name"] == "هاتف هشام"
    assert history[0]["source"] == "مخصص"
    assert history[0]["status"] == "اكتمل"
    assert controller.dismissDeviceOperation("SERIAL_A") is True
    assert "SERIAL_A" not in controller._active_operations
    assert len(controller.getTransferHistory()) == 1

    controller._start_operation(
        dict(
            rows(["SERIAL_B"])[0],
            model="Samsung B",
            source="source-folder",
            destination="/sdcard/GameData/",
        )
    )
    engine_for("SERIAL_B").finish(False, "device disconnected")
    history = controller.getTransferHistory()
    assert len(history) == 2
    assert history[0]["status"] == "فشل"
    assert history[0]["message"] == "device disconnected"


def test_copy_history_retry_review_and_storage_counters():
    controller = make_controller()

    def finish_copy(serial, success, message):
        operation = dict(
            rows([serial])[0],
            model="Phone " + serial,
            source="source-folder",
            destination="/sdcard/GameData/",
            history_recorded=False,
        )
        controller._start_operation(operation)
        FakeOperationEngine.instances[-1].finish(success, message)

    finish_copy("SERIAL_A", False, "device disconnected")
    summary = controller.getTransferSummary()
    assert summary["failed"] == 1
    assert summary["completed"] == 0

    finish_copy("SERIAL_A", True, "copy complete after retry")
    history = controller.getTransferHistory()
    assert history[0]["status"] == "اكتمل"
    assert history[-1]["status"] == "تمت المعالجة"
    summary = controller.getTransferSummary()
    assert summary["failed"] == 0
    assert summary["completed"] == 1

    finish_copy("SERIAL_A", True, "repeat copy")
    assert controller.getTransferHistory()[0]["status"] == "مراجعة"
    summary = controller.getTransferSummary()
    assert summary["completed"] == 1
    assert summary["review"] == 1

    finish_copy("SERIAL_B", False, "adb: failed to copy: No space left on device")
    summary = controller.getTransferSummary()
    assert summary["insufficientSpace"] == 1
    assert summary["failed"] == 0

    finish_copy("SERIAL_B", True, "copy complete after freeing space")
    summary = controller.getTransferSummary()
    assert summary["insufficientSpace"] == 0
    assert summary["completed"] == 2


def test_amount_based_install_randomly_selects_unique_game_folders():
    controller = make_controller()
    assert [
        controller.getGameCountForAmount(str(amount))
        for amount in (100, 200, 300, 400, 500)
    ] == [10, 25, 40, 55, 70]
    assert controller.getGameCountForAmount("١٠٠") == 10
    assert controller.getGameCountForAmount("150") == 18
    assert controller.getGameCountForAmount("170") == 21
    assert controller.getGameCountForAmount("١٥٠") == 18
    assert controller.getGameCountForAmount("90") == 0
    assert controller.getGameCountForAmount("abc") == 0

    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        for index in range(13):
            game_folder = source_root / f"Game {index:02d}"
            game_folder.mkdir()
            if index == 0:
                (game_folder / "base.apk").write_bytes(b"base")
                (game_folder / "split_config.apk").write_bytes(b"split")
            else:
                (game_folder / "game.apk").write_bytes(b"apk")

        controller.install_source_path = str(source_root)
        controller._device_info = {
            "SERIAL_A": {"name": "Test Android", "model": "Test Android"}
        }
        controller.selected_devices = ["SERIAL_A"]

        preview = controller._collect_install_batch()
        assert len({entry["gameKey"] for entry in preview}) == 13
        split_entry = next(entry for entry in preview if entry["type"] == "apk_group")
        assert len(split_entry["paths"]) == 2

        assert controller.installGamesByAmount("١٠٠") is True
        operation = controller._active_operations["SERIAL_A"]
        assert operation["install_amount"] == 100
        assert operation["game_count"] == 10
        assert len(operation["selected_games"]) == 10
        assert len(set(operation["selected_games"])) == 10
        assert len({entry["gameKey"] for entry in operation["batch_entries"]}) == 10
        assert FakeOperationEngine.instances[-1].calls[0][0] == "install_batch"

        # Invalid text must not queue a second operation.
        assert controller.installGamesByAmount("abc") is False
        assert len(FakeOperationEngine.instances) == 1




def test_amount_200_uses_tariff_count_not_currency_as_game_count():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        for index in range(40):
            folder = source_root / f"Tariff Game {index:02d}"
            folder.mkdir()
            (folder / "game.apk").write_bytes(b"apk")

        controller.install_source_path = str(source_root)
        controller._device_info = {
            "SERIAL_A": {"name": "Test Android", "model": "Test Android"}
        }
        controller.selected_devices = ["SERIAL_A"]
        messages = []
        controller.messageChanged.connect(messages.append)

        assert controller.installGamesByAmount("200") is True
        operation = controller._active_operations["SERIAL_A"]
        assert operation["install_amount"] == 200
        assert operation["requested_game_count"] == 25
        assert operation["available_game_count"] == 40
        assert operation["game_count"] == 25
        assert len(operation["selected_games"]) == 25
        assert len({entry["gameKey"] for entry in operation["batch_entries"]}) == 25
        assert len(operation["batch_entries"]) == 25
        assert any("السعر 200 يقابل 25 لعبة" in message for message in messages)


def test_amount_install_supports_intermediate_amounts():
    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        for index in range(25):
            game_folder = source_root / f"Intermediate Game {index:02d}"
            game_folder.mkdir()
            (game_folder / "game.apk").write_bytes(b"apk")

        for amount, expected_count in (("150", 18), ("170", 21)):
            controller = make_controller()
            controller.install_source_path = str(source_root)
            controller._device_info = {
                "SERIAL_A": {"name": "Test Android", "model": "Test Android"}
            }
            controller.selected_devices = ["SERIAL_A"]

            assert controller.getGameCountForAmount(amount) == expected_count
            assert controller.installGamesByAmount(amount) is True
            operation = controller._active_operations["SERIAL_A"]
            assert operation["install_amount"] == int(amount)
            assert operation["game_count"] == expected_count
            assert len(operation["selected_games"]) == expected_count
            assert len(set(operation["selected_games"])) == expected_count


def test_amount_install_scans_nested_game_subfolders():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        nested_apk_folder = source_root / "Category" / "Deep Game" / "APK"
        nested_apk_folder.mkdir(parents=True)
        (nested_apk_folder / "base.apk").write_bytes(b"base")
        (nested_apk_folder / "split_config.apk").write_bytes(b"split")

        simple_game = source_root / "Another Game"
        simple_game.mkdir()
        (simple_game / "game.apk").write_bytes(b"apk")

        controller.install_source_path = str(source_root)
        entries = controller._collect_install_batch()
        assert len({entry["gameKey"] for entry in entries}) == 2
        assert any(entry["gameName"] == "Category / Deep Game" for entry in entries)
        assert any(entry["type"] == "apk_group" for entry in entries)


def test_amount_install_discovers_xapk_as_one_package():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        game_folder = source_root / "XAPK Game"
        game_folder.mkdir()
        (game_folder / "game.xapk").write_bytes(b"synthetic-xapk")

        controller.install_source_path = str(source_root)
        entries = controller._collect_install_batch()
        xapk_entries = [entry for entry in entries if entry["type"] == "xapk"]

        assert len(xapk_entries) == 1
        assert xapk_entries[0]["name"] == "XAPK Game"
        assert Path(xapk_entries[0]["path"]).name == "game.xapk"
        assert len({entry["gameKey"] for entry in entries}) == 1


def test_adb_batch_installs_xapk_splits_and_copies_obb():
    import subprocess
    import zipfile
    import engine.adb_engine as adb_module

    with TemporaryDirectory() as directory:
        root = Path(directory)
        xapk_path = root / "sample.xapk"
        with zipfile.ZipFile(xapk_path, "w") as archive:
            archive.writestr(
                "manifest.json",
                json.dumps({"package_name": "com.example.kafiatest"}),
            )
            archive.writestr("base.apk", b"base-apk")
            archive.writestr("config.arm64_v8a.apk", b"split-apk")
            archive.writestr(
                "Android/obb/com.example.kafiatest/main.7.com.example.kafiatest.obb",
                b"obb-data",
            )
            # Unsafe archive paths must never be extracted or passed to ADB.
            archive.writestr("../outside.apk", b"outside")

        engine = ADBEngine()
        commands = []
        finished = []

        def fake_run_install_command(args, timeout):
            commands.append(list(args))
            return subprocess.CompletedProcess(
                ["adb", *args], 0, "Success", ""
            )

        class ImmediateThread:
            """Run the batch worker inline so Qt signal delivery needs no event loop."""
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        engine._run_install_command = fake_run_install_command
        engine.finished.connect(
            lambda success, message: finished.append((success, message))
        )

        original_thread = adb_module.threading.Thread
        adb_module.threading.Thread = ImmediateThread
        try:
            engine.install_batch(
                "SERIAL_XAPK",
                [{
                    "name": "XAPK sample",
                    "type": "xapk",
                    "path": str(xapk_path),
                }],
            )
        finally:
            adb_module.threading.Thread = original_thread

        assert finished and finished[-1][0] is True, finished
        install_command = next(
            command for command in commands if "install-multiple" in command
        )
        assert len([item for item in install_command if item.endswith(".apk")]) == 2
        assert all("outside.apk" not in item for item in install_command)
        assert any(
            command[:4] == ["-s", "SERIAL_XAPK", "shell", "mkdir"]
            and command[-1] == "/sdcard/Android/obb/com.example.kafiatest"
            for command in commands
        )
        push_command = next(command for command in commands if "push" in command)
        assert push_command[-1] == (
            "/sdcard/Android/obb/com.example.kafiatest/"
            "main.7.com.example.kafiatest.obb"
        )


def test_amount_install_targets_only_primary_selected_phone():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        for index in range(12):
            game_folder = source_root / f"Phone Game {index:02d}"
            game_folder.mkdir()
            (game_folder / "game.apk").write_bytes(b"apk")

        controller.install_source_path = str(source_root)
        controller._device_info = {
            "SERIAL_A": {"name": "Android A", "model": "Android A"},
            "SERIAL_B": {"name": "Android B", "model": "Android B"},
        }
        controller.selected_devices = ["SERIAL_A", "SERIAL_B"]
        controller.selected_device = "SERIAL_B"

        assert controller.installGamesByAmountOnSelectedPhone("100") is True
        assert set(controller._active_operations) == {"SERIAL_B"}
        operation = controller._active_operations["SERIAL_B"]
        assert operation["install_target_scope"] == "selected_device"
        assert operation["game_count"] == 10
        assert FakeOperationEngine.instances[-1].serial == "SERIAL_B"
        assert FakeOperationEngine.instances[-1].calls[0][0] == "install_batch"


def test_amount_install_uses_all_games_when_source_is_smaller_than_tier():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        for index in range(4):
            game_folder = source_root / f"Small Game {index}"
            game_folder.mkdir()
            (game_folder / "game.apk").write_bytes(b"apk")

        controller.install_source_path = str(source_root)
        controller._device_info = {
            "SERIAL_A": {"name": "Test Android", "model": "Test Android"}
        }
        controller.selected_devices = ["SERIAL_A"]
        assert controller.installGamesByAmount("100") is True
        operation = controller._active_operations["SERIAL_A"]
        assert operation["game_count"] == 4
        assert len(operation["selected_games"]) == 4



def test_android_copy_starts_without_preinstalling_companion_apps():
    controller = make_controller()
    operation = dict(
        rows(["SERIAL_A"])[0],
        kind="copy",
        game="PUBG Mobile",
        source="C:/games/PUBG/Saved",
        destination="/sdcard/Android/data/com.tencent.ig/files/Saved",
        target_package="com.tencent.ig",
    )
    controller._start_operation(operation)
    fake_engine = operation["engine"]
    assert fake_engine.calls == [(
        "push",
        "SERIAL_A",
        "C:/games/PUBG/Saved",
        "/sdcard/Android/data/com.tencent.ig/files/Saved",
    )]
    assert operation["phase"] == "adb_direct"


def test_shizuku_startup_supports_running_and_both_versions():
    command = ADBEngine._build_shizuku_start_command()
    assert "KAFIA_SHIZUKU_ALREADY_RUNNING" in command
    assert "pidof shizuku_server" in command
    assert "libshizuku.so" in command
    assert '"$apk_dir"/lib/*/libshizuku.so' in command
    assert "start.sh" in command
    assert "KAFIA_SHIZUKU_STARTED" in command
    assert "find -type f -name libshizuku.so" not in command


def test_installation_does_not_increment_copy_counters():
    controller = make_controller()
    operation = dict(
        rows(["SERIAL_C"])[0],
        kind="install",
        game="PUBG Mobile",
        apk="game.apk",
        install_type="apk",
        percent=-1,
    )
    controller._start_operation(operation)
    FakeOperationEngine.instances[-1].finish(True, "install complete")

    history = controller.getTransferHistory()
    assert history[0]["kind"] == "تثبيت"
    assert history[0]["status"] == "تم التثبيت"
    assert controller.getTransferSummary()["completed"] == 0
    assert controller.getTransferSummary()["failed"] == 0


def test_pubg_source_size_selects_sibling_folder():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        game = root / "ببجي (PUBG)"
        for folder_name in ("Saved", "Medium", "Low"):
            folder = game / folder_name
            folder.mkdir(parents=True)
            payload = folder
            if folder_name in ("Medium", "Low"):
                payload = folder / "Saved"
                payload.mkdir()
            (payload / "asset.pak").write_bytes(folder_name.encode("ascii"))

        controller.current_source = "CUSTOM"
        controller.custom_source_path = str(root)
        controller.source_root = root
        controller.selected_devices = ["SERIAL_A"]
        controller._device_info["SERIAL_A"] = {
            "serial": "SERIAL_A",
            "model": "Galaxy Test",
            "name": "Galaxy Test",
            "display_name": "Galaxy Test",
        }
        original_find_game = app_qml.find_game_folder
        original_get_game = app_qml.get_game
        app_qml.find_game_folder = lambda selected_root, _name: game
        app_qml.get_game = lambda _name: {"name": "PUBG Mobile"}

        expected_destination = (
            "/sdcard/Android/data/com.tencent.ig/files/UE4Game/"
            "ShadowTrackerExtra/ShadowTrackerExtra/Saved/"
        )
        for size_name in ("Saved", "Medium", "Low"):
            assert controller.setPubgSourceSize(size_name) is True
            assert controller.getPubgSourceSize() == size_name
            controller.copyGame("PUBG Mobile")
            engine = FakeOperationEngine.instances[-1]
            expected_source = (
                game / size_name / "Saved"
                if size_name in ("Medium", "Low")
                else game / size_name
            )
            assert engine.calls == [(
                "push",
                "SERIAL_A",
                str(expected_source),
                expected_destination,
            )]
            operation = controller._active_operations["SERIAL_A"]
            assert operation["phase"] == "adb_direct"
            assert operation["transfer_backend"] == "adb"
            assert operation["target_package"] == "com.tencent.ig"
            engine.finish(True, "copy complete")

        summary = controller.getTransferSummary()
        assert summary["completed"] == 1
        assert summary["review"] == 2

        # A missing selected variant must be rejected before creating an ADB job.
        (game / "Low" / "Saved" / "asset.pak").unlink()
        (game / "Low" / "Saved").rmdir()
        (game / "Low").rmdir()
        before_count = len(FakeOperationEngine.instances)
        assert controller.setPubgSourceSize("Low") is True
        controller.copyGame("PUBG Mobile")
        assert len(FakeOperationEngine.instances) == before_count

        # Restore the real game resolver after this fixture.
        app_qml.find_game_folder = original_find_game
        app_qml.get_game = original_get_game


def test_legacy_history_migration_separates_installs_retries_and_duplicates():
    controller = make_controller()
    controller._transfer_history = [
        {
            "id": "1", "kind": "تثبيت", "status": "اكتمل",
            "serial": "SERIAL_A", "game": "PUBG Mobile",
        },
        {
            "id": "2", "kind": "نسخ", "status": "غير مكتمل",
            "serial": "SERIAL_A", "game": "PUBG Mobile",
            "message": "device disconnected",
        },
        {
            "id": "3", "kind": "نسخ", "status": "اكتمل",
            "serial": "SERIAL_A", "game": "PUBG Mobile",
        },
        {
            "id": "4", "kind": "نسخ", "status": "اكتمل",
            "serial": "SERIAL_A", "game": "PUBG Mobile",
        },
    ]
    assert controller._migrate_transfer_history() is True
    entries = controller._transfer_history
    assert entries[0]["status"] == "تم التثبيت"
    assert entries[1]["status"] == "تمت المعالجة"
    assert entries[2]["status"] == "اكتمل"
    assert entries[3]["status"] == "مراجعة"
    summary = controller.getTransferSummary()
    assert summary["completed"] == 1
    assert summary["failed"] == 0
    assert summary["review"] == 1


def test_pubg_flash_uses_fixed_source_and_exact_package_destination():
    controller = make_controller()
    original_usb_root = app_qml.build_usb_source_root
    original_find_game = app_qml.find_game_folder
    original_get_game = app_qml.get_game
    with TemporaryDirectory() as directory:
        root = Path(directory) / "games"
        flash_folder = root / "ببجي (PUBG FLASH)"
        expected_sources = {}
        for size_name in ("Saved", "Medium", "Low"):
            size_root = flash_folder / size_name
            payload = size_root if size_name == "Saved" else size_root / "Saved"
            payload.mkdir(parents=True)
            (payload / "flash_data.pak").write_bytes(size_name.encode("ascii"))
            expected_sources[size_name] = payload

        app_qml.build_usb_source_root = lambda: root
        app_qml.find_game_folder = lambda selected_root, _name: (
            Path(selected_root) / "ببجي (PUBG FLASH)"
        )
        app_qml.get_game = lambda _name: {"name": "PUBG Mobile Flash"}
        controller.source_root = Path(directory) / "network-source"
        controller.selected_devices = ["SERIAL_A"]
        controller._device_info["SERIAL_A"] = {
            "serial": "SERIAL_A",
            "model": "Galaxy Flash",
            "name": "Galaxy Flash",
            "display_name": "Galaxy Flash",
        }

        expected_destination = (
            "/storage/emulated/0/Android/data/com.tencent.igfit/"
            "files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved"
        )
        for size_name in ("Saved", "Medium", "Low"):
            assert controller.setPubgSourceSize(size_name) is True
            controller.copyGame("PUBG Mobile Flash")
            # Each copy starts a fresh engine; inspect the engine created for this size.
            engine = FakeOperationEngine.instances[-1]
            assert engine.serial == "SERIAL_A"
            operation = controller._active_operations["SERIAL_A"]
            assert engine.calls[-1] == (
                "push",
                "SERIAL_A",
                str(expected_sources[size_name]),
                expected_destination,
            )
            assert operation["phase"] == "adb_direct"
            assert operation["transfer_backend"] == "adb"
            assert operation["target_package"] == "com.tencent.igfit"
            assert operation["source_root"] == str(root)
            assert operation["destination"] == expected_destination
            engine.finish(True, "copy complete")

    app_qml.build_usb_source_root = original_usb_root
    app_qml.find_game_folder = original_find_game
    app_qml.get_game = original_get_game

def test_pubg_flash_requires_saved_source_directory():
    controller = make_controller()
    original_usb_root = app_qml.build_usb_source_root
    original_find_game = app_qml.find_game_folder
    original_get_game = app_qml.get_game
    with TemporaryDirectory() as directory:
        root = Path(directory) / "games"
        (root / "ببجي (PUBG FLASH)").mkdir(parents=True)
        app_qml.build_usb_source_root = lambda: root
        app_qml.find_game_folder = lambda selected_root, _name: (
            Path(selected_root) / "ببجي (PUBG FLASH)"
        )
        app_qml.get_game = lambda _name: {"name": "PUBG Mobile Flash"}
        controller.selected_devices = ["SERIAL_A"]
        controller._device_info["SERIAL_A"] = {
            "serial": "SERIAL_A", "model": "Galaxy Flash", "name": "Galaxy Flash",
        }
        messages = []
        controller.messageChanged.connect(messages.append)
        controller.copyGame("PUBG Mobile Flash")
        assert not FakeOperationEngine.instances
        assert any("مجلد Saved داخل مصدر PUBG FLASH غير موجود" in item for item in messages)
    app_qml.build_usb_source_root = original_usb_root
    app_qml.find_game_folder = original_find_game
    app_qml.get_game = original_get_game


def test_uninstall_operation_is_logged_separately_from_copy():
    controller = make_controller()
    operation = dict(
        rows(["SERIAL_C"])[0],
        kind="uninstall",
        game="PUBG Mobile Flash",
        package_id="com.tencent.igfit",
        percent=-1,
    )
    controller._start_operation(operation)
    engine = engine_for("SERIAL_C")
    assert engine.calls == [
        ("uninstall", "SERIAL_C", "com.tencent.igfit")
    ]
    engine.finish(True, "uninstalled")
    history = controller.getTransferHistory()
    assert history[0]["kind"] == "إزالة تثبيت"
    assert history[0]["status"] == "تمت إزالة التثبيت"
    assert controller.getTransferSummary()["completed"] == 0


def test_transfer_history_is_not_truncated_after_500_entries():
    controller = make_controller()
    controller._transfer_history = []

    for index in range(520):
        operation = {
            "serial": f"SERIAL_{index:04d}",
            "device_name": f"Phone {index:04d}",
            "game": "PUBG Mobile",
            "kind": "copy",
            "target_type": "android",
            "history_recorded": False,
        }
        assert controller._record_transfer_history(
            operation, True, "copy complete"
        ) == "اكتمل"

    entries = controller.getTransferHistory()
    assert len(entries) == 520
    assert entries[0]["serial"] == "SERIAL_0519"
    assert entries[-1]["serial"] == "SERIAL_0000"
    assert all(len(str(item.get("date") or "")) >= 10 for item in entries)


def test_direct_stream_rejects_source_size_changes():
    from io import BytesIO

    exact = b"0123456789"
    chunks = list(ADBEngine._iter_file_chunks_exact(
        BytesIO(exact), len(exact), chunk_size=3
    ))
    assert b"".join(chunks) == exact

    truncated_error = None
    try:
        list(ADBEngine._iter_file_chunks_exact(
            BytesIO(b"short"), 10, chunk_size=3
        ))
    except EOFError as exc:
        truncated_error = str(exc)
    assert truncated_error and "truncated" in truncated_error.lower()

    grown_error = None
    try:
        list(ADBEngine._iter_file_chunks_exact(
            BytesIO(b"longer"), 3, chunk_size=2
        ))
    except RuntimeError as exc:
        grown_error = str(exc)
    assert grown_error and "grew" in grown_error.lower()


def test_direct_stream_manifest_preserves_game_relative_paths():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / "PUBG Mobile"
        (source / "Saved" / "Config").mkdir(parents=True)
        (source / "Saved" / "Paks").mkdir(parents=True)
        (source / "Saved" / "Config" / "Custom.ini").write_bytes(b"[Game]\\n")
        (source / "Saved" / "Paks" / "pakchunk0.pak").write_bytes(b"pak-data")
        (source / "manifest.dat").write_bytes(b"manifest")

        files, total = ADBEngine._build_direct_stream_manifest(source)
        by_relative = {
            relative: file_path
            for file_path, relative, _size, _mtime in files
        }
        sizes = {
            relative: size
            for _file_path, relative, size, _mtime in files
        }

        assert set(by_relative) == {
            "Saved/Config/Custom.ini",
            "Saved/Paks/pakchunk0.pak",
            "manifest.dat",
        }
        assert sizes == {
            "Saved/Config/Custom.ini": len(b"[Game]\\n"),
            "Saved/Paks/pakchunk0.pak": len(b"pak-data"),
            "manifest.dat": len(b"manifest"),
        }
        assert total == sum(sizes.values())
        assert all(file_path.is_file() for file_path in by_relative.values())
        assert all(len(item) == 4 for item in files)

        single_file = source / "manifest.dat"
        single_manifest, single_total = ADBEngine._build_direct_stream_manifest(single_file)
        assert len(single_manifest) == 1
        assert single_manifest[0][1] == "manifest.dat"
        assert single_total == len(b"manifest")
        assert len(single_manifest[0]) == 4
        assert single_manifest[0][2] == len(b"manifest")



def test_saved_copy_never_changes_permissions_or_writes_probe_files():
    adb_source = Path(ROOT / "engine/adb_engine.py").read_text(encoding="utf-8")
    app_source = Path(ROOT / "app_qml.py").read_text(encoding="utf-8")
    push_start = adb_source.index("    def _push_thread(")
    push_end = adb_source.index("    @Slot(str, str, str)\n    def push(", push_start)
    push_source = adb_source[push_start:push_end]

    assert "chmod -R a+rwX" not in adb_source
    assert "_set_remote_game_permissions" not in adb_source
    assert "KAFIANET_POST_COPY_PERMISSION_ERROR" not in app_source
    assert "write_test_" not in push_source
    assert '"touch"' not in push_source
    assert '"rm"' not in push_source
    assert "if is_saved_destination and not self._remote_directory_exists(device, check_path):" not in push_source
    assert "KAFIANET_ANDROID_DATA_ACCESS_UNVERIFIED" not in push_source
    assert "Never create Saved or its parents." in push_source
    for engine_path in (
        "engine/efootball_saf_engine.py",
        "engine/transfer_engine.py",
    ):
        engine_source = Path(ROOT / engine_path).read_text(encoding="utf-8")
        assert "chmod" not in engine_source.lower()
        assert "setreadable" not in engine_source.lower()
        assert "setwritable" not in engine_source.lower()

    helper_source = Path(
        ROOT / "android-helper/app/src/main/java/com/kafianet/shizukubridge/TransferUserService.java"
    ).read_text(encoding="utf-8")
    assert "Os.chmod(" not in helper_source
    assert "setReadable(" not in helper_source
    assert "setWritable(" not in helper_source
    copy_start = app_source.index("    def copyGame(self, game_name):")
    copy_end = app_source.index("    def _pull_destination_for_game(", copy_start)
    copy_source = app_source[copy_start:copy_end]
    assert "source.rglob(" not in copy_source
    assert '"SOURCE FILE COUNT:"' not in copy_source




def test_fullscreen_button_requires_native_guard_and_restores_it():
    qml = Path("qml/main.qml").read_text(encoding="utf-8")

    # The in-app ⛶ control is the entry point under repair, not the title-bar
    # maximize button. Keep its click path tied to the password-protected lock.
    button_start = qml.index("id: fullscreenLockButton")
    button_end = qml.index("// REFRESH", button_start)
    button = qml[button_start:button_end]
    assert 'text: window.fullscreenLocked ? "↙" : "⛶"' in button
    assert "window.enterFullscreenLock()" in button
    assert "window.askWindowUnlock()" in button

    start = qml.index("    function enterFullscreenLock()")
    end = qml.index("    function openFullscreenPasswordDialog", start)
    enter = qml[start:end]
    guard_ready = enter.index("var guardReady = Boolean(")
    guard_failure = enter.index("if (!guardReady)", guard_ready)
    failure_return = enter.index("return", guard_failure)
    lock_transition = enter.index("fullscreenLocked = true")
    fullscreen_transition = enter.index("window.showFullScreen()")
    assert guard_ready < guard_failure < failure_return < lock_transition
    assert lock_transition < fullscreen_transition
    assert "fullscreenGuardEnabled = false" in enter
    assert "fullscreenGuardEnabled = true" in enter

    leave_start = qml.index("    function leaveFullscreenLock(")
    leave_end = qml.index("    onClosing:", leave_start)
    leave = qml[leave_start:leave_end]
    assert "if (appController && fullscreenGuardEnabled)" in leave
    assert "appController.setFullscreenProtection(false)" in leave
    assert "fullscreenGuardEnabled = false" in leave
    assert "window.showNormal()" in leave

    # Losing visibility/focus must not leave the in-app lock unprotected.
    handlers_start = qml.index("    onVisibilityChanged:")
    handlers_end = qml.index("    // Stable model:", handlers_start)
    handlers = qml[handlers_start:handlers_end]
    assert "window.showFullScreen()" in handlers
    assert "appController.setFullscreenProtection(true)" in handlers

def test_legacy_successful_phone_register_is_no_longer_used():
    controller_source = Path("app_qml.py").read_text(encoding="utf-8")
    history_qml = Path("qml/pages/TransferHistory.qml").read_text(encoding="utf-8")
    # Existing Documents JSON is intentionally left on disk, but the app no
    # longer reads or writes a lifetime phone register. Date-stamped transfer
    # history is the sole source for the period-specific successful-device list.
    assert "_successful_phones" not in controller_source
    assert "KafiaNetControlPro_SuccessfulPhones.json" not in controller_source
    assert "successfulPhonesChanged" not in controller_source
    assert "function buildSuccessfulDevices(history)" in history_qml
    assert "root.visibleEntries" in history_qml
    assert "لا توجد هواتف ناجحة في هذه الفترة" in history_qml


def test_launch_game_resolves_and_starts_installed_package():
    controller = make_controller()
    controller.selected_devices = ["SERIAL_A"]
    controller.selected_device = "SERIAL_A"
    controller._selected_game_package_id = lambda _name: "com.tencent.igfit"
    messages = []
    controller.messageChanged.connect(messages.append)
    calls = []

    def fake_run(args, timeout=None):
        calls.append((list(args), timeout))
        if args[2:5] == ["shell", "pm", "path"]:
            return SimpleNamespace(
                returncode=0,
                stdout="package:/data/app/com.tencent.igfit/base.apk\n",
                stderr="",
            )
        if args[2:5] == ["shell", "cmd", "package"]:
            return SimpleNamespace(
                returncode=0,
                stdout="com.tencent.igfit/com.tencent.igfit.SplashActivity\n",
                stderr="",
            )
        if args[2:5] == ["shell", "am", "start"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "Starting: Intent { act=android.intent.action.MAIN }\n"
                    "Status: ok\n"
                    "Activity: com.tencent.igfit/com.tencent.igfit.SplashActivity\n"
                ),
                stderr="",
            )
        raise AssertionError("Unexpected launch command: " + repr(args))

    controller.adb_engine._run = fake_run
    controller.launchGame("PUBG Mobile Flash")

    assert [call[0][2:5] for call in calls] == [
        ["shell", "pm", "path"],
        ["shell", "cmd", "package"],
        ["shell", "am", "start"],
    ]
    assert calls[-1][0][-1] == (
        "com.tencent.igfit/com.tencent.igfit.SplashActivity"
    )
    assert "تم فتح PUBG Mobile Flash" in messages

    calls.clear()
    messages.clear()

    def missing_package(args, timeout=None):
        calls.append(list(args))
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Unknown package: com.tencent.igfit",
        )

    controller.adb_engine._run = missing_package
    controller.launchGame("PUBG Mobile Flash")
    assert len(calls) == 1
    assert any(
        "غير مثبتة" in message and "com.tencent.igfit" in message
        for message in messages
    )


def test_adb_game_close_is_silent_and_package_scoped():
    engine = ADBEngine()
    calls = []

    def fake_run(args, timeout=None):
        calls.append((list(args), timeout))
        if args[2:5] == ["shell", "pm", "path"]:
            return SimpleNamespace(
                returncode=0,
                stdout="package:/data/app/com.tencent.ig/base.apk",
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    engine._run = fake_run
    success, message = engine.force_stop_app(
        "SERIAL_A", "com.tencent.ig"
    )
    assert success is True
    assert "بصمت" in message
    assert calls == [
        (
            ["-s", "SERIAL_A", "shell", "pm", "path", "com.tencent.ig"],
            20,
        ),
        (
            ["-s", "SERIAL_A", "shell", "am", "force-stop", "com.tencent.ig"],
            20,
        ),
    ]
    assert not any(
        "uiautomator" in str(args) or "swipe" in str(args)
        or "keyevent" in str(args)
        for args, _timeout in calls
    )

    calls.clear()
    success, message = engine.force_stop_app("SERIAL_A", "invalid package")
    assert success is False
    assert calls == []

    engine._run = lambda args, timeout=None: SimpleNamespace(
        returncode=1, stdout="", stderr="Unknown package"
    )
    success, message = engine.force_stop_app("SERIAL_A", "com.tencent.ig")
    assert success is False
    assert "غير مثبتة" in message


def test_custom_source_keeps_game_specific_phone_destination(controller):
    original_find_game = app_qml.find_game_folder
    original_get_game = app_qml.get_game
    with TemporaryDirectory() as directory:
        root = Path(directory) / "4" / "games"
        game = root / "PUBG Mobile"
        (game / "saved").mkdir(parents=True)
        (game / "saved" / "asset.pak").write_bytes(b"test")

        controller.current_source = "CUSTOM"
        controller.custom_source_path = str(root)
        controller.source_root = root
        controller.selected_devices = ["SERIAL_A"]
        controller._device_info["SERIAL_A"] = {
            "serial": "SERIAL_A",
            "model": "Galaxy Test",
            "name": "Galaxy Test",
            "display_name": "Galaxy Test",
        }
        app_qml.find_game_folder = lambda selected_root, _name: (
            Path(selected_root) / "PUBG Mobile"
        )
        app_qml.get_game = lambda _name: {"name": "PUBG Mobile"}

        controller.copyGame("PUBG Mobile")
        operation_engine = engine_for("SERIAL_A")
        expected_destination = (
            "/sdcard/Android/data/com.tencent.ig/files/UE4Game/"
            "ShadowTrackerExtra/ShadowTrackerExtra/Saved/"
        )
        assert operation_engine.calls[-1] == (
            "push",
            "SERIAL_A",
            str(game / "saved"),
            expected_destination,
        )

    app_qml.find_game_folder = original_find_game
    app_qml.get_game = original_get_game


def test_zero_selection(controller):
    rejected = []
    controller.operationRejected.connect(rejected.append)
    controller.copyGame("Free Fire")
    controller.installGame("Free Fire")
    assert "تحديد هاتف" in rejected[0]
    assert "تحديد هاتف" in rejected[1]


def test_independent_operations(controller):
    controller._start_operation(
        dict(rows(["SERIAL_A"])[0], source="src", destination="dst")
    )
    controller._start_operation(
        dict(rows(["SERIAL_B"])[0], source="src", destination="dst")
    )
    assert set(controller._active_operations) == {"SERIAL_A", "SERIAL_B"}
    assert engine_for("SERIAL_A").calls[-1][1] == "SERIAL_A"
    assert engine_for("SERIAL_B").calls[-1][1] == "SERIAL_B"
    engine_for("SERIAL_A").progress(35, "10 MB/s", "00:10")
    engine_for("SERIAL_B").progress(78, "20 MB/s", "00:05")
    state = {row["serial"]: row for row in controller._operation_rows()}
    assert state["SERIAL_A"]["percent"] == 35
    assert state["SERIAL_B"]["percent"] == 78
    assert state["SERIAL_A"]["speed"] == "10 MB/s"
    assert state["SERIAL_B"]["speed"] == "20 MB/s"


def test_busy_serial_and_idle_serial(controller):
    original_find_game = app_qml.find_game_folder
    original_get_game = app_qml.get_game
    controller._start_operation(
        dict(rows(["SERIAL_A"])[0], source="src", destination="dst")
    )
    rejected = []
    controller.messageChanged.connect(rejected.append)
    controller.selected_devices = ["SERIAL_A", "SERIAL_B"]
    with TemporaryDirectory() as directory:
        root = Path(directory)
        game = root / "game"
        (game / "saved").mkdir(parents=True)
        controller.source_root = root
        app_qml.find_game_folder = lambda _root, _name: game
        app_qml.get_game = lambda _name: {"name": "test"}
        controller.copyGame("PUBG Mobile")
    assert "SERIAL_A" not in [
        call[1] for call in FakeOperationEngine.instances[-1].calls
    ]
    assert "SERIAL_B" in controller._active_operations
    assert any("عملية نقل قيد التنفيذ" in message for message in rejected)
    # The phone that accepts ADB starts with the direct push only.
    b_engine = engine_for("SERIAL_B")
    assert b_engine.calls[0][0] == "push"
    assert b_engine.calls[0][1] == "SERIAL_B"
    # The controller must pass the source root, not append '/.' and accidentally
    # let adb create a nested `saved` folder under an already-existing `Saved`.
    assert b_engine.calls[-1] == (
        "push",
        "SERIAL_B",
        str(game / "saved"),
        "/sdcard/Android/data/com.tencent.ig/files/UE4Game/ShadowTrackerExtra/ShadowTrackerExtra/Saved/"
    )

    app_qml.find_game_folder = original_find_game
    app_qml.get_game = original_get_game


def test_adb_source_arguments_and_progress_offset():
    with TemporaryDirectory() as directory:
        source = Path(directory) / "saved"
        source.mkdir()
        (source / "asset.pak").write_bytes(b"data")
        nested = source / "Paks"
        nested.mkdir()
        args, target = ADBEngine._adb_source_plan(
            source, "/sdcard/ShadowTrackerExtra/Saved"
        )
        # Case-insensitive Windows path semantics allow the local "saved"
        # directory to be passed as "Saved", pushing it to the remote parent.
        if (source.parent / "Saved").exists():
            assert args == [str(source.parent / "Saved")]
            assert target == "/sdcard/ShadowTrackerExtra"
        else:
            assert args == [str(source / "asset.pak"), str(nested)]
            assert target == "/sdcard/ShadowTrackerExtra/Saved"

        asset = source / "asset.pak"
        file_args, file_target = ADBEngine._adb_source_plan(
            asset, "/sdcard/ShadowTrackerExtra/Saved"
        )
        assert file_args == [str(asset)]
        assert file_target == "/sdcard/ShadowTrackerExtra/Saved"

        synced_command = ADBEngine._synced_push_command(
            "adb", "SERIAL_A", [str(asset)], "/sdcard/ShadowTrackerExtra/Saved"
        )
        assert synced_command == [
            "adb", "-s", "SERIAL_A", "push", "--sync",
            str(asset), "/sdcard/ShadowTrackerExtra/Saved",
        ]
        assert ADBEngine._relative_source_file(source, asset) == "asset.pak"
        assert ADBEngine._remote_file_target_for_source_file(
            source, asset, "/sdcard/ShadowTrackerExtra/Saved"
        ) == "/sdcard/ShadowTrackerExtra/Saved/asset.pak"
        assert ADBEngine._interrupted_source_file(source, str(asset), 48) == asset
        assert ADBEngine._interrupted_source_file(source, str(asset), 100) is None
        assert ADBEngine._interrupted_source_file(source, str(asset), None) is None

        # A directory with a different name is pushed as its direct children.
        different = source.parent / "different"
        different.mkdir()
        (different / "one.bin").write_bytes(b"1")
        child_args, child_target = ADBEngine._adb_source_plan(
            different, "/sdcard/ShadowTrackerExtra/Saved"
        )
        assert child_args == [str(different / "one.bin")]
        assert child_target == "/sdcard/ShadowTrackerExtra/Saved"

    assert ADBEngine._combined_progress_percent(1000, 600, 0) == 60
    assert ADBEngine._combined_progress_percent(1000, 600, 150) == 75
    assert ADBEngine._combined_progress_percent(1000, 1000, 0) == 99
    assert ADBEngine._combined_progress_percent(0, 0, 0) == 0

    assert ADBEngine._combined_native_progress_percent(0, 0) == 0
    assert ADBEngine._combined_native_progress_percent(0, 42) == 42
    assert ADBEngine._combined_native_progress_percent(42, 0) == 42
    assert ADBEngine._combined_native_progress_percent(42, 50) == 71
    assert ADBEngine._combined_native_progress_percent(90, 100) == 99

    # Current ADB writes native progress rows to stdout for non-console runs.
    parsed = ADBEngine._parse_adb_progress_details(
        "[ 42%] C:\\games\\asset.pak: 65%"
    )
    assert parsed == (42, r"C:\games\asset.pak", 65)
    single = ADBEngine._parse_adb_progress_details(
        "[ 7%] C:\\games\\single.pak"
    )
    assert single == (7, r"C:\games\single.pak", 7)
    assert ADBEngine._parse_adb_progress_details("Transfer started") == (
        None, None, None
    )
    measured_speed = ADBEngine._parse_adb_transfer_speed(
        "1 file pushed, 0 skipped. 10.0 MB/s (1048576 bytes in 0.100s)"
    )
    assert measured_speed is not None
    assert abs(measured_speed - 10485760) < 1





def test_failure_cancel_and_completion_isolation(controller):
    for serial in ("SERIAL_A", "SERIAL_B", "SERIAL_C"):
        controller._start_operation(
            dict(rows([serial])[0], source="src", destination="dst")
        )
    a = engine_for("SERIAL_A")
    b = engine_for("SERIAL_B")
    c = engine_for("SERIAL_C")
    a.finish(False, "A original error")
    assert controller._active_operations["SERIAL_B"]["status"] != "فشل"
    controller.cancelDevice("SERIAL_A")
    assert not b.cancelled and not c.cancelled
    b.finish(True, "B complete")
    c.finish(True, "C complete")
    assert controller._active_operations["SERIAL_A"]["error"] == "A original error"
    assert controller._active_operations["SERIAL_B"]["status"] == "اكتمل"
    assert controller._active_operations["SERIAL_C"]["status"] == "اكتمل"
    assert not controller.operationBusy
    assert controller._operation_state == "IDLE"


def test_install_operations(controller):
    for serial in ("SERIAL_A", "SERIAL_B", "SERIAL_C"):
        operation = dict(rows([serial])[0])
        operation.update(
            kind="install",
            game="Free Fire",
            percent=-1,
            apk="game.apk",
            install_type="apk",
        )
        controller._start_operation(operation)
    assert [
        engine.calls[-1][1] for engine in FakeOperationEngine.instances
    ] == ["SERIAL_A", "SERIAL_B", "SERIAL_C"]
    assert all(
        controller._active_operations[serial]["percent"] == -1
        for serial in ("SERIAL_A", "SERIAL_B", "SERIAL_C")
    )
    controller.cancelDevice("SERIAL_A")
    assert not engine_for("SERIAL_B").cancelled
    engine_for("SERIAL_A").finish(False, "cancelled")
    engine_for("SERIAL_B").finish(False, "B install error")
    engine_for("SERIAL_C").finish(True, "C installed")
    assert controller._active_operations["SERIAL_B"]["error"] == "B install error"
    assert controller._active_operations["SERIAL_C"]["status"] == "اكتمل"


def test_pause_resume_operations():
    FakeOperationEngine.instances = []
    controller = make_controller()
    for serial in ("SERIAL_A", "SERIAL_B"):
        controller._start_operation(
            dict(rows([serial])[0], source="src", destination="dst")
        )

    pause_events = []
    controller.transferPausedChanged.connect(pause_events.append)
    controller.pauseTransfer()
    assert controller._transfer_paused
    assert engine_for("SERIAL_A").paused
    assert engine_for("SERIAL_B").paused
    assert pause_events[-1] is True

    controller.resumeTransfer()
    assert not controller._transfer_paused
    assert not engine_for("SERIAL_A").paused
    assert not engine_for("SERIAL_B").paused
    assert pause_events[-1] is False

    controller.cancelDevice("SERIAL_A")
    controller.cancelDevice("SERIAL_B")
    engine_for("SERIAL_A").finish(False, "cancelled")
    engine_for("SERIAL_B").finish(False, "cancelled")




def test_adb_oem_compatibility_probe_reads_real_facts_without_writes():
    engine = ADBEngine()
    commands = []

    values = {
        "ro.product.manufacturer": "Xiaomi",
        "ro.product.model": "Redmi Note 13 Pro",
        "ro.build.version.release": "14",
        "ro.build.version.sdk": "34",
    }

    def fake_run(arguments, timeout=None):
        args = list(arguments)
        commands.append(args)
        if args == ["devices"]:
            return SimpleNamespace(
                returncode=0,
                stdout="List of devices attached\nSERIAL_A\tdevice\n",
                stderr=""
            )
        if args[:3] == ["-s", "SERIAL_A", "shell"] and args[3] == "getprop":
            return SimpleNamespace(returncode=0, stdout=values.get(args[4], "") + "\n", stderr="")
        if args[:4] == ["-s", "SERIAL_A", "shell", "id"]:
            return SimpleNamespace(returncode=0, stdout="uid=2000(shell) gid=2000(shell) groups=1004,1007\n", stderr="")
        if args[:5] == ["-s", "SERIAL_A", "shell", "pm", "path"]:
            package = args[5]
            installed = package in ("moe.shizuku.privileged.api", "com.kafianet.shizukubridge")
            return SimpleNamespace(
                returncode=0 if installed else 1,
                stdout=("package:/data/app/" + package + "/base.apk\n") if installed else "",
                stderr=""
            )
        if args[:5] == ["-s", "SERIAL_A", "shell", "pidof", "shizuku_server"]:
            return SimpleNamespace(returncode=0, stdout="23145\n", stderr="")
        raise AssertionError("Unexpected non-read-only ADB command: " + repr(args))

    engine._run = fake_run
    result = engine.inspect_device_compatibility("SERIAL_A")

    assert result["ok"] is True
    assert result["connected"] is True
    assert result["manufacturer"] == "Xiaomi"
    assert result["model"] == "Redmi Note 13 Pro"
    assert result["android_release"] == "14"
    assert result["android_sdk"] == "34"
    assert result["adb_shell_uid"] == 2000
    assert result["adb_identity_label"] == "ADB Shell (uid=2000)"
    assert result["shizuku_installed"] is True
    assert result["helper_installed"] is True
    assert result["shizuku_server_running"] is True
    assert result["shizuku_server_pid"] == "23145"
    assert result["oem_setting_name"] == "USB debugging (Security options)"
    assert result["oem_setting_value_readable"] is False

    # Only device enumeration and read-only shell inspection are permitted.
    assert commands[0] == ["devices"]
    assert all(
        command[:3] == ["-s", "SERIAL_A", "shell"]
        and (
            command[3] in ("getprop", "id", "pidof")
            or command[3:5] == ["pm", "path"]
        )
        for command in commands[1:]
    )
    forbidden = {"push", "install", "uninstall", "settings", "setprop", "chmod", "rm", "mkdir"}
    assert not any(token in forbidden for command in commands for token in command)

    unauthorized = ADBEngine()
    unauthorized_calls = []

    def unauthorized_run(arguments, timeout=None):
        unauthorized_calls.append(list(arguments))
        return SimpleNamespace(
            returncode=0,
            stdout="List of devices attached\nSERIAL_A\tunauthorized\n",
            stderr=""
        )

    unauthorized._run = unauthorized_run
    blocked = unauthorized.inspect_device_compatibility("SERIAL_A")
    assert blocked["ok"] is False
    assert blocked["device_state"] == "unauthorized"
    assert unauthorized_calls == [["devices"]]


def test_oem_compatibility_settings_toggle_and_disable_gate():
    controller = make_controller()
    calls = []

    class ReadOnlyProbe:
        def inspect_device_compatibility(self, serial):
            calls.append(serial)
            return {
                "ok": True,
                "serial": serial,
                "manufacturer": "Samsung",
                "model": "SM-A556E",
                "android_release": "15",
                "android_sdk": "35",
                "adb_shell_identity": "uid=2000(shell)",
                "adb_shell_uid": 2000,
                "adb_identity_label": "ADB Shell (uid=2000)",
                "shizuku_installed": False,
                "shizuku_server_running": None,
                "helper_installed": False,
                "oem_setting_name": "",
                "oem_setting_source": "",
                "oem_setting_value_readable": False,
                "oem_setting_note": "Manual verification required.",
            }

    controller.adb_engine = ReadOnlyProbe()
    controller._device_info["SERIAL_A"] = {
        "serial": "SERIAL_A",
        "name": "Initial Device Name",
    }

    assert controller.getOemCompatibilityChecksEnabled() is False
    blocked = controller.inspectDeviceCompatibility("SERIAL_A")
    assert blocked["ok"] is False
    assert "معطل" in blocked["message"]
    assert calls == []

    assert controller.setOemCompatibilityChecksEnabled(True) is True
    assert controller.getOemCompatibilityChecksEnabled() is True
    result = controller.inspectDeviceCompatibility("SERIAL_A")
    assert result["ok"] is True
    assert calls == ["SERIAL_A"]
    assert controller._device_info["SERIAL_A"]["manufacturer"] == "Samsung"
    assert controller._device_info["SERIAL_A"]["model"] == "SM-A556E"

    assert controller.setOemCompatibilityChecksEnabled(False) is True
    assert controller.getOemCompatibilityChecksEnabled() is False
    blocked_again = controller.inspectDeviceCompatibility("SERIAL_A")
    assert blocked_again["ok"] is False
    assert calls == ["SERIAL_A"]

    settings_qml = (ROOT / "qml" / "pages" / "AppSettings.qml").read_text(encoding="utf-8")
    assert 'objectName: "oemCompatibilitySwitch"' in settings_qml
    assert "setOemCompatibilityChecksEnabled(checked)" in settings_qml
    assert 'objectName: "runOemCompatibilityCheckButton"' in settings_qml
    assert "inspectDeviceCompatibility(root.selectedSerial)" in settings_qml
    assert "لا يحاكي" in settings_qml


def test_oem_permission_guidance():
    controller = make_controller()
    serial = "XIAOMI_SERIAL"
    controller._device_info[serial] = {
        "manufacturer": "Xiaomi",
        "model": "Redmi Note"
    }
    operation = {
        "serial": serial,
        "kind": "copy",
        "destination": "/sdcard/Android/data/com.test.game/",
        "status": "فشل",
        "percent": 0,
        "operation": "فشل النسخ",
        "speed": "--",
        "eta": "--",
        "error": "Permission denied",
        "raw_error": "remote secure_mkdirs failed: Permission denied",
        "completed": True,
        "cancelled": False,
    }
    controller._active_operations[serial] = operation

    summary = controller._device_copy_failure_summary(
        serial, operation, operation["raw_error"]
    )
    assert "Android/data" in summary
    assert "إرشادات الإصلاح" in summary

    help_text = controller.deviceCopyHelp(serial)
    assert "Xiaomi Redmi Note" in help_text
    assert "USB debugging (Security settings)" in help_text
    assert "Install via USB" in help_text
    assert "remote secure_mkdirs failed: Permission denied" in help_text
    assert "ليس Root" in help_text


def test_shizuku_fallback_recognizes_android_data_remote_error_variants():
    destination = "/sdcard/Android/data/com.tencent.ig/files/UE4Game/Saved"
    remote_denials = (
        "adb: error: failed to copy: remote could not create file: Permission denied",
        "remote couldn't create file: Operation not permitted",
        "remote failed to create directory: Access denied",
        "remote file creation failed: EACCES",
    )
    for message in remote_denials:
        assert app_qml.AppController._is_android_data_permission_denial(
            destination, message
        ) is True, message

    # ADB inspection failure alone must not be emitted as a pre-copy denial.
    assert "KAFIANET_ANDROID_DATA_ACCESS_UNVERIFIED" not in adb_source

    # A confirmed missing path from the Shizuku-identity preflight is final;
    # it must not loop back into the same fallback.
    assert app_qml.AppController._is_android_data_permission_denial(
        destination,
        "KAFIANET_SHIZUKU_DESTINATION_MISSING: Saved not found",
    ) is False

    assert app_qml.AppController._is_android_data_permission_denial(
        "/sdcard/Android/obb/com.tencent.ig/",
        "remote open failed: Permission denied",
    ) is False



def test_secure_mkdirs_partial_directory_retry_is_scoped_to_saved():
    saved_root = (
        "/sdcard/Android/data/com.tencent.ig/files/UE4Game/"
        "ShadowTrackerExtra/ShadowTrackerExtra/Saved/"
    )
    nested_target = (
        saved_root.rstrip("/")
        + "/Config/Android/GameUserSettings.ini"
    )
    output = (
        "adb: error: failed to copy 'C:\\source\\GameUserSettings.ini' "
        + "to '" + nested_target + "': "
        + "remote secure_mkdirs failed: Operation not permitted"
    )

    assert ADBEngine._secure_mkdirs_retry_parent(
        saved_root, output
    ) == ADBEngine._normalize_android_remote_path(saved_root) + "/Config/Android"

    # A failed file directly under Saved is not treated as a partially-created
    # subdirectory; Saved itself is never created or repaired here.
    direct_target = saved_root.rstrip("/") + "/GameUserSettings.ini"
    direct_output = (
        "adb: error: failed to copy 'C:\\source\\GameUserSettings.ini' "
        + "to '" + direct_target + "': "
        + "remote secure_mkdirs failed: Operation not permitted"
    )
    assert ADBEngine._secure_mkdirs_retry_parent(
        saved_root, direct_output
    ) is None

    outside_target = "/sdcard/Android/data/com.other.game/Saved/sub/file.ini"
    outside_output = (
        "adb: error: failed to copy 'C:\\source\\file.ini' "
        + "to '" + outside_target + "': "
        + "remote secure_mkdirs failed: Operation not permitted"
    )
    assert ADBEngine._secure_mkdirs_retry_parent(
        saved_root, outside_output
    ) is None

    unrelated_error = (
        "adb: error: failed to copy 'C:\\source\\file.ini' "
        + "to '" + nested_target + "': remote open failed: Permission denied"
    )
    assert ADBEngine._secure_mkdirs_retry_parent(
        saved_root, unrelated_error
    ) is None


def test_legacy_root_fallback_prompt_only_after_android_data_denial():
    destination = "/sdcard/Android/data/com.tencent.ig/files/UE4Game/"
    reason = "remote secure_mkdirs failed: Permission denied"

    # ADB permission denial must launch Shizuku preparation, not show the
    # independent-root dialog immediately.
    controller = make_controller()
    fallback_requests = []
    controller.androidDataFallbackRequested.connect(
        lambda *values: fallback_requests.append(values)
    )
    operation = dict(
        rows(["SERIAL_A"])[0],
        source="source-folder",
        destination=destination,
        target_package="com.tencent.ig",
        shizuku_fallback_attempted=False,
        phase="adb_direct",
        source_label="اختبار",
        source_root="source-root",
    )

    controller._start_operation(operation)
    adb_engine = operation["engine"]
    assert adb_engine.calls == [
        ("push", "SERIAL_A", "source-folder", destination)
    ]

    adb_engine.finish(False, reason)
    assert fallback_requests == []
    assert operation["phase"] == "shizuku_prepare"
    assert operation["shizuku_fallback_attempted"] is True
    assert operation["shizuku_original_error"] == reason
    assert any(call[0] == "prepare_shizuku_bridge" for call in adb_engine.calls)

    # A successful Shizuku setup starts the same direct-stream engine.
    adb_engine.finish(True, json.dumps({"port": 18765, "token": "session-token"}))
    assert operation["phase"] == "shizuku_copy"
    assert any(call[0] == "copy_via_shizuku_bridge" for call in adb_engine.calls)
    assert fallback_requests == []

    # A failed Shizuku writer opens the same user-confirmed root fallback.
    writer_denial = (
        "KAFIANET_SHIZUKU_DESTINATION_WRITE_DENIED: "
        "open failed: EACCES (Permission denied)"
    )
    adb_engine.finish(False, writer_denial)
    assert len(fallback_requests) == 1
    serial, game_name, root_destination, combined_reason = fallback_requests[0]
    assert serial == "SERIAL_A"
    assert game_name == "PUBG Mobile"
    assert root_destination == "/sdcard/PUBG_Mobile/"
    assert "Android/data" in combined_reason
    assert writer_denial in combined_reason
    assert operation["phase"] == "awaiting_root_fallback"
    assert operation["awaiting_root_fallback"] is True
    assert operation["completed"] is True
    assert next(
        row for row in controller._operation_rows()
        if row["serial"] == "SERIAL_A"
    )["awaitingFallback"] is True

    # The dialog's affirmative action retains the old user-confirmed root retry.
    assert controller.copyToDeviceRoot("SERIAL_A") is True
    root_engine = operation["engine"]
    assert root_engine is not adb_engine
    assert root_engine.calls == [
        ("push", "SERIAL_A", "source-folder", "/sdcard/PUBG_Mobile/")
    ]
    assert operation["destination"] == "/sdcard/PUBG_Mobile/"
    assert operation["original_destination"] == destination
    assert operation["root_fallback_attempted"] is True
    assert operation["awaiting_root_fallback"] is False
    assert operation["completed"] is False

    root_engine.finish(True, "root copy complete")
    assert root_engine.calls[-1] == (
        "launch_phone_local_finalize",
        "SERIAL_A",
        "/sdcard/PUBG_Mobile/",
        "com.tencent.ig",
        "files/UE4Game",
        "PUBG Mobile",
    )
    assert operation["completed"] is True
    assert operation["status"] == "اكتمل"
    assert len(controller.getTransferHistory()) == 1
    assert controller.getTransferHistory()[0]["status"] == "اكتمل"

    # A Shizuku setup failure also unlocks the root fallback, without deleting the source.
    setup_controller = make_controller()
    setup_fallbacks = []
    setup_controller.androidDataFallbackRequested.connect(
        lambda *values: setup_fallbacks.append(values)
    )
    setup_operation = dict(
        rows(["SERIAL_B"])[0],
        source="source-folder",
        destination=destination,
        target_package="com.tencent.ig",
        phase="adb_direct",
    )
    setup_controller._start_operation(setup_operation)
    setup_engine = setup_operation["engine"]
    setup_engine.finish(False, reason)
    assert setup_operation["phase"] == "shizuku_prepare"
    setup_engine.finish(False, "لم يمنح الهاتف تطبيق KafiaNet إذن Shizuku.")
    assert setup_operation["phase"] == "awaiting_root_fallback"
    assert setup_operation["awaiting_root_fallback"] is True
    assert setup_operation["completed"] is True
    assert len(setup_fallbacks) == 1
    assert setup_fallbacks[0][0] == "SERIAL_B"

    # ADB success and failures outside Android/data do not invoke Shizuku or root.
    success_controller = make_controller()
    success_fallbacks = []
    success_controller.androidDataFallbackRequested.connect(
        lambda *values: success_fallbacks.append(values)
    )
    success_operation = dict(
        rows(["SERIAL_C"])[0],
        source="source-folder",
        destination=destination,
        target_package="com.tencent.ig",
        phase="adb_direct",
    )
    success_controller._start_operation(success_operation)
    success_engine = success_operation["engine"]
    success_engine.finish(True, "direct complete")
    assert success_fallbacks == []
    assert not any(call[0] == "prepare_shizuku_bridge" for call in success_engine.calls)

    unrelated_controller = make_controller()
    unrelated_fallbacks = []
    unrelated_controller.androidDataFallbackRequested.connect(
        lambda *values: unrelated_fallbacks.append(values)
    )
    unrelated_operation = dict(
        rows(["SERIAL_A"])[0],
        source="source-folder",
        destination="/sdcard/GameData/",
        target_package="",
        phase="adb_direct",
    )
    unrelated_controller._start_operation(unrelated_operation)
    unrelated_engine = unrelated_operation["engine"]
    unrelated_engine.finish(False, "Permission denied")
    assert unrelated_operation["completed"] is True
    assert unrelated_fallbacks == []
    assert not any(
        call[0] == "prepare_shizuku_bridge"
        for engine in FakeOperationEngine.instances
        for call in engine.calls
    )



def test_duplicate_completion(controller):
    operation = dict(rows(["SERIAL_A"])[0], source="src", destination="dst")
    controller._start_operation(operation)
    engine = engine_for("SERIAL_A")
    completions = []
    controller.operationFinished.connect(completions.append)
    engine.finish(True, "complete")
    engine.finish(True, "duplicate")
    assert len(completions) == 1


class FakeProcess:
    next_pid = 100

    def __init__(self):
        FakeProcess.next_pid += 1
        self.pid = FakeProcess.next_pid
        self.returncode = 0
        self.done = threading.Event()
        self.terminated = False

    def communicate(self, timeout=None):
        if timeout is not None and not self.done.wait(timeout):
            raise subprocess.TimeoutExpired([], timeout)
        return "stdout", ""

    def poll(self):
        return None if not self.done.is_set() else self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self.done.set()


class FakeStartupInfo:
    def __init__(self):
        self.dwFlags = 0
        self.wShowWindow = 0


def test_process_ownership():
    import engine.adb_engine as adb_module

    original = {
        key: getattr(adb_module.subprocess, key, None)
        for key in ("Popen", "STARTUPINFO", "STARTF_USESHOWWINDOW", "SW_HIDE")
    }
    processes = []

    def fake_popen(*_args, **_kwargs):
        process = FakeProcess()
        processes.append(process)
        return process

    adb_module.subprocess.Popen = fake_popen
    adb_module.subprocess.STARTUPINFO = FakeStartupInfo
    adb_module.subprocess.STARTF_USESHOWWINDOW = 1
    adb_module.subprocess.SW_HIDE = 0
    engine = adb_module.ADBEngine()
    try:
        first = threading.Thread(
            target=lambda: engine._run_install_command(["install", "a"], 30)
        )
        first.start()
        while len(processes) < 1:
            pass
        old = processes[0]
        second = threading.Thread(
            target=lambda: engine._run_install_command(["install", "b"], 30)
        )
        second.start()
        while len(processes) < 2:
            pass
        new = processes[1]
        old.done.set()
        first.join(1)
        assert engine._current_process is new
        new.done.set()
        second.join(1)
        assert engine._current_process is None
        third = threading.Thread(
            target=lambda: engine._run_install_command(["install", "c"], 30)
        )
        third.start()
        while engine._current_process is None:
            pass
        active = engine._current_process
        engine.cancel()
        third.join(1)
        assert active.terminated and engine._current_process is None
    finally:
        for key, value in original.items():
            if value is not None:
                setattr(adb_module.subprocess, key, value)


def test_windows_task_manager_policy_is_scoped_and_restored():
    from engine.windows_fullscreen import WindowsFullscreenProtection

    class FakeWinreg:
        HKEY_CURRENT_USER = "HKCU"
        KEY_READ = 0x20019
        KEY_QUERY_VALUE = 0x0001
        KEY_SET_VALUE = 0x0002
        REG_DWORD = 4
        REG_SZ = 1

        def __init__(self):
            self.keys = {}

        def OpenKey(self, root, path, reserved=0, access=0):
            if path not in self.keys:
                raise FileNotFoundError(path)
            return (root, path)

        def CreateKeyEx(self, root, path, reserved=0, access=0):
            self.keys.setdefault(path, {})
            return (root, path)

        def QueryValueEx(self, key, name):
            values = self.keys.get(key[1], {})
            if name not in values:
                raise FileNotFoundError(name)
            return values[name]

        def SetValueEx(self, key, name, reserved, value_type, value):
            self.keys.setdefault(key[1], {})[name] = (value, value_type)

        def DeleteValue(self, key, name):
            values = self.keys.get(key[1], {})
            if name not in values:
                raise FileNotFoundError(name)
            del values[name]

        def DeleteKey(self, root, path):
            if path not in self.keys:
                raise FileNotFoundError(path)
            if self.keys[path]:
                raise OSError("registry key is not empty")
            del self.keys[path]

        def CloseKey(self, key):
            return None

    protection = WindowsFullscreenProtection()
    fake = FakeWinreg()
    policy_path = protection.TASK_MANAGER_POLICY_KEY
    recovery_path = protection.TASK_MANAGER_RECOVERY_KEY

    # No previous setting: policy is set only during the lock, then removed.
    fake.keys[policy_path] = {"UnrelatedSetting": (7, fake.REG_DWORD)}
    assert protection._set_task_manager_disabled(fake) is True
    assert fake.keys[policy_path][protection.TASK_MANAGER_VALUE] == (
        1, fake.REG_DWORD
    )
    assert protection._set_task_manager_disabled(fake) is True
    assert fake.keys[recovery_path][protection.RECOVERY_PREVIOUS_EXISTS_VALUE] == (
        0, fake.REG_DWORD
    )
    assert protection._restore_task_manager_policy(fake) is True
    assert protection.TASK_MANAGER_VALUE not in fake.keys[policy_path]
    assert fake.keys[policy_path]["UnrelatedSetting"] == (7, fake.REG_DWORD)
    assert recovery_path not in fake.keys

    # An existing user choice is restored exactly, including its registry type.
    fake.keys[policy_path][protection.TASK_MANAGER_VALUE] = (
        "0", fake.REG_SZ
    )
    assert protection._set_task_manager_disabled(fake) is True
    assert fake.keys[policy_path][protection.TASK_MANAGER_VALUE] == (
        1, fake.REG_DWORD
    )
    assert protection._restore_task_manager_policy(fake) is True
    assert fake.keys[policy_path][protection.TASK_MANAGER_VALUE] == (
        "0", fake.REG_SZ
    )

    # The same restore path also handles an interrupted fullscreen session.
    assert protection._set_task_manager_disabled(fake) is True
    assert protection._restore_task_manager_policy(fake) is True
    assert fake.keys[policy_path][protection.TASK_MANAGER_VALUE] == (
        "0", fake.REG_SZ
    )




def test_versioned_games_discover_select_and_route_exact_folders():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        root = Path(directory)

        efootball_root = root / "بيس (eFootball™)"
        efootball_2021 = efootball_root / "2021"
        efootball_2027 = efootball_root / "2027"
        for version in (efootball_2021, efootball_2027):
            (version / "data").mkdir(parents=True)
            (version / "obb").mkdir()
            (version / "folder.jpg").write_bytes(b"cover")
            (version / "PES.apk").write_bytes(b"not-used-for-efootball")
        (efootball_2021 / "data" / "shared.dat").write_bytes(b"efootball-data")
        (efootball_2021 / "obb" / "main.1.com.konami.pesam.obb").write_bytes(b"efootball-obb")

        gta_root = root / "جراند ثفت اوتو (Grand Theft Auto)"
        gta_version = gta_root / "Grand Theft Auto San Andreas (2004)"
        (gta_version / "data").mkdir(parents=True)
        (gta_version / "obb").mkdir()
        (gta_version / "folder.jpg").write_bytes(b"cover")
        (gta_version / "GTA_ SA_2.10.apk").write_bytes(b"placeholder")
        import struct
        import zipfile
        apk_path = gta_version / "GTA_ SA_2.10.apk"

        # A compact real binary-XML manifest fixture: string pool + manifest start tag.
        string_values = ["manifest", "package", "com.example.gtasa"]
        encoded_strings = []
        string_offsets = []
        for value in string_values:
            encoded = value.encode("utf-8")
            string_offsets.append(sum(len(item) for item in encoded_strings))
            encoded_strings.append(bytes([len(value), len(encoded)]) + encoded + b"\x00")
        string_data = b"".join(encoded_strings)
        pool_size = 40 + len(string_data)
        string_pool = (
            struct.pack("<HHI", 0x0001, 28, pool_size)
            + struct.pack("<IIIII", len(string_values), 0, 0x100, 40, 0)
            + b"".join(struct.pack("<I", value) for value in string_offsets)
            + string_data
        )
        element_size = 56
        element = (
            struct.pack("<HHIII", 0x0102, 16, element_size, 1, 0xFFFFFFFF)
            + struct.pack("<IIHHHHHH", 0xFFFFFFFF, 0, 20, 20, 1, 0, 0, 0)
            + struct.pack("<IIIHBBI", 0xFFFFFFFF, 1, 2, 8, 0, 3, 2)
        )
        binary_manifest = (
            struct.pack("<HHI", 0x0003, 8, 8 + len(string_pool) + len(element))
            + string_pool + element
        )
        assert app_qml._read_axml_package_name(binary_manifest) == "com.example.gtasa"
        with zipfile.ZipFile(apk_path, "w") as archive:
            archive.writestr("AndroidManifest.xml", binary_manifest)
        (gta_version / "data" / "settings.dat").write_bytes(b"gta-data")
        (gta_version / "obb" / "main.2.com.example.gtasa.obb").write_bytes(b"gta-obb")

        controller.source_root = root
        controller.selected_devices = ["SERIAL_A"]
        controller._device_info = {
            "SERIAL_A": {"name": "Test Android", "model": "Test Android", "type": "android"}
        }
        controller._app_data_dir = root / "settings"
        controller._settings_file = controller._app_data_dir / "settings.json"
        controller._save_settings = lambda: app_qml.AppController._save_settings(controller)
        captured = []
        controller._start_operation = lambda operation: captured.append(dict(operation))

        efootball_versions = controller.getGameVersions("eFootball")
        resolved_efootball_root = app_qml.find_game_folder(root, "eFootball")
        root_entries = sorted(
            (item.name, item.is_dir()) for item in root.iterdir()
        )
        print("VERSIONED GAME TEST ROOT:", root)
        print("VERSIONED GAME TEST ROOT ENTRIES:", root_entries)
        print("VERSIONED GAME RESOLVED EFOOTBALL ROOT:", resolved_efootball_root)
        print("VERSIONED GAME DISCOVERED EFOOTBALL ENTRIES:", efootball_versions)
        assert resolved_efootball_root == efootball_root, (
            "eFootball alias resolution mismatch: "
            f"root={root!s}; entries={root_entries!r}; "
            f"resolved={resolved_efootball_root!r}; expected={efootball_root!s}"
        )
        assert [item["name"] for item in efootball_versions] == ["2027", "2021"], (
            "eFootball version discovery mismatch: "
            f"root={root!s}; resolved={resolved_efootball_root!s}; "
            f"versions={efootball_versions!r}"
        )
        version_2021 = next(item for item in efootball_versions if item["name"] == "2021")
        assert version_2021["hasApk"] is True
        assert version_2021["hasData"] is True
        assert version_2021["hasObb"] is True
        assert version_2021["image"].endswith("folder.jpg")
        assert controller.selectGameVersion("eFootball", "2021") is True
        assert controller.getSelectedGameVersion("eFootball") == "2021"
        saved_settings = json.loads(controller._settings_file.read_text(encoding="utf-8"))
        assert saved_settings["selected_game_versions"]["eFootball"] == "2021"
        reloaded = make_controller()
        reloaded._settings_file = controller._settings_file
        reloaded._history_file = root / "settings" / "history.json"
        reloaded._load_persistent_state()
        assert reloaded.getSelectedGameVersion("eFootball") == "2021"

        controller.copyGame("eFootball")
        assert len(captured) == 1
        efootball_copy = captured[-1]
        assert efootball_copy["game_version"] == "2021"
        assert efootball_copy["target_package"] == "com.ziru.official"
        assert efootball_copy["transfer_backend"] == "efootball_saf"
        assert efootball_copy["phase"] == "efootball_saf"
        assert len(efootball_copy["copy_parts"]) == 1
        assert efootball_copy["copy_parts"][0]["destination"] == "SAF://eFootball/data"
        assert Path(efootball_copy["copy_parts"][0]["source"]) == efootball_2021 / "data"
        assert Path(efootball_copy["source"]) == efootball_2021 / "data"

        gta_versions = controller.getGameVersions("Grand Theft Auto")
        assert len(gta_versions) == 1
        assert gta_versions[0]["packageId"] == "com.example.gtasa"
        assert controller.selectGameVersion(
            "Grand Theft Auto", "Grand Theft Auto San Andreas (2004)"
        ) is True
        assert app_qml._extract_apk_package_name(apk_path) == "com.example.gtasa"

        controller.copyGame("Grand Theft Auto")
        assert len(captured) == 2
        gta_copy = captured[-1]
        assert gta_copy["game_version"] == "Grand Theft Auto San Andreas (2004)"
        assert gta_copy["target_package"] == "com.example.gtasa"
        assert gta_copy["copy_parts"][0]["destination"] == (
            "/sdcard/Android/data/com.example.gtasa/"
        )
        assert gta_copy["copy_parts"][1]["destination"] == (
            "/sdcard/Android/obb/com.example.gtasa/"
        )
        assert Path(gta_copy["source"]) == gta_version / "data"

        # Installation uses the APK contained in the chosen version only.
        controller.installGame("Grand Theft Auto")
        assert len(captured) == 3
        gta_install = captured[-1]
        assert gta_install["kind"] == "install"
        assert gta_install["game_version"] == "Grand Theft Auto San Andreas (2004)"
        assert Path(gta_install["apk"]) == apk_path




def test_gta_copy_refuses_to_guess_unverified_package_id():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        gta_version = (
            root / "جراند ثفت اوتو (Grand Theft Auto)"
            / "Grand Theft Auto VI (2026)"
        )
        (gta_version / "data").mkdir(parents=True)
        (gta_version / "obb").mkdir()
        (gta_version / "data" / "unknown.bin").write_bytes(b"payload")
        controller.source_root = root
        controller.selected_devices = ["SERIAL_A"]
        controller._device_info = {
            "SERIAL_A": {"name": "Test Android", "model": "Test Android", "type": "android"}
        }
        assert controller.selectGameVersion(
            "Grand Theft Auto", "Grand Theft Auto VI (2026)"
        ) is True
        messages = []
        controller.messageChanged.connect(messages.append)
        started = []
        controller._start_operation = lambda operation: started.append(operation)

        controller.copyGame("Grand Theft Auto")
        assert started == []
        assert any("Package ID" in message for message in messages)
        assert not any("com.rockstar" in message for message in messages)


def test_efootball_uses_saf_engine_and_never_starts_shizuku_fallback():
    controller = make_controller()
    with TemporaryDirectory() as directory:
        source = Path(directory) / "data"
        (source / "files").mkdir(parents=True)
        (source / "files" / "assetpacks.bin").write_bytes(b"efootball")
        operation = {
            "serial": "SERIAL_A",
            "model": "Test Phone",
            "device_name": "Test Phone",
            "game": "eFootball",
            "game_version": "2027",
            "kind": "copy",
            "target_type": "android",
            "source": str(source),
            "destination": "SAF://eFootball/data",
            "copy_parts": [{
                "source": str(source),
                "destination": "SAF://eFootball/data",
                "label": "بيانات eFootball (Ziru_27)",
            }],
            "copy_part_index": 0,
            "transfer_backend": "efootball_saf",
            "phase": "efootball_saf",
            "completed": False,
            "cancelled": False,
            "history_recorded": False,
        }
        controller._begin_shizuku_fallback = lambda *_args: (
            (_ for _ in ()).throw(AssertionError("eFootball SAF must not fall back to Shizuku"))
        )
        controller._start_operation(operation)
        assert operation["percent"] == 0
        assert operation["status"]
        assert operation["operation"]
        assert operation["speed"] == "0 B/s"
        assert operation["eta"] == "--"
        assert operation["error"] == ""
        engine = operation["engine"]
        assert isinstance(engine, FakeEFootballSAFEngine)
        assert engine.calls == [(
            "push", "SERIAL_A", str(source), "SAF://eFootball/data"
        )]
        engine.finish(False, "simulated SAF error")
        assert operation["phase"] == "final"
        assert operation["completed"] is True


def test_copy_parts_are_one_sequential_device_operation():
    controller = make_controller()
    operation = {
        "serial": "SERIAL_A",
        "game": "eFootball",
        "kind": "copy",
        "phase": "adb_direct",
        "completed": False,
        "percent": 0,
        "copy_part_index": 0,
        "copy_parts": [
            {"source": "C:/eFootball/2021/data", "destination": "/sdcard/Android/data/com.konami.pesam/", "label": "بيانات اللعبة"},
            {"source": "C:/eFootball/2021/obb", "destination": "/sdcard/Android/obb/com.konami.pesam/", "label": "ملفات OBB"},
        ],
    }
    first_engine = FakeOperationEngine()
    operation["engine"] = first_engine
    controller._active_operations["SERIAL_A"] = operation

    assert controller._advance_copy_part("SERIAL_A", operation) is True
    assert operation["copy_part_index"] == 1
    assert operation["source"] == "C:/eFootball/2021/obb"
    assert operation["destination"] == "/sdcard/Android/obb/com.konami.pesam/"
    next_engine = operation["engine"]
    assert next_engine is not first_engine
    assert next_engine.calls == [
        ("push", "SERIAL_A", "C:/eFootball/2021/obb", "/sdcard/Android/obb/com.konami.pesam/")
    ]
    assert controller._advance_copy_part("SERIAL_A", operation) is False


def test_dashboard_discovers_new_source_folders_without_duplicate_known_cards():
    from engine.game_config import discover_dashboard_games

    with TemporaryDirectory() as directory:
        source_root = Path(directory)
        (source_root / "Fortnite").mkdir()
        custom = source_root / "لعبة تجريبية"
        custom.mkdir()
        (custom / "cover.jpg").write_bytes(b"image-placeholder")
        (custom / "Data").mkdir()

        cards = discover_dashboard_games(source_root, flash_source_root=source_root)
        assert len([item for item in cards if not item["isCustom"]]) == 8
        discovered = [item for item in cards if item["isCustom"]]
        assert len(discovered) == 1
        assert discovered[0]["name"] == "لعبة تجريبية"
        assert discovered[0]["sourcePath"] == str(custom.resolve())
        assert discovered[0]["imageSource"].startswith("file:")
        assert "cover.jpg" in discovered[0]["imageSource"]
        assert discovered[0]["color"].startswith("#")
        assert discovered[0]["sourceExists"] is True


def test_discovered_folder_package_ids_are_resolved_without_guessing():
    import zipfile

    controller = make_controller()
    with TemporaryDirectory() as directory:
        root = Path(directory)
        controller.source_root = root

        def add_xapk(folder, filename, package_id):
            apk_path = folder / filename
            apk_path.parent.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(apk_path, "w") as archive:
                archive.writestr(
                    "manifest.json",
                    json.dumps({"package_name": package_id}),
                )
            return apk_path

        discovered = root / "لعبة جديدة"
        add_xapk(discovered / "الإصدار", "package.xapk", "com.example.discovered")
        assert controller._selected_game_package_id("لعبة جديدة") == "com.example.discovered"

        ambiguous = root / "مجموعة ألعاب"
        add_xapk(ambiguous, "one.xapk", "com.example.one")
        add_xapk(ambiguous, "two.xapk", "com.example.two")
        assert controller._selected_game_package_id("مجموعة ألعاب") == ""

        no_apk = root / "مجلد بيانات فقط"
        no_apk.mkdir()
        assert controller._selected_game_package_id("مجلد بيانات فقط") == ""


def test_paths_and_ui():
    current = Path("app_qml.py").read_text(encoding="utf-8")
    head = subprocess.check_output(
        ["git", "show", "HEAD:app_qml.py"],
        encoding="utf-8",
        errors="strict"
    )
    for start, end in (
        ('        elif game_name == "Call of Duty Mobile":', "        else:"),
    ):
        a, b = current.index(start), current.index(end, current.index(start))
        x, y = head.index(start), head.index(end, head.index(start))
        assert current[a:b] == head[x:y]
    engine_source = Path("engine/adb_engine.py").read_text()
    install_start = engine_source.index("def install_apks")
    install_end = engine_source.index("def _suspend_current_process")
    assert "progressChanged.emit(\n                        100" not in (
        engine_source[install_start:install_end]
    )
    qml = Path("qml/main.qml").read_text()
    history_qml = Path("qml/pages/TransferHistory.qml").read_text()
    assert "appController.chooseCustomSource()" in qml
    assert "transferHistoryPage" in qml
    assert "settingsPage" in qml
    settings_qml = Path("qml/pages/AppSettings.qml").read_text(encoding="utf-8")
    assert 'objectName: "oemCompatibilitySwitch"' in settings_qml
    assert "appController.inspectDeviceCompatibility(root.selectedSerial)" in settings_qml
    assert "لا يحاكي MIUI أو OxygenOS/ColorOS" in settings_qml
    assert "dismissDeviceOperation" in qml
    assert "clearTransferHistory" not in history_qml
    assert "clearHistoryDialog" not in history_qml
    assert "مسح سجل النقل" not in history_qml
    assert "appController.getSuccessfulPhones()" not in history_qml
    assert 'id: periodSelector' in history_qml
    assert 'model: ["اليوم", "أمس", "آخر 7 أيام", "آخر شهرين", "مخصص"]' in history_qml
    assert 'property string periodMode: "today"' in history_qml
    assert "function selectedDateRange()" in history_qml
    assert "function filterEntries()" in history_qml
    assert "function buildSuccessfulDevices(history)" in history_qml
    assert "customFromDate" in history_qml and "customToDate" in history_qml
    assert "هواتف ناجحة" in history_qml
    assert "root.visibleEntries" in history_qml
    assert "clearTransferHistory" not in current
    assert "successfulPhonesChanged" not in current
    assert "_successful_phones" not in current
    assert "KafiaNetControlPro_SuccessfulPhones.json" not in current
    assert "-500:" not in current
    assert "جاري اختبار الكتابة داخل مجلد Saved الموجود مسبقًا دون إنشائه..." not in engine_source
    assert "if not is_saved_destination:" in engine_source
    assert 'id: successfulDeviceList' in history_qml
    assert 'id: historyList' in history_qml
    assert 'objectName: "fullscreenLockButton"' in qml
    assert 'objectName: "windowUnlockDialog"' in qml
    assert 'transferCompletedCountVisible' in qml
    assert 'appController.verifyWindowPassword' in qml
    assert "appController.setFullscreenProtection(true)" in qml
    assert "appController.setFullscreenProtection(false)" in qml
    assert "window.showFullScreen()" in qml
    assert "window.showNormal()" in qml
    assert "window.showMaximized()" in qml
    assert "window.flags =" not in qml
    assert "window.hide()" not in qml
    assert "property bool pendingFullscreenClose: false" in qml
    assert "openFullscreenPasswordDialog(true)" in qml
    assert "closeEvent.accepted = false" in qml
    assert "closeEvent.accepted = true" in qml
    assert "window.leaveFullscreenLock(closeAfterUnlock)" in qml
    assert "appController.setFullscreenProtection(false)" in qml
    window_guard = Path("engine/windows_fullscreen.py").read_text(encoding="utf-8")
    assert "WH_KEYBOARD_LL" in window_guard
    assert "Shell_TrayWnd" in window_guard
    assert "VK_LWIN" in window_guard and "VK_RWIN" in window_guard
    assert "VK_ESCAPE" in window_guard and "VK_TAB" in window_guard
    assert "setFullscreenProtection(self, enabled)" in current
    assert "aboutToQuit.connect(controller.releaseFullscreenProtection)" in current
    assert "DisableTaskMgr" in window_guard
    assert "RECOVERY_PREVIOUS_VALUE" in window_guard
    assert "_recover_stale_task_manager_policy()" in window_guard
    assert "_restore_task_manager_policy()" in window_guard
    assert "تعطيل إدارة المهام" in current
    assert "enumerate_removable_storage_devices" in current
    assert "target_type" in current and "local_usb_copy" in current
    assert "getSelectedTargetUnits" in current
    storage_source = Path("engine/windows_storage.py").read_text(encoding="utf-8")
    assert "DRIVE_REMOVABLE" in storage_source
    assert "IOCTL_STORAGE_QUERY_PROPERTY" in storage_source
    assert "BUS_TYPE_USB" in storage_source
    local_transfer_source = Path("engine/transfer_engine.py").read_text(encoding="utf-8")
    assert "shutil.disk_usage(destination)" in local_transfer_source
    assert "Not enough free space for new files" in local_transfer_source
    assert '"usb_storage"' in storage_source
    assert '"units": 1' in storage_source
    assert '"billing_value": 500' in storage_source
    assert "modelData.units" in qml
    assert "modelData.billingValue" in qml
    assert "modelData.freeText" in qml
    history_qml = Path("qml/pages/TransferHistory.qml").read_text(encoding="utf-8")
    assert "successfulDeviceUnits" in history_qml
    assert "readonly property var successfulDevices:" in history_qml
    assert "modelData.successful_copies" in history_qml
    assert "onSuccessfulPhonesChanged(phones)" not in history_qml
    assert 'modelData.target_type === "usb_storage"' in history_qml
    assert "فلاشة • جديد:" in history_qml
    assert "deviceUnitsForSerials" in qml
    assert 'def verifyWindowPassword(self, password)' in current
    assert 'windowUnlockPassword.text ===' not in qml
    assert 'onClosing: function(closeEvent)' in qml
    assert "completedCount" in history_qml
    assert "failedCount" in history_qml
    assert "reviewCount" in history_qml
    assert "insufficientSpaceCount" in history_qml
    assert "modelData.serial" in history_qml
    assert "ScrollBar.AsNeeded" in history_qml
    assert "Layout.minimumHeight: 260" in history_qml
    game_card_qml = Path("qml/components/GameCard.qml").read_text(encoding="utf-8")
    assert "versionPicker" in game_card_qml
    assert "GridView" in game_card_qml
    assert "appController.getGameVersions(root.gameName)" in game_card_qml
    assert "appController.selectGameVersion(root.gameName, versionInfo.name)" in game_card_qml
    assert "onSelectedGameVersionChanged(gameName, versionName)" in game_card_qml
    assert "z: 1" not in game_card_qml
    assert "VERSIONED_GAME_NAMES" in current
    assert "def _known_gta_package_id" not in current
    assert "onSelectedGameVersionChanged(gameName, versionName)" in game_card_qml
    assert "z: 1" not in game_card_qml
    assert "folder.jpg" in current or "folder.jpg" in game_card_qml
    assert "appController.uninstallGame(root.gameName)" in game_card_qml
    assert "appController.closeGame(root.gameName)" in game_card_qml
    assert 'text: "إغلاق اللعبة"' in game_card_qml
    assert "uninstallConfirmDialog" in game_card_qml
    assert "PUBG Mobile Flash" in current
    # Versioned games replace the default package ID only after validating
    # the selected version's actual package metadata.
    assert 'target_package = ADB_PACKAGE_IDS.get(game_name, "")' in current
    assert "target_package = package_id" in current
    assert "com.tencent.igfit" in current
    assert "/storage/emulated/0/Android/data/com.tencent.igfit/" in current
    assert 'if size_folder in ("Medium", "Low")' in current
    adb_source = Path("engine/adb_engine.py").read_text(encoding="utf-8")
    assert "def uninstall(self, device, package_id)" in adb_source
    push_start = adb_source.index("def _push_thread(")
    push_end = adb_source.index("def push(", push_start)
    push_source = adb_source[push_start:push_end]
    # Bulk copies use --sync; only a detected incomplete file is forced through a normal push.
    assert "self._synced_push_command(" in push_source
    assert '"push", "--sync"' in adb_source
    assert "ADB RESUMING INTERRUPTED FILE:" in adb_source
    assert "ADB RESUME CHECKPOINT:" in adb_source
    assert "if retry_file_path is not None" in push_source
    assert "if root_exists and partial_parent_exists and connected:" in adb_source

    preflight_start = push_source.index("is_saved_destination =")
    preflight_end = push_source.index(
        "manifest, total_bytes = self._build_direct_stream_manifest(source_path)",
        preflight_start,
    )
    preflight_source = push_source[preflight_start:preflight_end]
    assert "if not is_saved_destination:" in preflight_source
    assert "Never create Saved or its parents." in push_source
    assert '"mkdir", "-p", check_path' in preflight_source
    # Saved is not probed before transfer; the real ADB push decides whether it can write.
    assert "KAFIANET_ANDROID_DATA_ACCESS_UNVERIFIED" not in preflight_source
    assert "if is_saved_destination and not self._remote_directory_exists(device, check_path):" not in preflight_source
    assert "self._remote_directory_exists(device, check_path)" not in preflight_source
    assert "Never create Saved or its parents." in preflight_source
    assert '"push"' in push_source
    assert "self._preexisting_sync_plan" not in push_source
    assert "self._stream_pending_files" not in push_source
    assert "self._push_pending_files_native" not in push_source
    assert "def force_stop_app(self, device, package_id)" in adb_source
    assert '["-s", device, "shell", "am", "force-stop", package_id]' in adb_source
    assert "dismiss_app_from_recents" not in adb_source
    assert "uiautomator" not in adb_source
    assert '"input", "swipe"' not in adb_source
    assert '"input", "keyevent"' not in adb_source
    game_config_source = Path("engine/game_config.py").read_text(encoding="utf-8")
    assert '"PUBG Mobile Flash"' in game_config_source
    games_config = Path("games.json").read_text(encoding="utf-8")
    assert '"PUBG Mobile Flash"' in games_config
    dashboard_qml = Path("qml/pages/Dashboard.qml").read_text(encoding="utf-8")
    assert "getDashboardGames" in dashboard_qml
    assert "Repeater" in dashboard_qml and "model: root.games" in dashboard_qml
    game_config_source = Path("engine/game_config.py").read_text(encoding="utf-8")
    assert "def discover_dashboard_games(source_root, flash_source_root=None)" in game_config_source
    assert "def _dashboard_artwork(folder)" in game_config_source
    card_qml = Path("qml/components/GameCard.qml").read_text(encoding="utf-8")
    assert "property bool isCustom: false" in card_qml
    assert "مجلد مكتشف تلقائيًا" not in card_qml
    assert "نسخ المجلد المكتشف" not in card_qml
    assert "appController.openDashboardFolder(root.sourcePath)" not in card_qml
    assert "appController.copyDiscoveredFolder(" in card_qml
    assert "visible: !root.isCustom" not in card_qml
    assert "visible: root.isCustom" not in card_qml
    assert "if (root.isCustom)" in card_qml
    for action in (
        'text: "تثبيت"',
        'text: "فتح اللعبة"',
        'text: "نسخ"',
        'text: "إزالة التثبيت"',
        'text: "إغلاق اللعبة"',
    ):
        assert action in card_qml
    assert "def _discovered_folder_package_id(self, game_name)" in current
    assert "transferSummary.completed" not in dashboard_qml
    assert "النسخ المكتملة" not in dashboard_qml
    assert "successfulDevices" in history_qml
    assert "device_name" in history_qml
    assert "transferPanelCollapsed" in qml
    assert 'text: window.transferPanelCollapsed ? "تكبير" : "تصغير"' in qml
    assert "transferCompletedBadge" in qml
    assert "onTransferSummaryChanged(summary)" in qml
    assert qml.index('text: "📱 الأجهزة ووسائط التخزين"') < qml.index("id: transferCompletedBadge")
    assert qml.index("id: transferCompletedBadge") < qml.index("// REFRESH")
    assert "appController.setPubgSourceSize(values[index])" in qml
    assert "حجم PUBG / FLASH" in qml
    assert "property int totalGames: 0" in qml
    assert "function refreshDashboardGameCount()" in qml
    assert "appController.getDashboardGames()" in qml
    assert "window.totalGames = discoveredGames.length" in qml
    assert "property int totalGames: 8" not in qml
    assert "getPubgSourceSize" in current
    assert "PUBG SELECTED SIZE" in current
    assert "onTransferHistoryChanged" in history_qml
    assert "history_recorded" in current
    assert "source_label" in current
    assert "self.adb_engine.force_stop_app(serial, package_id)" in current
    # Transfer-history clearing was intentionally removed from the date-based history view.
    assert "clearTransferHistory" not in current
    assert "60335d944c680352860237c155c04fec8ee82ccddefc33b40f3e5ee92f842670" not in current
    assert "def verifyWindowPassword(self, password)" in current
    assert "ؤمثشقمخل" not in current
    # Frequent transferBatchChanged signals must patch stable rows in place;
    # replacing a JavaScript-array ListView model resets contentY and scroll.
    assert "model: transferDevicesModel" in qml
    assert "function syncTransferDeviceModel(devices)" in qml
    assert "transferDevicesModel.setProperty(" in qml
    assert "transferDevicesModel.clear()" not in qml
    assert "model: window.transferDevices" not in qml
    assert "onClicked: appController.cancelDevice" in qml
    assert '"إيقاف مؤقت"' in qml
    assert '"استئناف"' in qml
    assert "appController.pauseTransfer()" in qml
    assert "appController.resumeTransfer()" in qml
    assert "إرشادات" in qml
    assert "deviceCopyHelpDialog.open()" in qml
    assert "deviceCopyHelpText" in qml
    assert "TransferWindow()" not in current
    assert "PRE_COPY_APPS_ROOT = Path(r\"D:\\games\\تطبيقات للنسخ\")" in current
    assert "def build_mt_manager_apk_path()" in current
    assert "def build_shizuku_apk_path()" in current
    assert "def build_shizuku_helper_apk_path()" in current
    assert "engine.prepare_apps_before_copy(" not in current
    assert 'operation["phase"] = "preinstall_apps"' not in current
    assert "KAFIANET_POST_COPY_PERMISSION_ERROR" not in current
    assert "KAFIANET_POST_COPY_PERMISSION_ERROR" not in engine_source
    assert "shizuku_permissions_only" not in current
    assert "shizuku_permissions" not in current
    assert "engine.prepare_game_permissions_via_shizuku(" not in current
    assert "def prepare_game_permissions_via_shizuku(" not in engine_source
    assert "def _begin_shizuku_fallback(" in current
    assert "engine.prepare_shizuku_bridge(" in current
    assert "engine.copy_via_shizuku_bridge(" in current
    assert '"phase": "local_usb_copy" if target_type == "usb_storage" else "adb_direct"' in current
    assert "def prepare_shizuku_bridge(" in engine_source
    assert "def copy_via_shizuku_bridge(" in engine_source
    assert "def prepare_apps_before_copy(" not in engine_source
    assert '"bin.mt.plus"' not in engine_source
    assert "uid=2000" in engine_source
    assert "function queueAndroidDataFallback" in qml
    assert "function onAndroidDataFallbackRequested" in qml
    assert "نسخ إلى الجذر" in qml
    assert "def copyToDeviceRoot(" in current
    assert "def declineRootFallback(" in current
    assert "def launch_phone_local_finalize(" in engine_source
    helper_root = ROOT / "android-helper" / "app" / "src" / "main"
    helper_manifest = (helper_root / "AndroidManifest.xml").read_text(encoding="utf-8")
    assert "PubgOfflineFinalizeService" in helper_manifest
    assert "EFootballActivity" in helper_manifest
    assert (helper_root / "java" / "com" / "kafianet" / "shizukubridge" / "HomeActivity.java").is_file()
    assert "tree_uri_" in (helper_root / "java" / "com" / "kafianet" / "shizukubridge" / "PubgOfflineFinalizeService.java").read_text(encoding="utf-8")
    assert "com.kafianet.shizukubridge" in (ROOT / "engine" / "efootball_saf_engine.py").read_text(encoding="utf-8")
    assert "build/android_support" in Path("build_full.spec").read_text(encoding="utf-8")
    assert '["-s", device, "install", "-r", apk_path]' in engine_source
    assert "install_failed_update_incompatible" in engine_source
    assert '["-s", device, "uninstall", helper_package]' in engine_source
    assert '"signature mismatch" in detail' in engine_source
    assert '"signatures do not match" in detail' in engine_source
    workflow_source = Path(".github/workflows/build-self-contained-installer.yml").read_text(encoding="utf-8")
    assert "CNPlayHubShizukuBridge.apk" in workflow_source
    assert "KafiaNetEFootballSAF.apk" not in workflow_source
    unified_efootball_source = (
        helper_root / "java" / "com" / "kafianet" / "shizukubridge" / "EFootballActivity.java"
    ).read_text(encoding="utf-8")
    assert "ACTION_OPEN_DOCUMENT_TREE" in unified_efootball_source
    assert "com.ziru.official.MTDataFilesProvider" in unified_efootball_source
    assert "takePersistableUriPermission" in unified_efootball_source
    assert "onNewIntent(Intent intent)" in unified_efootball_source
    assert '"/file"' in unified_efootball_source
    assert "DocumentsContract.renameDocument(" in unified_efootball_source
    assert "FOREGROUND_SERVICE_DATA_SYNC" in helper_manifest
    assert "READ_EXTERNAL_STORAGE" in helper_manifest
    assert "from engine.efootball_saf_engine import EFootballSAFEngine" in current
    transfer_user_java = Path(
        "android-helper/app/src/main/java/com/kafianet/shizukubridge/TransferUserService.java"
    ).read_text(encoding="utf-8")
    helper_activity_java = Path(
        "android-helper/app/src/main/java/com/kafianet/shizukubridge/MainActivity.java"
    ).read_text(encoding="utf-8")
    helper_aidl = Path(
        "android-helper/app/src/main/aidl/com/kafianet/shizukubridge/ITransferUserService.aidl"
    ).read_text(encoding="utf-8")
    assert "beginDirectStreamJob" in helper_aidl
    assert "inspectDestination(String destination)" in helper_aidl
    assert "prepareDestinationWritable" not in helper_aidl
    assert "startDirectStreamFile" in helper_aidl
    assert "getDirectStreamFileStatus" in helper_aidl
    assert "checkDirectStreamFiles" in helper_aidl
    assert "getLastStartError" in helper_aidl
    assert '"/destination/check"' in helper_activity_java
    assert "ParcelFileDescriptor.createPipe()" in helper_activity_java
    assert "ParcelFileDescriptor.AutoCloseOutputStream" in helper_activity_java
    assert '"/stream/file"' in helper_activity_java
    assert '"/permissions"' not in helper_activity_java
    assert '"/stream/check"' in helper_activity_java
    assert "userService.checkDirectStreamFiles(" in helper_activity_java
    assert "private JSONObject receiveDirectStreamFile(" in helper_activity_java
    assert "userService.startDirectStreamFile(" in helper_activity_java
    assert "private void executeCopy(" not in helper_activity_java
    assert '"/copy"' not in helper_activity_java
    assert "KafiaNetTransfer" not in helper_activity_java
    assert "requestSafGrantAfterDirectFailure" not in helper_activity_java
    assert "ACTION_OPEN_DOCUMENT_TREE" in helper_activity_java
    assert "takePersistableUriPermission" in helper_activity_java
    assert "request_local_finalize" in helper_activity_java
    assert "PubgOfflineFinalizeService" in helper_activity_java
    assert "ready_saf" not in helper_activity_java
    assert "اختر مجلد Android/data" not in engine_source
    assert "private File checkedRelativeDestination(" in transfer_user_java
    assert "beginDirectStreamJob(" in transfer_user_java
    assert "inspectDestination(String destination)" in transfer_user_java
    assert "stream.completed.await(" in transfer_user_java
    assert "stream.completed.countDown()" in transfer_user_java
    assert "temporary.length()" not in transfer_user_java
    assert "output.getFD().sync()" not in transfer_user_java
    assert "Thread.sleep(100L)" not in helper_activity_java
    assert "Os.stat(root.getAbsolutePath())" in transfer_user_java
    assert "error.errno == OsConstants.ENOENT" in transfer_user_java
    assert "prepareDestinationWritable(" not in transfer_user_java
    assert "makeDestinationWritableTree(" not in transfer_user_java
    assert "MessageDigest" not in transfer_user_java
    assert "Os.chmod(" not in transfer_user_java
    assert "startDirectStreamFile(" in transfer_user_java
    assert "new FileOutputStream(temporary, false)" in transfer_user_java
    assert "Os.rename(" in transfer_user_java
    assert ".kafianet-" in transfer_user_java and ".part" in transfer_user_java
    assert "KafiaNetTransfer" not in transfer_user_java
    assert "STAGING_BASE" not in transfer_user_java
    assert "startDirectCopy" not in transfer_user_java
    assert "createManifest" not in transfer_user_java
    assert "openSourceFile" not in transfer_user_java
    assert "shizuku_stage" not in current
    assert "staging_root" not in current
    assert "KafiaNetTransfer" not in current
    assert '"/stream/start"' in adb_source
    assert '"/stream/check"' in adb_source
    assert '"/stream/file"' in adb_source
    assert "X-Kafia-Sha256" not in adb_source
    assert "def _verify_remote_manifest(" not in adb_source
    assert "toybox sha256sum" not in adb_source
    assert "sha256" not in adb_source.lower()
    assert "chmod -R a+rwX" not in adb_source
    assert "_set_remote_game_permissions" not in adb_source
    assert "write_test_" not in adb_source
    assert '"/stream/finish"' in adb_source
    assert "def _build_direct_stream_manifest(source_path)" in adb_source
    assert "def _iter_file_chunks_exact(source, expected_size" in adb_source
    assert "Source file grew during transfer" in adb_source
    assert "Source file was truncated during transfer" in adb_source
    assert "KafiaNetTransfer" not in adb_source
    helper_gradle = Path("android-helper/app/build.gradle").read_text(encoding="utf-8")
    assert "versionCode 13" in helper_gradle
    assert "versionName '1.6.7'" in helper_gradle
    assert "install_helper_if_outdated()" in engine_source
    assert "installed_code >= 14" in engine_source
    assert ".version(13)" in helper_activity_java
    assert "Shizuku.addBinderDeadListener(binderDead);" in helper_activity_java
    assert "private volatile boolean bindingService" in helper_activity_java
    assert "private volatile boolean permissionRequestInFlight" in helper_activity_java
    helper_create = helper_activity_java[helper_activity_java.index("protected void onCreate("):helper_activity_java.index("protected void onNewIntent(")]
    assert helper_create.index("addRequestPermissionResultListener") < helper_create.index("addBinderReceivedListenerSticky")
    assert "initializeShizuku();" not in helper_create
    assert "Os.chmod(folder.getAbsolutePath(), 0777)" not in transfer_user_java
    helper_manifest = Path("android-helper/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")
    assert "targetSdk 29" in helper_gradle
    assert "disable 'ExpiredTargetSdkVersion'" in helper_gradle
    assert 'android:requestLegacyExternalStorage="true"' in helper_manifest
    assert "android-helper/**" in Path(".github/workflows/build-self-contained-installer.yml").read_text(encoding="utf-8")
    assert "preinstall_mt_manager" not in current
    assert "build_mt_manager_apk_path" in current

    # Regression checks for the repaired lifecycle and physical source paths.
    assert 'return Path(r"D:\\games")' in current
    assert str(app_qml.build_usb_source_root()) == r"D:\games"
    assert "source = build_usb_source_root()" in current
    assert "if not source.is_dir():" in current
    assert "self.adbDiscoveryCompleted.connect(self._apply_adb_devices)" in current
    assert "def refreshADBDevices(self):" in current
    assert "active = any(" in current
    launch_start = current.index("    def launchGame(self, game_name):")
    launch_end = current.find("\n    @Slot(", launch_start)
    assert launch_end > launch_start
    launch = current[launch_start:launch_end]
    assert "self.adb_engine.get_devices()" not in launch
    assert "يرجى تحديد هاتف واحد على الأقل قبل تشغيل اللعبة." in launch
    assert '"pm", "path", package_id' in launch
    assert '"resolve-activity", "--brief"' in launch
    assert '"am", "start", "-W"' in launch
    assert 'Status:\\s*ok' in launch

    # Disconnected phones resume from the interrupted file; bulk pushes use ADB sync.
    assert "def _device_is_connected(self, device):" in engine_source
    assert "self._wait_for_device_reconnect(device)" in engine_source
    assert "انقطع اتصال الهاتف. الرجاء إعادة توصيله لاستمرار النسخ..." in engine_source
    assert "ADB RESUME MODE: retry only the interrupted file" in engine_source
    assert "def _interrupted_source_file(" in engine_source
    assert "def _synced_push_command(" in engine_source
    assert '"push", "--sync"' in engine_source
    disconnect_start = engine_source.index("if not device_connected or looks_disconnected:")
    reconnect_branch = engine_source[
        disconnect_start:engine_source.index(
            "if self._wait_for_device_reconnect(device):",
            disconnect_start,
        )
    ]
    assert "self._progress_offset_bytes = 0" not in reconnect_branch
    assert "KAFIANET_ANDROID_DATA_ACCESS_UNVERIFIED" not in push_source
    assert "KAFIANET_ANDROID_DATA_PERMISSION_DENIED" in push_source
    assert '"/destination/check"' in engine_source
    assert "KAFIANET_SHIZUKU_DESTINATION_MISSING" in engine_source
    assert "KAFIANET_SHIZUKU_DESTINATION_ACCESS_DENIED" in engine_source
    assert "def _secure_mkdirs_retry_parent(" in engine_source
    assert "def _remote_directory_exists(" in engine_source
    assert "تمت إعادة توصيل الهاتف" in engine_source

    # Protect against ADB stderr pipe deadlocks and false 100% completion.
    wait_start = engine_source.index("def _push_thread")
    stderr_start = engine_source.index("stderr_reader.start()", wait_start)
    stdout_start = engine_source.index("stdout_reader.start()", wait_start)
    wait_call = engine_source.index("return_code = process.wait()", wait_start)
    assert stderr_start < wait_call
    assert stdout_start < wait_call
    assert "stderr_reader.join(timeout=2.0)" in engine_source
    assert "stdout_reader.join(timeout=2.0)" in engine_source
    assert "stdout=subprocess.PIPE" in engine_source[wait_start:]
    assert 'args=(process.stdout, stdout_chunks, "stdout")' in engine_source
    push_method = engine_source[wait_start:engine_source.index("@Slot(str, str, str)", wait_start)]
    assert "stdout=subprocess.PIPE" in push_method
    assert "stdout=subprocess.DEVNULL" not in push_method
    assert "def _parse_adb_progress_details(line)" in engine_source
    assert "self._parse_adb_progress_details(line)" in engine_source
    assert "def _parse_adb_transfer_speed(line)" in engine_source
    assert "def _combined_progress_percent(" in engine_source
    assert "min(int(transferred * 100 / total_bytes), 99)" in engine_source
    assert "self._combined_progress_percent(" in engine_source
    assert "def _combined_native_progress_percent(" in engine_source
    assert "self._combined_native_progress_percent(" in engine_source
    assert 'progress_state["native_display_percent"]' in engine_source
    assert 'progress_state["native_bytes"]' not in engine_source
    assert 'start_time = time.monotonic()' in push_source
    assert engine_source.index(
        'local_adb = BASE_DIR / "platform-tools" / "adb.exe"'
    ) < engine_source.index('external_adb = Path(')

    assert "appController.refreshADBDevices()" in qml
    assert 'objectName: "mainSidebar"' in qml
    assert 'objectName: "dashboardPage"' in qml
    assert 'objectName: "transferPanel"' in qml
    assert "TRANSFER_PANEL_SIGNAL_SMOKE_TEST: PASS" in current
    # Device names must have explicit readable text and selection contrast.
    assert "id: deviceSelectionCheckBox" in qml
    assert "text: deviceSelectionCheckBox.text" in qml
    assert 'deviceSelectionCheckBox.checked' in qml
    assert '"#FFFFFF"' in qml
    assert '"#DCE4F0"' in qml
    assert '? "#153845"' in qml
    assert 'border.color: deviceSelectionCheckBox.checked' in qml
    assert "appController.getADBDevices()" not in qml
    assert "if (appController.useWirelessSource())" in qml
    assert "if (appController.useUsbSource())" in qml

    # Shipping launchers must use the local Python runtime and stay ASCII-only.
    launcher = Path("Start-KAFIANET-CONTROL-PRO.cmd").read_text(encoding="utf-8")
    assert all(ord(char) < 128 for char in launcher)
    assert "Launch-KAFIA-NET-CONTROL-PRO.vbs" in launcher
    assert "wscript.exe" in launcher
    hidden_launcher = Path("Launch-KAFIA-NET-CONTROL-PRO.vbs").read_text(encoding="utf-8")
    assert all(ord(char) < 128 for char in hidden_launcher)
    assert "Start-KAFIANET-CONTROL-PRO.ps1" in hidden_launcher
    assert "shell.Run(command, 0, True)" in hidden_launcher

    external_launcher = Path("Start-KAFIANET-CONTROL-PRO.ps1").read_text(
        encoding="utf-8"
    )
    assert all(ord(char) < 128 for char in external_launcher)
    assert "D:\\games\\py" in external_launcher
    assert "SERVER06\\c$" not in external_launcher
    assert "PYTHONHOME" in external_launcher
    assert "PYTHONPATH" in external_launcher
    assert "--qt-import-smoke-test" in external_launcher
    assert "kafianet_external_runtime*.pyd" in external_launcher
    # The VBS hides PowerShell itself; the Qt app process must not be
    # created with a hidden window style or the desktop launch may appear blank.
    assert "& $pythonExe -c $bootstrapCommand" in external_launcher
    assert "$appProcess = Start-Process -FilePath $pythonExe" not in external_launcher
    assert "Start-Process -FilePath $pythonExe" not in external_launcher
    assert "-WindowStyle Hidden" not in external_launcher
    assert "WScript.Shell" in external_launcher
    assert "Invoke-WebRequest" not in external_launcher
    assert "pip install" not in external_launcher
    assert "vc_redist" not in external_launcher

    bootstrap = Path("kafianet_external_runtime.py").read_text(encoding="utf-8")
    runtime_root = 'Path(r"D:\\games\\py")'
    assert runtime_root in bootstrap
    assert "SERVER06\\c$" not in bootstrap
    assert "os.add_dll_directory" in bootstrap
    assert "QT_QPA_PLATFORM_PLUGIN_PATH" in bootstrap
    assert 'importlib.import_module("app_qml")' in bootstrap
    assert "from PySide6.QtWidgets import QApplication" in current
    assert "QTWIDGETS_QAPPLICATION_SMOKE_TEST: PASS" in current
    assert "OEM_DIAGNOSTICS_SETTINGS_UI_SMOKE_TEST: PASS" in current
    assert "pyside_parts < (6, 10, 1)" in current
    requirements = Path("requirements.txt").read_text(encoding="utf-8")
    assert "PySide6>=6.10.1,<7" in requirements

    workflow = Path(
        ".github/workflows/build-self-contained-installer.yml"
    ).read_text(encoding="utf-8")
    assert "https://dl.google.com/android/repository/platform-tools-latest-windows.zip" in workflow
    assert "37\\.0\\.3" in workflow
    assert "Google official Platform-Tools SHA256" in workflow
    spec = Path("build_full.spec").read_text(encoding="utf-8")

    # Only the self-contained installer workflow publishes a release build.
    assert "PyInstaller" in workflow
    assert "CN_PLAYHUB_Setup.exe" in workflow
    assert "SetupIconFile=assets\\app_icon.ico" in workflow
    assert 'title: "CN PlayHub"' in qml
    assert 'QIcon(":/assets/app_icon.png")' in current
    assert 'icon="assets/app_icon.ico"' in spec
    assert "pyside6-rcc" in workflow
    assert "ui_resources_rc" in workflow
    assert "python314.dll" in workflow
    assert "Qt6Core.dll" in workflow
    assert "--protected-build-smoke-test" in workflow
    assert "CN_PLAYHUB_Setup.exe" in workflow
    assert "Download official PUBG MOBILE FLASH icon" in workflow
    assert "Commit downloaded official PUBG MOBILE FLASH icon to project" in workflow
    assert "play-lh.googleusercontent.com" in workflow
    assert "from PIL import Image" in workflow
    assert 'format="PNG"' in workflow
    assert "git push origin" in workflow
    assert "contents: write" in workflow
    assert "D:\\games\\py" not in workflow

    # Loose application sources are embedded instead of copied as data.
    assert '"ui_resources_rc"' in spec
    assert '("platform-tools", "platform-tools")' in spec
    for loose_source_entry in (
        '("qml", "qml")',
        '("engine", "engine")',
        '("ui", "ui")',
        '("assets", "assets")',
        '("games.json", ".")',
    ):
        assert loose_source_entry not in spec

    readme = Path("README.md").read_text(encoding="utf-8")
    assert "does not need an external Python runtime" in readme


def run_once():
    controller = make_controller()
    test_discovery_and_selection(controller)
    test_device_sorting_uses_latest_first_seen(make_controller())
    test_usb_storage_is_merged_and_counted_as_one_unit()
    test_usb_copy_target_routes_to_local_drive_with_one_unit()
    test_local_usb_transfer_engine_copies_files_and_checks_free_space()
    test_usb_insufficient_space_error_is_classified_correctly()
    test_transfer_summary_excludes_usb_flash_and_counts_phone_copy()
    test_usb_storage_cannot_receive_android_install_commands()
    # Finish tests sharing the base controller before isolated tests reset the
    # FakeOperationEngine registry for their own controllers.
    test_zero_selection(controller)
    test_independent_operations(controller)
    test_transfer_history_and_completed_card_dismissal()
    test_copy_history_retry_review_and_storage_counters()
    test_legacy_history_migration_separates_installs_retries_and_duplicates()
    test_amount_based_install_randomly_selects_unique_game_folders()
    test_amount_200_uses_tariff_count_not_currency_as_game_count()
    test_amount_install_supports_intermediate_amounts()
    test_amount_install_scans_nested_game_subfolders()
    test_amount_install_discovers_xapk_as_one_package()
    test_adb_batch_installs_xapk_splits_and_copies_obb()
    test_amount_install_targets_only_primary_selected_phone()
    test_amount_install_uses_all_games_when_source_is_smaller_than_tier()
    test_installation_does_not_increment_copy_counters()
    test_pubg_source_size_selects_sibling_folder()
    test_android_copy_starts_without_preinstalling_companion_apps()
    test_shizuku_startup_supports_running_and_both_versions()
    test_pubg_flash_uses_fixed_source_and_exact_package_destination()
    test_uninstall_operation_is_logged_separately_from_copy()
    test_transfer_history_is_not_truncated_after_500_entries()
    test_direct_stream_rejects_source_size_changes()
    test_direct_stream_manifest_preserves_game_relative_paths()
    test_saved_copy_never_changes_permissions_or_writes_probe_files()
    test_fullscreen_button_requires_native_guard_and_restores_it()
    test_legacy_successful_phone_register_is_no_longer_used()
    test_launch_game_resolves_and_starts_installed_package()
    test_adb_game_close_is_silent_and_package_scoped()
    test_custom_source_keeps_game_specific_phone_destination(make_controller())
    test_busy_serial_and_idle_serial(make_controller())
    test_adb_source_arguments_and_progress_offset()
    test_failure_cancel_and_completion_isolation(make_controller())
    test_install_operations(make_controller())
    test_pause_resume_operations()
    test_oem_permission_guidance()
    test_adb_oem_compatibility_probe_reads_real_facts_without_writes()
    test_oem_compatibility_settings_toggle_and_disable_gate()
    test_versioned_games_discover_select_and_route_exact_folders()
    test_efootball_uses_saf_engine_and_never_starts_shizuku_fallback()
    test_gta_copy_refuses_to_guess_unverified_package_id()
    test_copy_parts_are_one_sequential_device_operation()
    test_secure_mkdirs_partial_directory_retry_is_scoped_to_saved()
    test_legacy_root_fallback_prompt_only_after_android_data_denial()
    test_duplicate_completion(make_controller())
    test_process_ownership()
    test_windows_task_manager_policy_is_scoped_and_restored()
    test_dashboard_discovers_new_source_folders_without_duplicate_known_cards()
    test_discovered_folder_package_ids_are_resolved_without_guessing()
    test_paths_and_ui()


def main():
    QApplication.instance() or QApplication([])
    run_once()
    run_once()
    print("RUN 1: ALL PASS")
    print("RUN 2: ALL PASS")


if __name__ == "__main__":
    main()
