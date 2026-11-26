"""Phone-to-PC pull skips existing files and reports live byte progress."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.adb_engine import ADBEngine  # noqa: E402


def test_pull_skips_existing_files_and_reports_progress():
    with tempfile.TemporaryDirectory(prefix="kafianet_pull_transfer_") as temporary:
        root = Path(temporary)
        remote_root = (
            "/sdcard/Android/data/com.tencent.ig/files/UE4Game/"
            "ShadowTrackerExtra/ShadowTrackerExtra/Saved"
        )
        comparison = root / "ببجي (PUBG)" / "Saved"
        destination = root / "من الهاتف" / "PUBG" / "Saved"
        comparison.mkdir(parents=True)
        destination.mkdir(parents=True)

        # One file already exists in the original PC PUBG folder.
        (comparison / "main-existing.bin").write_bytes(b"main-original")
        # Another file was recovered in an earlier run; never replace it.
        (destination / "recovered-earlier.bin").write_bytes(b"old-recovery")

        manifest = "\n".join((
            f"13|{remote_root}/main-existing.bin",
            f"13|{remote_root}/recovered-earlier.bin",
            f"6|{remote_root}/new-from-phone.bin",
        ))

        engine = ADBEngine()
        progress = []
        completed = []
        engine.progressChanged.connect(
            lambda percent, speed, eta: progress.append(percent)
        )
        engine.finished.connect(
            lambda success, message: completed.append((success, message))
        )

        def fake_run_pull_process(
            device, args, timeout=6 * 60 * 60, progress_callback=None
        ):
            assert device == "SERIAL_TEST"
            if args[0] == "shell":
                assert "find " in args[1] and "stat -c" in args[1]
                return 0, manifest

            assert args[0] == "pull", args
            assert args[1] == f"{remote_root}/new-from-phone.bin"
            temporary_file = Path(args[2])
            payload = b"phone!"
            with temporary_file.open("wb") as output:
                for end in (2, 4, 6):
                    output.write(payload[end - (2 if end < 6 else 2):end])
                    output.flush()
                    if progress_callback is not None:
                        progress_callback()
            return 0, "1 file pulled"

        engine._run_pull_process = fake_run_pull_process
        engine._pull_thread(
            "SERIAL_TEST",
            remote_root,
            str(destination),
            str(comparison),
        )

        assert completed and completed[-1][0] is True, completed
        assert engine.pull_stats == {
            "copied_files": 1,
            "skipped_files": 2,
            "total_files": 3,
            "total_bytes": 32,
        }, engine.pull_stats
        assert (comparison / "main-existing.bin").read_bytes() == b"main-original"
        assert (
            destination / "recovered-earlier.bin"
        ).read_bytes() == b"old-recovery"
        assert (
            destination / "new-from-phone.bin"
        ).read_bytes() == b"phone!"
        assert not (comparison / "new-from-phone.bin").exists()

        # Progress must advance while the file is being transferred, never
        # exceed 99 before successful completion, and only then report 100.
        assert progress, progress
        assert progress[-1] == 100, progress
        assert any(0 < value < 100 for value in progress[:-1]), progress
        assert progress == sorted(progress), progress
        assert "1 ملف جديد" in completed[-1][1]
        assert "2 ملف موجود مسبقًا" in completed[-1][1]


def test_pull_refuses_to_start_when_required_comparison_root_is_missing():
    with tempfile.TemporaryDirectory(prefix="kafianet_pull_preflight_") as temporary:
        root = Path(temporary)
        destination = root / "من الهاتف" / "PUBG" / "Saved"
        missing_comparison = root / "ببجي (PUBG)" / "Saved"
        engine = ADBEngine()
        completed = []
        engine.finished.connect(
            lambda success, message: completed.append((success, message))
        )

        engine._pull_thread(
            "SERIAL_TEST",
            "/sdcard/Android/data/com.tencent.ig/files/Saved",
            str(destination),
            str(missing_comparison),
        )

        assert completed and completed[-1][0] is False, completed
        assert "مجلد المقارنة غير موجود" in completed[-1][1]
        assert not destination.exists()


def test_pull_manifest_rejects_paths_outside_the_remote_game_folder():
    root = "/sdcard/Android/data/com.tencent.ig/files/Saved"
    try:
        ADBEngine._parse_pull_manifest(
            f"4|/sdcard/Android/data/com.tencent.ig/files/Other/escape.bin",
            root,
        )
    except RuntimeError as exc:
        assert "خارج مجلد اللعبة" in str(exc)
    else:
        raise AssertionError("An out-of-root phone path was accepted.")


def main():
    test_pull_skips_existing_files_and_reports_progress()
    test_pull_refuses_to_start_when_required_comparison_root_is_missing()
    test_pull_manifest_rejects_paths_outside_the_remote_game_folder()
    print("PHONE PULL SKIP-EXISTING / LIVE BYTE PROGRESS: PASS")


if __name__ == "__main__":
    main()
