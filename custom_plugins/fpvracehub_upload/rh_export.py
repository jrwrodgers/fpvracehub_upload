"""
Build RotorHazard complete JSON export in-process.

Matches bundled exporter ``JSON (Complete) / All`` without requiring
``export_manager`` (which can be unset when exports run from plugin greenlets).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from RHAPI import RHAPI
from sqlalchemy import inspect

logger = logging.getLogger(__name__)

ROTORHAZARD_FULL_EXPORTER = "JSON__Complete____All"


def _row_to_dict(obj: Any) -> dict[str, Any]:
    """Serialize one SQLAlchemy row like rh_data_export_json.AlchemyEncoder."""
    mapped = inspect(obj)
    fields: dict[str, Any] = {}
    for field in mapped.attrs.keys():
        if field in ("query", "query_class"):
            continue
        data = getattr(obj, field)
        try:
            json.dumps(data)
            if field in ("frequencies", "enter_ats", "exit_ats") and isinstance(data, str):
                fields[field] = json.loads(data)
            else:
                fields[field] = data
        except TypeError:
            fields[field] = None
    return fields


def _rows_to_dicts(rows: Any) -> list[dict[str, Any]]:
    return [_row_to_dict(row) for row in rows]


def build_complete_export_dict(rhapi: RHAPI) -> dict[str, Any]:
    """
    Same top-level keys as RotorHazard ``JSON (Complete) / All`` export.
    """
    return {
        "Pilot": _rows_to_dicts(rhapi.db.pilots),
        "Heat": _rows_to_dicts(rhapi.db.heats),
        "HeatNode": _rows_to_dicts(rhapi.db.slots),
        "RaceClass": _rows_to_dicts(rhapi.db.raceclasses),
        "RaceFormat": _rows_to_dicts(rhapi.db.raceformats),
        "SavedRaceMeta": _rows_to_dicts(rhapi.db.races),
        "SavedPilotRace": _rows_to_dicts(rhapi.db.pilotruns),
        "SavedRaceLap": _rows_to_dicts(rhapi.db.laps),
        "Profiles": _rows_to_dicts(rhapi.db.frequencysets),
        "GlobalSettings": _rows_to_dicts(rhapi.db.options),
    }


def _export_via_manager(rhapi: RHAPI) -> dict[str, Any] | None:
    """Use the registered RH exporter when export_manager is available."""
    racecontext = getattr(rhapi.io, "_racecontext", None)
    if racecontext is None:
        return None

    export_manager = getattr(racecontext, "export_manager", None)
    if export_manager is None:
        return None

    exporter = export_manager.exporters.get(ROTORHAZARD_FULL_EXPORTER)
    if exporter is None:
        return None

    try:
        return exporter.export(rhapi)
    except Exception:
        logger.exception("FPV Race Hub: bundled JSON exporter failed")
        return None


def run_full_export(rhapi: RHAPI) -> dict[str, Any] | None:
    """
    Return export payload ``{data, encoding, ext}`` for browser download.

    Prefers the bundled exporter; falls back to in-process assembly.
    """
    export = _export_via_manager(rhapi)
    if export and export.get("data"):
        return export

    try:
        data = build_complete_export_dict(rhapi)
    except Exception:
        logger.exception("FPV Race Hub: in-process export failed")
        return None

    logger.debug("FPV Race Hub: using in-process complete JSON export")
    return {
        "data": json.dumps(data, indent="\t"),
        "encoding": "application/json",
        "ext": "json",
    }
