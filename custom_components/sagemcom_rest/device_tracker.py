"""Device trackers for clients known to the router.

Like other router integrations, HA creates these disabled by default unless the
MAC already belongs to a device in the registry; enable the ones you want
(e.g. phones) for presence detection.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.device_tracker import ScannerEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import SagemcomConfigEntry, subentry_id
from .const import CLIENTS_SUBENTRY_TYPE, DOMAIN
from .coordinator import SagemcomCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    tracked: set[str] = set()
    clients_id = subentry_id(entry, CLIENTS_SUBENTRY_TYPE)
    # The router device is registered in async_setup_entry, before the platforms.
    router_key = coordinator.data.info.get("serial") or coordinator.client.host
    router = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, router_key), entry.entry_id
    )
    router_device_id = router.id if router else None

    @callback
    def _add_new() -> None:
        new = [mac for mac in coordinator.data.hosts if mac not in tracked]
        if new:
            tracked.update(new)
            async_add_entities(
                (RouterClientTracker(coordinator, mac, router_device_id) for mac in new),
                config_subentry_id=clients_id,
            )

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))


class RouterClientTracker(CoordinatorEntity[SagemcomCoordinator], ScannerEntity):
    """A LAN client seen by the router."""

    _unrecorded_attributes = frozenset({"ipv6", "last_seen", "rate_mbps", "signal_dbm"})

    def __init__(
        self, coordinator: SagemcomCoordinator, mac: str, router_device_id: str | None
    ) -> None:
        super().__init__(coordinator)
        self._mac = mac
        self._attr_unique_id = f"{mac}_tracker"
        host = self._host
        # A name the user sets in the UI (name_by_user) still wins.
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, mac)},
            name=host.get("name") or host.get("hostname") or mac,
            via_device_id=router_device_id,
        )

    @property
    def _host(self) -> dict[str, Any]:
        return self.coordinator.data.hosts.get(self._mac, {})

    @property
    def name(self) -> str:
        return self._host.get("name") or self._mac

    @property
    def is_connected(self) -> bool:
        return bool(self._host.get("active"))

    @property
    def mac_address(self) -> str:
        return self._mac

    @property
    def ip_address(self) -> str | None:
        return self._host.get("ip")

    @property
    def hostname(self) -> str | None:
        return self._host.get("hostname")

    @property
    def icon(self) -> str:
        if not self.is_connected:
            return "mdi:lan-disconnect"
        return "mdi:wifi" if self._host.get("connection") == "wifi" else "mdi:lan-connect"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        h = self._host
        attrs = {
            "connection": h.get("connection"),
            "band": h.get("band"),
            "signal_dbm": h.get("signal"),
            "link_quality": h.get("link_quality"),
            "rate_mbps": h.get("rate_mbps"),
            "access_point": h.get("access_point"),
            "ipv6": h.get("ipv6") or None,
            "last_seen": h.get("last_seen"),
        }
        return {k: v for k, v in attrs.items() if v not in (None, "", [])}
