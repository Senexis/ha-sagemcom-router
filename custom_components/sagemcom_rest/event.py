"""Event entity: a client the integration hasn't seen before joined the network."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SagemcomConfigEntry, subentry_id
from .const import ROUTER_SUBENTRY_TYPE
from .coordinator import SagemcomCoordinator
from .entity import SagemcomEntity

EVENT_NEW_CLIENT = "new_client"
_EVENT_FIELDS = ("mac", "name", "hostname", "ip", "connection", "band", "network")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [NewClientEvent(entry.runtime_data)],
        config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )


class NewClientEvent(SagemcomEntity, EventEntity):
    """Fires once per new client, e.g. to notify about unknown devices.

    Event data: mac, name, hostname, ip, connection, band, network (main/guest).
    """

    _attr_name = "New client"
    _attr_icon = "mdi:account-network"
    _attr_event_types = [EVENT_NEW_CLIENT]

    def __init__(self, coordinator: SagemcomCoordinator) -> None:
        super().__init__(coordinator, "new_client")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Clients found on the first refresh (joined while HA was down).
        self._fire(self.coordinator.data.new_clients)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._fire(self.coordinator.data.new_clients)
        super()._handle_coordinator_update()

    @callback
    def _fire(self, clients: list[dict[str, Any]]) -> None:
        # One state write per event, so automations see every client.
        for host in clients:
            self._trigger_event(
                EVENT_NEW_CLIENT,
                {k: host.get(k) for k in _EVENT_FIELDS if host.get(k) is not None},
            )
            self.async_write_ha_state()
