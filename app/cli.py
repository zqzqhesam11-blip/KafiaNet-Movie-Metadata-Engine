from __future__ import annotations

import argparse
import json

from app.config import Settings
from app.engine import MetadataEngine


def main() -> int:
    parser = argparse.ArgumentParser(description="KafiaNet TMDB metadata batch worker")
    parser.add_argument("run", nargs="?", default="run", choices=["run"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Write result JSON to the public poster repository")
    parser.add_argument("--dry-run", action="store_true", help="Explicitly force read-only planning mode")
    args = parser.parse_args()

    apply = bool(args.apply and not args.dry_run)
    settings = Settings.from_env()
    settings.validate_for_processing(apply=apply)
    limit = args.limit or settings.batch_limit

    engine = MetadataEngine(settings)
    try:
        result = engine.run_batch(limit=limit, apply=apply)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
