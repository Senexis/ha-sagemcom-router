"""Diagnostics download (redacted)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import SagemcomConfigEntry

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    "serial",
    "serialNumber",
    "serial_number",
    "serialnumber",
    "ipv4",
    "ipv6",
    "publicIpv4Address",
    "publicIpv4",
    "publicIpv6",
    "wan_ipv4",
    "address",
    "globalGatewayIpv6Address",
    "imei",
    "uuid",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SagemcomConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "unsupported_endpoints": sorted(coordinator._unsupported),
        "data": async_redact_data(asdict(coordinator.data), TO_REDACT),
    }
