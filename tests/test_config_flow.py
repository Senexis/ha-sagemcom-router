"""Config, reauth, reconfigure and options flows."""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry

from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import entity_registry as er

from custom_components.sagemcom_rest.api import (
    SagemcomAuthError,
    SagemcomConnectionError,
)
from custom_components.sagemcom_rest.const import (
    CONF_ENABLE_CONTROLS,
    CONF_SCAN_INTERVAL,
    CONF_VERIFY_SSL,
    DOMAIN,
)

from . import router_data as rd
from .common import ENTRY_DATA, SERIAL, FakeRouter, make_entry


async def test_user_flow(hass: HomeAssistant, router: FakeRouter) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Sagemcom F0000T"
    assert result["data"] == ENTRY_DATA
    assert result["result"].unique_id == SERIAL


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (SagemcomAuthError("rejected"), "invalid_auth"),
        (SagemcomConnectionError("timeout"), "cannot_connect"),
    ],
)
async def test_user_flow_errors_then_recovers(
    hass: HomeAssistant, router: FakeRouter, error: Exception, reason: str
) -> None:
    router.fail_login = error
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": reason}

    router.fail_login = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], ENTRY_DATA)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_user_flow_already_configured_updates_host(
    hass: HomeAssistant, router: FakeRouter
) -> None:
    entry = make_entry()
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_HOST: "192.0.2.2"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_HOST] == "192.0.2.2"


async def test_reauth(hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter) -> None:
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    router.fail_login = SagemcomAuthError("rejected")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    router.fail_login = None
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_USERNAME: "admin", CONF_PASSWORD: "new-secret"}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data[CONF_PASSWORD] == "new-secret"
    assert entry.data[CONF_HOST] == ENTRY_DATA[CONF_HOST]  # kept


async def test_reconfigure(hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter) -> None:
    result = await entry.start_reconfigure_flow(hass)
    assert result["step_id"] == "reconfigure"

    new = {**ENTRY_DATA, CONF_HOST: "192.0.2.9", CONF_PASSWORD: "new", CONF_VERIFY_SSL: True}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], new)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert dict(entry.data) == new


async def test_reconfigure_refuses_other_router(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    result = await entry.start_reconfigure_flow(hass)
    router.raw["device_info"][0]["serialNumber"] = "OTHERSERIAL"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**ENTRY_DATA, CONF_HOST: "192.0.2.9"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_router"
    assert entry.data[CONF_HOST] == ENTRY_DATA[CONF_HOST]


async def test_options_enable_controls(
    hass: HomeAssistant, entry: MockConfigEntry, router: FakeRouter
) -> None:
    ent_reg = er.async_get(hass)

    def registered(domain: str, unique_id: str) -> bool:
        return ent_reg.async_get_entity_id(domain, DOMAIN, unique_id) is not None

    # Read-only twins while controls are off.
    assert registered("binary_sensor", f"{SERIAL}_guest_wifi")
    assert registered("binary_sensor", f"{rd.PHONE}_blocked")
    assert registered("sensor", f"{SERIAL}_led_brightness")
    assert not registered("switch", f"{SERIAL}_guest_wifi_switch")

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_CONTROLS: True}
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_SCAN_INTERVAL: 60, CONF_ENABLE_CONTROLS: True}
    assert entry.runtime_data.update_interval.total_seconds() == 60

    # The controls replace the twins.
    assert registered("switch", f"{SERIAL}_guest_wifi_switch")
    assert registered("switch", f"{rd.PHONE}_block_internet")
    assert registered("light", f"{SERIAL}_led")
    assert not registered("binary_sensor", f"{SERIAL}_guest_wifi")
    assert not registered("binary_sensor", f"{rd.PHONE}_blocked")
    assert not registered("sensor", f"{SERIAL}_led_brightness")
    # Other binary sensors stay.
    assert registered("binary_sensor", f"{SERIAL}_internet")
    assert registered("binary_sensor", f"mesh_{rd.EXTENDER_ID}_connected")


async def test_options_reject_short_interval(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    with pytest.raises(InvalidData):  # below MIN_SCAN_INTERVAL
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: 1, CONF_ENABLE_CONTROLS: False}
        )
