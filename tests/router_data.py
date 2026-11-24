"""Synthetic router responses shaped like the real F5598T API.

All identifiers are made up: documentation IP ranges, locally invented MACs,
fake serials and SSIDs. Only the structure mirrors the router.
"""

from __future__ import annotations

from typing import Any

GATEWAY_ID = "02:00:00:00:00:01"
EXTENDER_ID = "02:00:00:00:00:02"

PHONE = "02:00:00:00:10:01"  # Wi-Fi 5 GHz via the gateway
PLUG = "02:00:00:00:10:02"  # Wi-Fi 2.4 GHz via the extender
NAS = "02:00:00:00:10:03"  # wired, behind the 10G port
MAIN_PSK = "main-passphrase-1"
GUEST_PSK = "guest;pass:2"  # needs escaping in the QR text
GUEST = "02:00:00:00:10:04"  # guest network, no station info
OLD = "02:00:00:00:10:05"  # offline


# Endpoint of each raw() key (the coordinator's fast and slow tiers).
PATHS: dict[str, str] = {
    "device_info": "/api/v1/deviceInfo",
    "device": "/api/v1/device",
    "wan_stats": "/api/v1/wan/ip/stats",
    "lan_stats": "/api/v1/lan/stats",
    "hosts": "/api/v1/hosts",
    "mesh": "/api/v4/easymesh/meshdevices",
    "wifi_stats_2.4": "/api/v2/wireless/stats/24",
    "wifi_stats_5": "/api/v2/wireless/stats/5",
    "wifi_2.4": "/api/v2/wireless/24",
    "wifi_5": "/api/v2/wireless/5",
    "guest_wifi_2.4": "/api/v2/wireless/guest24",
    "led": "/api/v1/led",
    "parental": "/api/v1/parental_control",
    "dhcp_clients": "/api/v1/dhcp/clients",
}


def raw(rx_bytes: int = 1_000_000_000, tx_bytes: int = 100_000_000) -> dict[str, Any]:
    return {
        "device_info": [
            {
                "serialNumber": "TESTSERIAL01",
                "productClass": "F0000T",
                "manufacturer": "Sagemcom",
                "softwareVersion": "TESTFW1",
                "macAddress": "02:00:00:00:00:F0",
                "publicIpv4Address": "198.51.100.7",
                "wanEthernet": [{"ethernetWanState": "Up", "ethernetWanBitrate": "2500"}],
                "onlineDuration": "3600",
                "totalMemory": "1000000",
                "freeMemory": "400000",
                "cachedMemory": "100000",
                "natTotalEnteries": "1000",
                "natUsedEnteries": "250",
                "wireless": [
                    {"24": {"ssidName": "TestNet", "ssidStatus": "Up"}},
                    {"5": {"ssidName": "TestNet", "ssidStatus": "Up"}},
                    {"guest24": {"ssidName": "TestGuest", "ssidStatus": "Down"}},
                ],
            }
        ],
        "device": [{"device": {"temperature": {"current": 55, "status": "OK"}}}],
        "wan_stats": [
            {"wan": {"ip": {"stats": {"rx": {"bytes": rx_bytes}, "tx": {"bytes": tx_bytes}}}}}
        ],
        "lan_stats": [{"lan": {"interfaces": []}}],
        "wifi_stats_2.4": [{"wireless": {"ssid": {"status": "Up", "stats": {}}}}],
        "wifi_stats_5": [{"wireless": {"ssid": {"status": "Up", "stats": {}}}}],
        "wifi_2.4": [
            {
                "wireless": {
                    "radio": {"enable": True, "channel": "6"},
                    "ssid": {
                        "id": "TestNet",
                        "enable": True,
                        "hidden": False,
                        "security": {"protocol": "WPA3-Personal-Transition", "passphrase": MAIN_PSK},
                    },
                }
            }
        ],
        "guest_wifi_2.4": [
            {
                "wireless": {
                    "ssid": {
                        "id": "TestGuest",
                        "enable": False,
                        "hidden": False,
                        "security": {"protocol": "WPA2-Personal", "passphrase": GUEST_PSK},
                    }
                }
            }
        ],
        "wifi_5": [{"wireless": {"radio": {"enable": True, "channel": "36"}, "ssid": {"id": "TestNet"}}}],
        "hosts": [
            {
                "hosts": {
                    "list": [
                        host(2, PHONE, "Phone", "192.0.2.20", link="WiFi"),
                        host(3, PLUG, "Plug", "192.0.2.21", link="WiFi"),
                        host(4, NAS, "NAS", "192.0.2.10", link="Other"),
                        host(5, GUEST, "Guest-Laptop", "192.0.2.150", link="WiFi", iface="Guest"),
                        host(6, OLD, "Old-Tablet", "192.0.2.30", link="WiFi", active=False),
                    ]
                }
            }
        ],
        "mesh": [
            {
                "meshDevices": [
                    {
                        "deviceId": GATEWAY_ID,
                        "type": "gateway",
                        "hostname": "mygateway",
                        "model": "F0000T",
                        "wifiRadios": [
                            {
                                "band": "5",
                                "ssids": [
                                    {
                                        "ssid": "TestNet",
                                        "stations": [_station(PHONE, -60, "866700", "720600")],
                                    }
                                ],
                            }
                        ],
                        "ethernetPorts": [
                            {"speed": 10000, "neighbours": [{"macAddress": NAS}]},
                        ],
                    },
                    {
                        "deviceId": EXTENDER_ID,
                        "type": "extender",
                        "hostname": "upstairs",
                        "model": "X0000",
                        "manufacturer": "Sagemcom",
                        "serialNumber": "TESTSERIAL02",
                        "softwareVersion": "TESTFW2",
                        "upTime": "7200",
                        "backhaul": {"linkType": "Wi-Fi", "linkQuality": "GOOD"},
                        "wifiRadios": [
                            {
                                "band": "2.4",
                                "ssids": [
                                    {
                                        "ssid": "TestNet",
                                        "stations": [_station(PLUG, 0, "72200", "65000")],
                                    }
                                ],
                            }
                        ],
                    },
                ]
            }
        ],
        "led": [{"leds_brightness": {"brightness": "50", "brightness_possible": "0,25,50,75,100"}}],
        "parental": [
            {
                "enable": True,
                "schedules": [
                    {"id": 1, "enable": False, "AlwaysBlock": True, "SourceMac": "", "timeslots": []},
                    {"id": 2, "enable": True, "AlwaysBlock": True, "SourceMac": PLUG.upper(), "timeslots": []},
                ],
            }
        ],
        "dhcp_clients": [{"dhcp": {"clients": [{"macaddress": NAS, "enable": True}]}}],
    }


def host(
    host_id: int,
    mac: str,
    name: str,
    ip: str,
    *,
    link: str,
    active: bool = True,
    iface: str = "LAN",
) -> dict[str, Any]:
    return {
        "id": host_id,
        "macaddress": mac,
        "friendlyHostname": name,
        "hostname": name.lower(),
        "ipaddress": ip,
        "active": active,
        "link": link,
        "interface_l3": iface,
        "type": "DHCP",
        "devicetype": "MISCELLANEOUS",
        "lastseen": "2026-01-01T00:00:00Z",
    }


def _station(mac: str, signal: int, rate: str, uplink: str) -> dict[str, Any]:
    return {
        "macAddress": mac,
        "signalStrength": signal,
        "linkQuality": "GOOD",
        "rate": rate,
        "uplinkRate": uplink,
    }
