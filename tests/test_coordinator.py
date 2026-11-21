"""Parsing of router responses into RouterData, and new-client detection."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sagemcom_rest.coordinator import SagemcomCoordinator, detect_new_clients

from . import router_data as rd


@pytest.fixture
def coordinator() -> SagemcomCoordinator:
    client = SimpleNamespace(host="192.0.2.1")
    return SagemcomCoordinator(None, SimpleNamespace(entry_id="test"), client, 30)


def test_router_info_and_wan(coordinator: SagemcomCoordinator) -> None:
    data = coordinator._parse(rd.raw(), 100.0)
    assert data.info["serial"] == "TESTSERIAL01"
    assert data.info["model"] == "F0000T"
    assert data.wan["connected"] is True
    assert data.wan["ipv4"] == "198.51.100.7"
    assert data.system["nat_usage"] == 25.0
    assert data.system["memory_usage"] == 50.0


def test_throughput_from_counter_deltas(coordinator: SagemcomCoordinator) -> None:
    first = coordinator._parse(rd.raw(rx_bytes=1_000_000_000, tx_bytes=0), 100.0)
    assert first.wan["download_mbps"] is None  # needs two samples
    second = coordinator._parse(rd.raw(rx_bytes=1_037_500_000, tx_bytes=3_750_000), 130.0)
    assert second.wan["download_mbps"] == 10.0  # 37.5 MB in 30 s
    assert second.wan["upload_mbps"] == 1.0


def test_counter_reset_gives_no_throughput(coordinator: SagemcomCoordinator) -> None:
    coordinator._parse(rd.raw(rx_bytes=5_000_000), 100.0)
    after_reboot = coordinator._parse(rd.raw(rx_bytes=1_000), 130.0)
    assert after_reboot.wan["download_mbps"] is None


def test_wifi_client(coordinator: SagemcomCoordinator) -> None:
    phone = coordinator._parse(rd.raw(), 100.0).hosts[rd.PHONE]
    assert phone["connection"] == "wifi"
    assert phone["band"] == "5"
    assert phone["ssid"] == "TestNet"
    assert phone["signal"] == -60
    assert phone["rate_mbps"] == 866.7
    assert phone["uplink_rate_mbps"] == 720.6
    assert phone["access_point_id"] == rd.GATEWAY_ID
    assert phone["network"] == "main"
    assert phone["router_id"] == 2


def test_zero_signal_is_unknown(coordinator: SagemcomCoordinator) -> None:
    plug = coordinator._parse(rd.raw(), 100.0).hosts[rd.PLUG]
    assert plug["signal"] is None
    assert plug["access_point"] == "upstairs"


def test_wired_client_behind_port(coordinator: SagemcomCoordinator) -> None:
    nas = coordinator._parse(rd.raw(), 100.0).hosts[rd.NAS]
    assert nas["connection"] == "ethernet"
    assert nas["wired_port_mbps"] == 10000
    assert nas["dhcp_reservation"] is True


def test_guest_and_offline_clients(coordinator: SagemcomCoordinator) -> None:
    hosts = coordinator._parse(rd.raw(), 100.0).hosts
    assert hosts[rd.GUEST]["network"] == "guest"
    assert hosts[rd.OLD]["active"] is False


def test_mesh_nodes_and_client_counts(coordinator: SagemcomCoordinator) -> None:
    nodes = {n["id"]: n for n in coordinator._parse(rd.raw(), 100.0).mesh_nodes}
    assert nodes[rd.GATEWAY_ID]["is_gateway"] is True
    extender = nodes[rd.EXTENDER_ID]
    assert extender["is_gateway"] is False
    assert extender["name"] == "upstairs"
    assert extender["serial"] == "TESTSERIAL02"
    assert extender["backhaul"] == "Wi-Fi"
    assert extender["backhaul_quality"] == "GOOD"
    assert extender["uptime"] == 7200
    assert extender["clients"] == 1
    assert nodes[rd.GATEWAY_ID]["clients"] == 1


def test_controls_state(coordinator: SagemcomCoordinator) -> None:
    controls = coordinator._parse(rd.raw(), 100.0).controls
    assert controls["led_brightness"] == 50
    assert controls["led_levels"] == [0, 25, 50, 75, 100]
    assert controls["parental_enabled"] is True
    # Rules are keyed by lower-case MAC; the empty default rule is ignored.
    assert set(controls["parental_rules"]) == {rd.PLUG}


def test_wifi_bands_and_guest(coordinator: SagemcomCoordinator) -> None:
    data = coordinator._parse(rd.raw(), 100.0)
    assert set(data.wifi) == {"2.4", "5"}
    assert data.wifi["5"]["clients"] == 1  # the phone
    assert data.guest_wifi["2.4"] == {"ssid": "TestGuest", "status": "Down"}


def test_new_clients_fresh_install_announces_nothing() -> None:
    new, known = detect_new_clients(None, {"a", "b"}, set())
    assert new == set()
    assert known == {"a", "b"}


def test_new_clients_after_restart_reports_unregistered() -> None:
    # "c" joined while HA was down: it has no device yet.
    new, known = detect_new_clients(None, {"a", "b", "c"}, {"a", "b"})
    assert new == {"c"}
    assert known == {"a", "b", "c"}


def test_new_clients_only_once() -> None:
    new, known = detect_new_clients({"a"}, {"a", "d"}, set())
    assert new == {"d"}
    new, _ = detect_new_clients(known, {"a", "d"}, set())
    assert new == set()
