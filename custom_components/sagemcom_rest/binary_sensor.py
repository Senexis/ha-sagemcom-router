"""Binary sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SagemcomConfigEntry, subentry_id
from .const import ROUTER_SUBENTRY_TYPE
from .coordinator import RouterData
from .entity import SagemcomEntity


def _up(value: Any) -> bool | None:
    if value is None:
        return None
    return str(value).lower() == "up"


@dataclass(frozen=True, kw_only=True)
class RouterBinaryDescription(BinarySensorEntityDescription):
    value_fn: Callable[[RouterData], bool | None]
    attrs_fn: Callable[[RouterData], dict[str, Any]] | None = None


BINARY_SENSORS: tuple[RouterBinaryDescription, ...] = (
    RouterBinaryDescription(
        key="internet",
        name="Internet",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        value_fn=lambda d: d.wan.get("connected"),
        attrs_fn=lambda d: {"status": d.wan.get("status"), "public_ipv4": d.wan.get("ipv4")},
    ),
    RouterBinaryDescription(
        key="guest_wifi",
        name="Guest Wi-Fi",
        icon="mdi:wifi-star",
        value_fn=lambda d: (
            any(_up(g.get("status")) for g in d.guest_wifi.values())
            if d.guest_wifi
            else None
        ),
        attrs_fn=lambda d: {
            "ssid": next((g.get("ssid") for g in d.guest_wifi.values()), None),
            "bands_up": [b for b, g in d.guest_wifi.items() if _up(g.get("status"))],
        },
    ),
    RouterBinaryDescription(
        key="ntp_synced",
        entity_registry_enabled_default=False,
        name="Time synchronized",
        icon="mdi:clock-check-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (
            d.misc["ntp_status"] == "SYNCHRONIZED" if d.misc.get("ntp_status") else None
        ),
    ),
    RouterBinaryDescription(
        key="temperature_problem",
        name="Temperature problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: (
            d.system["temperature_status"].lower() != "ok"
            if d.system.get("temperature_status")
            else None
        ),
    ),
)


def _band_binary(band: str) -> RouterBinaryDescription:
    return RouterBinaryDescription(
        key=f"wifi_{band.replace('.', '')}_up",
        name=f"Wi-Fi {band} GHz",
        icon="mdi:wifi",
        value_fn=lambda d: _up(d.wifi.get(band, {}).get("status")),
        attrs_fn=lambda d: {"ssid": d.wifi.get(band, {}).get("ssid")},
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    descriptions = list(BINARY_SENSORS) + [_band_binary(b) for b in coordinator.data.wifi]
    async_add_entities(
        (RouterBinarySensor(coordinator, d) for d in descriptions),
        config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )


class RouterBinarySensor(SagemcomEntity, BinarySensorEntity):
    entity_description: RouterBinaryDescription

    def __init__(self, coordinator, description: RouterBinaryDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._update_values()

    def _update_values(self) -> None:
        data = self.coordinator.data
        self._attr_is_on = self.entity_description.value_fn(data)
        if self.entity_description.attrs_fn:
            self._attr_extra_state_attributes = self.entity_description.attrs_fn(data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_values()
        super()._handle_coordinator_update()
