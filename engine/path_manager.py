import json
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = BASE_DIR / "paths.json"


class PathManager:

    def __init__(self):

        self.usb_root = None
        self.android_root = None

        self.load_defaults()


    def load_defaults(self):

        if not CONFIG_FILE.exists():
            return

        with open(
            CONFIG_FILE,
            "r",
            encoding="utf-8"
        ) as file:

            config = json.load(file)

        self.usb_root = Path(
            config.get("source_root", "")
        )

        self.android_root = Path(
            config.get("destination_root", "")
        )


    def set_usb_root(self, path):

        self.usb_root = Path(path)


    def set_android_root(self, path):

        self.android_root = Path(path)


    def source_path(self, relative_path):

        if not self.usb_root:
            raise RuntimeError(
                "لم يتم تحديد المصدر"
            )

        return self.usb_root / relative_path


    def destination_path(self, relative_path):

        if not self.android_root:
            raise RuntimeError(
                "لم يتم تحديد الوجهة"
            )

        return self.android_root / relative_path
