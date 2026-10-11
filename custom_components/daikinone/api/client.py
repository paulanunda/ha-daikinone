"""Async client for the Daikin One Open API.

This module has no Home Assistant imports. It talks to exactly one host, sends
every request through one method (``_send``), and enforces Daikin's documented
limit of three open HTTP requests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import ssl
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final
from urllib.parse import quote

import aiohttp

from .errors import (
    AuthenticationError,
    BadRequestError,
    ConnectionFailedError,
    DaikinOneError,
    DeviceOfflineError,
    MalformedResponseError,
    RateLimitError,
    ServerError,
)
from .models import (
    DeviceInfo,
    FanCirculate,
    FanCirculateSpeed,
    Mode,
    ThermostatState,
    parse_device_list,
)

_LOGGER = logging.getLogger(__name__)

API_HOST: Final = "integrator-api.daikinskyport.com"
BASE_URL: Final = f"https://{API_HOST}"

# Documented usage limits.
MAX_OPEN_REQUESTS: Final = 3
MIN_POLL_INTERVAL_SECONDS: Final = 180
WRITE_SETTLE_SECONDS: Final = 15

DEFAULT_REQUEST_TIMEOUT: Final = 20.0

# Backoff after HTTP 429. Daikin does not document a Retry-After header, so it
# is honored when present and replaced by exponential backoff when absent.
RATE_LIMIT_BACKOFF_BASE: Final = 180.0
RATE_LIMIT_BACKOFF_MAX: Final = 1800.0
RETRY_AFTER_MIN: Final = 1.0
RETRY_AFTER_MAX: Final = 3600.0

# Refresh the access token this long before it is due to expire, or after 90%
# of its lifetime, whichever comes first.
TOKEN_EXPIRY_MARGIN: Final = 60.0

_MSG_DEVICE_OFFLINE: Final = "DeviceOfflineException"
_MSG_NOT_AUTHORIZED: Final = "NotAuthorizedException"

_TOKEN_PATH: Final = "/v1/token"  # noqa: S105 - a URL path, not a secret
_DEVICES_PATH: Final = "/v1/devices"


def create_ssl_context() -> ssl.SSLContext:
    """Return a TLS context that refuses anything older than TLS 1.3.

    Loading the system certificates reads from disk, so call this from a worker
    thread when running inside an event loop.
    """
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    return context


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delay in seconds, or an HTTP date)."""
    if value is None:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except TypeError, ValueError:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        seconds = (when - datetime.now(UTC)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return min(max(seconds, RETRY_AFTER_MIN), RETRY_AFTER_MAX)


def _message_of(payload: object) -> str:
    """Return the documented message string from a response body, if any."""
    if not isinstance(payload, dict):
        return ""
    for key in ("messages", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


class DaikinOneClient:
    """Client for one Daikin account."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        *,
        email: str,
        api_key: str,
        integrator_token: str,
        ssl_context: ssl.SSLContext | None = None,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create a client. ``session`` is borrowed and never closed here."""
        self._session = session
        self._email = email
        self._api_key = api_key
        self._integrator_token = integrator_token
        self._ssl_context = ssl_context
        self._timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._monotonic = monotonic

        self._open_requests = asyncio.Semaphore(MAX_OPEN_REQUESTS)
        self._token_lock = asyncio.Lock()
        self._access_token: str | None = None
        self._token_expires_at: float | None = None
        self._blocked_until = 0.0
        self._consecutive_rate_limits = 0

    # ------------------------------------------------------------------ public

    async def authenticate(self) -> None:
        """Fetch a fresh access token, raising if the credentials are rejected."""
        async with self._token_lock:
            await self._fetch_token()

    async def list_devices(self) -> list[DeviceInfo]:
        """Return every thermostat on the account."""
        payload = await self._request("GET", _DEVICES_PATH, label="GET /v1/devices")
        return parse_device_list(payload)

    async def get_thermostat(self, device_id: str) -> ThermostatState:
        """Return the current state of one thermostat."""
        payload = await self._request(
            "GET", self._device_path(device_id), label="GET /v1/devices/<id>"
        )
        return ThermostatState.from_api(payload)

    async def set_mode_setpoints(
        self, device_id: str, *, mode: Mode, heat_setpoint: float, cool_setpoint: float
    ) -> None:
        """Set mode and both setpoints (Celsius). Daikin requires all three.

        A successful call also turns off the thermostat's schedule and away state.
        """
        body = {
            "mode": int(mode),
            "heatSetpoint": round(float(heat_setpoint), 1),
            "coolSetpoint": round(float(cool_setpoint), 1),
        }
        await self._request(
            "PUT",
            self._device_path(device_id, "msp"),
            body=body,
            label="PUT /v1/devices/<id>/msp",
        )

    async def set_schedule(self, device_id: str, *, enabled: bool) -> None:
        """Enable or disable the thermostat's own schedule."""
        await self._request(
            "PUT",
            self._device_path(device_id, "schedule"),
            body={"scheduleEnabled": bool(enabled)},
            label="PUT /v1/devices/<id>/schedule",
        )

    async def set_fan(
        self, device_id: str, *, circulate: FanCirculate, speed: FanCirculateSpeed
    ) -> None:
        """Set the fan circulation mode and speed (unitary systems only)."""
        await self._request(
            "PUT",
            self._device_path(device_id, "fan"),
            body={"fanCirculate": int(circulate), "fanCirculateSpeed": int(speed)},
            label="PUT /v1/devices/<id>/fan",
        )

    # ----------------------------------------------------------------- private

    @staticmethod
    def _device_path(device_id: str, suffix: str | None = None) -> str:
        if not isinstance(device_id, str) or not device_id:
            raise BadRequestError("Thermostat id is empty")
        path = f"{_DEVICES_PATH}/{quote(device_id, safe='')}"
        return f"{path}/{suffix}" if suffix else path

    async def _request(
        self, method: str, path: str, *, label: str, body: Mapping[str, object] | None = None
    ) -> object:
        """Send an authenticated request, renewing the access token once on 401."""
        for attempt in (1, 2):
            token = await self._ensure_token()
            status, payload, retry_after = await self._send(
                method, path, body=body, token=token, label=label
            )
            if status == 401 and attempt == 1:
                # The token expired earlier than expected. Get a new one and retry once.
                await self._discard_token(token)
                continue
            self._raise_for_status(status, payload, retry_after)
            return payload
        raise AssertionError("unreachable")  # pragma: no cover

    async def _ensure_token(self) -> str:
        async with self._token_lock:
            expired = (
                self._token_expires_at is not None and self._monotonic() >= self._token_expires_at
            )
            if self._access_token is None or expired:
                await self._fetch_token()
            assert self._access_token is not None  # noqa: S101 - set by _fetch_token
            return self._access_token

    async def _discard_token(self, token: str) -> None:
        async with self._token_lock:
            # Another request may already have replaced it.
            if self._access_token == token:
                self._access_token = None
                self._token_expires_at = None

    async def _fetch_token(self) -> None:
        """POST /v1/token. Must be called with the token lock held."""
        self._access_token = None
        self._token_expires_at = None
        status, payload, retry_after = await self._send(
            "POST",
            _TOKEN_PATH,
            body={"email": self._email, "integratorToken": self._integrator_token},
            token=None,
            label="POST /v1/token",
        )
        if status in (400, 401, 403):
            # The request body is well-formed by construction, so any of these
            # means Daikin did not accept the credentials.
            raise AuthenticationError(
                f"Daikin rejected the credentials (HTTP {status})", status=status
            )
        self._raise_for_status(status, payload, retry_after)
        token = payload.get("accessToken") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise MalformedResponseError("Token response has no access token", status=status)
        self._access_token = token
        self._token_expires_at = self._expiry_deadline(
            payload.get("accessTokenExpiresIn") if isinstance(payload, dict) else None
        )

    def _expiry_deadline(self, expires_in: object) -> float | None:
        """Turn accessTokenExpiresIn into a monotonic deadline.

        The unit is undocumented and assumed to be seconds. If that is wrong, or
        the value is unusable, the 401 retry in ``_request`` still renews the token.
        """
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            return None
        lifetime = float(expires_in)
        if not math.isfinite(lifetime) or lifetime <= 0:
            return None
        margin = min(TOKEN_EXPIRY_MARGIN, lifetime * 0.1)
        return self._monotonic() + lifetime - margin

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None,
        token: str | None,
        label: str,
    ) -> tuple[int, object, float | None]:
        """Perform one HTTP exchange. The only place a request leaves the client.

        Returns the status, the decoded JSON body (None if it is not JSON), and
        the parsed Retry-After delay.
        """
        remaining = self._blocked_until - self._monotonic()
        if remaining > 0:
            raise RateLimitError(
                "Still backing off after an earlier HTTP 429", retry_after=remaining, status=None
            )

        headers = {"Content-Type": "application/json", "x-api-key": self._api_key}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        async with self._open_requests:
            try:
                async with self._session.request(
                    method,
                    f"{BASE_URL}{path}",
                    json=body,
                    headers=headers,
                    timeout=self._timeout,
                    ssl=self._ssl_context if self._ssl_context is not None else True,
                    allow_redirects=False,
                ) as response:
                    status = response.status
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                    raw = await response.read()
            except TimeoutError:
                raise ConnectionFailedError(f"{label} timed out") from None
            except aiohttp.ClientError as err:
                # Only the exception type is kept: aiohttp messages can quote the request.
                raise ConnectionFailedError(f"{label} failed ({type(err).__name__})") from None

        _LOGGER.debug("%s -> HTTP %s", label, status)
        try:
            payload: object = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        return status, payload, retry_after

    def _raise_for_status(self, status: int, payload: object, retry_after: float | None) -> None:
        """Raise the matching error for anything other than a clean success."""
        message = _message_of(payload)

        if status == 429:
            self._consecutive_rate_limits += 1
            if retry_after is None:
                retry_after = min(
                    RATE_LIMIT_BACKOFF_BASE * 2 ** (self._consecutive_rate_limits - 1),
                    RATE_LIMIT_BACKOFF_MAX,
                )
            self._blocked_until = self._monotonic() + retry_after
            raise RateLimitError("Daikin rate limit exceeded (HTTP 429)", retry_after=retry_after)
        self._consecutive_rate_limits = 0

        # Daikin does not document which status accompanies an offline thermostat,
        # so the documented message decides, whatever the status.
        if _MSG_DEVICE_OFFLINE in message:
            raise DeviceOfflineError(f"Thermostat is offline (HTTP {status})", status=status)
        if status in (401, 403) or _MSG_NOT_AUTHORIZED in message:
            raise AuthenticationError(f"Daikin rejected the request (HTTP {status})", status=status)
        if 200 <= status < 300:
            return
        if 400 <= status < 500:
            raise BadRequestError(f"Daikin refused the request (HTTP {status})", status=status)
        if status >= 500:
            raise ServerError(f"Daikin server error (HTTP {status})", status=status)
        raise ServerError(f"Unexpected response from Daikin (HTTP {status})", status=status)


__all__ = [
    "API_HOST",
    "BASE_URL",
    "MAX_OPEN_REQUESTS",
    "MIN_POLL_INTERVAL_SECONDS",
    "WRITE_SETTLE_SECONDS",
    "DaikinOneClient",
    "DaikinOneError",
    "create_ssl_context",
]
