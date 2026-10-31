from __future__ import annotations

import argparse
import json
from typing import Any

from app.config import Settings
from app.engine import MetadataEngine


def format_summary(result: dict[str, Any]) -> str:
    """Return a small terminal summary without per-film JSON or file-path arrays."""
    mode = str(result.get("mode") or "UNKNOWN")
    lines = [
        "[KafiaNet] BATCH SUMMARY",
        f"mode={mode}",
        (
            f"selected={result.get('batch_selected', 0)}/"
            f"{result.get('batch_requested', 0)}"
        ),
        (
            f"ready={result.get('ready', 0)} "
            f"review={result.get('review', 0)} "
            f"duplicates={result.get('duplicates_in_this_batch', 0)}"
        ),
    ]

    if mode == "DRY_RUN":
        lines.append(
            "planned_json_files="
            f"{len(result.get('planned_output_files') or [])} "
            "planned_image_moves="
            f"{len(result.get('planned_image_moves') or [])} "
            "planned_deletes="
            f"{len(result.get('planned_source_image_deletes') or [])}"
        )
    else:
        lines.append(
            f"written_files={result.get('written_files', 0)} "
            f"moved_image_files={result.get('moved_image_files', 0)} "
            f"deleted_old_files={result.get('deleted_old_files', 0)}"
        )

    if result.get("commit_sha"):
        lines.append(f"commit_sha={result['commit_sha']}")
    if "next_pending_estimate" in result:
        lines.append(f"next_pending_estimate={result['next_pending_estimate']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="KafiaNet TMDB metadata batch worker")
    parser.add_argument("run", nargs="?", default="run", choices=["run"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Write result JSON to the public poster repository")
    parser.add_argument("--dry-run", action="store_true", help="Explicitly force read-only planning mode")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print the full JSON report (large output; normally keep disabled)",
    )
    args = parser.parse_args()

    apply = bool(args.apply and not args.dry_run)
    settings = Settings.from_env()
    settings.validate_for_processing(apply=apply)
    limit = args.limit or settings.batch_limit

    engine = MetadataEngine(settings)
    try:
        result = engine.run_batch(limit=limit, apply=apply)
        if args.verbose:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            print(format_summary(result))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
