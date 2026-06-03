"""
FPV Race Hub API client.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from RHAPI import RHAPI

from .rh_export import build_complete_export_dict, run_full_export
from .state import (
    extract_runs_payload,
    extract_structure_payload,
    is_unknown_structure_error,
)

logger = logging.getLogger(__name__)

UPLOAD_RETRY_ATTEMPTS = 3
UPLOAD_RETRY_BACKOFF_SECS = 2.0


def build_base_url(host: str, port: int | str) -> str:
    """Build API base URL from host and port."""
    host = (host or "http://127.0.0.1").strip().rstrip("/")
    if not host.startswith(("http://", "https://")):
        host = f"http://{host}"
    return f"{host}:{port}"


def fetch_events(base_url: str, api_key: str = "") -> list[dict[str, Any]]:
    """
    Fetch events from FPV Race Hub.

    Returns a list of dicts with at least ``id`` and ``name`` keys.
    """
    url = f"{base_url.rstrip('/')}/api/events"
    headers = {"Accept": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        logger.warning(
            "Failed to fetch events (%s): %s", exc.code, exc.read().decode()
        )
        return []
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("Failed to fetch events: %s", exc)
        return []

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "events" in data:
        return data["events"]
    return []


def export_full_results(rhapi: RHAPI) -> dict[str, Any] | None:
    """Export the full RotorHazard event database as parsed JSON."""
    try:
        return build_complete_export_dict(rhapi)
    except Exception:
        logger.exception("FPV Race Hub: failed to export RotorHazard event data")
        return None


def export_structure_data(rhapi: RHAPI) -> dict[str, Any] | None:
    """Build structure-mode payload from the current RH export."""
    full_data = export_full_results(rhapi)
    if full_data is None:
        return None
    return extract_structure_payload(full_data)


def export_append_data(rhapi: RHAPI, race_meta_id: int) -> dict[str, Any] | None:
    """
    Build append-mode payload for a single saved run (SavedRaceMeta.id).
    """
    if rhapi.db.race_by_id(race_meta_id) is None:
        logger.error("FPV Race Hub: saved race %s not found", race_meta_id)
        return None

    full_data = export_full_results(rhapi)
    if full_data is None:
        return None

    payload = extract_runs_payload(full_data, {race_meta_id})
    if not payload.get("SavedRaceMeta"):
        logger.error(
            "FPV Race Hub: race %s missing from export data", race_meta_id
        )
        return None

    return payload


def get_upload_config(rhapi: RHAPI) -> tuple[str, int, str] | None:
    """Return (base_url, event_id, api_key) when upload settings are valid."""
    host = rhapi.db.option("fpvrh_api_host") or ""
    port = rhapi.db.option("fpvrh_api_port") or 8000
    event_id = rhapi.db.option("fpvrh_event_id") or ""
    api_key = rhapi.db.option("fpvrh_api_key") or ""

    if not str(event_id).strip() or not api_key:
        return None

    try:
        event_id_int = int(event_id)
    except (TypeError, ValueError):
        return None

    return build_base_url(host, port), event_id_int, api_key


def upload_results(
    base_url: str,
    event_id: int,
    api_key: str,
    data: dict[str, Any],
    *,
    upload_format: str = "rotorhazard",
    mode: str = "structure",
) -> tuple[bool, str]:
    """
    Upload race results JSON to FPV Race Hub.

    Payload: ``{"format": ..., "mode": ..., "data": ...}``

    Returns (success, message).
    """
    body = json.dumps(
        {"format": upload_format, "mode": mode, "data": data}
    ).encode("utf-8")
    url = f"{base_url.rstrip('/')}/api/events/{event_id}/upload"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    }

    last_error = "Upload failed: unknown error"
    for attempt in range(1, UPLOAD_RETRY_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode())
                logger.info(
                    "FPV Race Hub upload ok (mode=%s, attempt=%s): %s",
                    mode,
                    attempt,
                    body,
                )
                return True, json.dumps(body, indent=2)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode()
            last_error = f"Upload failed ({exc.code}): {detail}"
            logger.error(
                "FPV Race Hub HTTP error (mode=%s, attempt=%s): %s",
                mode,
                attempt,
                last_error,
            )
            if exc.code in (401, 404, 400):
                return False, last_error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = f"Upload failed: {exc}"
            logger.warning(
                "FPV Race Hub transient error (mode=%s, attempt=%s): %s",
                mode,
                attempt,
                exc,
            )

        if attempt < UPLOAD_RETRY_ATTEMPTS:
            time.sleep(UPLOAD_RETRY_BACKOFF_SECS * attempt)

    return False, last_error


def push_structure_results(
    rhapi: RHAPI,
    *,
    notify: bool = False,
    mark_pushed: bool = True,
    state: Any | None = None,
) -> bool:
    """Upload event setup (pilots, classes, heats, profiles) in structure mode."""
    config = get_upload_config(rhapi)
    if config is None:
        _notify_config_missing(rhapi, notify, auto=False)
        return False

    base_url, event_id, api_key = config

    if notify:
        rhapi.ui.message_notify(
            rhapi.language.__("Uploading event setup to FPV Race Hub...")
        )

    results_data = export_structure_data(rhapi)
    if results_data is None:
        message = "Failed to export event setup from RotorHazard."
        _notify_failure(rhapi, notify, message)
        return False

    success, detail = upload_results(
        base_url, event_id, api_key, results_data, mode="structure"
    )

    if success:
        logger.info("FPV Race Hub structure upload successful: %s", detail)
        if mark_pushed and state is not None:
            state.mark_structure_pushed()
        if notify:
            rhapi.ui.message_notify(
                rhapi.language.__("Event setup uploaded to FPV Race Hub.")
            )
        return True

    _notify_failure(rhapi, notify, detail)
    return False


def push_append_results(
    rhapi: RHAPI,
    race_meta_id: int,
    *,
    notify: bool = False,
    state: Any | None = None,
    retry_structure: bool = True,
) -> bool:
    """
    Upload a single saved run using append mode.

    On unknown class/heat errors, pushes structure first and retries append once.
    """
    config = get_upload_config(rhapi)
    if config is None:
        _notify_config_missing(rhapi, notify, auto=not notify)
        return False

    base_url, event_id, api_key = config
    results_data = export_append_data(rhapi, race_meta_id)
    if results_data is None:
        message = "Failed to export saved round data from RotorHazard."
        _notify_failure(rhapi, notify, message)
        return False

    success, detail = _upload_append_with_structure_retry(
        rhapi,
        base_url,
        event_id,
        api_key,
        results_data,
        race_meta_id=race_meta_id,
        retry_structure=retry_structure,
        state=state,
    )

    if success:
        logger.info(
            "FPV Race Hub append upload successful for SavedRaceMeta.id=%s: %s",
            race_meta_id,
            detail,
        )
        if state is not None:
            state.record_race_meta_pushed(race_meta_id)
        if notify:
            rhapi.ui.message_notify(
                rhapi.language.__("Race results uploaded to FPV Race Hub.")
            )
        return True

    _notify_failure(rhapi, notify, detail)
    return False


def _upload_append_with_structure_retry(
    rhapi: RHAPI,
    base_url: str,
    event_id: int,
    api_key: str,
    results_data: dict[str, Any],
    *,
    race_meta_id: int,
    retry_structure: bool,
    state: Any | None,
) -> tuple[bool, str]:
    success, detail = upload_results(
        base_url, event_id, api_key, results_data, mode="append"
    )
    if success:
        return True, detail

    if not retry_structure or not is_unknown_structure_error(detail):
        return False, detail

    logger.info(
        "FPV Race Hub append for run %s needs structure first; retrying",
        race_meta_id,
    )
    if not push_structure_results(
        rhapi, notify=False, mark_pushed=True, state=state
    ):
        return False, detail

    return upload_results(
        base_url, event_id, api_key, results_data, mode="append"
    )


def push_full_results(rhapi: RHAPI, *, notify: bool = False) -> bool:
    """
    Manual-only full replace upload (wipes hub event data).

    Never call this from automatic live sync.
    """
    config = get_upload_config(rhapi)
    if config is None:
        _notify_config_missing(rhapi, notify, auto=False)
        return False

    base_url, event_id, api_key = config

    if notify:
        rhapi.ui.message_notify(
            rhapi.language.__(
                "Exporting full results and uploading to FPV Race Hub (replaces all hub data)..."
            )
        )

    results_data = export_full_results(rhapi)
    if results_data is None:
        message = "Failed to export event data from RotorHazard."
        _notify_failure(rhapi, notify, message)
        return False

    success, detail = upload_results(
        base_url, event_id, api_key, results_data, mode="full"
    )

    if success:
        logger.info("FPV Race Hub full upload successful: %s", detail)
        if notify:
            rhapi.ui.message_notify(
                rhapi.language.__("Full results uploaded to FPV Race Hub.")
            )
        return True

    _notify_failure(rhapi, notify, detail)
    return False


def test_upload_config(rhapi: RHAPI) -> tuple[bool, str]:
    """Validate settings with a lightweight structure upload."""
    config = get_upload_config(rhapi)
    if config is None:
        return False, "Select an event and enter an API key."

    base_url, event_id, api_key = config
    results_data = export_structure_data(rhapi)
    if results_data is None:
        return False, "Failed to export event setup from RotorHazard."

    return upload_results(
        base_url, event_id, api_key, results_data, mode="structure"
    )


def _notify_config_missing(rhapi: RHAPI, notify: bool, *, auto: bool) -> None:
    if notify:
        rhapi.ui.message_alert(
            rhapi.language.__(
                "Select an event and enter an API key before pushing to FPV Race Hub."
            )
        )
    elif auto:
        logger.warning(
            "FPV Race Hub auto upload skipped: event or API key not configured"
        )


def _notify_failure(rhapi: RHAPI, notify: bool, message: str) -> None:
    logger.error("FPV Race Hub upload failed: %s", message)
    if notify:
        rhapi.ui.message_alert(rhapi.language.__(message))
