"""
FPV Race Hub Upload plugin for RotorHazard.
"""

import logging

from RHAPI import RHAPI

from .coordinator import UploadCoordinator
from .uimanager import UIManager

logger = logging.getLogger(__name__)

_ui_manager: UIManager | None = None
_upload_coordinator: UploadCoordinator | None = None


def initialize(rhapi: RHAPI) -> None:
    """
    Initializes the plugin. Called by RotorHazard when registering the plugin.

    :param rhapi: The RotorHazard API object
    """
    global _ui_manager, _upload_coordinator
    logger.info("Initializing FPV Race Hub Upload plugin")
    _upload_coordinator = UploadCoordinator(rhapi)
    _ui_manager = UIManager(rhapi, _upload_coordinator)
    _upload_coordinator.attach_ui_manager(_ui_manager)
