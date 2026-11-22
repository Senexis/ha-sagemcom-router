"""API client helpers: login crypto, secret scrubbing, form encoding."""

from __future__ import annotations

from collections.abc import AsyncGenerator

from aiohttp import ClientSession
import pytest
from pytest_homeassistant.test_util.aiohttp import (
    AiohttpClientMocker,
    AiohttpClientMockResponse,
)

from homeassistant.core import HomeAssistant

from custom_components.sagemcom_rest import api


def test_sha512_crypt_glibc_vectors() -> None:
    # Reference vectors from the glibc SHA-crypt specification.
    assert api.sha512_crypt("Hello world!", "saltstring") == (
        "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJu"
        "esI68u4OTLiBFdcbYEdFCoEOfaS35inz1"
    )
    assert api.sha512_crypt("Hello world!", "saltstringsaltstring", rounds=10000) == (
        "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHb"
        "bMCVNSnCM/UrjmM0Dp8vOuZeHBy/YTBmSK6H9qs/y3RnOaw5v."
    )


def test_parse_salt() -> None:
    assert api._parse_salt("abc") == ("abc", 5000)
    assert api._parse_salt("$6$rounds=8000$abc") == ("abc", 8000)


def test_scrub_removes_secrets_only() -> None:
    data = {
        "ssid": "Net",
        "passphrase": "hunter2",
        "wpa_key": "x",
        "radius_secret": "y",
        "nested": [{"Password": "z", "pingrespond": True, "mappings": []}],
    }
    assert api.scrub(data) == {
        "ssid": "Net",
        "nested": [{"pingrespond": True, "mappings": []}],
    }


def test_form_values() -> None:
    assert api._form_value(True) == "true"
    assert api._form_value(False) == "false"
    assert api._form_value(1) == "1"
    assert api._form_value("a b") == "a b"


def test_unwrap() -> None:
    assert api.unwrap([{"a": 1}]) == {"a": 1}
    assert api.unwrap([1, 2]) == [1, 2]
    assert api.unwrap(None) is None


# ---------------------------------------------------------------- HTTP client
BASE = "https://192.0.2.1"


@pytest.fixture
async def session(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> AsyncGenerator[ClientSession]:
    session = aioclient_mock.create_session(hass.loop)
    yield session
    await session.close()


@pytest.fixture
def client(session: ClientSession, aioclient_mock: AiohttpClientMocker) -> api.SagemcomRestClient:
    aioclient_mock.post(f"{BASE}/api/v2/login-params", json=[{"salt": "abc", "nonce": "n0"}])
    aioclient_mock.post(f"{BASE}/api/v1/login", status=204)
    aioclient_mock.post(f"{BASE}/api/v1/logout", status=204)
    return api.SagemcomRestClient(session, "192.0.2.1", "admin", "secret")


def _calls(aioclient_mock: AiohttpClientMocker) -> list[str]:
    return [f"{method.upper()} {url.path}" for method, url, _, _ in aioclient_mock.mock_calls]


async def test_login_sends_digest(client, aioclient_mock: AiohttpClientMocker) -> None:
    await client.login()
    _, _, params, _ = aioclient_mock.mock_calls[0]
    _, _, login, _ = aioclient_mock.mock_calls[1]
    assert params == "login=admin"
    fields = dict(part.split("=") for part in login.split("&"))
    assert fields["login"] == "admin"
    assert len(fields["auth_key"]) == 128  # sha512 hex
    assert len(fields["cnonce"]) == 19
    # Same digest as the router's web UI computes.
    crypt = api.sha512_crypt("secret", "abc")
    ha1 = api._sha512_hex(f"admin:n0:{crypt[3:]}")
    assert fields["auth_key"] == api._sha512_hex(f"{ha1}:0:{fields['cnonce']}")


async def test_login_rejected(session: ClientSession, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.post(f"{BASE}/api/v2/login-params", json=[{"salt": "abc", "nonce": "n0"}])
    aioclient_mock.post(f"{BASE}/api/v1/login", status=401)
    client = api.SagemcomRestClient(session, "192.0.2.1", "a", "b")
    with pytest.raises(api.SagemcomAuthError):
        await client.login()


async def test_get_logs_in_and_scrubs(client, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(
        f"{BASE}/api/v2/wireless/5", json=[{"wireless": {"ssid": {"id": "Net", "passphrase": "x"}}}]
    )
    data = await client.get("/api/v2/wireless/5")
    assert data == [{"wireless": {"ssid": {"id": "Net"}}}]
    assert _calls(aioclient_mock) == [
        "POST /api/v2/login-params",
        "POST /api/v1/login",
        "GET /api/v2/wireless/5",
    ]


async def test_get_relogs_in_when_session_expired(
    client, aioclient_mock: AiohttpClientMocker
) -> None:
    responses = iter([401, 200])

    async def _hosts(method, url, data):
        return AiohttpClientMockResponse(method, url, status=next(responses), json={"ok": 1})

    aioclient_mock.get(f"{BASE}/api/v1/hosts", side_effect=_hosts)
    aioclient_mock.get(f"{BASE}/api/v1/authenticated", json={"authenticated": False})
    await client.login()
    assert await client.get("/api/v1/hosts") == {"ok": 1}
    assert _calls(aioclient_mock)[2:] == [
        "GET /api/v1/hosts",
        "GET /api/v1/authenticated",
        "POST /api/v2/login-params",
        "POST /api/v1/login",
        "GET /api/v1/hosts",
    ]


async def test_unsupported_endpoint(client, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/api/v9/nope", status=404)
    await client.login()
    with pytest.raises(api.SagemcomEndpointError):
        await client.get("/api/v9/nope")


async def test_connection_error(client, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE}/api/v1/hosts", exc=TimeoutError())
    await client.login()
    with pytest.raises(api.SagemcomConnectionError):
        await client.get("/api/v1/hosts")


async def test_write_form_encodes(client, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.put(f"{BASE}/api/v2/wireless/5", status=200, text="")
    await client.write("PUT", "/api/v2/wireless/5", {"radio_enable": False, "ssid": "My Net"})
    _, _, body, headers = aioclient_mock.mock_calls[-1]
    assert body == "radio_enable=false&ssid=My+Net"
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"


async def test_write_refused(client, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.delete(f"{BASE}/api/v1/host/3", status=500, text="busy")
    with pytest.raises(api.SagemcomEndpointError, match="HTTP 500 busy"):
        await client.write("DELETE", "/api/v1/host/3")
    with pytest.raises(ValueError):
        await client.write("PATCH", "/api/v1/host/3")


async def test_logout_only_when_logged_in(client, aioclient_mock: AiohttpClientMocker) -> None:
    await client.logout()
    assert aioclient_mock.call_count == 0
    await client.login()
    await client.logout()
    assert _calls(aioclient_mock)[-1] == "POST /api/v1/logout"
