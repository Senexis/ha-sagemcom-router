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
from .entity import (
    SagemcomClientEntity,
    SagemcomEntity,
    SagemcomExtenderEntity,
    async_add_client_entities,
    async_add_extender_entities,
    async_remove_stale_entities,
    client_blocked,
    controls_enabled,
    router_key,
)


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


def _radio_binary(band: str) -> RouterBinaryDescription:
    """Read-only twin of the band's radio switch (used while controls are off)."""
    return RouterBinaryDescription(
        key=f"wifi_{band.replace('.', '')}_radio",
        name=f"Wi-Fi {band} GHz radio",
        icon="mdi:wifi",
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.wifi.get(band, {}).get("enabled"),
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    controls = controls_enabled(entry)
    if controls:
        # The Guest Wi-Fi and radio switches show these states.
        descriptions = [d for d in BINARY_SENSORS if d.key != "guest_wifi"]
    else:
        descriptions = list(BINARY_SENSORS) + [_radio_binary(b) for b in coordinator.data.wifi]
    async_add_entities(
        (RouterBinarySensor(coordinator, d) for d in descriptions),
        config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )
    if not controls:
        async_add_client_entities(
            hass,
            entry,
            coordinator,
            async_add_entities,
            lambda c, mac, via: [ClientBlockedSensor(c, mac, via)],
        )

    # Remove what this setup no longer provides (the old per-band status sensors,
    # and the read-only twins while controls are on).
    prefix = f"{router_key(coordinator)}_"
    wanted = {prefix + d.key for d in descriptions}
    async_remove_stale_entities(
        hass,
        entry,
        "binary_sensor",
        lambda uid: (
            uid in wanted
            if uid.startswith(prefix)
            # Client "Internet blocked" twins exist only while controls are off.
            else not (controls and uid.endswith("_blocked"))
        ),
    )
    async_add_extender_entities(
        hass,
        entry,
        coordinator,
        async_add_entities,
        lambda c, node_id, via: [ExtenderConnectedSensor(c, node_id, via)],
    )


class ExtenderConnectedSensor(SagemcomExtenderEntity, BinarySensorEntity):
    """Whether the mesh extender is currently part of the mesh."""

    _attr_name = "Connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator, node_id: str, via: str | None) -> None:
        super().__init__(coordinator, node_id, "connected", via)

    @property
    def available(self) -> bool:
        # Leaving the mesh is the "off" state, not "unavailable".
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        return self.node is not None

    @property
    def extra_state_attributes(self) -> dict:
        node = self.node or {}
        return {k: node.get(k) for k in ("ipv4", "backhaul_quality") if node.get(k)}


class ClientBlockedSensor(SagemcomClientEntity, BinarySensorEntity):
    """Read-only twin of "Block internet" (used while controls are off)."""

    _attr_name = "Internet blocked"
    _attr_icon = "mdi:web-cancel"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, mac: str, via: str | None) -> None:
        super().__init__(coordinator, mac, "blocked", via)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.controls.get("parental_enabled") is not None

    @property
    def is_on(self) -> bool:
        return client_blocked(self.coordinator, self._mac)


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
