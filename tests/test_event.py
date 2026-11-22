"""The "New client" event entity."""

from __future__ import annotations

from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from pytest_homeassistant.common import MockConfigEntry

from . import router_data as rd
from .common import SERIAL, FakeRouter, entity_id, make_entry, setup_entry


def _record_events(hass: HomeAssistant) -> list[dict]:
    """Attributes of every new_client event the event entity reports."""
    fired: list[dict] = []

    @callback
    def _listener(event: Event) -> None:
        new = event.data["new_state"]
        if new and new.domain == "event" and new.attributes.get("event_type") == "new_client":
            fired.append(dict(new.attributes))

    hass.bus.async_listen(EVENT_STATE_CHANGED, _listener)
    return fired


async def test_fresh_install_announces_nothing(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    assert hass.states.get(entity_id(hass, "event", f"{SERIAL}_new_client")).state == STATE_UNKNOWN


async def test_new_client_fires_once(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    fired = _record_events(hass)
    mac = "02:00:00:00:10:99"
    router.raw["hosts"][0]["hosts"]["list"].append(
        rd.host(9, mac, "Visitor", "192.0.2.160", link="WiFi", iface="Guest")
    )
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    await entry.runtime_data.async_refresh()  # still there: no second event
    await hass.async_block_till_done()

    assert len(fired) == 1
    event = fired[0]
    assert event["mac"] == mac
    assert event["name"] == "Visitor"
    assert event["ip"] == "192.0.2.160"
    assert event["connection"] == "wifi"
    assert event["network"] == "guest"
    assert "band" not in event  # unknown values are left out


async def test_clients_that_joined_while_ha_was_down(
    hass: HomeAssistant, router: FakeRouter
) -> None:
    """Only clients without a device in HA count as new after a restart."""
    entry = make_entry()
    entry.add_to_hass(hass)
    dev_reg = dr.async_get(hass)
    for mac in (rd.PHONE, rd.PLUG, rd.NAS, rd.OLD):
        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, mac)}
        )
    fired = _record_events(hass)
    await setup_entry(hass, entry)
    assert [e["mac"] for e in fired] == [rd.GUEST]
