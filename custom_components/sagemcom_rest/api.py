"""Async client for the Sagemcom "REST GUI" API (e.g. DELTA F5598T).

The firmware exposes ``/api/v1/*`` and ``/api/v2/*`` JSON endpoints that the
web interface uses. Only ``/api/v1/open`` works without a session; everything
else needs a challenge/response login:

    POST /api/v2/login-params  login=<user>              -> [{salt, nonce}]
    crypt   = sha512_crypt(password, salt)                 ("$6$salt$hash")
    ha1     = sha512_hex(f"{user}:{nonce}:{crypt[3:]}")
    authkey = sha512_hex(f"{ha1}:0:{cnonce}")
    POST /api/v1/login  login=<user>&auth_key=<authkey>&cnonce=<cnonce> -> 204

The session lives in cookies; every request after login must also carry the
``__Host-csrf_token`` cookie value in an ``X-CSRF-Token`` header.

Reads use GET. ``write`` sends PUT/POST/DELETE requests; the integration only
calls it from control entities, which exist only when the user turns on
"Enable controls" in the options.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import aiohttp

_LOGGER = logging.getLogger(__name__)

_B64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
_CRYPT_ORDER = (
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26),
    (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32),
    (12, 33, 54), (34, 55, 13), (56, 14, 35), (15, 36, 57), (37, 58, 16),
    (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41),
)

CSRF_COOKIE = "__Host-csrf_token"
CSRF_HEADER = "X-CSRF-Token"

# Keys whose values must never leave the client (Wi-Fi passphrases etc.).
_SENSITIVE_KEY_PARTS = ("password", "passphrase", "secret", "psk")
_SENSITIVE_KEYS = {"security", "pin", "puk", "key", "wpa_key", "auth_key", "new_pin"}


class SagemcomError(Exception):
    """Generic API error."""


class SagemcomConnectionError(SagemcomError):
    """Router unreachable / timeout."""


class SagemcomBusyError(SagemcomConnectionError):
    """The router answers, but its API is failing (5xx, or a bare 400 page).

    Seen when the router's web service is overloaded, e.g. with too many open
    sessions. It passes on its own; logging in again only makes it worse.
    """


class SagemcomAuthError(SagemcomError):
    """Login rejected."""


class SagemcomEndpointError(SagemcomError):
    """Endpoint not available on this firmware (while logged in)."""


def _api_exception(text: str) -> bool:
    """Whether a body is the API's own JSON error ({"exception": ...}).

    The API answers unknown endpoints and requests without a session this way;
    a broken web service answers with a plain "400 Bad Request" or an HTML page.
    """
    try:
        data = unwrap(json.loads(text))
    except ValueError:
        return False
    return isinstance(data, dict) and "exception" in data


def _failure(method: str, path: str, status: int, text: str) -> SagemcomError:
    if status >= 500 or (status == 400 and not _api_exception(text)):
        return SagemcomBusyError(f"{method} {path}: HTTP {status} (router API not responding)")
    detail = f" {text[:200]}" if method != "GET" else ""
    return SagemcomEndpointError(f"{method} {path}: HTTP {status}{detail}")


def sha512_crypt(password: str, salt: str, rounds: int = 5000) -> str:
    """Pure-python SHA-512 crypt ($6$), as glibc / the router's web UI."""
    salt = salt[:16]
    p = password.encode()
    s = salt.encode()
    b = hashlib.sha512(p + s + p).digest()
    a = hashlib.sha512(p + s)
    for i in range(len(p), 0, -64):
        a.update(b if i > 64 else b[:i])
    i = len(p)
    while i:
        a.update(b if i & 1 else p)
        i >>= 1
    a_dig = a.digest()
    dp = hashlib.sha512(p * len(p)).digest()
    p_bytes = (dp * (len(p) // 64 + 1))[: len(p)]
    ds = hashlib.sha512(s * (16 + a_dig[0])).digest()
    s_bytes = (ds * (len(s) // 64 + 1))[: len(s)]
    c = a_dig
    for r in range(rounds):
        h = hashlib.sha512()
        h.update(p_bytes if r & 1 else c)
        if r % 3:
            h.update(s_bytes)
        if r % 7:
            h.update(p_bytes)
        h.update(c if r & 1 else p_bytes)
        c = h.digest()
    out = []
    for x, y, z in _CRYPT_ORDER:
        v = (c[x] << 16) | (c[y] << 8) | c[z]
        for _ in range(4):
            out.append(_B64[v & 63])
            v >>= 6
    v = c[63]
    for _ in range(2):
        out.append(_B64[v & 63])
        v >>= 6
    prefix = "$6$" if rounds == 5000 else f"$6$rounds={rounds}$"
    return f"{prefix}{salt}${''.join(out)}"


def _sha512_hex(value: str) -> str:
    return hashlib.sha512(value.encode()).hexdigest()


def _parse_salt(raw: str) -> tuple[str, int]:
    """Accept a bare salt or a '$6$[rounds=N$]salt' spec."""
    if "$" not in raw:
        return raw, 5000
    parts = [p for p in raw.split("$") if p]
    rounds = 5000
    salt = parts[-1]
    for part in parts:
        if part.startswith("rounds="):
            rounds = min(max(int(part[7:]), 1000), 999_999_999)
    return salt, rounds


def scrub(data: Any) -> Any:
    """Recursively drop credential-like keys from API data."""
    if isinstance(data, dict):
        return {
            k: scrub(v)
            for k, v in data.items()
            if not any(part in k.lower() for part in _SENSITIVE_KEY_PARTS)
            and k.lower() not in _SENSITIVE_KEYS
        }
    if isinstance(data, list):
        return [scrub(v) for v in data]
    return data


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def unwrap(data: Any) -> Any:
    """Most endpoints return a one-element list; return that element."""
    if isinstance(data, list) and len(data) == 1:
        return data[0]
    return data


class SagemcomRestClient:
    """Minimal read-only client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        host: str,
        username: str,
        password: str,
        *,
        verify_ssl: bool = False,
        timeout: float = 15,
    ) -> None:
        self._session = session
        self._base = f"https://{host}"
        self._username = username
        self._password = password
        self._ssl: bool | None = None if verify_ssl else False
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._lock = asyncio.Lock()
        self._logged_in = False

    @property
    def host(self) -> str:
        return self._base.removeprefix("https://")

    # ------------------------------------------------------------------ http
    def _csrf(self) -> str | None:
        for cookie in self._session.cookie_jar:
            if cookie.key == CSRF_COOKIE:
                return cookie.value
        return None

    async def _raw(
        self,
        method: str,
        path: str,
        data: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        headers = {"Accept": "application/json"}
        if (token := self._csrf()) is not None:
            headers[CSRF_HEADER] = token
        body: str | None = None
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            body = urlencode(data)
        try:
            async with self._session.request(
                method,
                self._base + path,
                data=body,
                headers=headers,
                ssl=self._ssl,
                timeout=aiohttp.ClientTimeout(total=timeout) if timeout else self._timeout,
            ) as resp:
                return resp.status, await resp.text()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise SagemcomConnectionError(f"{method} {path}: {err!r}") from err

    @staticmethod
    def _json(path: str, text: str) -> Any:
        try:
            return json.loads(text) if text.strip() else None
        except ValueError as err:
            raise SagemcomError(f"{path}: invalid JSON") from err

    # ----------------------------------------------------------------- auth
    async def login(self) -> None:
        """Log in (or re-login)."""
        async with self._lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        # End the old session first: the router keeps sessions open until they
        # time out, and too many of them break its API.
        await self._drop_session()
        status, text = await self._raw(
            "POST", "/api/v2/login-params", {"login": self._username}
        )
        if status != 200:
            if status >= 500 or (status == 400 and not _api_exception(text)):
                raise _failure("POST", "/api/v2/login-params", status, text)
            raise SagemcomError(f"login-params failed (HTTP {status})")
        params = unwrap(self._json("login-params", text)) or {}
        raw_salt, nonce = params.get("salt"), params.get("nonce")
        if not raw_salt or not nonce:
            raise SagemcomError("login-params returned no salt/nonce")
        salt, rounds = _parse_salt(raw_salt)
        crypt = await asyncio.get_running_loop().run_in_executor(
            None, sha512_crypt, self._password, salt, rounds
        )
        ha1 = _sha512_hex(f"{self._username}:{nonce}:{crypt[3:]}")
        cnonce = str(secrets.randbelow(10**19)).zfill(19)
        status, text = await self._raw(
            "POST",
            "/api/v1/login",
            {
                "login": self._username,
                "auth_key": _sha512_hex(f"{ha1}:0:{cnonce}"),
                "cnonce": cnonce,
            },
        )
        if status in (200, 204):
            self._logged_in = True
            _LOGGER.debug("Logged in to %s", self.host)
            return
        if status >= 500 or (status == 400 and not _api_exception(text)):
            raise _failure("POST", "/api/v1/login", status, text)
        if status in (400, 401, 403):
            raise SagemcomAuthError(f"login rejected (HTTP {status})")
        raise SagemcomError(f"login failed (HTTP {status}): {text[:200]}")

    async def logout(self) -> None:
        async with self._lock:
            await self._drop_session()

    async def _drop_session(self) -> None:
        """Log the current session out (best effort) and forget its cookies."""
        if self._logged_in:
            self._logged_in = False
            try:
                await self._raw("POST", "/api/v1/logout", {}, timeout=5)
            except SagemcomError:
                pass
        self._session.cookie_jar.clear()

    async def _session_state(self) -> bool | None:
        """True/False when the router says whether we're logged in, None if it can't."""
        try:
            status, text = await self._raw("GET", "/api/v1/authenticated")
        except SagemcomConnectionError:
            return None
        if status in (401, 403) or (status == 400 and _api_exception(text)):
            return False
        if status != 200:
            return None
        try:
            data = unwrap(self._json("authenticated", text))
        except SagemcomError:
            return None
        return bool(isinstance(data, dict) and data.get("authenticated"))

    async def _request(
        self,
        method: str,
        path: str,
        body: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        """Send a request with the session, logging in again once if it expired.

        Logs in again only when the router confirms the session is gone. When
        its API is failing instead, a new login would just add another open
        session, so that raises SagemcomBusyError.
        """
        async with self._lock:
            if not self._logged_in:
                await self._login_locked()
            status, text = await self._raw(method, path, body, timeout)
            if status in (400, 401, 403):
                state = await self._session_state()
                if state is None:
                    raise _failure(method, path, status, text)
                if state is False:
                    _LOGGER.debug("Session expired, logging in again")
                    await self._login_locked()
                    status, text = await self._raw(method, path, body, timeout)
        return status, text

    # ------------------------------------------------------------------ api
    async def get(
        self, path: str, *, sanitize: bool = True, timeout: float | None = None
    ) -> Any:
        """GET an endpoint, re-logging in once if the session expired.

        Raises SagemcomEndpointError if the API refuses the endpoint while the
        session is valid (i.e. not supported by this firmware), and
        SagemcomBusyError when the router's API is failing.
        """
        if not path.startswith("/"):
            path = "/" + path
        status, text = await self._request("GET", path, timeout=timeout)
        if status != 200:
            raise _failure("GET", path, status, text)
        data = self._json(path, text)
        return scrub(data) if sanitize else data

    async def write(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> Any:
        """PUT/POST/DELETE like the web UI does: flat form-encoded body.

        Booleans are sent as "true"/"false" (what the UI's HttpParams produce);
        callers pass 1/0 where the UI converts to numbers. Re-logs in once if
        the session expired. Raises SagemcomEndpointError when the router
        rejects the request.
        """
        if method not in ("PUT", "POST", "DELETE"):
            raise ValueError(method)
        body = {k: _form_value(v) for k, v in (data or {}).items()}
        status, text = await self._request(method, path, body)
        if status not in (200, 201, 202, 204):
            raise _failure(method, path, status, text)
        _LOGGER.debug("%s %s -> %s", method, path, status)
        return self._json(path, text) if text.strip() else None

    async def get_open(self) -> Any:
        """Unauthenticated summary endpoint."""
        status, text = await self._raw("GET", "/api/v1/open")
        if status != 200:
            raise SagemcomEndpointError(f"GET /api/v1/open: HTTP {status}")
        return scrub(unwrap(self._json("open", text)))
