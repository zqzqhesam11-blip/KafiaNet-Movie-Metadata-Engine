"""Regression checks: phone-to-PC paths mirror the exact PC-to-phone game source."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_qml import AppController  # noqa: E402


def controller_for(source_root, destination_root, pubg_size="Saved"):
    return SimpleNamespace(
        source_root=source_root,
        destination_root=destination_root,
        _pubg_source_size=pubg_size,
        _find_case_insensitive_child_directory=(
            AppController._find_case_insensitive_child_directory
        ),
    )


def destination_for(controller, game_name):
    return AppController._pull_destination_for_game(controller, game_name)


def main():
    with tempfile.TemporaryDirectory(prefix="kafianet_pull_paths_") as temporary:
        root = Path(temporary)
        source_root = root / "source"
        destination_root = root / "D_games"
        pubg = source_root / "PUBG"
        local_pubg = destination_root / "ببجي (PUBG)"

        # Default/full Saved source and the explicit read-only comparison root.
        (pubg / "Saved").mkdir(parents=True)
        (local_pubg / "Saved").mkdir(parents=True)
        controller = controller_for(source_root, destination_root, "Saved")
        actual = destination_for(controller, "PUBG Mobile")
        expected = destination_root / "من الهاتف" / "PUBG" / "Saved"
        assert actual == expected, (actual, expected)
        comparison = AppController._pull_comparison_for_game(
            controller, "PUBG Mobile"
        )
        assert comparison == local_pubg / "Saved", comparison

        # Medium has a nested Saved folder: pull must mirror what copyGame()
        # selects, not flatten it to PUBG/Saved.
        (pubg / "Medium" / "Saved").mkdir(parents=True)
        (local_pubg / "Medium" / "Saved").mkdir(parents=True)
        controller = controller_for(source_root, destination_root, "Medium")
        actual = destination_for(controller, "PUBG Mobile")
        expected = destination_root / "من الهاتف" / "PUBG" / "Medium" / "Saved"
        assert actual == expected, (actual, expected)
        comparison = AppController._pull_comparison_for_game(
            controller, "PUBG Mobile"
        )
        assert comparison == local_pubg / "Medium" / "Saved", comparison

        # Low without nested Saved mirrors the Low folder itself.
        (pubg / "Low").mkdir()
        (local_pubg / "Low").mkdir(parents=True)
        controller = controller_for(source_root, destination_root, "Low")
        actual = destination_for(controller, "PUBG Mobile")
        expected = destination_root / "من الهاتف" / "PUBG" / "Low"
        assert actual == expected, (actual, expected)
        comparison = AppController._pull_comparison_for_game(
            controller, "PUBG Mobile"
        )
        assert comparison == local_pubg / "Low", comparison

        # A selected size that does not exist must fail preflight rather than
        # silently pulling into a different game's/size's destination.
        controller = controller_for(source_root, destination_root, "Ultra")
        assert destination_for(controller, "PUBG Mobile") is None

        # Non-PUBG game destinations preserve the same source-relative folder
        # and the package subdirectories already appended by copyGame().
        free_fire = source_root / "free fire" / "com.dts.freefireth"
        free_fire.mkdir(parents=True)
        controller = controller_for(source_root, destination_root)
        actual = destination_for(controller, "Free Fire")
        expected = (
            destination_root / "من الهاتف" / "free fire"
            / "com.dts.freefireth"
        )
        assert actual == expected, (actual, expected)

        cod = source_root / "call of duty" / "com.activision.callofduty.shooter"
        cod.mkdir(parents=True)
        actual = destination_for(controller, "Call of Duty Mobile")
        expected = (
            destination_root / "من الهاتف" / "call of duty"
            / "com.activision.callofduty.shooter"
        )
        assert actual == expected, (actual, expected)

    print("PHONE PULL PATH MAPPING / PUBG SIZE MIRROR: PASS")


if __name__ == "__main__":
    main()
