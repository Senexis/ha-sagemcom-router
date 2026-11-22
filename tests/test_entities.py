"""Read-only entities: router sensors, client trackers and sensors, mesh extenders."""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.const import STATE_HOME, STATE_NOT_HOME, STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.sagemcom_rest.const import DOMAIN, ROUTER_SUBENTRY_TYPE

from . import router_data as rd
from .common import SERIAL, FakeRouter, entity_id

pytestmark = pytest.mark.usefixtures("entry")


def _state(hass: HomeAssistant, domain: str, unique_id: str):
    state = hass.states.get(entity_id(hass, domain, unique_id))
    assert state, unique_id
    return state


async def _poll(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()


async def test_router_sensors(hass: HomeAssistant) -> None:
    connected = _state(hass, "sensor", f"{SERIAL}_connected_devices")
    assert connected.state == "4"  # the offline tablet doesn't count
    assert connected.attributes["wifi"] == 3
    assert connected.attributes["wired"] == 1
    assert connected.attributes["known_devices"] == 5
    assert [d["name"] for d in connected.attributes["devices"]] == [
        "Guest-Laptop",
        "NAS",
        "Phone",
        "Plug",
    ]
    assert _state(hass, "sensor", f"{SERIAL}_public_ipv4").state == "198.51.100.7"
    assert _state(hass, "sensor", f"{SERIAL}_wifi_5_clients").state == "1"


async def test_router_binary_sensors(hass: HomeAssistant) -> None:
    assert _state(hass, "binary_sensor", f"{SERIAL}_internet").state == STATE_ON
    guest = _state(hass, "binary_sensor", f"{SERIAL}_guest_wifi")
    assert guest.state == STATE_OFF
    assert guest.attributes["ssid"] == "TestGuest"
    assert _state(hass, "binary_sensor", f"{SERIAL}_temperature_problem").state == STATE_OFF


async def test_read_only_twins_disabled_by_default(hass: HomeAssistant) -> None:
    ent_reg = er.async_get(hass)
    for domain, unique_id in (
        ("binary_sensor", f"{SERIAL}_wifi_5_radio"),
        ("binary_sensor", f"{rd.PLUG}_blocked"),
        ("sensor", f"{SERIAL}_led_brightness"),
    ):
        entry = ent_reg.async_get(entity_id(hass, domain, unique_id))
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, unique_id


async def test_no_control_entities_while_read_only(hass: HomeAssistant) -> None:
    for domain in ("switch", "light", "button"):
        assert hass.states.async_entity_ids(domain) == []


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_read_only_twin_states(hass: HomeAssistant) -> None:
    assert _state(hass, "binary_sensor", f"{SERIAL}_wifi_5_radio").state == STATE_ON
    assert _state(hass, "binary_sensor", f"{rd.PLUG}_blocked").state == STATE_ON
    assert _state(hass, "binary_sensor", f"{rd.PHONE}_blocked").state == STATE_OFF
    assert _state(hass, "sensor", f"{SERIAL}_led_brightness").state == "50"


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_device_trackers(hass: HomeAssistant) -> None:
    phone = _state(hass, "device_tracker", rd.PHONE)
    assert phone.state == STATE_HOME
    assert phone.attributes["ip"] == "192.0.2.20"
    assert phone.attributes["connection"] == "wifi"
    assert phone.attributes["band"] == "5"
    assert phone.attributes["signal_dbm"] == -60
    assert phone.attributes["network"] == "main"

    assert _state(hass, "device_tracker", rd.GUEST).attributes["network"] == "guest"
    nas = _state(hass, "device_tracker", rd.NAS)
    assert nas.attributes["wired_port_mbps"] == 10000
    assert nas.attributes["dhcp_reservation"] is True
    assert _state(hass, "device_tracker", rd.OLD).state == STATE_NOT_HOME


async def test_client_sensors_disabled_by_default(hass: HomeAssistant) -> None:
    ent_reg = er.async_get(hass)
    for key in ("connection", "signal", "link_speed"):
        entry = ent_reg.async_get(entity_id(hass, "sensor", f"{rd.PHONE}_{key}"))
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key


@pytest.mark.usefixtures("entity_registry_enabled_by_default")
async def test_client_sensors(hass: HomeAssistant) -> None:
    assert _state(hass, "sensor", f"{rd.PHONE}_connection").state == "5 GHz"
    assert _state(hass, "sensor", f"{rd.PHONE}_signal").state == "-60"
    assert _state(hass, "sensor", f"{rd.PHONE}_link_speed").state == "866.7"
    assert _state(hass, "sensor", f"{rd.NAS}_connection").state == "Ethernet"
    assert _state(hass, "sensor", f"{rd.NAS}_link_speed").state == "10000"
    assert _state(hass, "sensor", f"{rd.OLD}_connection").state == "Offline"
    assert _state(hass, "sensor", f"{rd.OLD}_signal").state == "unknown"


async def test_client_appearing_later_gets_entities(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    mac = "02:00:00:00:10:99"
    router.raw["hosts"][0]["hosts"]["list"].append(
        rd.host(9, mac, "Laptop", "192.0.2.40", link="Ethernet")
    )
    await _poll(hass, entry)
    device = dr.async_get(hass).async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, mac), entry.entry_id
    )
    assert device and device.name == "Laptop"
    assert er.async_get(hass).async_get_entity_id("device_tracker", DOMAIN, mac)


async def test_mesh_extender(hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter) -> None:
    dev_reg = dr.async_get(hass)
    router_dev = dev_reg.async_get_device_by_identifier((DOMAIN, SERIAL), entry.entry_id)
    extender = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"mesh_{rd.EXTENDER_ID}"), entry.entry_id
    )
    assert extender
    assert extender.name == "upstairs"
    assert extender.model == "X0000"
    assert extender.sw_version == "TESTFW2"
    assert extender.via_device_id == router_dev.id
    router_sub = next(
        s.subentry_id for s in entry.subentries.values() if s.subentry_type == ROUTER_SUBENTRY_TYPE
    )
    assert extender.config_subentry_id == router_sub
    # The gateway node is the router itself, not an extra device.
    assert dev_reg.async_get_device_by_identifier((DOMAIN, f"mesh_{rd.GATEWAY_ID}"), entry.entry_id) is None

    connected = f"mesh_{rd.EXTENDER_ID}_connected"
    clients = f"mesh_{rd.EXTENDER_ID}_clients"
    backhaul = _state(hass, "sensor", f"mesh_{rd.EXTENDER_ID}_backhaul")
    assert _state(hass, "binary_sensor", connected).state == STATE_ON
    assert _state(hass, "sensor", clients).state == "1"
    assert backhaul.state == "Wi-Fi"
    assert backhaul.attributes["quality"] == "GOOD"

    # Leaving the mesh: "Connected" turns off, the other values become unavailable.
    mesh = router.raw["mesh"][0]
    mesh["meshDevices"] = [n for n in mesh["meshDevices"] if n["deviceId"] != rd.EXTENDER_ID]
    await _poll(hass, entry)
    assert _state(hass, "binary_sensor", connected).state == STATE_OFF
    assert _state(hass, "sensor", clients).state == STATE_UNAVAILABLE
