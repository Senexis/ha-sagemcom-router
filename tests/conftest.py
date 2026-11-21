"""Test setup: load the integration's pure modules without Home Assistant.

The API client and the parsing coordinator only touch a few HA names. Minimal
stand-ins for those are registered here, so the tests run on plain Python and
don't depend on a matching pytest-homeassistant-custom-component release.
Entity platforms are not imported (they need the real HA).
"""

from __future__ import annotations

import datetime
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1] / "custom_components"


def _stub(name: str, **attrs) -> types.ModuleType:
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    return module


class _DataUpdateCoordinator:
    def __init__(self, hass, logger, config_entry=None, name=None, update_interval=None):
        self.hass = hass
        self.config_entry = config_entry
        self.last_update_success = True

    def __class_getitem__(cls, item):
        return cls

    async def async_request_refresh(self):
        return None


class _UpdateFailed(Exception):
    pass


class _ConfigEntryAuthFailed(Exception):
    pass


_stub("homeassistant")
_stub("homeassistant.config_entries", ConfigEntry=object)
_stub("homeassistant.core", HomeAssistant=object)
_stub("homeassistant.exceptions", ConfigEntryAuthFailed=_ConfigEntryAuthFailed)
helpers = _stub("homeassistant.helpers")
helpers.device_registry = _stub(
    "homeassistant.helpers.device_registry",
    CONNECTION_NETWORK_MAC="mac",
    async_get=lambda hass: None,
    async_entries_for_config_entry=lambda registry, entry_id: [],
)
_stub(
    "homeassistant.helpers.update_coordinator",
    DataUpdateCoordinator=_DataUpdateCoordinator,
    UpdateFailed=_UpdateFailed,
)
util = _stub("homeassistant.util")
util.dt = _stub(
    "homeassistant.util.dt", utcnow=lambda: datetime.datetime.now(datetime.UTC)
)

# Package without running its __init__ (which imports HA config entry machinery).
_pkg = types.ModuleType("sagemcom_rest")
_pkg.__path__ = [str(ROOT / "sagemcom_rest")]
sys.modules["sagemcom_rest"] = _pkg
