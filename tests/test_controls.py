"""Control entities ("Enable controls" on): the writes they send and their state.

All writes go to the FakeRouter; nothing here talks to a real router.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN, SERVICE_PRESS
from homeassistant.components.light import ATTR_BRIGHTNESS, DOMAIN as LIGHT_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.sagemcom_rest.api import SagemcomEndpointError

from . import router_data as rd
from .common import SERIAL, FakeRouter, entity_id

pytestmark = pytest.mark.usefixtures("controls_entry")


async def _call(hass: HomeAssistant, domain: str, service: str, eid: str, **data) -> None:
    await hass.services.async_call(
        domain, service, {ATTR_ENTITY_ID: eid, **data}, blocking=True
    )


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_led_light(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    led = entity_id(hass, LIGHT_DOMAIN, f"{SERIAL}_led")
    state = hass.states.get(led)
    assert state.state == STATE_ON
    assert state.attributes[ATTR_BRIGHTNESS] == 128  # 50 %

    # The router only has a few levels: 40 % snaps to 50 %.
    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_ON, led, **{ATTR_BRIGHTNESS: 102})
    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_ON, led, **{ATTR_BRIGHTNESS: 255})
    await _poll(hass, controls_entry)
    assert hass.states.get(led).attributes[ATTR_BRIGHTNESS] == 255

    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_OFF, led)
    await _poll(hass, controls_entry)
    assert hass.states.get(led).state == STATE_OFF

    # A plain "turn on" restores the last level.
    await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_ON, led)
    assert router.writes == [
        ("PUT", "/api/v1/led", {"brightness": 50}),
        ("PUT", "/api/v1/led", {"brightness": 100}),
        ("PUT", "/api/v1/led", {"brightness": 0}),
        ("PUT", "/api/v1/led", {"brightness": 100}),
    ]


async def test_radio_switch(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    radio = entity_id(hass, SWITCH_DOMAIN, f"{SERIAL}_wifi_5_radio")
    assert hass.states.get(radio).state == STATE_ON

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, radio)
    assert router.writes == [
        ("PUT", "/api/v2/wireless/5", {"radio_enable": False, "ssid": "TestNet"})
    ]
    await _poll(hass, controls_entry)
    assert hass.states.get(radio).state == STATE_OFF


async def test_guest_wifi_switch(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    guest = entity_id(hass, SWITCH_DOMAIN, f"{SERIAL}_guest_wifi_switch")
    assert hass.states.get(guest).state == STATE_OFF

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, guest)
    # Only the bands that have a guest network.
    assert router.writes == [
        ("PUT", "/api/v2/wireless/guest24", {"enable": 1, "ssid": "TestGuest"})
    ]
    await _poll(hass, controls_entry)
    assert hass.states.get(guest).state == STATE_ON


async def test_block_new_client(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    block = entity_id(hass, SWITCH_DOMAIN, f"{rd.PHONE}_block_internet")
    assert hass.states.get(block).state == STATE_OFF

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, block)
    assert router.writes == [
        (
            "POST",
            "/api/v1/parental_control/device_schedule",
            {"MACAddress": rd.PHONE, "AlwaysBlock": True},
        ),
        (
            "PUT",
            "/api/v1/parental_control/device_schedule/3",
            {"enable": True, "macaddress": rd.PHONE.upper()},
        ),
    ]
    await _poll(hass, controls_entry)
    assert hass.states.get(block).state == STATE_ON


async def test_unblock_client(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    block = entity_id(hass, SWITCH_DOMAIN, f"{rd.PLUG}_block_internet")
    assert hass.states.get(block).state == STATE_ON

    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_OFF, block)
    assert router.writes == [
        (
            "PUT",
            "/api/v1/parental_control/device_schedule/2",
            {"enable": False, "macaddress": rd.PLUG.upper()},
        )
    ]
    await _poll(hass, controls_entry)
    assert hass.states.get(block).state == STATE_OFF


async def test_block_enables_parental_control_and_reuses_rule(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    router.parental["enable"] = False
    router.parental["schedules"][1].update(enable=False, AlwaysBlock=False)
    await controls_entry.runtime_data.async_refetch("parental")
    await _poll(hass, controls_entry)

    block = entity_id(hass, SWITCH_DOMAIN, f"{rd.PLUG}_block_internet")
    await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, block)
    assert router.writes == [
        ("PUT", "/api/v1/parental_control", {"enable": True}),
        ("PUT", "/api/v1/parental_control/device_schedule/config/2", {"AlwaysBlock": True}),
        (
            "PUT",
            "/api/v1/parental_control/device_schedule/2",
            {"enable": True, "macaddress": rd.PLUG.upper()},
        ),
    ]


async def test_block_refuses_scheduled_rule(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    router.parental["schedules"][1]["timeslots"] = [{"day": "mon"}]
    await controls_entry.runtime_data.async_refetch("parental")
    await _poll(hass, controls_entry)

    block = entity_id(hass, SWITCH_DOMAIN, f"{rd.PLUG}_block_internet")
    assert hass.states.get(block).attributes["custom_schedule"] is True
    with pytest.raises(HomeAssistantError, match="time schedule"):
        await _call(hass, SWITCH_DOMAIN, SERVICE_TURN_ON, block)
    assert router.writes == []


async def test_refused_write_raises(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    router.fail_write = SagemcomEndpointError("PUT /api/v1/led: HTTP 500")
    led = entity_id(hass, LIGHT_DOMAIN, f"{SERIAL}_led")
    with pytest.raises(HomeAssistantError, match="Router refused to set the LEDs"):
        await _call(hass, LIGHT_DOMAIN, SERVICE_TURN_OFF, led)


async def test_wps_button(hass: HomeAssistant, router: FakeRouter) -> None:
    await _call(
        hass, BUTTON_DOMAIN, SERVICE_PRESS, entity_id(hass, BUTTON_DOMAIN, f"{SERIAL}_wps_pushbutton")
    )
    assert router.writes == [("POST", "/api/v2/wireless/pairing/wps/pushbutton/all", {})]


async def test_reboot_button_disabled_by_default(hass: HomeAssistant) -> None:
    reboot = er.async_get(hass).async_get(entity_id(hass, BUTTON_DOMAIN, f"{SERIAL}_reboot"))
    assert reboot.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_forget_offline_device(
    hass: HomeAssistant, controls_entry: MockConfigEntry, router: FakeRouter
) -> None:
    online = entity_id(hass, BUTTON_DOMAIN, f"{rd.PHONE}_forget")
    offline = entity_id(hass, BUTTON_DOMAIN, f"{rd.OLD}_forget")
    assert hass.states.get(online).state == STATE_UNAVAILABLE  # connected devices can't

    await _call(hass, BUTTON_DOMAIN, SERVICE_PRESS, offline)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert router.writes == [("DELETE", "/api/v1/host/6", None)]
    assert (
        dr.async_get(hass).async_get_device_by_connection(
            (dr.CONNECTION_NETWORK_MAC, rd.OLD), controls_entry.entry_id
        )
        is None
    )
    assert hass.states.get(offline) is None
