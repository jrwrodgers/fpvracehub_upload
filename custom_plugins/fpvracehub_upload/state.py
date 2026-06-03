"""
Local sync state for FPV Race Hub structure/append uploads.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from RHAPI import RHAPI

logger = logging.getLogger(__name__)

OPTION_STRUCTURE_GENERATION = "fpvrh_structure_generation"
OPTION_LAST_STRUCTURE_PUSHED = "fpvrh_last_structure_pushed"
OPTION_PUSHED_RACE_META_IDS = "fpvrh_pushed_race_meta_ids"

STRUCTURE_KEYS = (
    "Pilot",
    "RaceClass",
    "RaceFormat",
    "Heat",
    "HeatNode",
    "Profiles",
    "GlobalSettings",
)


def extract_structure_payload(full_data: dict[str, Any]) -> dict[str, Any]:
    """Strip a full RH export to structure-mode keys only."""
    payload: dict[str, Any] = {}
    for key in STRUCTURE_KEYS:
        rows = full_data.get(key)
        if rows:
            payload[key] = rows
    return payload


def extract_runs_payload(
    full_data: dict[str, Any], race_meta_ids: set[int]
) -> dict[str, Any]:
    """Build append-mode payload for one or more SavedRaceMeta ids."""
    meta_rows = [
        row
        for row in full_data.get("SavedRaceMeta", []) or []
        if row.get("id") in race_meta_ids
    ]
    if not meta_rows:
        return {}

    pilot_race_rows = [
        row
        for row in full_data.get("SavedPilotRace", []) or []
        if row.get("race_id") in race_meta_ids
    ]
    pilot_race_ids = {row.get("id") for row in pilot_race_rows if row.get("id") is not None}

    lap_rows = [
        row
        for row in full_data.get("SavedRaceLap", []) or []
        if row.get("pilotrace_id") in pilot_race_ids
    ]

    format_ids = {row.get("format_id") for row in meta_rows if row.get("format_id") is not None}
    race_formats = [
        row
        for row in full_data.get("RaceFormat", []) or []
        if row.get("id") in format_ids
    ]

    pilot_ids = {row.get("pilot_id") for row in pilot_race_rows if row.get("pilot_id") is not None}
    pilots = [
        row
        for row in full_data.get("Pilot", []) or []
        if row.get("id") in pilot_ids
    ]

    payload: dict[str, Any] = {
        "SavedRaceMeta": meta_rows,
        "SavedPilotRace": pilot_race_rows,
        "SavedRaceLap": lap_rows,
    }
    if race_formats:
        payload["RaceFormat"] = race_formats
    if pilots:
        payload["Pilot"] = pilots
    return payload


def compute_structure_fingerprint(full_data: dict[str, Any]) -> int:
    """
    Hash setup-relevant tables so we can detect when structure must be re-pushed.
    """
    snapshot: dict[str, Any] = {}
    for key in ("RaceClass", "Heat", "HeatNode", "Pilot", "GlobalSettings", "Profiles"):
        rows = full_data.get(key) or []
        snapshot[key] = sorted(rows, key=lambda row: (row.get("id"), json.dumps(row, sort_keys=True)))

    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return int(digest[:12], 16)


def is_unknown_structure_error(detail: str) -> bool:
    """True when the hub needs a structure upload before append can succeed."""
    lowered = detail.lower()
    return "unknown class_id" in lowered or "unknown heat_id" in lowered


class SyncState:
    """Tracks structure generation and which runs have been appended."""

    def __init__(self, rhapi: RHAPI):
        self._rhapi = rhapi

    def load(self) -> None:
        """Ensure option keys exist (values may be empty)."""
        if self._rhapi.db.option(OPTION_STRUCTURE_GENERATION) is None:
            self._rhapi.db.option_set(OPTION_STRUCTURE_GENERATION, "0")
        if self._rhapi.db.option(OPTION_LAST_STRUCTURE_PUSHED) is None:
            self._rhapi.db.option_set(OPTION_LAST_STRUCTURE_PUSHED, "0")
        if self._rhapi.db.option(OPTION_PUSHED_RACE_META_IDS) is None:
            self._rhapi.db.option_set(OPTION_PUSHED_RACE_META_IDS, "[]")

    def reset(self) -> None:
        """Clear sync state after a new RH database/event load."""
        self._rhapi.db.option_set(OPTION_STRUCTURE_GENERATION, "0")
        self._rhapi.db.option_set(OPTION_LAST_STRUCTURE_PUSHED, "0")
        self._rhapi.db.option_set(OPTION_PUSHED_RACE_META_IDS, "[]")
        logger.info("FPV Race Hub sync state reset")

    @property
    def structure_generation(self) -> int:
        raw = self._rhapi.db.option(OPTION_STRUCTURE_GENERATION) or "0"
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    @structure_generation.setter
    def structure_generation(self, value: int) -> None:
        self._rhapi.db.option_set(OPTION_STRUCTURE_GENERATION, str(value))

    @property
    def last_structure_generation_pushed(self) -> int:
        raw = self._rhapi.db.option(OPTION_LAST_STRUCTURE_PUSHED) or "0"
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    @last_structure_generation_pushed.setter
    def last_structure_generation_pushed(self, value: int) -> None:
        self._rhapi.db.option_set(OPTION_LAST_STRUCTURE_PUSHED, str(value))

    def needs_structure_push(self) -> bool:
        return self.last_structure_generation_pushed != self.structure_generation

    def bump_structure_generation(self) -> None:
        self.structure_generation = self.structure_generation + 1
        logger.debug(
            "FPV Race Hub structure_generation bumped to %s",
            self.structure_generation,
        )

    def mark_structure_pushed(self) -> None:
        self.last_structure_generation_pushed = self.structure_generation

    def pushed_race_meta_ids(self) -> set[int]:
        raw = self._rhapi.db.option(OPTION_PUSHED_RACE_META_IDS) or "[]"
        try:
            ids = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return set()
        return {int(i) for i in ids if i is not None}

    def record_race_meta_pushed(self, race_meta_id: int) -> None:
        ids = self.pushed_race_meta_ids()
        ids.add(race_meta_id)
        self._rhapi.db.option_set(
            OPTION_PUSHED_RACE_META_IDS, json.dumps(sorted(ids))
        )
