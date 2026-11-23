"""Regression checks for ordinary USB copy safety (no overwrite, skip existing)."""
import os
import sys
import tempfile
from pathlib import Path

# Running a script by path places validation/ (not the repository root) on
# sys.path. Add the project root so engine.* imports work in CI and locally.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.transfer_engine import TransferEngine


def run_copy(engine, source, destination):
    results = []
    engine.finished.connect(lambda success, message: results.append((success, message)))
    engine._run(str(source), str(destination))
    assert results, "TransferEngine did not emit finished"
    return results[-1]


def main():
    with tempfile.TemporaryDirectory(prefix="kafianet_usb_copy_test_") as temporary:
        root = Path(temporary)
        source = root / "source"
        destination = root / "flash"
        (source / "nested").mkdir(parents=True)
        destination.mkdir()

        (source / "already-there.bin").write_bytes(b"new-data-must-not-replace")
        (source / "fresh.bin").write_bytes(b"fresh-data")
        expected_mtime = 1_700_000_000
        os.utime(source / "fresh.bin", (expected_mtime, expected_mtime))
        (source / "nested" / "empty.bin").write_bytes(b"")
        (destination / "already-there.bin").write_bytes(b"preserve-existing-data")

        engine = TransferEngine()
        success, message = run_copy(engine, source, destination)
        assert success, message
        assert (destination / "already-there.bin").read_bytes() == b"preserve-existing-data"
        assert (destination / "fresh.bin").read_bytes() == b"fresh-data"
        assert int((destination / "fresh.bin").stat().st_mtime) == int(
            (source / "fresh.bin").stat().st_mtime
        )
        assert (destination / "nested" / "empty.bin").read_bytes() == b""
        assert engine.copy_stats == {"copied_files": 2, "skipped_files": 1}, engine.copy_stats

        # A second run must skip every file and must not inflate successful-copy totals.
        success, message = run_copy(engine, source, destination)
        assert success, message
        assert engine.copy_stats == {"copied_files": 0, "skipped_files": 3}, engine.copy_stats
        assert (destination / "already-there.bin").read_bytes() == b"preserve-existing-data"

        # Single-file mode must also use exclusive-create semantics.
        single_source = root / "single-source.bin"
        single_target = root / "single-target.bin"
        single_source.write_bytes(b"new")
        single_target.write_bytes(b"old")
        success, message = run_copy(engine, single_source, single_target)
        assert success, message
        assert single_target.read_bytes() == b"old"
        assert engine.copy_stats == {"copied_files": 0, "skipped_files": 1}, engine.copy_stats

    print("USB NO-OVERWRITE / SKIP SIMULATION: PASS")


if __name__ == "__main__":
    main()
