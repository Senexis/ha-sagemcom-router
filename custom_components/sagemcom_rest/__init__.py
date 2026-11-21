"""Sagemcom Router integration (read-only statistics)."""

from __future__ import annotations

from types import MappingProxyType

from aiohttp import CookieJar

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import SagemcomAuthError, SagemcomError, SagemcomRestClient
from .const import (
    CLIENTS_SUBENTRY_TYPE,
    CONF_ENABLE_CONTROLS,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    ROUTER_SUBENTRY_TYPE,
)
from .coordinator import SagemcomCoordinator
from .entity import router_device_info

PLATFORMS = [Platform.BINARY_SENSOR, Platform.DEVICE_TRACKER, Platform.SENSOR]
# Only set up when "Enable controls" is on: these entities change router settings.
CONTROL_PLATFORMS = [Platform.BUTTON, Platform.LIGHT, Platform.SWITCH]

type SagemcomConfigEntry = ConfigEntry[SagemcomCoordinator]


def create_client(hass: HomeAssistant, data: dict) -> SagemcomRestClient:
    """Client with its own cookie jar (session cookies are per router login).

    unsafe=True: aiohttp otherwise ignores cookies set by IP-address hosts.
    """
    session = async_create_clientsession(
        hass,
        verify_ssl=data.get(CONF_VERIFY_SSL, False),
        cookie_jar=CookieJar(unsafe=True),
    )
    return SagemcomRestClient(
        session,
        data[CONF_HOST],
        data[CONF_USERNAME],
        data[CONF_PASSWORD],
        verify_ssl=data.get(CONF_VERIFY_SSL, False),
    )


# Entities that became disabled-by-default in 1.2 (too technical for most users,
# or recorder noise). Matched on the unique_id suffix after the router serial.
_DISABLED_IN_1_2 = (
    "_wan_status", "_wan_link_speed", "_public_ipv6", "_wifi_devices", "_wired_devices",
    "_cpu_load", "_memory_usage", "_temperature", "_nat_table_usage", "_firmware",
    "_network_config", "_ntp_synced",
)


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.1 -> 1.2: disable the now disabled-by-default technical entities once."""
    if entry.version == 1 and entry.minor_version < 2:
        ent_reg = er.async_get(hass)
        for entity in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
            uid = entity.unique_id
            if entity.disabled_by is None and (
                uid.endswith(_DISABLED_IN_1_2)
                or (uid.rsplit("_", 1)[-1] == "channel" and "_wifi_" in uid)
                or ("_port_" in uid and uid.endswith("_speed"))
            ):
                ent_reg.async_update_entity(
                    entity.entity_id, disabled_by=er.RegistryEntryDisabler.INTEGRATION
                )
        hass.config_entries.async_update_entry(entry, minor_version=2)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SagemcomConfigEntry) -> bool:
    client = create_client(hass, dict(entry.data))
    try:
        await client.login()
    except SagemcomAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except SagemcomError as err:
        raise ConfigEntryNotReady(str(err)) from err

    coordinator = SagemcomCoordinator(
        hass,
        entry,
        client,
        entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
    )
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    _async_setup_subentries(hass, entry, coordinator)

    coordinator.platforms = _platforms(entry)
    await hass.config_entries.async_forward_entry_setups(entry, coordinator.platforms)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


def _platforms(entry: ConfigEntry) -> list[Platform]:
    if entry.options.get(CONF_ENABLE_CONTROLS, False):
        return PLATFORMS + CONTROL_PLATFORMS
    return PLATFORMS


def subentry_id(entry: ConfigEntry, subentry_type: str) -> str | None:
    """Id of the subentry of the given type (created in async_setup_entry)."""
    for subentry in entry.subentries.values():
        if subentry.subentry_type == subentry_type:
            return subentry.subentry_id
    return None


_SUBENTRY_TITLES = {ROUTER_SUBENTRY_TYPE: "Router", CLIENTS_SUBENTRY_TYPE: "Network clients"}


@callback
def _async_setup_subentries(
    hass: HomeAssistant, entry: SagemcomConfigEntry, coordinator: SagemcomCoordinator
) -> None:
    """Create the "Router" and "Network clients" subentries and move devices.

    The integration page then lists the router and its clients in separate groups.
    """
    ids: dict[str, str] = {}
    for sub_type, title in _SUBENTRY_TITLES.items():
        if (sub_id := subentry_id(entry, sub_type)) is None:
            subentry = ConfigSubentry(
                data=MappingProxyType({}),
                subentry_type=sub_type,
                title=title,
                unique_id=sub_type,
            )
            hass.config_entries.async_add_subentry(entry, subentry)
            sub_id = subentry.subentry_id
        ids[sub_type] = sub_id

    router_id = (DOMAIN, coordinator.data.info.get("serial") or coordinator.client.host)
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        target = ids[
            ROUTER_SUBENTRY_TYPE if router_id in device.identifiers else CLIENTS_SUBENTRY_TYPE
        ]
        if device.config_subentry_id == target:
            continue
        # Entities first: moving the device drops entities left in the old subentry.
        for entity in er.async_entries_for_device(
            ent_reg, device.id, include_disabled_entities=True
        ):
            if entity.config_entry_id == entry.entry_id:
                ent_reg.async_update_entity(entity.entity_id, config_subentry_id=target)
        dev_reg.async_update_device(device.id, new_config_subentry_id=target)
    # Register the router (new installs) so client devices can point to it.
    dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=ids[ROUTER_SUBENTRY_TYPE],
        **router_device_info(coordinator),
    )


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: SagemcomConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a client device in the UI once the router no longer lists it."""
    if any(ident[0] == DOMAIN for ident in device.identifiers):
        return False  # the router itself
    hosts = entry.runtime_data.data.hosts
    return not any(
        conn[0] == dr.CONNECTION_NETWORK_MAC and conn[1] in hosts
        for conn in device.connections
    )


async def _async_reload(hass: HomeAssistant, entry: SagemcomConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: SagemcomConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, entry.runtime_data.platforms
    )
    if unloaded:
        # Free the router's (limited) web session slot.
        await entry.runtime_data.client.logout()
    return unloaded
