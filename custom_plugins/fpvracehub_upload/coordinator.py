"""
Event hooks for automatic FPV Race Hub uploads.
"""

from __future__ import annotations

import logging
from typing import Union

import gevent
from eventmanager import Evt
from RHAPI import RHAPI

from .api_client import export_full_results, push_append_results, push_structure_results
from .state import SyncState, compute_structure_fingerprint

logger = logging.getLogger(__name__)

# Setup changes that require re-pushing structure before more appends.
SETUP_CHANGE_EVENTS = (
    Evt.CLASS_ADD,
    Evt.CLASS_ALTER,
    Evt.CLASS_DELETE,
    Evt.CLASS_DUPLICATE,
    Evt.HEAT_ADD,
    Evt.HEAT_ALTER,
    Evt.HEAT_DELETE,
    Evt.HEAT_DUPLICATE,
    Evt.HEAT_GENERATE,
    Evt.PILOT_ADD,
    Evt.PILOT_ALTER,
    Evt.PILOT_DELETE,
    Evt.PROFILE_SET,
    Evt.PROFILE_ADD,
    Evt.PROFILE_ALTER,
    Evt.PROFILE_DELETE,
    Evt.FREQUENCY_SET,
    Evt.ENTER_AT_LEVEL_SET,
    Evt.EXIT_AT_LEVEL_SET,
)

# RH results/event DB replaced or cleared — reset hub sync and turn off auto upload.
DATABASE_RESET_EVENTS = (
    Evt.DATABASE_RESET,
    Evt.DATABASE_IMPORT,
    Evt.DATABASE_RESTORE,
    Evt.DATABASE_INITIALIZE,
    Evt.DATABASE_RECOVER,
)


class UploadCoordinator:
    """Sync RotorHazard event data to FPV Race Hub (structure + append)."""

    def __init__(self, rhapi: RHAPI):
        self._rhapi = rhapi
        self._state = SyncState(rhapi)
        self._ui_manager = None
        self._state.load()

        rhapi.events.on(Evt.STARTUP, self.on_startup, name="fpvrh_startup")
        for event in DATABASE_RESET_EVENTS:
            rhapi.events.on(
                event, self.on_database_reset, name=f"fpvrh_db_{event}"
            )
        for event in SETUP_CHANGE_EVENTS:
            rhapi.events.on(
                event, self.on_setup_changed, name=f"fpvrh_setup_{event}"
            )

        rhapi.events.on(
            Evt.HEAT_SET, self.on_heat_selected, name="fpvrh_heat_set"
        )
        rhapi.events.on(
            Evt.RACE_STAGE, self.on_race_about_to_start, name="fpvrh_race_stage"
        )
        rhapi.events.on(Evt.LAPS_SAVE, self.on_laps_saved, name="fpvrh_laps_save")
        rhapi.events.on(
            Evt.LAPS_RESAVE, self.on_laps_saved, name="fpvrh_laps_resave"
        )

    def attach_ui_manager(self, ui_manager) -> None:
        """Link UI so database resets can refresh the auto-upload checkbox."""
        self._ui_manager = ui_manager

    def _auto_upload_enabled(self) -> bool:
        return self._rhapi.db.option("fpvrh_auto_upload") == "1"

    def on_startup(self, _args: Union[dict, None] = None) -> None:
        """Align structure generation fingerprint with the loaded RH database."""
        full_data = export_full_results(self._rhapi)
        if full_data is None:
            return
        fingerprint = compute_structure_fingerprint(full_data)
        if self._state.structure_generation == 0:
            self._state.structure_generation = fingerprint
        logger.info(
            "FPV Race Hub ready (structure_generation=%s, last_pushed=%s)",
            self._state.structure_generation,
            self._state.last_structure_generation_pushed,
        )

    def on_database_reset(self, _args: Union[dict, None] = None) -> None:
        self._state.reset()
        # Force a structure push after the operator re-enables auto upload.
        self._state.bump_structure_generation()
        self._rhapi.db.option_set("fpvrh_auto_upload", "0")
        logger.info(
            "FPV Race Hub: auto upload disabled after RotorHazard database change"
        )
        if self._ui_manager is not None:
            self._ui_manager.sync_auto_upload_disabled()

    def on_setup_changed(self, _args: Union[dict, None] = None) -> None:
        self._state.bump_structure_generation()

    def on_heat_selected(self, _args: Union[dict, None] = None) -> None:
        if not self._auto_upload_enabled():
            return
        gevent.spawn(self._ensure_structure_pushed)

    def on_race_about_to_start(self, _args: Union[dict, None] = None) -> None:
        if not self._auto_upload_enabled():
            return
        gevent.spawn(self._ensure_structure_pushed)

    def on_laps_saved(self, args: Union[dict, None] = None) -> None:
        if not self._auto_upload_enabled():
            return

        race_meta_id = args.get("race_id") if args else None
        if race_meta_id is None:
            logger.warning(
                "FPV Race Hub auto upload skipped: no race_id in event args"
            )
            return

        gevent.spawn(self._upload_saved_round, int(race_meta_id))

    def _ensure_structure_pushed(self) -> None:
        if not self._state.needs_structure_push():
            return

        logger.info("FPV Race Hub pushing structure before next run")
        if push_structure_results(
            self._rhapi, notify=False, state=self._state
        ):
            self._rhapi.ui.message_notify(
                self._rhapi.language.__(
                    "FPV Race Hub: event setup synced before race."
                )
            )
        else:
            self._rhapi.ui.message_notify(
                self._rhapi.language.__(
                    "FPV Race Hub: failed to sync event setup. Check settings and logs."
                )
            )

    def _upload_saved_round(self, race_meta_id: int) -> None:
        logger.info(
            "FPV Race Hub auto upload triggered for SavedRaceMeta.id=%s",
            race_meta_id,
        )

        if self._state.needs_structure_push():
            push_structure_results(
                self._rhapi, notify=False, state=self._state
            )

        success = push_append_results(
            self._rhapi,
            race_meta_id,
            notify=False,
            state=self._state,
        )
        if success:
            message = "FPV Race Hub: race results uploaded automatically."
            self._rhapi.ui.message_notify(self._rhapi.language.__(message))
        else:
            message = (
                "FPV Race Hub: automatic upload failed. Check settings and logs."
            )
            self._rhapi.ui.message_notify(self._rhapi.language.__(message))

    def push_structure_manual(self) -> None:
        """Force structure upload (operator button)."""
        push_structure_results(
            self._rhapi, notify=True, state=self._state
        )
