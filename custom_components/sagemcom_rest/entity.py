"""Base entity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SagemcomCoordinator


def router_device_info(coordinator: SagemcomCoordinator) -> DeviceInfo:
    info = coordinator.data.info
    host = coordinator.client.host
    connections = set()
    if info.get("mac"):
        connections.add((CONNECTION_NETWORK_MAC, info["mac"].lower()))
    return DeviceInfo(
        identifiers={(DOMAIN, info.get("serial") or host)},
        connections=connections,
        manufacturer=info.get("manufacturer"),
        model=info.get("model"),
        name=f"Router {info.get('model') or host}",
        serial_number=info.get("serial"),
        sw_version=info.get("firmware"),
        hw_version=info.get("hardware"),
        configuration_url=f"https://{host}",
    )


class SagemcomEntity(CoordinatorEntity[SagemcomCoordinator]):
    """Entity attached to the router device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: SagemcomCoordinator, key: str) -> None:
        super().__init__(coordinator)
        serial = coordinator.data.info.get("serial") or coordinator.client.host
        self._attr_unique_id = f"{serial}_{key}"
        self._attr_device_info = router_device_info(coordinator)
