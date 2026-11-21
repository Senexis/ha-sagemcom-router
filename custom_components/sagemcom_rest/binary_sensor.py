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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SagemcomConfigEntry, subentry_id
from .const import CONF_ENABLE_CONTROLS, ROUTER_SUBENTRY_TYPE
from .coordinator import RouterData
from .entity import SagemcomEntity, router_key


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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    controls = entry.options.get(CONF_ENABLE_CONTROLS, False)
    # With controls on, the Guest Wi-Fi switch shows the same state.
    descriptions = [d for d in BINARY_SENSORS if not (controls and d.key == "guest_wifi")]
    async_add_entities(
        (RouterBinarySensor(coordinator, d) for d in descriptions),
        config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )
    # Drop entities this setup no longer provides: the per-band "Wi-Fi X GHz"
    # sensors (status is an attribute of the band's clients sensor; the radio
    # switches cover on/off) and the guest sensor while controls are on.
    prefix = f"{router_key(coordinator)}_"
    wanted = {prefix + d.key for d in descriptions}
    registry = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(registry, entry.entry_id):
        if (
            ent.domain == "binary_sensor"
            and ent.unique_id.startswith(prefix)
            and ent.unique_id not in wanted
        ):
            registry.async_remove(ent.entity_id)


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
