"""Switches (only with "Enable controls"): guest Wi-Fi, Wi-Fi bands, internet blocking."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SagemcomConfigEntry, subentry_id
from .const import BANDS, ROUTER_SUBENTRY_TYPE
from .coordinator import SagemcomCoordinator
from .entity import SagemcomClientEntity, SagemcomEntity, async_add_client_entities, async_write

PARENTAL = "/api/v1/parental_control"
DEVICE_RULE = "/api/v1/parental_control/device_schedule"
DEVICE_RULE_CONFIG = "/api/v1/parental_control/device_schedule/config"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    router: list[SwitchEntity] = [GuestWifiSwitch(coordinator)]
    router += [WifiBandSwitch(coordinator, band) for band in coordinator.data.wifi]
    async_add_entities(router, config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE))
    async_add_client_entities(
        hass,
        entry,
        coordinator,
        async_add_entities,
        lambda c, mac, via: [BlockInternetSwitch(c, mac, via)],
    )


class GuestWifiSwitch(SagemcomEntity, SwitchEntity):
    """Guest network on all bands at once (like the router's guest page)."""

    _attr_name = "Guest Wi-Fi"
    _attr_icon = "mdi:wifi-star"

    def __init__(self, coordinator: SagemcomCoordinator) -> None:
        super().__init__(coordinator, "guest_wifi_switch")

    @property
    def is_on(self) -> bool | None:
        guest = self.coordinator.data.guest_wifi
        if not guest:
            return None
        return any(str(g.get("status")).lower() == "up" for g in guest.values())

    async def _set(self, on: bool) -> None:
        guest = self.coordinator.data.guest_wifi
        for band, ident in BANDS.items():
            if band not in guest:
                continue
            body: dict[str, Any] = {"enable": 1 if on else 0}
            if ssid := guest[band].get("ssid"):
                body["ssid"] = ssid
            await async_write(
                self.coordinator.client.write("PUT", f"/api/v2/wireless/guest{ident}", body),
                "switch guest Wi-Fi",
            )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class WifiBandSwitch(SagemcomEntity, SwitchEntity):
    """Radio of one band (turning it off also drops the guest SSID on it)."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SagemcomCoordinator, band: str) -> None:
        super().__init__(coordinator, f"wifi_{band.replace('.', '')}_radio")
        self._band = band
        self._attr_name = f"Wi-Fi {band} GHz radio"

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.data.wifi.get(self._band, {}).get("enabled")

    @property
    def icon(self) -> str:
        return "mdi:wifi" if self.is_on else "mdi:wifi-off"

    async def _set(self, on: bool) -> None:
        body: dict[str, Any] = {"radio_enable": on}
        # The web UI always sends the SSID along with a band change.
        if ssid := self.coordinator.data.wifi.get(self._band, {}).get("ssid"):
            body["ssid"] = ssid
        await async_write(
            self.coordinator.client.write(
                "PUT", f"/api/v2/wireless/{BANDS[self._band]}", body
            ),
            f"switch the {self._band} GHz radio",
        )
        await self.coordinator.async_refetch(f"wifi_{self._band}")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)


class BlockInternetSwitch(SagemcomClientEntity, SwitchEntity):
    """Block internet access for one client, via the router's parental control.

    On = an enabled "always block" rule exists for the MAC. Rules with time
    slots (made in the router UI) are left alone.
    """

    _attr_name = "Block internet"
    _attr_icon = "mdi:web-cancel"
    # Config category: HA doesn't auto-expose these to voice assistants.
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: SagemcomCoordinator, mac: str, via: str | None) -> None:
        super().__init__(coordinator, mac, "block_internet", via)

    @property
    def _rule(self) -> dict[str, Any] | None:
        return self.coordinator.data.controls.get("parental_rules", {}).get(self._mac)

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.controls.get("parental_enabled") is not None

    @property
    def is_on(self) -> bool:
        rule = self._rule
        return bool(
            self.coordinator.data.controls.get("parental_enabled")
            and rule
            and rule.get("enable")
            and rule.get("AlwaysBlock")
            and not rule.get("timeslots")
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rule = self._rule
        return {"custom_schedule": bool(rule and rule.get("timeslots"))}

    async def async_turn_on(self, **kwargs: Any) -> None:
        client = self.coordinator.client
        mac = self.host.get("router_mac") or self._mac
        if not self.coordinator.data.controls.get("parental_enabled"):
            await async_write(client.write("PUT", PARENTAL, {"enable": True}), "enable parental control")
        rule = self._rule
        if rule is None:
            await async_write(
                client.write("POST", DEVICE_RULE, {"MACAddress": mac, "AlwaysBlock": True}),
                "add a block rule",
            )
            await self.coordinator.async_refetch("parental")
            if (rule := self._rule) is None:
                raise HomeAssistantError(f"The router did not create a block rule for {mac}")
        elif rule.get("timeslots"):
            raise HomeAssistantError(
                "This device has a time schedule in the router's parental control; "
                "change it in the router's web interface"
            )
        elif not rule.get("AlwaysBlock"):
            await async_write(
                client.write("PUT", f"{DEVICE_RULE_CONFIG}/{rule['id']}", {"AlwaysBlock": True}),
                "update the block rule",
            )
        await async_write(
            client.write(
                "PUT", f"{DEVICE_RULE}/{rule['id']}", {"enable": True, "macaddress": rule.get("SourceMac") or mac}
            ),
            "enable the block rule",
        )
        await self.coordinator.async_refetch("parental")

    async def async_turn_off(self, **kwargs: Any) -> None:
        if (rule := self._rule) is None:
            return
        await async_write(
            self.coordinator.client.write(
                "PUT", f"{DEVICE_RULE}/{rule['id']}", {"enable": False, "macaddress": rule.get("SourceMac")}
            ),
            "disable the block rule",
        )
        await self.coordinator.async_refetch("parental")
