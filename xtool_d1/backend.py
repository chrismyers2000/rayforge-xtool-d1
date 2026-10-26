import logging
import shutil
from pathlib import Path

import pluggy

hookimpl = pluggy.HookimplMarker("rayforge")

logger = logging.getLogger(__name__)

PROFILE_DIR = Path(__file__).parent / "profile"


def _register_drivers():
    from rayforge.machine.driver import drivers, register_driver

    from .driver import XToolD1Driver
    from .wifi_driver import XToolD1WifiDriver

    for driver in (XToolD1Driver, XToolD1WifiDriver):
        if driver not in drivers:
            register_driver(driver)


@hookimpl
def register_machines(machine_manager):
    """Register the xTool D1 drivers (not called by Rayforge 1.13)."""
    _register_drivers()


@hookimpl
def rayforge_init(context):
    """Register the drivers and install the bundled device profiles."""
    from rayforge.config import USER_DEVICES_DIR

    _register_drivers()

    for src in PROFILE_DIR.iterdir():
        if not src.is_dir():
            continue
        dst = USER_DEVICES_DIR / src.name
        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
        except OSError as e:
            logger.warning(f"Could not install profile {src.name}: {e}")
