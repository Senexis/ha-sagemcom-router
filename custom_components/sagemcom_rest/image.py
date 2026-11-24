"""QR codes for joining the Wi-Fi networks (scan with a phone camera)."""

from __future__ import annotations

from io import BytesIO
from typing import Any

import segno
from segno import helpers as segno_helpers

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import SagemcomConfigEntry, subentry_id
from .const import ROUTER_SUBENTRY_TYPE
from .coordinator import SagemcomCoordinator, WifiCredentials
from .entity import SagemcomEntity

# Networks without a passphrase (open, or Enhanced Open / OWE).
_NO_PASSWORD = ("none", "open", "owe")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities = [WifiQrImage(hass, coordinator, "main")]
    if coordinator.data.guest_wifi or "guest" in coordinator.wifi_credentials:
        entities.append(WifiQrImage(hass, coordinator, "guest"))
    async_add_entities(
        entities, config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE)
    )


def wifi_qr_payload(creds: WifiCredentials) -> str:
    """The text of a Wi-Fi QR code (the WIFI: format phones understand)."""
    protocol = creds.protocol.lower()
    open_network = not creds.passphrase or any(p in protocol for p in _NO_PASSWORD)
    return segno_helpers.make_wifi_data(
        ssid=creds.ssid,
        password=None if open_network else creds.passphrase,
        security="nopass" if open_network else "WPA",
        hidden=creds.hidden,
    )


def wifi_qr_png(creds: WifiCredentials) -> bytes:
    buffer = BytesIO()
    segno.make_qr(wifi_qr_payload(creds), error="m").save(
        buffer, kind="png", scale=10, border=2
    )
    return buffer.getvalue()


class WifiQrImage(SagemcomEntity, ImageEntity):
    """QR code with the network name and passphrase.

    The passphrase is only in the image, never in states or diagnostics. The
    main network's code is disabled by default; the guest network's is meant
    for showing to visitors.
    """

    _attr_content_type = "image/png"

    def __init__(
        self, hass: HomeAssistant, coordinator: SagemcomCoordinator, network: str
    ) -> None:
        super().__init__(coordinator, f"{network}_wifi_qr_code")
        ImageEntity.__init__(self, hass)
        self._network = network
        self._attr_name = "Guest Wi-Fi QR code" if network == "guest" else "Wi-Fi QR code"
        self._attr_icon = "mdi:qrcode"
        self._attr_entity_registry_enabled_default = network == "guest"
        self._creds = coordinator.wifi_credentials.get(network)
        self._png: bytes | None = None
        if self._creds:
            self._attr_image_last_updated = dt_util.utcnow()

    @property
    def available(self) -> bool:
        return super().available and self._creds is not None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._creds:
            return None
        return {"ssid": self._creds.ssid, "network_enabled": self._creds.enabled}

    @callback
    def _handle_coordinator_update(self) -> None:
        creds = self.coordinator.wifi_credentials.get(self._network)
        if creds != self._creds:
            self._creds = creds
            self._png = None
            self._attr_image_last_updated = dt_util.utcnow()
        super()._handle_coordinator_update()

    async def async_image(self) -> bytes | None:
        if self._creds is None:
            return None
        if self._png is None:
            self._png = await self.hass.async_add_executor_job(wifi_qr_png, self._creds)
        return self._png
