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
            f"official_id_backlinks={result.get('reverse_image_official_id_backlinks', 0)} "
            f"resolved_identities={result.get('reverse_image_resolved_identities', 0)} "
            f"identity_conflicts={result.get('reverse_image_identity_conflicts', 0)}"
        ),
    ]
    if mode == "DRY_RUN":
        lines.append(
            f"planned_json_files={len(result.get('planned_output_files') or [])} "
            f"planned_image_moves={len(result.get('planned_image_moves') or [])} "
            f"planned_deletes={len(result.get('planned_source_image_deletes') or [])}"
        )
        if result.get("plan_file"):
            lines.append(f"plan_file={result['plan_file']} plan_id={result.get('plan_id', 'unknown')}")
        lines.append(
            "NOTE: DRY_RUN performs live Google Lens lookups. Exact Lens titles first get one "
            "TMDB exact-title verification; Google Search fallback runs only when that result is "
            "missing, incomplete, or ambiguous. Provider requests consume quota."
        )
        lines.append(
            "NOTE: Local poster OCR is disabled by default (LOCAL_OCR_ENABLED=false); "
            "it is not initialized unless explicitly enabled."
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
    parser.add_argument(
        "--plan-out",
        type=str,
        default=None,
        help="Save the full DRY_RUN decision/evidence plan to a local JSON file",
    )
    parser.add_argument(
        "--apply-plan",
        type=str,
        default=None,
        help="Apply a saved DRY_RUN plan without Lens, Google Search, or TMDB calls",
    )
    parser.add_argument(
        "--refresh-evidence",
        action="store_true",
        help="Explicitly rerun Google Lens/TMDB for unchanged REVIEW posters",
    )
    args = parser.parse_args()
    settings = Settings.from_env()
    if args.command == "check-brightdata-config":
        if (
            args.limit is not None or args.apply or args.dry_run or args.verbose
            or args.plan_out or args.apply_plan or args.refresh_evidence
        ):
            parser.error("check-brightdata-config does not accept batch or write options")
        return check_brightdata_config(settings)
    if args.plan_out and (args.apply or args.apply_plan):
        parser.error("--plan-out is DRY_RUN-only; do not combine it with --apply or --apply-plan")
    if args.apply_plan and (not args.apply or args.dry_run):
        parser.error("--apply-plan requires --apply and cannot be combined with --dry-run")
    if args.apply_plan and args.refresh_evidence:
        parser.error("--refresh-evidence cannot be combined with --apply-plan")

    apply = bool(args.apply and not args.dry_run)
    settings.validate_for_processing(
        apply=apply,
        require_research=not bool(args.apply_plan),
    )
    limit = args.limit or settings.batch_limit
    engine = MetadataEngine(settings, research_enabled=not bool(args.apply_plan))
    try:
        result = engine.run_batch(
            limit=limit,
            apply=apply,
            plan_out=args.plan_out,
            apply_plan_path=args.apply_plan,
            refresh_evidence=args.refresh_evidence,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.verbose else format_summary(result))
        return 0
    finally:
        engine.close()


if __name__ == "__main__":
    raise SystemExit(main())
