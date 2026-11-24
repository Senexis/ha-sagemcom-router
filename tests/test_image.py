"""Wi-Fi QR code images."""

from __future__ import annotations

import json

import pytest
from pytest_homeassistant.common import MockConfigEntry
from pytest_homeassistant.components.diagnostics import get_diagnostics_for_config_entry
from pytest_homeassistant.typing import ClientSessionGenerator

from homeassistant.components.image import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.sagemcom_rest.coordinator import WifiCredentials
from custom_components.sagemcom_rest.image import wifi_qr_payload

from . import router_data as rd
from .common import SERIAL, FakeRouter, entity_id

PNG = b"\x89PNG\r\n\x1a\n"


def _creds(**kwargs) -> WifiCredentials:
    values = {
        "ssid": "Net",
        "passphrase": "pw",
        "protocol": "WPA2-Personal",
        "hidden": False,
        "enabled": True,
    } | kwargs
    return WifiCredentials(**values)


@pytest.mark.parametrize(
    ("creds", "payload"),
    [
        (_creds(), "WIFI:T:WPA;S:Net;P:pw;;"),
        (_creds(protocol="WPA3-Personal-Transition"), "WIFI:T:WPA;S:Net;P:pw;;"),
        (_creds(ssid="My;Net", passphrase="a:b"), "WIFI:T:WPA;S:My\\;Net;P:a\\:b;;"),
        (_creds(hidden=True), "WIFI:T:WPA;S:Net;P:pw;H:true;"),
        (_creds(protocol="OWE", passphrase=""), "WIFI:T:nopass;S:Net;;"),
        (_creds(protocol="None", passphrase=""), "WIFI:T:nopass;S:Net;;"),
    ],
)
def test_qr_payload(creds: WifiCredentials, payload: str) -> None:
    assert wifi_qr_payload(creds) == payload


async def test_guest_qr_code(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    guest = entity_id(hass, "image", f"{SERIAL}_guest_wifi_qr_code")
    state = hass.states.get(guest)
    assert state.attributes["ssid"] == "TestGuest"
    assert state.attributes["network_enabled"] is False  # shown even while it's off
    image = await async_get_image(hass, guest)
    assert image.content_type == "image/png"
    assert image.content.startswith(PNG)


async def test_main_qr_code_disabled_by_default(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    main = er.async_get(hass).async_get(entity_id(hass, "image", f"{SERIAL}_main_wifi_qr_code"))
    assert main.disabled_by is er.RegistryEntryDisabler.INTEGRATION


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_qr_code_follows_passphrase_change(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    main = entity_id(hass, "image", f"{SERIAL}_main_wifi_qr_code")
    first = await async_get_image(hass, main)
    before = hass.states.get(main).state

    ssid = router.raw["wifi_2.4"][0]["wireless"]["ssid"]
    ssid["security"]["passphrase"] = "a-new-passphrase"
    coordinator = entry.runtime_data
    coordinator._slow_next = 0  # due now
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(main).state != before  # image_last_updated
    assert (await async_get_image(hass, main)).content != first.content


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_passphrases_stay_out_of_states_and_diagnostics(
    hass: HomeAssistant, hass_client: ClientSessionGenerator, entry: MockConfigEntry
) -> None:
    states = json.dumps([s.as_dict() for s in hass.states.async_all()], default=str)
    diag = json.dumps(await get_diagnostics_for_config_entry(hass, hass_client, entry))
    for secret in (rd.MAIN_PSK, rd.GUEST_PSK):
        assert secret not in states
        assert secret not in diag
