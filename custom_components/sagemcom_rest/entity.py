"""Base entities and helpers shared by the platforms."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SagemcomError
from .const import CLIENTS_SUBENTRY_TYPE, DOMAIN
from .coordinator import SagemcomCoordinator


def router_key(coordinator: SagemcomCoordinator) -> str:
    return coordinator.data.info.get("serial") or coordinator.client.host


def router_device_info(coordinator: SagemcomCoordinator) -> DeviceInfo:
    info = coordinator.data.info
    host = coordinator.client.host
    connections = set()
    if info.get("mac"):
        connections.add((CONNECTION_NETWORK_MAC, info["mac"].lower()))
    return DeviceInfo(
        identifiers={(DOMAIN, router_key(coordinator))},
        connections=connections,
        manufacturer=info.get("manufacturer"),
        model=info.get("model"),
        name=f"Router {info.get('model') or host}",
        serial_number=info.get("serial"),
        sw_version=info.get("firmware"),
        hw_version=info.get("hardware"),
        configuration_url=f"https://{host}",
    )


def router_device_id(
    hass: HomeAssistant, entry: ConfigEntry, coordinator: SagemcomCoordinator
) -> str | None:
    """Registry id of the router device (registered in async_setup_entry)."""
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, router_key(coordinator)), entry.entry_id
    )
    return device.id if device else None


def client_device_info(
    coordinator: SagemcomCoordinator, mac: str, via_device_id: str | None
) -> DeviceInfo:
    """Device of a network client (one per MAC, under "Network clients")."""
    host = coordinator.data.hosts.get(mac, {})
    return DeviceInfo(
        connections={(CONNECTION_NETWORK_MAC, mac)},
        # A name the user sets in the UI (name_by_user) still wins.
        name=host.get("name") or host.get("hostname") or mac,
        via_device_id=via_device_id,
    )


def async_add_client_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: SagemcomCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[SagemcomCoordinator, str, str | None], list[Entity]],
) -> None:
    """Add entities for every client now and for clients that show up later."""
    from . import subentry_id  # noqa: PLC0415  (avoid import cycle)

    tracked: set[str] = set()
    clients_id = subentry_id(entry, CLIENTS_SUBENTRY_TYPE)
    via = router_device_id(hass, entry, coordinator)

    @callback
    def _add_new() -> None:
        new = [mac for mac in coordinator.data.hosts if mac not in tracked]
        if new:
            tracked.update(new)
            entities = [e for mac in new for e in factory(coordinator, mac, via)]
            async_add_entities(entities, config_subentry_id=clients_id)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


async def async_write(call: Awaitable[Any], action: str) -> Any:
    """Run a router write; report failures in the UI instead of a traceback."""
    try:
        return await call
    except SagemcomError as err:
        raise HomeAssistantError(f"Router refused to {action}: {err}") from err


class SagemcomEntity(CoordinatorEntity[SagemcomCoordinator]):
    """Entity attached to the router device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SagemcomCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{router_key(coordinator)}_{key}"
        self._attr_device_info = router_device_info(coordinator)


class SagemcomClientEntity(CoordinatorEntity[SagemcomCoordinator]):
    """Entity attached to a network client device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SagemcomCoordinator, mac: str, key: str, via: str | None
    ) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = f"{mac}_{key}"
        self._attr_device_info = client_device_info(coordinator, mac, via)

    @property
    def host(self) -> dict[str, Any]:
        return self.coordinator.data.hosts.get(self._mac, {})
