"""Base entities and helpers shared by the platforms."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SagemcomError
from .const import CLIENTS_SUBENTRY_TYPE, CONF_ENABLE_CONTROLS, DOMAIN
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


def async_add_dynamic_entities(
    entry: ConfigEntry,
    coordinator: SagemcomCoordinator,
    async_add_entities: AddEntitiesCallback,
    keys: Callable[[], list[str]],
    factory: Callable[[str], list[Entity]],
    config_subentry_id: str | None,
) -> None:
    """Add entities for every key now and for keys that show up later."""
    tracked: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [key for key in keys() if key not in tracked]
        if new:
            tracked.update(new)
            entities = [e for key in new for e in factory(key)]
            async_add_entities(entities, config_subentry_id=config_subentry_id)

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


def async_add_client_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: SagemcomCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[SagemcomCoordinator, str, str | None], list[Entity]],
) -> None:
    """Add entities for every network client (under "Network clients")."""
    from . import subentry_id  # noqa: PLC0415  (avoid import cycle)

    via = router_device_id(hass, entry, coordinator)
    async_add_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda: list(coordinator.data.hosts),
        lambda mac: factory(coordinator, mac, via),
        subentry_id(entry, CLIENTS_SUBENTRY_TYPE),
    )


def extender_ids(coordinator: SagemcomCoordinator) -> list[str]:
    return [n["id"] for n in coordinator.data.mesh_nodes if n["id"] and not n["is_gateway"]]


def async_add_extender_entities(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: SagemcomCoordinator,
    async_add_entities: AddEntitiesCallback,
    factory: Callable[[SagemcomCoordinator, str, str | None], list[Entity]],
) -> None:
    """Add entities for every mesh extender (under "Router")."""
    from . import subentry_id  # noqa: PLC0415  (avoid import cycle)
    from .const import ROUTER_SUBENTRY_TYPE  # noqa: PLC0415

    via = router_device_id(hass, entry, coordinator)
    async_add_dynamic_entities(
        entry,
        coordinator,
        async_add_entities,
        lambda: extender_ids(coordinator),
        lambda node_id: factory(coordinator, node_id, via),
        subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )


def controls_enabled(entry: ConfigEntry) -> bool:
    return bool(entry.options.get(CONF_ENABLE_CONTROLS, False))


def client_blocked(coordinator: SagemcomCoordinator, mac: str) -> bool:
    """Whether an enabled "always block" parental-control rule exists for the MAC."""
    controls = coordinator.data.controls
    rule = controls.get("parental_rules", {}).get(mac)
    return bool(
        controls.get("parental_enabled")
        and rule
        and rule.get("enable")
        and rule.get("AlwaysBlock")
        and not rule.get("timeslots")
    )


@callback
def async_remove_stale_entities(
    hass: HomeAssistant, entry: ConfigEntry, domain: str, keep: Callable[[str], bool]
) -> None:
    """Remove this entry's `domain` entities for which keep(unique_id) is false.

    Used where the entity set depends on the "Enable controls" option, so a
    switched-off variant doesn't linger as "unavailable".
    """
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if ent.domain == domain and not keep(ent.unique_id):
            registry.async_remove(ent.entity_id)


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


class SagemcomExtenderEntity(CoordinatorEntity[SagemcomCoordinator]):
    """Entity attached to a mesh extender device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: SagemcomCoordinator, node_id: str, key: str, via: str | None
    ) -> None:
        super().__init__(coordinator)
        self._node_id = node_id
        self._attr_unique_id = f"mesh_{node_id}_{key}"
        node = self.node or {}
        self._attr_device_info = DeviceInfo(
            # No MAC connection: the extender may also be listed as a network
            # client with that MAC, and connections must be unique per entry.
            identifiers={(DOMAIN, f"mesh_{node_id}")},
            name=node.get("name") or f"Mesh node {node_id}",
            manufacturer=node.get("manufacturer"),
            model=node.get("model"),
            serial_number=node.get("serial"),
            sw_version=node.get("firmware"),
            via_device_id=via,
        )

    @property
    def node(self) -> dict[str, Any] | None:
        return next(
            (n for n in self.coordinator.data.mesh_nodes if n["id"] == self._node_id), None
        )

    @property
    def available(self) -> bool:
        return super().available and self.node is not None


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
