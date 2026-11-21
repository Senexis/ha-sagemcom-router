"""Buttons (only with "Enable controls"): reboot, WPS pairing, forget a client."""

from __future__ import annotations

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SagemcomConfigEntry, subentry_id
from .const import ROUTER_SUBENTRY_TYPE
from .coordinator import SagemcomCoordinator
from .entity import SagemcomClientEntity, SagemcomEntity, async_add_client_entities, async_write


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [RebootButton(coordinator), WpsButton(coordinator)],
        config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )
    async_add_client_entities(
        hass,
        entry,
        coordinator,
        async_add_entities,
        lambda c, mac, via: [ForgetDeviceButton(c, mac, via)],
    )


class RebootButton(SagemcomEntity, ButtonEntity):
    """Reboot the router. Disabled by default: the whole network goes down for minutes."""

    _attr_name = "Reboot"
    _attr_device_class = ButtonDeviceClass.RESTART
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: SagemcomCoordinator) -> None:
        super().__init__(coordinator, "reboot")

    async def async_press(self) -> None:
        await async_write(
            self.coordinator.client.write("POST", "/api/v1/device/reboot", {}), "reboot"
        )


class WpsButton(SagemcomEntity, ButtonEntity):
    """Start WPS push-button pairing on all bands (same as the router's WPS button)."""

    _attr_name = "Start WPS pairing"
    _attr_icon = "mdi:wifi-plus"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SagemcomCoordinator) -> None:
        super().__init__(coordinator, "wps_pushbutton")

    async def async_press(self) -> None:
        await async_write(
            self.coordinator.client.write(
                "POST", "/api/v2/wireless/pairing/wps/pushbutton/all", {}
            ),
            "start WPS pairing",
        )


class ForgetDeviceButton(SagemcomClientEntity, ButtonEntity):
    """Remove an offline client from the router's device list and from HA."""

    _attr_name = "Forget device"
    _attr_icon = "mdi:delete-outline"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SagemcomCoordinator, mac: str, via: str | None) -> None:
        super().__init__(coordinator, mac, "forget", via)

    @property
    def available(self) -> bool:
        # Forgetting a connected device is pointless: the router re-adds it at once.
        return super().available and bool(self.host) and not self.host.get("active")

    async def async_press(self) -> None:
        host = self.host
        if not host or host.get("active"):
            raise HomeAssistantError("Only offline devices can be forgotten")
        if host.get("router_id") is None:
            raise HomeAssistantError("The router did not report an id for this device")
        await async_write(
            self.coordinator.client.write("DELETE", f"/api/v1/host/{host['router_id']}"),
            "forget the device",
        )
        entry = self.coordinator.config_entry
        registry = dr.async_get(self.hass)
        if device := registry.async_get_device_by_connection(
            (CONNECTION_NETWORK_MAC, self._mac), entry.entry_id
        ):
            registry.async_remove_device(device.id)
        # Rebuild the per-client entities without the forgotten device.
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
