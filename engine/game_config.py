import hashlib
import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "games.json"


def _load_embedded_games():
    try:
        import ui_resources_rc
        from PySide6.QtCore import QFile, QIODevice
    except ImportError:
        return None

    embedded_file = QFile(":/games.json")
    if not embedded_file.open(QIODevice.OpenModeFlag.ReadOnly):
        return None

    try:
        payload = bytes(embedded_file.readAll()).decode("utf-8-sig")
        return json.loads(payload)
    finally:
        embedded_file.close()


def load_games():
    embedded = _load_embedded_games()
    if embedded is not None:
        return embedded

    with open(
        CONFIG_FILE,
        "r",
        encoding="utf-8"
    ) as file:
        return json.load(file)


def get_game(name):

    return load_games().get(name)


def get_games():

    return load_games()


def find_game_folder(source_root, game_name):

    root = Path(source_root)

    if not root.exists():
        return None

    aliases = {
        "Fortnite": [
            "فورتنايت (Fortnite)",
            "Fortnite"
        ],

        "Genshin Impact": [
            "جينشن امباكت (Genshin Impact)",
            "Genshin Impact"
        ],

        "eFootball": [
            "بيس (eFootball™)",
            "eFootball",
            "eFootball™"
        ],

        "Free Fire": [
            "فري فاير (free fire)",
            "free fire",
            "Free Fire"
        ],

        "PUBG Mobile": [
            "ببجي (PUBG)",
            "PUBG",
            "PUBG Mobile"
        ],

        "PUBG Mobile Flash": [
            "ببجي (PUBG FLASH)",
            "PUBG FLASH",
            "PUBG MOBILE FLASH",
            "PUBG Mobile Flash"
        ],

        "Call of Duty Mobile": [
            "كول اوف ديوتي (call of duty)",
            "call of duty",
            "Call of Duty"
        ],

        "Grand Theft Auto": [
            "جراند ثفت اوتو (Grand Theft Auto)",
            "Grand Theft Auto",
            "GTA"
        ]
    }

    names = aliases.get(
        game_name,
        [game_name]
    )

    folders = {
        item.name: item
        for item in root.iterdir()
        if item.is_dir()
    }

    for name in names:

        if name in folders:
            return folders[name]

    lowered = {
        item.name.lower(): item
        for item in folders.values()
    }

    for name in names:

        result = lowered.get(name.lower())

        if result:
            return result

    return None



DASHBOARD_GAME_VISUALS = {
    "Fortnite": {"color": "#2D8CFF"},
    "Genshin Impact": {"color": "#8A5CFF"},
    "eFootball": {"color": "#00B894"},
    "Free Fire": {"color": "#FF9F1C"},
    "PUBG Mobile": {"color": "#F5C542"},
    "PUBG Mobile Flash": {"color": "#28A8F4"},
    "Call of Duty Mobile": {"color": "#FF4D4D"},
    "Grand Theft Auto": {"color": "#9B59B6"},
}

DASHBOARD_GAME_ORDER = (
    "Fortnite", "Genshin Impact", "eFootball", "Free Fire",
    "PUBG Mobile", "PUBG Mobile Flash", "Call of Duty Mobile",
    "Grand Theft Auto",
)

_DISCOVERY_IGNORED_FOLDERS = {
    "$recycle.bin", "system volume information", ".git", "__pycache__",
    "تطبيقات للنسخ", "من الهاتف", "تثبيت",
}


_DASHBOARD_IMAGE_EXTENSIONS = frozenset(
    (".png", ".jpg", ".jpeg", ".webp", ".bmp")
)
_DASHBOARD_ARTWORK_IGNORED_CHILD_DIRS = frozenset(
    (
        "$recycle.bin", "system volume information", ".git", "__pycache__",
        "android", "data", "obb", "cache", "saved", "temp", "tmp",
    )
)
_DASHBOARD_PREFERRED_IMAGE_NAMES = (
    "folder.png", "folder.jpg", "folder.jpeg", "folder.webp",
    "cover.png", "cover.jpg", "cover.jpeg", "cover.webp",
    "poster.png", "poster.jpg", "poster.jpeg", "poster.webp",
    "icon.png", "icon.jpg", "icon.jpeg", "icon.webp",
    "banner.png", "banner.jpg", "banner.jpeg", "banner.webp",
)


def _dashboard_artwork(folder):
    """Find a local cover at the game-folder root or one level below it.

    Only image files are considered. The search is deliberately bounded so
    large game-data trees are never walked just to paint a dashboard card.
    """
    folder = Path(folder)
    try:
        root = folder.resolve(strict=True)
        if not root.is_dir():
            return ""
    except (OSError, RuntimeError, TypeError, ValueError):
        return ""

    def image_uri(candidate):
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
            if (
                resolved.is_file()
                and resolved.suffix.casefold() in _DASHBOARD_IMAGE_EXTENSIONS
            ):
                return resolved.as_uri()
        except (OSError, RuntimeError, ValueError):
            pass
        return ""

    def first_image(directory):
        try:
            candidates = sorted(
                (
                    item for item in directory.iterdir()
                    if item.is_file()
                    and item.suffix.casefold() in _DASHBOARD_IMAGE_EXTENSIONS
                ),
                key=lambda item: item.name.casefold(),
            )
        except OSError:
            return ""

        for candidate in candidates:
            uri = image_uri(candidate)
            if uri:
                return uri
        return ""

    # Named cover files in the game folder always have first priority.
    for name in _DASHBOARD_PREFERRED_IMAGE_NAMES:
        uri = image_uri(folder / name)
        if uri:
            return uri

    # Otherwise use the first image directly inside the game folder.
    uri = first_image(folder)
    if uri:
        return uri

    # Some libraries keep covers alongside version folders. Search only
    # immediate child folders, newest/name-last first, and skip data trees.
    try:
        child_folders = []
        for item in folder.iterdir():
            if item.name.casefold() in _DASHBOARD_ARTWORK_IGNORED_CHILD_DIRS:
                continue
            if item.name.startswith(".") or not item.is_dir():
                continue
            try:
                resolved = item.resolve(strict=True)
                resolved.relative_to(root)
                if resolved.is_dir():
                    child_folders.append(item)
            except (OSError, RuntimeError, ValueError):
                continue
    except OSError:
        child_folders = []

    child_folders.sort(key=lambda item: item.name.casefold(), reverse=True)

    for child in child_folders:
        for name in _DASHBOARD_PREFERRED_IMAGE_NAMES:
            uri = image_uri(child / name)
            if uri:
                return uri

    for child in child_folders:
        uri = first_image(child)
        if uri:
            return uri

    return ""


def _discovered_card_color(folder_name):
    digest = hashlib.sha1(str(folder_name).encode("utf-8", errors="replace")).digest()
    palette = (
        "#00D9FF", "#8A5CFF", "#00B894", "#FF9F1C",
        "#F5C542", "#28A8F4", "#FF4D4D", "#9B59B6",
        "#3FA7D6", "#C06C84", "#6C9A8B", "#C28E5C",
    )
    return palette[int.from_bytes(digest[:4], "big") % len(palette)]


def discover_dashboard_games(source_root, flash_source_root=None):
    """Return the original built-in cards plus newly discovered top-level folders.

    The original eight cards remain first with their existing artwork/colors.
    Scanning is limited to the selected source root's first level so nested
    versions and data directories do not become duplicate cards.
    """
    root = Path(source_root)
    flash_root = Path(flash_source_root) if flash_source_root else root
    game_config = get_games()
    claimed_paths = set()
    used_names = set()
    cards = []

    for name in DASHBOARD_GAME_ORDER:
        config = game_config.get(name) or {}
        lookup_root = flash_root if name == "PUBG Mobile Flash" else root
        source_folder = find_game_folder(lookup_root, name)
        if source_folder is None:
            source_folder = lookup_root / str(config.get("source") or name)

        try:
            exists = source_folder.is_dir()
            if exists:
                claimed_paths.add(source_folder.resolve())
        except OSError:
            exists = False

        visual = DASHBOARD_GAME_VISUALS[name]
        cards.append({
            "name": name,
            "imageSource": _dashboard_artwork(source_folder) if exists else "",
            "color": visual["color"],
            "sourcePath": str(source_folder),
            "isCustom": False,
            "sourceExists": exists,
        })
        used_names.add(name.casefold())

    try:
        discovered_folders = sorted(
            (item for item in root.iterdir() if item.is_dir()),
            key=lambda item: item.name.casefold(),
        ) if root.is_dir() else []
    except OSError:
        discovered_folders = []

    try:
        resolved_root = root.resolve()
    except OSError:
        resolved_root = root

    for folder in discovered_folders:
        name = folder.name.strip()
        if not name or name.casefold() in _DISCOVERY_IGNORED_FOLDERS:
            continue
        if name.casefold() in used_names:
            continue
        try:
            resolved = folder.resolve()
            resolved.relative_to(resolved_root)
        except (OSError, ValueError):
            # Never expose a symlink whose real target is outside the selected root.
            continue
        if resolved in claimed_paths:
            continue

        cards.append({
            "name": name,
            "imageSource": _dashboard_artwork(folder),
            "color": _discovered_card_color(name),
            "sourcePath": str(resolved),
            "isCustom": True,
            "sourceExists": True,
        })
        used_names.add(name.casefold())
        claimed_paths.add(resolved)

    return cards
