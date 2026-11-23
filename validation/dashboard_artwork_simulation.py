import sys
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from engine.game_config import discover_dashboard_games


def _make_file(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"image fixture")
    return path


def main():
    with TemporaryDirectory(prefix="kafianet-dashboard-artwork-") as temporary:
        base = Path(temporary)
        source_root = base / "server-games"
        flash_root = base / "flash-games"
        source_root.mkdir()
        flash_root.mkdir()

        fortnite_image = _make_file(source_root / "Fortnite" / "cover.jpg")
        # Versioned games may keep their cover inside a version subfolder.
        efootball_image = _make_file(
            source_root / "eFootball" / "2026" / "folder.png"
        )
        flash_image = _make_file(
            flash_root / "PUBG FLASH" / "poster.webp"
        )

        # A static bundled image must not be used when the game folder has no
        # supported image. Video files are not considered artwork.
        genshin_folder = source_root / "Genshin Impact"
        genshin_folder.mkdir()
        (genshin_folder / "trailer.mp4").write_bytes(b"not an image")

        # Dynamic/custom cards keep using artwork from their own folders.
        custom_image = _make_file(source_root / "My Custom Game" / "banner.bmp")

        cards = discover_dashboard_games(
            source_root,
            flash_source_root=flash_root,
        )
        by_name = {item["name"]: item for item in cards}

        assert by_name["Fortnite"]["imageSource"] == fortnite_image.resolve().as_uri()
        assert by_name["eFootball"]["imageSource"] == efootball_image.resolve().as_uri()
        assert by_name["PUBG Mobile Flash"]["imageSource"] == flash_image.resolve().as_uri()
        assert by_name["Genshin Impact"]["imageSource"] == ""
        assert by_name["My Custom Game"]["imageSource"] == custom_image.resolve().as_uri()
        assert len(cards) == 9  # Eight fixed cards plus the discovered custom folder.
        assert sum(bool(item.get("isCustom")) for item in cards) == 1

        # Fixed cards stay in the dashboard even when their folders are absent,
        # but they no longer point at bundled static artwork.
        assert by_name["PUBG Mobile"]["imageSource"] == ""
        assert all(
            not str(item.get("imageSource") or "").startswith("../../assets/games/")
            for item in cards
        )

    print("DASHBOARD ARTWORK DISCOVERY SIMULATION: OK")


if __name__ == "__main__":
    main()
