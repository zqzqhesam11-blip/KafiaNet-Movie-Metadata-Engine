from __future__ import annotations

import argparse
import json
from typing import Any

from app.config import Settings
from app.engine import MetadataEngine
from app.tineye_api import TinEyeAPIClient


def format_summary(result: dict[str, Any]) -> str:
    """Return a small terminal summary without per-film JSON or file-path arrays."""
    mode = str(result.get("mode") or "UNKNOWN")
    lines = [
        "[KafiaNet] BATCH SUMMARY",
        f"mode={mode}",
        f"selected={result.get('batch_selected', 0)}/{result.get('batch_requested', 0)}",
        f"ready={result.get('ready', 0)} review={result.get('review', 0)} duplicates={result.get('duplicates_in_this_batch', 0)}",
        (
            f"tineye_unresponsive={result.get('tineye_unresponsive', 0)} "
            f"no_matches={result.get('tineye_no_matches', 0)} "
            f"reused_saved_evidence={result.get('tineye_previous_evidence_reused', 0)} "
            f"rejected_external_ids={result.get('tineye_rejected_external_ids', 0)}"
        ),
    ]
    if mode == "DRY_RUN":
        lines.append(
            f"planned_json_files={len(result.get('planned_output_files') or [])} "
            f"planned_image_moves={len(result.get('planned_image_moves') or [])} "
            f"planned_deletes={len(result.get('planned_source_image_deletes') or [])}"
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


def check_tineye(settings: Settings) -> int:
    settings.validate_tineye()
    client = TinEyeAPIClient(settings.tineye_api_key, timeout=max(45, settings.http_timeout_seconds))
    try:
        account = client.remaining_searches()
    finally:
        client.close()
    total = account.get("total_remaining_searches")
    bundles = account.get("bundles") or []
    print("[KafiaNet] TinEye API connection: OK")
    if total is not None:
        print(f"[KafiaNet] remaining_searches={total}")
    else:
        print(f"[KafiaNet] active_bundles={len(bundles)}")
    for bundle in bundles:
        print(f"[KafiaNet] bundle_remaining={bundle.get('remaining_searches')} expires={bundle.get('expire_date')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="KafiaNet TMDB metadata batch worker")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "check-tineye"])
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--apply", action="store_true", help="Write result JSON to the public poster repository")
    parser.add_argument("--dry-run", action="store_true", help="Explicitly force read-only planning mode")
    parser.add_argument("--verbose", action="store_true", help="Print the full JSON report")
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "check-tineye":
        if args.limit is not None or args.apply or args.dry_run or args.verbose:
            parser.error("check-tineye does not accept batch or write options")
        return check_tineye(settings)
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
