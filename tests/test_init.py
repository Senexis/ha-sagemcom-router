"""Setup, unload, migration, subentries and device removal."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.sagemcom_rest import async_remove_config_entry_device
from custom_components.sagemcom_rest.api import (
    SagemcomAuthError,
    SagemcomBusyError,
    SagemcomConnectionError,
)
from custom_components.sagemcom_rest.const import (
    CLIENTS_SUBENTRY_TYPE,
    DOMAIN,
    ROUTER_SUBENTRY_TYPE,
)

from . import router_data as rd
from .common import SERIAL, FakeRouter, entity_id, make_entry, setup_entry


def _subentry(entry: MockConfigEntry, subentry_type: str) -> str:
    return next(
        s.subentry_id for s in entry.subentries.values() if s.subentry_type == subentry_type
    )


def _client_device(hass: HomeAssistant, entry: MockConfigEntry, mac: str) -> dr.DeviceEntry:
    device = dr.async_get(hass).async_get_device_by_connection(
        (dr.CONNECTION_NETWORK_MAC, mac), entry.entry_id
    )
    assert device
    return device


async def test_setup_and_unload(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    assert entry.state is ConfigEntryState.LOADED
    assert router.logins == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
    assert router.logouts == 1  # frees the router's web session


async def test_logout_when_home_assistant_stops(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    """A restart must not leave the session open on the router."""
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert router.logouts == 1


async def test_logout_when_first_refresh_fails(
    hass: HomeAssistant, router: FakeRouter
) -> None:
    router.fail_get = SagemcomConnectionError("timeout")
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert router.logouts == router.logins == 1


async def test_router_and_clients_in_own_subentries(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    dev_reg = dr.async_get(hass)
    router_dev = dev_reg.async_get_device_by_identifier((DOMAIN, SERIAL), entry.entry_id)
    assert router_dev
    assert router_dev.name == "Router F0000T"
    assert router_dev.serial_number == SERIAL
    assert router_dev.config_subentry_id == _subentry(entry, ROUTER_SUBENTRY_TYPE)

    clients = _subentry(entry, CLIENTS_SUBENTRY_TYPE)
    for mac in (rd.PHONE, rd.PLUG, rd.NAS, rd.GUEST, rd.OLD):
        device = _client_device(hass, entry, mac)
        assert device.config_subentry_id == clients
        assert device.via_device_id == router_dev.id
    assert _client_device(hass, entry, rd.PHONE).name == "Phone"


async def test_subentries_created_once(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    types = sorted(s.subentry_type for s in entry.subentries.values())
    assert types == [CLIENTS_SUBENTRY_TYPE, ROUTER_SUBENTRY_TYPE]


async def test_existing_devices_move_to_subentries(
    hass: HomeAssistant, router: FakeRouter
) -> None:
    """Devices from before the subentries existed are moved, with their entities."""
    entry = make_entry()
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, rd.PHONE)},
        name="Phone",
    )
    tracker = er.async_get(hass).async_get_or_create(
        "device_tracker",
        DOMAIN,
        rd.PHONE,
        config_entry=entry,
        device_id=device.id,
    )
    await setup_entry(hass, entry)

    clients = _subentry(entry, CLIENTS_SUBENTRY_TYPE)
    assert dr.async_get(hass).async_get(device.id).config_subentry_id == clients
    moved = er.async_get(hass).async_get(tracker.entity_id)
    assert moved and moved.config_subentry_id == clients


@pytest.mark.parametrize(
    ("error", "state"),
    [
        (SagemcomAuthError("rejected"), ConfigEntryState.SETUP_ERROR),
        (SagemcomConnectionError("timeout"), ConfigEntryState.SETUP_RETRY),
    ],
)
async def test_setup_errors(
    hass: HomeAssistant, router: FakeRouter, error: Exception, state: ConfigEntryState
) -> None:
    router.fail_login = error
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is state
    reauth = [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]
    assert bool(reauth) == isinstance(error, SagemcomAuthError)


async def test_unreachable_router_makes_entities_unavailable(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    internet = entity_id(hass, "binary_sensor", f"{SERIAL}_internet")
    assert hass.states.get(internet).state == "on"
    router.fail_get = SagemcomConnectionError("timeout")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(internet).state == STATE_UNAVAILABLE

    router.fail_get = None
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(internet).state == "on"


async def test_failing_endpoint_is_retried_next_poll(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    """An endpoint that fails while the router's API struggles is not given up on."""
    connected = entity_id(hass, "sensor", f"{SERIAL}_connected_devices")
    router.fail_paths["/api/v1/hosts"] = SagemcomBusyError("GET /api/v1/hosts: HTTP 503")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    router.fail_paths.clear()
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(connected).state == "4"


async def test_failing_router_api_does_not_start_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    internet = entity_id(hass, "binary_sensor", f"{SERIAL}_internet")
    router.fail_get = SagemcomBusyError("GET /api/v1/deviceInfo: HTTP 400")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(internet).state == STATE_UNAVAILABLE
    assert hass.config_entries.flow.async_progress() == []
    assert router.logouts == 1  # nothing worked: the next poll starts a new session


async def test_clients_keep_updating_while_device_info_fails(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    """The router's API fails deviceInfo for an hour or more while /hosts works."""
    internet = entity_id(hass, "binary_sensor", f"{SERIAL}_internet")
    connected = entity_id(hass, "sensor", f"{SERIAL}_connected_devices")
    coordinator = entry.runtime_data
    router.fail_paths["/api/v1/deviceInfo"] = SagemcomBusyError(
        "GET /api/v1/deviceInfo: HTTP 400"
    )
    next(h for h in router.raw["hosts"][0]["hosts"]["list"] if h["active"])["active"] = False
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.last_update_success
    assert hass.states.get(connected).state == "3"  # clients still update
    assert hass.states.get(internet).state == STATE_UNKNOWN  # not "off"
    assert coordinator.data.info["serial"] == SERIAL
    assert router.logouts == 0

    router.fail_paths.clear()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(internet).state == "on"


async def test_restart_while_device_info_fails(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    """After a restart during an outage the router's identity comes from the registry."""
    connected = entity_id(hass, "sensor", f"{SERIAL}_connected_devices")
    await hass.config_entries.async_unload(entry.entry_id)
    router.fail_paths["/api/v1/deviceInfo"] = SagemcomBusyError(
        "GET /api/v1/deviceInfo: HTTP 400"
    )
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data.data.info["serial"] == SERIAL
    assert hass.states.get(connected).state == "4"
    device = dr.async_get(hass).async_get_device_by_identifier(
        ("sagemcom_rest", SERIAL), entry.entry_id
    )
    assert device.model == "F0000T"  # not overwritten with None

    router.fail_paths.clear()
    with patch.object(hass.config_entries, "async_schedule_reload") as reload:
        await entry.runtime_data.async_refresh()
        await hass.async_block_till_done()
    reload.assert_called_once_with(entry.entry_id)  # adds the Wi-Fi entities


async def test_device_info_failing_at_setup_retries(
    hass: HomeAssistant, router: FakeRouter
) -> None:
    """Without a first deviceInfo there is no serial to create entities with."""
    router.fail_paths["/api/v1/deviceInfo"] = SagemcomBusyError(
        "GET /api/v1/deviceInfo: HTTP 400"
    )
    entry = make_entry()
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_expired_credentials_start_reauth(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    router.fail_get = SagemcomAuthError("rejected")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_migration_disables_technical_entities(
    hass: HomeAssistant, router: FakeRouter
) -> None:
    entry = make_entry(minor_version=1)
    entry.add_to_hass(hass)
    ent_reg = er.async_get(hass)

    def add(unique_id: str, **kwargs) -> str:
        return ent_reg.async_get_or_create(
            "sensor", DOMAIN, unique_id, config_entry=entry, **kwargs
        ).entity_id

    technical = [
        add(f"{SERIAL}_cpu_load"),
        add(f"{SERIAL}_firmware"),
        add(f"{SERIAL}_wifi_5_channel"),
        add(f"{SERIAL}_port_eth1_speed"),
    ]
    kept = add(f"{SERIAL}_connected_devices")
    by_user = add(f"{SERIAL}_temperature", disabled_by=er.RegistryEntryDisabler.USER)

    await setup_entry(hass, entry)

    assert entry.minor_version == 2
    for eid in technical:
        assert ent_reg.async_get(eid).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert ent_reg.async_get(kept).disabled_by is None
    assert ent_reg.async_get(by_user).disabled_by is er.RegistryEntryDisabler.USER


async def test_technical_entities_disabled_on_new_install(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    ent_reg = er.async_get(hass)
    for key in ("cpu_load", "memory_usage", "firmware", "wifi_5_channel"):
        ent = ent_reg.async_get(entity_id(hass, "sensor", f"{SERIAL}_{key}"))
        assert ent.disabled_by is er.RegistryEntryDisabler.INTEGRATION, key
    for key in ("connected_devices", "download_throughput", "public_ipv4"):
        assert ent_reg.async_get(entity_id(hass, "sensor", f"{SERIAL}_{key}")).disabled_by is None


async def test_remove_devices(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    dev_reg = dr.async_get(hass)
    router_dev = dev_reg.async_get_device_by_identifier((DOMAIN, SERIAL), entry.entry_id)
    extender = dev_reg.async_get_device_by_identifier(
        (DOMAIN, f"mesh_{rd.EXTENDER_ID}"), entry.entry_id
    )
    assert extender

    # The router never; listed clients and mesh nodes not while they are present.
    assert not await async_remove_config_entry_device(hass, entry, router_dev)
    assert not await async_remove_config_entry_device(hass, entry, extender)
    assert not await async_remove_config_entry_device(
        hass, entry, _client_device(hass, entry, rd.OLD)
    )

    hosts = router.raw["hosts"][0]["hosts"]
    hosts["list"] = [h for h in hosts["list"] if h["macaddress"] != rd.OLD]
    mesh = router.raw["mesh"][0]
    mesh["meshDevices"] = [n for n in mesh["meshDevices"] if n["deviceId"] != rd.EXTENDER_ID]
    await entry.runtime_data.async_refresh()

    assert await async_remove_config_entry_device(
        hass, entry, _client_device(hass, entry, rd.OLD)
    )
    assert await async_remove_config_entry_device(hass, entry, extender)
    assert not await async_remove_config_entry_device(hass, entry, router_dev)
