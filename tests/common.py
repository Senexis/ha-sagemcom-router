"""Test helpers: a fake router behind the integration's API client."""

from __future__ import annotations

import copy
from typing import Any

from pytest_homeassistant.common import MockConfigEntry

from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.sagemcom_rest.api import SagemcomEndpointError
from custom_components.sagemcom_rest.const import (
    CONF_ENABLE_CONTROLS,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from . import router_data as rd

SERIAL = "TESTSERIAL01"
ENTRY_DATA = {
    CONF_HOST: "192.0.2.1",
    CONF_USERNAME: "admin",
    CONF_PASSWORD: "secret",
    CONF_VERIFY_SSL: False,
}


class FakeRouter:
    """Stand-in for SagemcomRestClient, serving router_data.raw() and applying writes.

    Set `fail_login` / `fail_get` to an exception to simulate errors, and edit
    `raw` to change what the next poll sees.
    """

    def __init__(self) -> None:
        self.host = ENTRY_DATA[CONF_HOST]
        self.raw: dict[str, Any] = rd.raw()
        self.writes: list[tuple[str, str, dict[str, Any] | None]] = []
        self.fail_login: Exception | None = None
        self.fail_get: Exception | None = None
        self.fail_write: Exception | None = None
        self.logins = 0
        self.logouts = 0

    def __call__(self, session, host, username, password, **kwargs) -> FakeRouter:
        """Used as the patched SagemcomRestClient class."""
        self.host = host
        self.password = password
        return self

    # ------------------------------------------------ SagemcomRestClient API
    async def login(self) -> None:
        if self.fail_login:
            raise self.fail_login
        self.logins += 1

    async def logout(self) -> None:
        self.logouts += 1

    async def get(self, path: str, *, sanitize: bool = True, timeout: float | None = None) -> Any:
        if self.fail_get:
            raise self.fail_get
        for key, key_path in rd.PATHS.items():
            if key_path == path and key in self.raw:
                return copy.deepcopy(self.raw[key])
        raise SagemcomEndpointError(f"GET {path}: HTTP 404")

    async def get_open(self) -> Any:
        return {"wan_status": "Up"}

    async def write(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        if self.fail_write:
            raise self.fail_write
        self.writes.append((method, path, data))
        self._apply(method, path, data or {})
        return None

    # --------------------------------------------------------- router state
    @property
    def parental(self) -> dict[str, Any]:
        return self.raw["parental"][0]

    def _apply(self, method: str, path: str, data: dict[str, Any]) -> None:
        """Mimic what the router does with the writes the integration sends."""
        if path == "/api/v1/led":
            self.raw["led"][0]["leds_brightness"]["brightness"] = str(data["brightness"])
        elif path == "/api/v1/parental_control":
            self.parental["enable"] = data["enable"]
        elif path == "/api/v1/parental_control/device_schedule" and method == "POST":
            rules = self.parental["schedules"]
            rules.append(
                {
                    "id": max(r["id"] for r in rules) + 1,
                    "enable": False,
                    "AlwaysBlock": data["AlwaysBlock"],
                    "SourceMac": data["MACAddress"].upper(),
                    "timeslots": [],
                }
            )
        elif path.startswith("/api/v1/parental_control/device_schedule/"):
            rule_id = int(path.rsplit("/", 1)[1])
            rule = next(r for r in self.parental["schedules"] if r["id"] == rule_id)
            rule.update({k: v for k, v in data.items() if k in ("enable", "AlwaysBlock")})
        elif path.startswith("/api/v2/wireless/guest"):
            ident = path.removeprefix("/api/v2/wireless/")
            for item in self.raw["device_info"][0]["wireless"]:
                if ident in item:
                    item[ident]["ssidStatus"] = "Up" if data["enable"] else "Down"
        elif path.startswith("/api/v2/wireless/") and "radio_enable" in data:
            band = {"24": "2.4", "5": "5", "6": "6"}[path.rsplit("/", 1)[1]]
            self.raw[f"wifi_{band}"][0]["wireless"]["radio"]["enable"] = data["radio_enable"]
        elif path.startswith("/api/v1/host/") and method == "DELETE":
            host_id = int(path.rsplit("/", 1)[1])
            hosts = self.raw["hosts"][0]["hosts"]
            hosts["list"] = [h for h in hosts["list"] if h["id"] != host_id]


def make_entry(*, controls: bool = False, **kwargs: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Sagemcom F0000T",
        unique_id=SERIAL,
        data=dict(ENTRY_DATA),
        options={CONF_ENABLE_CONTROLS: controls},
        version=1,
        minor_version=kwargs.pop("minor_version", 2),
        **kwargs,
    )


async def setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    """Add and set up the entry, then let the slow endpoints load."""
    if entry.entry_id not in hass.config_entries.async_entry_ids():
        entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    # The slow tier finishes in the background and requests a refresh.
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    return entry




def entity_id(hass: HomeAssistant, domain: str, unique_id: str) -> str:
    """Entity id registered for a unique id (fails the test if missing)."""
    found = er.async_get(hass).async_get_entity_id(domain, DOMAIN, unique_id)
    assert found, f"no {domain} entity with unique id {unique_id}"
    return found
