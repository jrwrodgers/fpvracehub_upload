"""
Format-page UI for FPV Race Hub upload settings.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Union

import gevent
from eventmanager import Evt
from RHAPI import RHAPI
from RHUI import UIField, UIFieldSelectOption, UIFieldType

from .api_client import (
    build_base_url,
    export_structure_data,
    fetch_events,
    push_full_results,
    run_full_export,
    test_upload_config,
)

if TYPE_CHECKING:
    from .coordinator import UploadCoordinator

logger = logging.getLogger(__name__)

PANEL_ID = "fpvracehub_upload"


class UIManager:
    """Registers and updates the FPV Race Hub panel on the Format page."""

    def __init__(self, rhapi: RHAPI, coordinator: UploadCoordinator | None = None):
        self._rhapi = rhapi
        self._coordinator = coordinator
        self._cached_events: list[dict] = []
        self._events_list_refreshed = False
        self._auto_upload_user_enabled = False
        self._event_select_in_progress = False
        self._register_panel()
        self._register_fields()
        self._register_buttons()
        rhapi.events.on(
            Evt.OPTION_SET, self.on_option_set, name="fpvrh_option_set"
        )

    def _register_panel(self) -> None:
        self._rhapi.ui.register_panel(
            PANEL_ID,
            "FPV Race Hub Upload",
            "format",
            order=0,
        )

    def _saved_option(self, name: str, default: str = "") -> str:
        """Read a plugin option from the DB for field registration."""
        val = self._rhapi.db.option(name)
        if val is None:
            return default
        return str(val)

    def _register_fields(self) -> None:
        api_host = UIField(
            name="fpvrh_api_host",
            label="API Web Address",
            field_type=UIFieldType.TEXT,
            value=self._saved_option("fpvrh_api_host", "http://127.0.0.1"),
            desc="Host or full URL (scheme optional). Port is set separately.",
        )
        self._rhapi.fields.register_option(api_host, PANEL_ID)

        port_raw = self._saved_option("fpvrh_api_port", "8000")
        try:
            api_port_value = int(port_raw)
        except (TypeError, ValueError):
            api_port_value = 8000

        api_port = UIField(
            name="fpvrh_api_port",
            label="API Port",
            field_type=UIFieldType.BASIC_INT,
            value=api_port_value,
        )
        self._rhapi.fields.register_option(api_port, PANEL_ID)

        api_key = UIField(
            name="fpvrh_api_key",
            label="Upload API Key",
            field_type=UIFieldType.PASSWORD,
            value=self._saved_option("fpvrh_api_key", ""),
            desc="UUID upload key from the FPV Race Hub event settings.",
        )
        self._rhapi.fields.register_option(api_key, PANEL_ID)

        auto_upload = UIField(
            name="fpvrh_auto_upload",
            label="Auto upload",
            field_type=UIFieldType.CHECKBOX,
            value=self._saved_option("fpvrh_auto_upload", "0"),
            desc=(
                "Sync event setup before the first run, then upload each saved "
                "or corrected heat run (append). Does not use full replace."
            ),
        )
        self._rhapi.fields.register_option(auto_upload, PANEL_ID)

        self._register_event_selector(self._cached_events)

    def sync_auto_upload_disabled(self) -> None:
        """Reflect auto upload off after a database restore/clear/import."""
        self._auto_upload_user_enabled = False
        self._event_select_in_progress = False
        self._register_fields()
        gevent.spawn_later(0.1, self._refresh_panel_after_db_change)

    def _refresh_panel_after_db_change(self) -> None:
        self._rhapi.ui.broadcast_ui("format", replace_panels=False)

    def _restore_auto_upload_if_enabled(self) -> None:
        """Keep auto upload on when the user changes another field (e.g. event ID)."""
        if not self._auto_upload_user_enabled:
            return
        if self._rhapi.db.option("fpvrh_auto_upload") != "1":
            self._rhapi.db.option_set("fpvrh_auto_upload", "1")

    def on_option_set(self, args: Union[dict, None] = None) -> None:
        if not args:
            return

        option = args.get("option")
        value = args.get("value")

        if option == "fpvrh_auto_upload":
            if value in (True, "1", "true", 1):
                self._auto_upload_user_enabled = True
            elif not self._event_select_in_progress:
                self._auto_upload_user_enabled = False
            else:
                self._restore_auto_upload_if_enabled()
            return

        if option == "fpvrh_event_id":
            self._event_select_in_progress = True
            self._restore_auto_upload_if_enabled()
            self._register_event_selector(
                self._cached_events, selected=value
            )
            # Checkbox can blur and emit a late "off" when the select changes.
            gevent.spawn_later(0.15, self._finish_event_select)
            return

        if option in ("fpvrh_api_host", "fpvrh_api_port", "fpvrh_api_key"):
            gevent.spawn(self.refresh_event_list)

    def _finish_event_select(self) -> None:
        self._event_select_in_progress = False
        if not self._auto_upload_user_enabled:
            return
        self._restore_auto_upload_if_enabled()
        self._register_fields()
        self._rhapi.ui.broadcast_ui("format", replace_panels=False)

    def _register_event_selector(
        self, events: list[dict], selected: Union[str, int, None] = None
    ) -> None:
        if not self._events_list_refreshed:
            placeholder = "Please refresh list"
        else:
            placeholder = "— Select event —"
        options = [UIFieldSelectOption(value="", label=placeholder)]
        for event in events:
            event_id = event.get("id", "")
            label = event.get("name") or event.get("title") or str(event_id)
            options.append(
                UIFieldSelectOption(value=str(event_id), label=f"({event_id}) {label}")
            )

        event_field = UIField(
            name="fpvrh_event_id",
            label="Event ID",
            field_type=UIFieldType.SELECT,
            options=options,
            value=str(selected) if selected is not None else "",
            desc="Pre-created hub event. Load from API or enter via Refresh List.",
        )
        self._rhapi.fields.register_option(event_field, PANEL_ID)

    def _register_buttons(self) -> None:
        self._rhapi.ui.register_quickbutton(
            PANEL_ID,
            "fpvrh_refresh_events",
            "Refresh List",
            self.refresh_event_list,
            args={"refreshed": True},
        )
        self._rhapi.ui.register_quickbutton(
            PANEL_ID,
            "fpvrh_test_connection",
            "Test Connection",
            self.test_connection,
        )
        self._rhapi.ui.register_quickbutton(
            PANEL_ID,
            "fpvrh_structure_push",
            "Push Structure",
            self.structure_push,
        )
        self._rhapi.ui.register_quickbutton(
            PANEL_ID,
            "fpvrh_full_results_push",
            "Full Replace (Manual)",
            self.full_results_push,
        )
        self._rhapi.ui.register_quickbutton(
            PANEL_ID,
            "fpvrh_export_structure",
            "Export Structure",
            self.export_structure,
        )
        self._rhapi.ui.register_quickbutton(
            PANEL_ID,
            "fpvrh_export_results",
            "Export Results",
            self.export_results,
        )

    def refresh_event_list(self, args: Union[dict, None] = None) -> None:
        """Load events from the API and refresh the dropdown."""
        host = self._rhapi.db.option("fpvrh_api_host") or "http://127.0.0.1"
        port = self._rhapi.db.option("fpvrh_api_port") or 8000
        api_key = self._rhapi.db.option("fpvrh_api_key") or ""

        base_url = build_base_url(host, port)
        events = fetch_events(base_url, api_key)

        self._cached_events = events
        self._events_list_refreshed = True
        current = self._rhapi.db.option("fpvrh_event_id")
        self._register_fields()

        if args and args.get("refreshed"):
            self._rhapi.ui.broadcast_ui("format", replace_panels=False)

        if not events:
            logger.info(
                "FPV Race Hub: no events returned from %s", base_url
            )

    def test_connection(self, _args: Union[dict, None] = None) -> None:
        """Validate API key and event with a structure upload."""
        self._rhapi.ui.message_notify(
            self._rhapi.language.__("Testing FPV Race Hub connection...")
        )
        success, detail = test_upload_config(self._rhapi)
        if success:
            self._rhapi.ui.message_notify(
                self._rhapi.language.__("FPV Race Hub connection OK.")
            )
            logger.info("FPV Race Hub test upload: %s", detail)
            return

        self._rhapi.ui.message_alert(self._rhapi.language.__(detail))

    def structure_push(self, _args: Union[dict, None] = None) -> None:
        """Manually push pilots, classes, heats, and frequency profile."""
        if self._coordinator is None:
            self._rhapi.ui.message_alert(
                self._rhapi.language.__("Upload coordinator not initialized.")
            )
            return
        self._coordinator.push_structure_manual()

    def export_structure(self, _args: Union[dict, None] = None) -> None:
        """Export structure-only JSON (pilots, classes, heats, profiles) for download."""
        self._rhapi.ui.message_notify(
            self._rhapi.language.__("Exporting event structure to JSON file...")
        )

        structure_data = export_structure_data(self._rhapi)
        if structure_data is None:
            message = "Failed to export event structure from RotorHazard."
            self._rhapi.ui.message_alert(self._rhapi.language.__(message))
            return

        filename = (
            "FPV Race Hub Structure "
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        self._rhapi.ui.socket_broadcast(
            "exported_data",
            {
                "filename": filename,
                "encoding": "application/json",
                "data": json.dumps(structure_data, indent=2),
            },
        )
        self._rhapi.ui.message_notify(
            self._rhapi.language.__("Structure export file download started.")
        )

    def export_results(self, _args: Union[dict, None] = None) -> None:
        """Export full results JSON and download it in the browser."""
        self._rhapi.ui.message_notify(
            self._rhapi.language.__("Exporting results to JSON file...")
        )

        export = run_full_export(self._rhapi)
        if export is None:
            message = "Failed to export event data from RotorHazard."
            self._rhapi.ui.message_alert(self._rhapi.language.__(message))
            return

        filename = (
            "FPV Race Hub Export "
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        )
        self._rhapi.ui.socket_broadcast(
            "exported_data",
            {
                "filename": filename,
                "encoding": export.get("encoding", "application/json"),
                "data": export["data"],
            },
        )
        self._rhapi.ui.message_notify(
            self._rhapi.language.__("Results export file download started.")
        )

    def full_results_push(self, _args: Union[dict, None] = None) -> None:
        """
        Manual full replace — wipes all hub data for the event then re-imports.

        For live events use Auto upload (structure + append) instead.
        """
        push_full_results(self._rhapi, notify=True)
