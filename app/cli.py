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
        f"selected={result.get('batch_selected', 0)}/{result.get('batch_requested', 0)}",
        f"ready={result.get('ready', 0)} review={result.get('review', 0)} duplicates={result.get('duplicates_in_this_batch', 0)}",
        (
            f"reverse_image_unresponsive={result.get('reverse_image_unresponsive', 0)} "
            f"no_matches={result.get('reverse_image_no_matches', 0)} "
            f"reused_saved_evidence={result.get('reverse_image_previous_evidence_reused', 0)} "
            f"rejected_external_ids={result.get('reverse_image_rejected_external_ids', 0)}"
        ),
    ]
    if mode == "DRY_RUN":
        lines.append(
            f"planned_json_files={len(result.get('planned_output_files') or [])} "
            f"planned_image_moves={len(result.get('planned_image_moves') or [])} "
            f"planned_deletes={len(result.get('planned_source_image_deletes') or [])}"
        )
        lines.append("NOTE: DRY_RUN performs live Google Lens lookups and consumes free-tier requests.")
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


def check_brightdata_config(settings: Settings) -> int:
    settings.validate_brightdata()
    print("[KafiaNet] Bright Data configuration is present.")
    print("[KafiaNet] No network request was sent; API authentication and remaining free quota were not tested.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KafiaNet TMDB metadata batch worker")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "check-brightdata-config"],
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Write result JSON to the public poster repository")
    parser.add_argument("--dry-run", action="store_true", help="Explicitly force read-only catalog planning mode")
    parser.add_argument("--verbose", action="store_true", help="Print the full JSON report")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "check-brightdata-config":
        if args.limit is not None or args.apply or args.dry_run or args.verbose:
            parser.error("check-brightdata-config does not accept batch or write options")
        return check_brightdata_config(settings)
    apply = bool(args.apply and not args.dry_run)
    settings.validate_for_processing(apply=apply)
    limit = args.limit or settings.batch_limit
    engine = MetadataEngine(settings)
    try:
        result = engine.run_batch(limit=limit, apply=apply)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.verbose else format_summary(result))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
