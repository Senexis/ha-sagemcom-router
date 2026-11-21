"""Data update coordinator: polls the router and normalises the payloads."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    SagemcomAuthError,
    SagemcomConnectionError,
    SagemcomEndpointError,
    SagemcomError,
    SagemcomRestClient,
    unwrap,
)
from .const import BANDS, DOMAIN, SLOW_INTERVAL

_LOGGER = logging.getLogger(__name__)


def _slow_paths() -> dict[str, str]:
    """Slow-changing or expensive endpoints, fetched every SLOW_INTERVAL."""
    paths = {
        # Fallback for firmware without the v4 mesh endpoint (v2 takes ~20 s).
        "mesh_v2": "/api/v2/easymesh/meshdevices",
        "dhcp_clients": "/api/v1/dhcp/clients",
        "ntp": "/api/v1/ntp",
        "dns_ipv4": "/api/v1/dns/ipv4",
        "dns_ipv6": "/api/v1/dns/ipv6",
        "dhcp": "/api/v1/dhcp",
        "wan_ipv4": "/api/v1/wan/ipv4",
        "lan_ipv6": "/api/v1/lan/ipv6",
        "upnp": "/api/v1/advanced/upnp",
        "firewall": "/api/v2/firewall",
        "nat_rules": "/api/v1/nat/rules",
        "mlo": "/api/v2/wireless/mlo/state",
        "led": "/api/v1/led",
        "parental": "/api/v1/parental_control",
    }
    for band, ident in BANDS.items():
        paths[f"wifi_{band}"] = f"/api/v2/wireless/{ident}"
    return paths


SLOW_PATHS = _slow_paths()
MESH_V4 = "/api/v4/easymesh/meshdevices"


def _kbit_to_mbit(value: Any) -> float | None:
    rate = _int(value)
    return round(rate / 1000, 1) if rate else None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _dig(data: Any, *keys: Any, default: Any = None) -> Any:
    for key in keys:
        if isinstance(data, dict):
            data = data.get(key)
        elif isinstance(data, list) and isinstance(key, int) and -len(data) <= key < len(data):
            data = data[key]
        else:
            return default
        if data is None:
            return default
    return data


def _strip_brackets(value: Any) -> str | None:
    if not value or not isinstance(value, str):
        return None
    value = value.strip("[]")
    return value or None


@dataclass
class RouterData:
    """Normalised snapshot of the router state."""

    info: dict[str, Any] = field(default_factory=dict)
    system: dict[str, Any] = field(default_factory=dict)
    wan: dict[str, Any] = field(default_factory=dict)
    wifi: dict[str, dict[str, Any]] = field(default_factory=dict)
    guest_wifi: dict[str, dict[str, Any]] = field(default_factory=dict)
    lan_ports: list[dict[str, Any]] = field(default_factory=list)
    hosts: dict[str, dict[str, Any]] = field(default_factory=dict)
    mesh_nodes: list[dict[str, Any]] = field(default_factory=list)
    misc: dict[str, Any] = field(default_factory=dict)
    # State behind the control entities (LED, parental control).
    controls: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)  # for diagnostics


class SagemcomCoordinator(DataUpdateCoordinator[RouterData]):
    """Polls the router."""

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: SagemcomRestClient,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self._unsupported: set[str] = set()
        self._slow_cache: dict[str, Any] = {}
        self._slow_next = 0.0
        self._slow_task: asyncio.Task[None] | None = None
        self._refetched_at: dict[str, float] = {}
        self.platforms: list[str] = []  # set up by async_setup_entry
        self._prev_counters: tuple[float, int, int] | None = None

    # ------------------------------------------------------------ fetching
    async def _fetch(self, path: str, timeout: float | None = None) -> Any:
        """GET an optional endpoint; None if unsupported or it failed this time."""
        if path in self._unsupported:
            return None
        try:
            return await self.client.get(path, timeout=timeout)
        except SagemcomEndpointError as err:
            _LOGGER.debug("Endpoint unavailable, skipping from now on: %s", err)
            self._unsupported.add(path)
        except SagemcomConnectionError as err:
            _LOGGER.debug("Optional endpoint failed this cycle: %s", err)
        return None

    async def _async_update_data(self) -> RouterData:
        try:
            return await self._update()
        except SagemcomAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except SagemcomConnectionError as err:
            raise UpdateFailed(f"Router unreachable: {err}") from err
        except SagemcomError as err:
            raise UpdateFailed(str(err)) from err

    async def _update(self) -> RouterData:
        # Critical endpoint: failures here mark the whole update failed.
        raw: dict[str, Any] = {"device_info": await self.client.get("/api/v1/deviceInfo")}
        # Cheap endpoints (~0.1-2 s each), every poll.
        fast = {
            "device": "/api/v1/device",
            "wan_stats": "/api/v1/wan/ip/stats",
            "lan_stats": "/api/v1/lan/stats",
            "hosts": "/api/v1/hosts",
            # Mesh nodes + per-station signal/rate; v4 answers in ~1.5 s.
            "mesh": MESH_V4,
        }
        for band, ident in BANDS.items():
            fast[f"wifi_stats_{band}"] = f"/api/v2/wireless/stats/{ident}"
        for key, path in fast.items():
            raw[key] = await self._fetch(path)

        now = time.monotonic()
        if now >= self._slow_next and (self._slow_task is None or self._slow_task.done()):
            # Runs next to the regular polls instead of inside one: the mesh
            # endpoint alone takes ~20 s, which used to stall HA startup and
            # every fifth-minute poll. Its data shows up one refresh later.
            self._slow_next = now + SLOW_INTERVAL.total_seconds()
            self._slow_task = self.config_entry.async_create_background_task(
                self.hass, self._refresh_slow(), f"{DOMAIN} slow endpoints"
            )
        raw.update(self._slow_cache)
        return self._parse(raw, now)

    async def _refresh_slow(self) -> None:
        """Fetch slow-changing or expensive endpoints, then refresh entities."""
        started = time.monotonic()
        cache: dict[str, Any] = {}
        try:
            cache["open"] = await self.client.get_open()
        except SagemcomError:
            cache["open"] = None
        try:
            for key, path in SLOW_PATHS.items():
                if key == "mesh_v2" and MESH_V4 not in self._unsupported:
                    continue
                cache[key] = await self._fetch(path, timeout=60)
        except SagemcomError as err:  # e.g. re-login failed; the next poll reports it
            _LOGGER.debug("Slow endpoints skipped this cycle: %s", err)
            return
        # Keep values re-read after a write that happened while this ran.
        for key, at in self._refetched_at.items():
            if at > started and key in self._slow_cache:
                cache[key] = self._slow_cache[key]
        self._slow_cache = cache
        await self.async_request_refresh()

    async def async_refetch(self, *keys: str) -> None:
        """Re-read some slow-tier endpoints right away (after a write)."""
        for key in keys:
            self._slow_cache[key] = await self._fetch(SLOW_PATHS[key])
            self._refetched_at[key] = time.monotonic()
        await self.async_request_refresh()

    # ------------------------------------------------------------- parsing
    def _parse(self, raw: dict[str, Any], now: float) -> RouterData:
        di = unwrap(raw["device_info"]) or {}
        dev = _dig(unwrap(raw.get("device")), "device", default={})
        data = RouterData(raw=raw)

        data.info = {
            "serial": di.get("serialNumber") or dev.get("serialnumber"),
            "model": di.get("productClass") or dev.get("modelname"),
            "manufacturer": di.get("manufacturer") or "Sagemcom",
            "firmware": di.get("softwareVersion") or _dig(dev, "running", "version"),
            "recovery_firmware": _dig(dev, "reco", "version"),
            "hardware": di.get("hardwareVersion") or dev.get("hardware_version"),
            "mac": di.get("macAddress"),
            "kernel": di.get("kernelVersion"),
            "architecture": di.get("architecture"),
        }

        mem_total = _int(di.get("totalMemory"))
        mem_free = _int(di.get("freeMemory"))
        mem_cached = _int(di.get("cachedMemory")) or 0
        nat_total = _int(di.get("natTotalEnteries"))
        nat_used = _int(di.get("natUsedEnteries"))
        data.system = {
            "cpu_load": _int(di.get("cpuLoad")),
            "memory_total_kb": mem_total,
            "memory_free_kb": mem_free,
            "memory_usage": (
                round(100 * (mem_total - mem_free - mem_cached) / mem_total, 1)
                if mem_total and mem_free is not None
                else None
            ),
            "temperature": _int(_dig(dev, "temperature", "current")),
            "temperature_status": _dig(dev, "temperature", "status"),
            "boots": _int(dev.get("numberofboots")),
            "uptime": _int(di.get("uptimeSystem") or dev.get("uptime")),
            "nat_total": nat_total,
            "nat_used": nat_used,
            "nat_usage": (
                round(100 * nat_used / nat_total, 1)
                if nat_total and nat_used is not None
                else None
            ),
            "flash_total_kb": _int(di.get("totalFlashMemory")),
            "flash_free_kb": _int(di.get("freeFlashMemory")),
        }

        # ---- WAN
        stats = _dig(unwrap(raw.get("wan_stats")), "wan", "ip", "stats", default={})
        rx_bytes = _int(_dig(stats, "rx", "bytes"))
        tx_bytes = _int(_dig(stats, "tx", "bytes"))
        down = up = None
        if rx_bytes is not None and tx_bytes is not None:
            if self._prev_counters is not None:
                t0, rx0, tx0 = self._prev_counters
                dt = now - t0
                if dt > 0 and rx_bytes >= rx0 and tx_bytes >= tx0:
                    down = round((rx_bytes - rx0) * 8 / dt / 1e6, 2)
                    up = round((tx_bytes - tx0) * 8 / dt / 1e6, 2)
            self._prev_counters = (now, rx_bytes, tx_bytes)
        wan_eth = _dig(di, "wanEthernet", 0, default={})
        wan_ipv4 = _dig(unwrap(raw.get("wan_ipv4")), "wan", "ipv4", default={})
        open_ = raw.get("open") or {}
        data.wan = {
            "status": (open_.get("wan_status") or wan_ipv4.get("status") or (
                "Up" if wan_eth.get("ethernetWanState") == "Up" and di.get("publicIpv4Address") else "Down"
            )),
            "connected": bool(di.get("publicIpv4Address"))
            and wan_eth.get("ethernetWanState", "Up") == "Up",
            "ipv4": di.get("publicIpv4Address") or None,
            "ipv4_subnet": di.get("publicSubnetMask"),
            "ipv4_gateway": di.get("defaultGateway"),
            "ipv6": _strip_brackets(di.get("globalGatewayIpv6Address")),
            "ipv6_gateway": di.get("defaultGatewayIpv6Address"),
            "dns_servers": [
                s for s in (di.get("primaryDnsServer"), di.get("secondaryDnsServer")) if s
            ],
            "dns_servers_ipv6": [
                s
                for s in (di.get("dnsServerIpv6Address"), di.get("secondaryDnsServerIpv6Address"))
                if s
            ],
            "addressing": wan_ipv4.get("addressing_type"),
            "mtu": wan_ipv4.get("mtu"),
            "online_duration": _int(di.get("onlineDuration")),
            "link_state": wan_eth.get("ethernetWanState"),
            "link_speed": _int(wan_eth.get("ethernetWanBitrate")),
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "rx_errors": _int(_dig(stats, "rx", "packetserrors")),
            "tx_errors": _int(_dig(stats, "tx", "packetserrors")),
            "rx_discards": _int(_dig(stats, "rx", "packetsdiscards")),
            "tx_discards": _int(_dig(stats, "tx", "packetsdiscards")),
            "download_mbps": down,
            "upload_mbps": up,
        }

        # ---- Wi-Fi (from deviceInfo.wireless + v2 radio/stats)
        wireless: dict[str, Any] = {}
        for item in di.get("wireless") or []:
            if isinstance(item, dict):
                wireless.update(item)
        for band, ident in BANDS.items():
            info = wireless.get(ident, {})
            radio = _dig(unwrap(raw.get(f"wifi_{band}")), "wireless", "radio", default={})
            ssid = _dig(unwrap(raw.get(f"wifi_{band}")), "wireless", "ssid", default={})
            st = _dig(unwrap(raw.get(f"wifi_stats_{band}")), "wireless", "ssid", default={})
            if not info and not radio and not st:
                continue
            bw = radio.get("curr_oper_chan_bw")
            data.wifi[band] = {
                "ssid": info.get("ssidName") or ssid.get("id"),
                "status": info.get("ssidStatus") or st.get("status"),
                "enabled": radio.get("enable"),
                "standard": info.get("standardMode") or radio.get("standard"),
                "security_mode": info.get("securityMode"),
                "max_bitrate": _int(st.get("maxbitrate")) or _int(radio.get("max_bit_rate")),
                "channel": _int(radio.get("channel")),
                "auto_channel": radio.get("auto_channel_enable"),
                "bandwidth": _int(str(bw).split("_")[0]) if bw else None,
                "rx_bytes": _int(_dig(st, "stats", "rx", "bytes")),
                "tx_bytes": _int(_dig(st, "stats", "tx", "bytes")),
                "rx_errors": _int(_dig(st, "stats", "rx", "packetserrors")),
                "tx_errors": _int(_dig(st, "stats", "tx", "packetserrors")),
                "bssid": info.get("macAddress") or ssid.get("bssid"),
            }
        for band, ident in (("2.4", "guest24"), ("5", "guest5"), ("6", "guest6")):
            if ident in wireless:
                g = wireless[ident]
                data.guest_wifi[band] = {
                    "ssid": g.get("ssidName"),
                    "status": g.get("ssidStatus"),
                }

        # ---- Control state
        led = _dig(unwrap(raw.get("led")), "leds_brightness", default={})
        parental = unwrap(raw.get("parental"))
        data.controls = {
            "led_brightness": _int(led.get("brightness")),
            "led_levels": sorted(
                {
                    level
                    for level in (_int(x) for x in str(led.get("brightness_possible") or "").split(","))
                    if level is not None
                }
            ),
            "parental_enabled": parental.get("enable") if isinstance(parental, dict) else None,
            "parental_rules": {
                str(rule.get("SourceMac") or "").lower(): rule
                for rule in (parental.get("schedules") or [] if isinstance(parental, dict) else [])
                if rule.get("SourceMac")
            },
        }

        # ---- LAN ports
        for port in _dig(unwrap(raw.get("lan_stats")), "lan", "interfaces", default=[]):
            data.lan_ports.append(
                {
                    "name": port.get("name"),
                    "alias": port.get("alias"),
                    "role": port.get("role"),
                    "status": port.get("status"),
                    "enabled": port.get("enable"),
                    "speed": _int(port.get("curbitrate")),
                    "duplex": port.get("currentduplex"),
                    "rx_bytes": _int(_dig(port, "rx", "bytes")),
                    "tx_bytes": _int(_dig(port, "tx", "bytes")),
                }
            )

        # ---- Mesh nodes and station signal info
        stations: dict[str, dict[str, Any]] = {}
        mesh = unwrap(raw.get("mesh")) or unwrap(raw.get("mesh_v2"))
        wired_ports: dict[str, int | None] = {}
        for node in _dig(mesh, "meshDevices", default=[]):
            node_name = node.get("hostname") or node.get("model") or node.get("deviceId")
            data.mesh_nodes.append(
                {
                    "id": node.get("deviceId"),
                    "type": node.get("type"),
                    "model": node.get("model"),
                    "name": node_name,
                    "ipv4": node.get("ipv4") or None,
                    "firmware": node.get("softwareVersion"),
                    "uptime": _int(node.get("upTime")),
                    "backhaul": _dig(node, "backhaul", "linkType"),
                    "backhaul_quality": _dig(node, "backhaul", "linkQuality"),
                }
            )
            for radio in node.get("wifiRadios") or []:
                for ssid in radio.get("ssids") or []:
                    for sta in ssid.get("stations") or []:
                        mac = (sta.get("macAddress") or "").lower()
                        if mac:
                            stations[mac] = {
                                "band": radio.get("band"),
                                "ssid": ssid.get("ssid"),
                                # 0 means "not measured" (seen on 6 GHz/MLO links).
                                "signal": _int(sta.get("signalStrength")) or None,
                                "link_quality": sta.get("linkQuality"),
                                "rate_mbps": _kbit_to_mbit(sta.get("rate")),
                                "uplink_rate_mbps": _kbit_to_mbit(sta.get("uplinkRate")),
                                "access_point": node_name,
                            }
            for port in node.get("ethernetPorts") or []:
                for neighbour in port.get("neighbours") or []:
                    mac = (neighbour.get("macAddress") or "").lower()
                    if mac:
                        wired_ports[mac] = _int(port.get("speed"))
        reserved = {
            (c.get("macaddress") or "").lower()
            for c in _dig(unwrap(raw.get("dhcp_clients")), "dhcp", "clients", default=[])
            if c.get("enable", True)
        }

        # ---- Hosts
        for host in _dig(unwrap(raw.get("hosts")), "hosts", "list", default=[]):
            mac = (host.get("macaddress") or "").lower()
            if not mac:
                continue
            name = host.get("friendlyHostname") or host.get("hostname") or mac
            if name.lower() == mac:
                name = mac
            wl = _dig(host, "wireless", 0, default={})
            sta = stations.get(mac, {})
            ipv6 = [
                a.get("ipaddress")
                for a in host.get("ip6address") or []
                if a.get("ipaddress") and not a["ipaddress"].startswith("fe80")
            ]
            link = host.get("link") or "Unknown"
            band = sta.get("band") or wl.get("band")
            entry = {
                "mac": mac,
                "router_mac": host.get("macaddress"),  # as the router writes it
                "router_id": host.get("id"),
                "name": name,
                "hostname": host.get("hostname"),
                "ip": host.get("ipaddress") or None,
                "ipv6": ipv6,
                "active": bool(host.get("active")),
                "connection": "wifi" if (link == "WiFi" or band) else (
                    "ethernet"
                    if link.lower().startswith("eth") or mac in wired_ports
                    else "wired/unknown"
                ),
                "band": band,
                "signal": sta.get("signal") if sta else (_int(wl.get("rssi0")) or None),
                "link_quality": sta.get("link_quality"),
                "rate_mbps": sta.get("rate_mbps") or _kbit_to_mbit(wl.get("rate")),
                "uplink_rate_mbps": sta.get("uplink_rate_mbps"),
                "ssid": sta.get("ssid"),
                "network": "guest" if "guest" in str(host.get("interface_l3") or "").lower() else "main",
                "wired_port_mbps": wired_ports.get(mac),
                "dhcp_reservation": mac in reserved,
                "access_point": sta.get("access_point"),
                "device_type": host.get("devicetype"),
                "address_type": host.get("type"),
                "last_seen": host.get("lastseen"),
            }
            # Duplicate MACs: keep the active one.
            if mac in data.hosts and data.hosts[mac]["active"] and not entry["active"]:
                continue
            data.hosts[mac] = entry

        for band, wifi in data.wifi.items():
            wifi["clients"] = sum(
                1 for h in data.hosts.values() if h["active"] and h["band"] == band
            )

        # ---- Misc / configuration facts useful for summaries
        ntp = unwrap(raw.get("ntp")) or {}
        ntp = ntp.get("ntp", {}) if isinstance(ntp, dict) else {}
        dhcp = unwrap(raw.get("dhcp")) or {}
        dns4 = _dig(unwrap(raw.get("dns_ipv4")), "DNS", default={})
        data.misc = {
            "ntp_status": ntp.get("status"),
            "ntp_servers": [ntp.get(f"server{i}") for i in range(1, 6) if ntp.get(f"server{i}")],
            "time_zone": ntp.get("time_zone_name"),
            "lan_dns_mode": dns4.get("dnsMode"),
            "lan_dns_servers": _dig(dns4, "static", "servers"),
            "dhcp_enabled": _dig(dhcp, "dhcp", "enable"),
            "dhcp_range": (
                f"{_dig(dhcp, 'dhcp', 'minaddress')} - {_dig(dhcp, 'dhcp', 'maxaddress')}"
                if _dig(dhcp, "dhcp", "minaddress")
                else None
            ),
            "dhcp_lease_time": _dig(dhcp, "dhcp", "leasetime"),
            "upnp_enabled": _dig(unwrap(raw.get("upnp")), "UPNPEnable"),
            "firewall_level": _dig(unwrap(raw.get("firewall")), "firewall", "level"),
            "port_forwards": len(_dig(unwrap(raw.get("nat_rules")), "nat", "rules", default=[]))
            if raw.get("nat_rules") is not None
            else None,
            "mlo_enabled": (
                _dig(unwrap(raw.get("mlo")), "mlo_state") == "1"
                if raw.get("mlo") is not None
                else None
            ),
            "lan_ipv6": _dig(unwrap(raw.get("lan_ipv6")), "lan", "ipv6", "address"),
            "fetched_at": dt_util.utcnow().isoformat(),
        }
        return data
