"""Router LEDs as a dimmable light (only with "Enable controls")."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import ATTR_BRIGHTNESS, ColorMode, LightEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import SagemcomConfigEntry, subentry_id
from .const import ROUTER_SUBENTRY_TYPE
from .coordinator import SagemcomCoordinator
from .entity import SagemcomEntity, async_write

LED = "/api/v1/led"
DEFAULT_LEVELS = [0, 25, 50, 75, 100]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        [RouterLedLight(entry.runtime_data)],
        config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )


class RouterLedLight(SagemcomEntity, LightEntity):
    """LED brightness; the router only supports a few levels (0/25/50/75/100 %)."""

    _attr_name = "LEDs"
    _attr_icon = "mdi:led-on"
    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: SagemcomCoordinator) -> None:
        super().__init__(coordinator, "led")
        self._last_on_level = self._level or 100

    @property
    def _level(self) -> int | None:
        return self.coordinator.data.controls.get("led_brightness")

    @property
    def _levels(self) -> list[int]:
        return [lvl for lvl in self.coordinator.data.controls.get("led_levels") or DEFAULT_LEVELS if lvl > 0]

    @property
    def is_on(self) -> bool | None:
        return None if self._level is None else self._level > 0

    @property
    def brightness(self) -> int | None:
        if not self._level:
            return None
        return round(self._level * 255 / 100)

    @callback
    def _handle_coordinator_update(self) -> None:
        if self._level:
            self._last_on_level = self._level  # restored by a plain "turn on"
        super()._handle_coordinator_update()

    async def _set(self, level: int) -> None:
        await async_write(
            self.coordinator.client.write("PUT", LED, {"brightness": level}), "set the LEDs"
        )
        await self.coordinator.async_refetch("led")

    async def async_turn_on(self, **kwargs: Any) -> None:
        level = self._last_on_level
        if ATTR_BRIGHTNESS in kwargs:
            wanted = kwargs[ATTR_BRIGHTNESS] * 100 / 255
            level = min(self._levels or [100], key=lambda lvl: abs(lvl - wanted))
        await self._set(level)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(0)
