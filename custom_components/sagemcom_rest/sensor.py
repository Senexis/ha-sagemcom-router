"""Sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfDataRate,
    UnitOfInformation,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

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
    controls_enabled,
    router_key,
)

BAND_LABEL = {"2.4": "2.4 GHz", "5": "5 GHz", "6": "6 GHz"}


@dataclass(frozen=True, kw_only=True)
class RouterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[RouterData], Any]
    attrs_fn: Callable[[RouterData], dict[str, Any]] | None = None
    # Value is "seconds since X": exposed as a stable timestamp.
    since_seconds: bool = False


def _active(d: RouterData) -> list[dict[str, Any]]:
    return [h for h in d.hosts.values() if h["active"]]


def _device_list(d: RouterData) -> dict[str, Any]:
    devices = sorted(_active(d), key=lambda h: h["name"].lower())
    return {
        "devices": [
            {
                k: v
                for k, v in {
                    "name": h["name"],
                    "ip": h["ip"],
                    "mac": h["mac"],
                    "connection": h["connection"],
                    "band": h["band"],
                    "signal_dbm": h["signal"],
                }.items()
                if v not in (None, "")
            }
            for h in devices
        ],
        "wifi": sum(1 for h in devices if h["connection"] == "wifi"),
        "wired": sum(1 for h in devices if h["connection"] != "wifi"),
        "known_devices": len(d.hosts),
    }


SENSORS: tuple[RouterSensorDescription, ...] = (
    # ---- Internet / WAN
    RouterSensorDescription(
        key="download_throughput",
        name="Download throughput",
        icon="mdi:download-network",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.wan.get("download_mbps"),
    ),
    RouterSensorDescription(
        key="upload_throughput",
        name="Upload throughput",
        icon="mdi:upload-network",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda d: d.wan.get("upload_mbps"),
    ),
    RouterSensorDescription(
        key="wan_downloaded",
        name="Data downloaded",
        icon="mdi:download",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.wan.get("rx_bytes"),
    ),
    RouterSensorDescription(
        key="wan_uploaded",
        name="Data uploaded",
        icon="mdi:upload",
        device_class=SensorDeviceClass.DATA_SIZE,
        native_unit_of_measurement=UnitOfInformation.BYTES,
        suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
        suggested_display_precision=2,
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda d: d.wan.get("tx_bytes"),
    ),
    RouterSensorDescription(
        key="wan_status",
        entity_registry_enabled_default=False,
        name="Internet status",
        icon="mdi:web",
        value_fn=lambda d: d.wan.get("status"),
        attrs_fn=lambda d: {
            "addressing": d.wan.get("addressing"),
            "link_state": d.wan.get("link_state"),
        },
    ),
    RouterSensorDescription(
        key="wan_connected_since",
        name="Internet connected since",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.TIMESTAMP,
        since_seconds=True,
        value_fn=lambda d: d.wan.get("online_duration") if d.wan.get("connected") else None,
    ),
    RouterSensorDescription(
        key="wan_link_speed",
        entity_registry_enabled_default=False,
        name="WAN link speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.wan.get("link_speed"),
    ),
    RouterSensorDescription(
        key="public_ipv4",
        name="Public IPv4 address",
        icon="mdi:ip-network",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.wan.get("ipv4"),
        attrs_fn=lambda d: {
            "subnet_mask": d.wan.get("ipv4_subnet"),
            "gateway": d.wan.get("ipv4_gateway"),
            "dns_servers": d.wan.get("dns_servers"),
            "carrier_grade_nat": _is_cgnat(d.wan.get("ipv4")),
        },
    ),
    RouterSensorDescription(
        key="public_ipv6",
        entity_registry_enabled_default=False,
        name="Public IPv6 address",
        icon="mdi:ip-network-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.wan.get("ipv6"),
        attrs_fn=lambda d: {
            "gateway": d.wan.get("ipv6_gateway"),
            "dns_servers": d.wan.get("dns_servers_ipv6"),
            "lan_address": d.misc.get("lan_ipv6"),
        },
    ),
    RouterSensorDescription(
        key="wan_errors",
        name="WAN packet errors",
        icon="mdi:alert-circle-outline",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: (
            (d.wan.get("rx_errors") or 0) + (d.wan.get("tx_errors") or 0)
            if d.wan.get("rx_errors") is not None
            else None
        ),
        attrs_fn=lambda d: {
            "rx_errors": d.wan.get("rx_errors"),
            "tx_errors": d.wan.get("tx_errors"),
            "rx_discards": d.wan.get("rx_discards"),
            "tx_discards": d.wan.get("tx_discards"),
        },
    ),
    # ---- Clients
    RouterSensorDescription(
        key="connected_devices",
        name="Connected devices",
        icon="mdi:devices",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: len(_active(d)),
        attrs_fn=_device_list,
    ),
    RouterSensorDescription(
        key="wifi_devices",
        entity_registry_enabled_default=False,
        name="Wi-Fi devices",
        icon="mdi:wifi",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: sum(1 for h in _active(d) if h["connection"] == "wifi"),
    ),
    RouterSensorDescription(
        key="wired_devices",
        entity_registry_enabled_default=False,
        name="Wired devices",
        icon="mdi:ethernet",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: sum(1 for h in _active(d) if h["connection"] != "wifi"),
    ),
    # ---- System
    RouterSensorDescription(
        key="cpu_load",
        entity_registry_enabled_default=False,
        name="CPU load",
        icon="mdi:cpu-32-bit",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.system.get("cpu_load"),
    ),
    RouterSensorDescription(
        key="memory_usage",
        entity_registry_enabled_default=False,
        name="Memory usage",
        icon="mdi:memory",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.system.get("memory_usage"),
        attrs_fn=lambda d: {
            "total_mb": round(d.system["memory_total_kb"] / 1024)
            if d.system.get("memory_total_kb")
            else None,
            "free_mb": round(d.system["memory_free_kb"] / 1024)
            if d.system.get("memory_free_kb") is not None
            else None,
        },
    ),
    RouterSensorDescription(
        key="temperature",
        entity_registry_enabled_default=False,
        name="Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.system.get("temperature"),
        attrs_fn=lambda d: {"status": d.system.get("temperature_status")},
    ),
    RouterSensorDescription(
        key="nat_table_usage",
        entity_registry_enabled_default=False,
        name="NAT table usage",
        icon="mdi:table-network",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.system.get("nat_usage"),
        attrs_fn=lambda d: {
            "used_entries": d.system.get("nat_used"),
            "total_entries": d.system.get("nat_total"),
        },
    ),
    RouterSensorDescription(
        key="last_boot",
        name="Last boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        since_seconds=True,
        value_fn=lambda d: d.system.get("uptime"),
        attrs_fn=lambda d: {"boot_count": d.system.get("boots")},
    ),
    RouterSensorDescription(
        key="firmware",
        entity_registry_enabled_default=False,
        name="Firmware",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.info.get("firmware"),
        attrs_fn=lambda d: {
            "recovery_firmware": d.info.get("recovery_firmware"),
            "kernel": d.info.get("kernel"),
            "architecture": d.info.get("architecture"),
        },
    ),
    RouterSensorDescription(
        key="mesh_nodes",
        name="Mesh nodes",
        icon="mdi:access-point-network",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: len(d.mesh_nodes) if d.mesh_nodes else None,
        attrs_fn=lambda d: {"nodes": d.mesh_nodes},
    ),
    RouterSensorDescription(
        key="network_config",
        entity_registry_enabled_default=False,
        name="Network configuration",
        icon="mdi:cog-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.misc.get("ntp_status"),
        attrs_fn=lambda d: {
            k: v for k, v in d.misc.items() if k not in ("fetched_at",)
        },
    ),
)


LED_SENSOR = RouterSensorDescription(
    key="led_brightness",
    name="LED brightness",
    icon="mdi:led-on",
    native_unit_of_measurement=PERCENTAGE,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
    value_fn=lambda d: d.controls.get("led_brightness"),
)


def _is_cgnat(ip: str | None) -> bool | None:
    """100.64.0.0/10 = carrier-grade NAT (no real public IPv4)."""
    if not ip:
        return None
    try:
        a, b, *_ = (int(x) for x in ip.split("."))
    except ValueError:
        return None
    return a == 100 and 64 <= b <= 127


def _band_sensors(band: str) -> tuple[RouterSensorDescription, ...]:
    label = BAND_LABEL.get(band, f"{band} GHz")
    slug = band.replace(".", "")

    def w(key: str) -> Callable[[RouterData], Any]:
        return lambda d: d.wifi.get(band, {}).get(key)

    return (
        RouterSensorDescription(
            key=f"wifi_{slug}_clients",
            name=f"Wi-Fi {label} clients",
            icon="mdi:wifi",
                state_class=SensorStateClass.MEASUREMENT,
            value_fn=w("clients"),
            attrs_fn=lambda d: {
                "ssid": d.wifi.get(band, {}).get("ssid"),
                "status": d.wifi.get(band, {}).get("status"),
                "devices": sorted(
                    h["name"] for h in _active(d) if h.get("band") == band
                ),
            },
        ),
        RouterSensorDescription(
            key=f"wifi_{slug}_channel",
            entity_registry_enabled_default=False,
            name=f"Wi-Fi {label} channel",
            icon="mdi:radio-tower",
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=w("channel"),
            attrs_fn=lambda d: {
                "bandwidth_mhz": d.wifi.get(band, {}).get("bandwidth"),
                "auto_channel": d.wifi.get(band, {}).get("auto_channel"),
                "standard": d.wifi.get(band, {}).get("standard"),
                "security_mode": d.wifi.get(band, {}).get("security_mode"),
                "max_bitrate_mbps": d.wifi.get(band, {}).get("max_bitrate"),
            },
        ),
        RouterSensorDescription(
            key=f"wifi_{slug}_received",
            name=f"Wi-Fi {label} received",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            state_class=SensorStateClass.TOTAL_INCREASING,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=w("rx_bytes"),
        ),
        RouterSensorDescription(
            key=f"wifi_{slug}_sent",
            name=f"Wi-Fi {label} sent",
            device_class=SensorDeviceClass.DATA_SIZE,
            native_unit_of_measurement=UnitOfInformation.BYTES,
            suggested_unit_of_measurement=UnitOfInformation.GIGABYTES,
            suggested_display_precision=2,
            state_class=SensorStateClass.TOTAL_INCREASING,
            entity_category=EntityCategory.DIAGNOSTIC,
            entity_registry_enabled_default=False,
            value_fn=w("tx_bytes"),
        ),
    )


def _port_sensor(index: int, port: dict[str, Any]) -> RouterSensorDescription:
    name = port.get("name") or str(index)
    alias = port.get("alias") or name
    label = "WAN port" if port.get("role") == "WAN" else f"LAN port {alias.removeprefix('PHY')}"

    def find(d: RouterData) -> dict[str, Any]:
        return next((p for p in d.lan_ports if p.get("name") == name), {})

    return RouterSensorDescription(
        key=f"port_{name}_speed",
        entity_registry_enabled_default=False,
        name=f"{label} speed",
        icon="mdi:ethernet-cable",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: find(d).get("speed"),
        attrs_fn=lambda d: {
            "status": find(d).get("status"),
            "duplex": find(d).get("duplex"),
            "interface": name,
        },
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SagemcomConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    descriptions = list(SENSORS)
    for band in coordinator.data.wifi:
        descriptions.extend(_band_sensors(band))
    for i, port in enumerate(coordinator.data.lan_ports):
        if port.get("role") != "WAN":  # WAN port is covered by "WAN link speed"
            descriptions.append(_port_sensor(i, port))
    controls = controls_enabled(entry)
    if not controls:
        descriptions.append(LED_SENSOR)  # read-only twin of the LEDs light
    else:
        led_uid = f"{router_key(coordinator)}_{LED_SENSOR.key}"
        async_remove_stale_entities(hass, entry, "sensor", lambda uid: uid != led_uid)
    async_add_entities(
        (RouterSensor(coordinator, desc) for desc in descriptions),
        config_subentry_id=subentry_id(entry, ROUTER_SUBENTRY_TYPE),
    )
    async_add_client_entities(
        hass,
        entry,
        coordinator,
        async_add_entities,
        lambda c, mac, via: [
            ClientSensor(c, mac, via, desc) for desc in CLIENT_SENSORS
        ],
    )
    async_add_extender_entities(
        hass,
        entry,
        coordinator,
        async_add_entities,
        lambda c, node_id, via: [
            ExtenderSensor(c, node_id, via, desc) for desc in EXTENDER_SENSORS
        ],
    )


class RouterSensor(SagemcomEntity, SensorEntity):
    entity_description: RouterSensorDescription
    # Big list attributes: don't store in recorder history on every change.
    _unrecorded_attributes = frozenset({"devices", "nodes"})

    def __init__(self, coordinator, description: RouterSensorDescription) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description
        self._since: datetime | None = None
        self._update_values()

    def _update_values(self) -> None:
        desc = self.entity_description
        value = desc.value_fn(self.coordinator.data)
        if desc.since_seconds:
            if value is None:
                self._since = None
            else:
                since = (dt_util.utcnow() - timedelta(seconds=value)).replace(microsecond=0)
                # Only move the timestamp on a real reset, not on poll jitter.
                if self._since is None or abs((since - self._since).total_seconds()) > 120:
                    self._since = since
            self._attr_native_value = self._since
        else:
            self._attr_native_value = value
        if desc.attrs_fn is not None:
            self._attr_extra_state_attributes = desc.attrs_fn(self.coordinator.data)

    @callback
    def _handle_coordinator_update(self) -> None:
        self._update_values()
        super()._handle_coordinator_update()


CONNECTION_OPTIONS = ["2.4 GHz", "5 GHz", "6 GHz", "Wi-Fi", "Ethernet", "Offline"]


def _connection(host: dict[str, Any]) -> str | None:
    if not host:
        return None
    if not host.get("active"):
        return "Offline"
    if host.get("connection") == "wifi":
        return BAND_LABEL.get(host.get("band") or "", "Wi-Fi")
    return "Ethernet"


@dataclass(frozen=True, kw_only=True)
class ClientSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]


# Per-client sensors, all disabled by default (enable them for the devices you care
# about; the same values are attributes of the device tracker).
CLIENT_SENSORS: tuple[ClientSensorDescription, ...] = (
    ClientSensorDescription(
        key="connection",
        name="Connection",
        icon="mdi:lan-connect",
        device_class=SensorDeviceClass.ENUM,
        options=CONNECTION_OPTIONS,
        entity_registry_enabled_default=False,
        value_fn=_connection,
    ),
    ClientSensorDescription(
        key="signal",
        name="Signal strength",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda h: h.get("signal") if h.get("active") else None,
    ),
    ClientSensorDescription(
        key="link_speed",
        name="Link speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.DATA_RATE,
        native_unit_of_measurement=UnitOfDataRate.MEGABITS_PER_SECOND,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda h: (h.get("rate_mbps") or h.get("wired_port_mbps")) if h.get("active") else None,
    ),
)


class ClientSensor(SagemcomClientEntity, SensorEntity):
    entity_description: ClientSensorDescription

    def __init__(self, coordinator, mac: str, via: str | None, description: ClientSensorDescription) -> None:
        super().__init__(coordinator, mac, description.key, via)
        self.entity_description = description

    @property
    def native_value(self) -> Any:
        return self.entity_description.value_fn(self.host)


@dataclass(frozen=True, kw_only=True)
class ExtenderSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict[str, Any]], Any]
    since_seconds: bool = False


EXTENDER_SENSORS: tuple[ExtenderSensorDescription, ...] = (
    ExtenderSensorDescription(
        key="clients",
        name="Wi-Fi clients",
        icon="mdi:devices",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda n: n.get("clients"),
    ),
    ExtenderSensorDescription(
        key="backhaul",
        name="Backhaul",
        icon="mdi:access-point-network",
        value_fn=lambda n: n.get("backhaul"),
    ),
    ExtenderSensorDescription(
        key="last_boot",
        name="Last boot",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        since_seconds=True,
        value_fn=lambda n: n.get("uptime"),
    ),
)


class ExtenderSensor(SagemcomExtenderEntity, SensorEntity):
    entity_description: ExtenderSensorDescription

    def __init__(self, coordinator, node_id: str, via: str | None, description: ExtenderSensorDescription) -> None:
        super().__init__(coordinator, node_id, description.key, via)
        self.entity_description = description
        self._since: datetime | None = None

    @property
    def native_value(self) -> Any:
        value = self.entity_description.value_fn(self.node or {})
        if not self.entity_description.since_seconds:
            return value
        if value is None:
            return None
        since = (dt_util.utcnow() - timedelta(seconds=value)).replace(microsecond=0)
        # Only move the timestamp on a real reboot, not on poll jitter.
        if self._since is None or abs((since - self._since).total_seconds()) > 120:
            self._since = since
        return self._since

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key == "backhaul":
            node = self.node or {}
            return {"quality": node.get("backhaul_quality")}
        return None
