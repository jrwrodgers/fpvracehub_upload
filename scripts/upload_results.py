#!/usr/bin/env python3
"""Upload RotorHazard export JSON to FPV Race Hub (structure, append, or full)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT / "custom_plugins" / "fpvracehub_upload"
sys.path.insert(0, str(PLUGIN_DIR))

from api_client import upload_results  # noqa: E402
from state import extract_runs_payload, extract_structure_payload  # noqa: E402


def _load_payload(path: Path, mode: str, race_ids: list[int] | None) -> dict:
    with path.open(encoding="utf-8") as f:
        source = json.load(f)

    if mode == "structure":
        return extract_structure_payload(source)
    if mode == "append":
        if race_ids:
            payload = extract_runs_payload(source, set(race_ids))
            if not payload.get("SavedRaceMeta"):
                raise ValueError(f"No SavedRaceMeta rows matched race ids {race_ids}")
            return payload
        if not source.get("SavedRaceMeta"):
            raise ValueError(
                "Append requires SavedRaceMeta in the file or --race-ids with a full export"
            )
        return source
    return source


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload RotorHazard JSON to FPV Race Hub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/upload_results.py --event-id 3 --api-key KEY --file export.json --mode structure
  python scripts/upload_results.py --event-id 3 --api-key KEY --file export.json --mode append --race-ids 61 62
  python scripts/upload_results.py --event-id 3 --api-key KEY --file export.json --mode full
        """,
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="API base URL")
    parser.add_argument("--event-id", type=int, required=True, help="Hub event ID")
    parser.add_argument("--api-key", required=True, help="Event upload API key")
    parser.add_argument("--file", required=True, type=Path, help="RotorHazard export JSON")
    parser.add_argument(
        "--mode",
        default="structure",
        choices=["structure", "append", "full"],
        help="structure | append | full (full replaces entire hub event)",
    )
    parser.add_argument(
        "--race-ids",
        nargs="+",
        type=int,
        metavar="ID",
        help="SavedRaceMeta.id values to extract when --mode append (from a full export)",
    )
    args = parser.parse_args()

    if args.mode != "append" and args.race_ids:
        print("--race-ids is only valid with --mode append", file=sys.stderr)
        return 1

    try:
        data = _load_payload(args.file, args.mode, args.race_ids)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.mode == "structure" and not data.get("RaceClass") and not data.get("Heat"):
        print("No RaceClass or Heat rows found in export file.", file=sys.stderr)
        return 1

    success, detail = upload_results(
        args.url.rstrip("/"),
        args.event_id,
        args.api_key,
        data,
        mode=args.mode,
    )

    if success:
        print("Upload successful:")
        print(detail)
        return 0

    print(detail, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
