"""The client against the documented contract, and every documented failure."""

from __future__ import annotations

import asyncio
import logging
import ssl
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import aiohttp
import pytest

from custom_components.daikinone.api import (
    API_HOST,
    BASE_URL,
    MAX_OPEN_REQUESTS,
    MIN_POLL_INTERVAL_SECONDS,
    WRITE_SETTLE_SECONDS,
    AuthenticationError,
    BadRequestError,
    ConnectionFailedError,
    DaikinOneClient,
    DaikinOneError,
    DeviceOfflineError,
    FanCirculate,
    FanCirculateSpeed,
    MalformedResponseError,
    Mode,
    RateLimitError,
    ServerError,
)
from custom_components.daikinone.api.client import DEFAULT_REQUEST_TIMEOUT
from tests.api.conftest import FakeClock
from tests.fake_daikin import (
    ACCESS_TOKEN_PREFIX,
    API_KEY,
    CONTRACT,
    EMAIL,
    INTEGRATOR_TOKEN,
    MAIN_ID,
    UPSTAIRS_ID,
    FakeDaikin,
    Reply,
)

DEVICE = f"/v1/devices/{MAIN_ID}"
SECRETS = (API_KEY, INTEGRATOR_TOKEN, ACCESS_TOKEN_PREFIX, EMAIL, EMAIL.split("@", maxsplit=1)[0])


# ---------------------------------------------------------------- contract


def test_constants_match_the_contract() -> None:
    assert CONTRACT["transport"]["base_url"] == BASE_URL
    assert CONTRACT["transport"]["host"] == API_HOST
    limits = CONTRACT["usage_limits"]
    assert limits["max_open_requests"] == MAX_OPEN_REQUESTS
    assert limits["min_poll_interval_seconds"] == MIN_POLL_INTERVAL_SECONDS
    assert limits["min_seconds_before_change_is_reflected"] == WRITE_SETTLE_SECONDS


async def test_token_request_matches_the_contract(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    await client.authenticate()
    (call,) = daikin.calls
    spec = CONTRACT["endpoints"]["token"]
    assert (call.method, call.path) == (spec["method"], spec["path"])
    assert set(call.json) == set(spec["request_body"])
    # The email is case sensitive and must be sent exactly as entered.
    assert call.json == {"email": EMAIL, "integratorToken": INTEGRATOR_TOKEN}
    assert call.headers == {"Content-Type": "application/json", "x-api-key": API_KEY}


async def test_read_requests_match_the_contract(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    devices = await client.list_devices()
    state = await client.get_thermostat(MAIN_ID)

    assert [d.id for d in devices] == [MAIN_ID, UPSTAIRS_ID]
    assert devices[0].name == "Main Floor"
    assert devices[0].location_name == "Home"
    assert state.temp_indoor == 21.5

    token_call, list_call, get_call = daikin.calls
    assert (list_call.method, list_call.path) == ("GET", "/v1/devices")
    assert (get_call.method, get_call.path) == ("GET", DEVICE)
    for call in (list_call, get_call):
        assert call.json is None
        assert call.headers == {
            "Content-Type": "application/json",
            "x-api-key": API_KEY,
            "Authorization": f"Bearer {ACCESS_TOKEN_PREFIX}1",
        }
    assert token_call.path == "/v1/token"


async def test_write_requests_match_the_contract(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    await client.set_mode_setpoints(
        MAIN_ID, mode=Mode.COOL, heat_setpoint=20.04, cool_setpoint=24.26
    )
    await client.set_schedule(MAIN_ID, enabled=True)
    await client.set_fan(MAIN_ID, circulate=FanCirculate.SCHEDULE, speed=FanCirculateSpeed.LOW)

    msp, schedule, fan = daikin.calls[1:]
    endpoints = CONTRACT["endpoints"]
    assert (msp.method, msp.path) == ("PUT", f"{DEVICE}/msp")
    assert set(msp.json) == set(endpoints["set_mode_setpoints"]["request_body"])
    # Setpoints are sent in the documented 0.1 degree increments.
    assert msp.json == {"mode": 2, "heatSetpoint": 20.0, "coolSetpoint": 24.3}
    assert type(msp.json["mode"]) is int

    assert (schedule.method, schedule.path) == ("PUT", f"{DEVICE}/schedule")
    assert schedule.json == {"scheduleEnabled": True}
    assert set(schedule.json) == set(endpoints["set_schedule"]["request_body"])

    assert (fan.method, fan.path) == ("PUT", f"{DEVICE}/fan")
    assert fan.json == {"fanCirculate": 2, "fanCirculateSpeed": 0}
    assert set(fan.json) == set(endpoints["set_fan"]["request_body"])
    assert type(fan.json["fanCirculate"]) is int

    # The fake applied them the way the documentation describes.
    assert daikin.states[MAIN_ID]["mode"] == 2
    assert daikin.states[MAIN_ID]["scheduleEnabled"] is True


async def test_every_request_has_a_timeout_and_never_follows_redirects(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    await client.list_devices()
    await client.get_thermostat(MAIN_ID)
    await client.set_schedule(MAIN_ID, enabled=False)
    assert len(daikin.calls) == 4
    for call in daikin.calls:
        timeout = call.kwargs["timeout"]
        assert isinstance(timeout, aiohttp.ClientTimeout)
        assert timeout.total == DEFAULT_REQUEST_TIMEOUT
        assert call.kwargs["allow_redirects"] is False
        assert call.kwargs["ssl"] is True


async def test_ssl_context_is_used_for_every_request(daikin: FakeDaikin) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client = DaikinOneClient(
        daikin.session(),
        email=EMAIL,
        api_key=API_KEY,
        integrator_token=INTEGRATOR_TOKEN,
        ssl_context=context,
        request_timeout=7,
    )
    await client.list_devices()
    assert [call.kwargs["ssl"] for call in daikin.calls] == [context, context]
    assert [call.kwargs["timeout"].total for call in daikin.calls] == [7, 7]


def test_created_ssl_context_requires_tls_1_3() -> None:
    from custom_components.daikinone.api import create_ssl_context  # noqa: PLC0415

    context = create_ssl_context()
    assert context.minimum_version is ssl.TLSVersion.TLSv1_3
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_device_id_is_escaped_in_the_path(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    with pytest.raises(BadRequestError):
        await client.get_thermostat("../token?x=1")
    assert daikin.calls[-1].path == "/v1/devices/..%2Ftoken%3Fx%3D1"


async def test_empty_device_id_is_refused_without_a_request(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    with pytest.raises(BadRequestError):
        await client.get_thermostat("")
    assert daikin.calls == []


# ------------------------------------------------------------------ tokens


async def test_token_is_reused_until_it_expires(
    client: DaikinOneClient, daikin: FakeDaikin, clock: FakeClock
) -> None:
    await client.list_devices()
    await client.get_thermostat(MAIN_ID)
    assert daikin.tokens_issued == 1

    clock.advance(3600 - 61)
    await client.get_thermostat(MAIN_ID)
    assert daikin.tokens_issued == 1

    clock.advance(2)  # inside the 60 second safety margin
    await client.get_thermostat(MAIN_ID)
    assert daikin.tokens_issued == 2


@pytest.mark.parametrize("expires_in", [None, "soon", 0, -5, True, float("nan"), [3600]])
async def test_unusable_token_lifetime_falls_back_to_renewing_on_401(
    client: DaikinOneClient, daikin: FakeDaikin, clock: FakeClock, expires_in: Any
) -> None:
    daikin.expires_in = expires_in
    await client.list_devices()
    clock.advance(10_000_000)
    await client.list_devices()
    assert daikin.tokens_issued == 1

    daikin.expire_tokens()
    await client.list_devices()
    assert daikin.tokens_issued == 2


async def test_401_renews_the_token_once_and_retries(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    await client.get_thermostat(MAIN_ID)
    daikin.expire_tokens()
    state = await client.get_thermostat(MAIN_ID)
    assert state.temp_indoor == 21.5
    assert [c.path for c in daikin.calls] == ["/v1/token", DEVICE, DEVICE, "/v1/token", DEVICE]


async def test_persistent_401_raises_after_exactly_one_retry(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    daikin.always("GET", DEVICE, Reply(401, {"messages": "NotAuthorizedException"}))
    with pytest.raises(AuthenticationError) as err:
        await client.get_thermostat(MAIN_ID)
    assert err.value.status == 401
    assert len(daikin.calls_to("GET", DEVICE)) == 2
    assert daikin.tokens_issued == 2


async def test_concurrent_401s_share_one_token_renewal(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    await client.list_devices()
    daikin.expire_tokens()
    await asyncio.gather(client.get_thermostat(MAIN_ID), client.get_thermostat(UPSTAIRS_ID))
    assert daikin.tokens_issued == 2


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_rejected_credentials_raise_authentication_error(
    client: DaikinOneClient, daikin: FakeDaikin, status: int
) -> None:
    daikin.queue("POST", "/v1/token", Reply(status, {"messages": "whatever"}))
    with pytest.raises(AuthenticationError) as err:
        await client.authenticate()
    assert err.value.status == status
    assert len(daikin.calls) == 1


async def test_wrong_credentials_are_rejected_by_the_fake(daikin: FakeDaikin) -> None:
    for email, api_key, integrator_token in (
        (EMAIL.lower(), API_KEY, INTEGRATOR_TOKEN),
        (EMAIL, "wrong", INTEGRATOR_TOKEN),
        (EMAIL, API_KEY, "wrong"),
    ):
        wrong = DaikinOneClient(
            daikin.session(), email=email, api_key=api_key, integrator_token=integrator_token
        )
        with pytest.raises(AuthenticationError):
            await wrong.authenticate()


@pytest.mark.parametrize("body", [None, [], {}, {"accessToken": ""}, {"accessToken": 5}])
async def test_token_response_without_a_token_is_malformed(
    client: DaikinOneClient, daikin: FakeDaikin, body: Any
) -> None:
    daikin.queue("POST", "/v1/token", Reply(200, body))
    with pytest.raises(MalformedResponseError):
        await client.authenticate()


# -------------------------------------------------------------- HTTP errors


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (400, BadRequestError),
        (403, AuthenticationError),
        (404, BadRequestError),
        (415, BadRequestError),
        (500, ServerError),
        (502, ServerError),
        (503, ServerError),
        (302, ServerError),
        (100, ServerError),
    ],
)
async def test_documented_statuses_map_to_errors(
    client: DaikinOneClient, daikin: FakeDaikin, status: int, expected: type[DaikinOneError]
) -> None:
    daikin.queue("GET", DEVICE, Reply(status, {"messages": "Invalid request body"}))
    with pytest.raises(expected) as err:
        await client.get_thermostat(MAIN_ID)
    assert type(err.value) is expected
    assert err.value.status == status


def test_every_documented_status_is_covered() -> None:
    assert set(CONTRACT["http_status"]) == {"200", "400", "401", "403", "415", "429", "500"}


@pytest.mark.parametrize("status", [200, 400, 404, 500, 503])
@pytest.mark.parametrize("key", ["messages", "message"])
async def test_device_offline_message_wins_over_the_status(
    client: DaikinOneClient, daikin: FakeDaikin, status: int, key: str
) -> None:
    assert "DeviceOfflineException" in CONTRACT["messages"]
    daikin.queue("GET", DEVICE, Reply(status, {key: "DeviceOfflineException"}))
    with pytest.raises(DeviceOfflineError):
        await client.get_thermostat(MAIN_ID)


async def test_not_authorized_message_is_an_authentication_error(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    daikin.queue("PUT", f"{DEVICE}/schedule", Reply(400, {"messages": "NotAuthorizedException"}))
    with pytest.raises(AuthenticationError):
        await client.set_schedule(MAIN_ID, enabled=True)


@pytest.mark.parametrize("raw", [b"", b"<html>oops</html>", b"{truncated", b"\xff\xfe"])
async def test_non_json_success_body_is_malformed(
    client: DaikinOneClient, daikin: FakeDaikin, raw: bytes
) -> None:
    daikin.queue("GET", DEVICE, Reply(200, raw=raw))
    daikin.queue("GET", "/v1/devices", Reply(200, raw=raw))
    with pytest.raises(MalformedResponseError):
        await client.get_thermostat(MAIN_ID)
    with pytest.raises(MalformedResponseError):
        await client.list_devices()


async def test_non_json_error_body_still_maps_by_status(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    daikin.queue("GET", DEVICE, Reply(502, raw=b"<html>Bad Gateway</html>"))
    with pytest.raises(ServerError):
        await client.get_thermostat(MAIN_ID)


async def test_write_succeeds_on_200_whatever_the_body(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    daikin.queue("PUT", f"{DEVICE}/schedule", Reply(200, raw=b""))
    await client.set_schedule(MAIN_ID, enabled=True)


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError(),
        aiohttp.ClientConnectionError("Cannot connect"),
        aiohttp.ServerDisconnectedError(),
        aiohttp.ClientPayloadError("short read"),
        aiohttp.ClientOSError(54, "Connection reset by peer"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_transport_failures_raise_connection_failed(
    client: DaikinOneClient, daikin: FakeDaikin, failure: BaseException
) -> None:
    daikin.queue("GET", "/v1/devices", failure)
    with pytest.raises(ConnectionFailedError) as err:
        await client.list_devices()
    assert err.value.__cause__ is None
    assert err.value.__suppress_context__ is True
    # The client itself never retries.
    assert len(daikin.calls_to("GET", "/v1/devices")) == 1


async def test_failed_request_releases_its_slot(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    daikin.queue("GET", DEVICE, *[aiohttp.ClientConnectionError()] * (MAX_OPEN_REQUESTS * 2))
    for _ in range(MAX_OPEN_REQUESTS * 2):
        with pytest.raises(ConnectionFailedError):
            await client.get_thermostat(MAIN_ID)
    assert (await client.get_thermostat(MAIN_ID)).temp_indoor == 21.5


# -------------------------------------------------------------- rate limits


async def test_429_honors_retry_after_seconds(
    client: DaikinOneClient, daikin: FakeDaikin, clock: FakeClock
) -> None:
    await client.list_devices()
    daikin.queue(
        "GET", DEVICE, Reply(429, {"messages": "Too Many Requests"}, {"Retry-After": "42"})
    )
    with pytest.raises(RateLimitError) as err:
        await client.get_thermostat(MAIN_ID)
    assert err.value.retry_after == 42
    assert err.value.status == 429

    # Until the delay has passed, nothing is sent at all, for any endpoint.
    sent = len(daikin.calls)
    clock.advance(41)
    with pytest.raises(RateLimitError) as blocked:
        await client.list_devices()
    assert blocked.value.retry_after == pytest.approx(1)
    with pytest.raises(RateLimitError):
        await client.set_schedule(MAIN_ID, enabled=True)
    assert len(daikin.calls) == sent

    clock.advance(1)
    assert (await client.get_thermostat(MAIN_ID)).temp_indoor == 21.5


async def test_429_honors_retry_after_http_date(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=300), usegmt=True)
    daikin.queue("GET", "/v1/devices", Reply(429, None, {"Retry-After": when}))
    with pytest.raises(RateLimitError) as err:
        await client.list_devices()
    assert 290 <= err.value.retry_after <= 300


@pytest.mark.parametrize(
    ("header", "expected"),
    [("0", 1.0), ("-30", 1.0), ("999999", 3600.0), ("0.5", 1.0), (" 30 ", 30.0)],
)
async def test_retry_after_is_clamped(
    client: DaikinOneClient, daikin: FakeDaikin, header: str, expected: float
) -> None:
    daikin.queue("GET", "/v1/devices", Reply(429, None, {"Retry-After": header}))
    with pytest.raises(RateLimitError) as err:
        await client.list_devices()
    assert err.value.retry_after == expected


@pytest.mark.parametrize("header", [None, "", "soon", "nan", "inf"])
async def test_429_without_usable_retry_after_backs_off_exponentially(
    client: DaikinOneClient, daikin: FakeDaikin, clock: FakeClock, header: str | None
) -> None:
    headers = {} if header is None else {"Retry-After": header}
    daikin.expires_in = None  # keep token renewals out of the request sequence
    await client.list_devices()
    delays = []
    for _ in range(6):
        daikin.queue("GET", DEVICE, Reply(429, None, headers))
        with pytest.raises(RateLimitError) as err:
            await client.get_thermostat(MAIN_ID)
        delays.append(err.value.retry_after)
        clock.advance(err.value.retry_after)
    assert delays == [180, 360, 720, 1440, 1800, 1800]

    # A success resets the backoff.
    await client.get_thermostat(MAIN_ID)
    daikin.queue("GET", DEVICE, Reply(429))
    with pytest.raises(RateLimitError) as err:
        await client.get_thermostat(MAIN_ID)
    assert err.value.retry_after == 180


async def test_429_on_the_token_endpoint_is_a_rate_limit(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    daikin.queue("POST", "/v1/token", Reply(429, None, {"Retry-After": "10"}))
    with pytest.raises(RateLimitError) as err:
        await client.list_devices()
    assert err.value.retry_after == 10
    assert len(daikin.calls) == 1


# -------------------------------------------------------------- concurrency


async def test_never_more_than_three_open_requests(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    await client.authenticate()
    daikin.hold = asyncio.Event()
    tasks = [asyncio.create_task(client.get_thermostat(MAIN_ID)) for _ in range(12)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert daikin.in_flight == MAX_OPEN_REQUESTS
    daikin.hold.set()
    await asyncio.gather(*tasks)
    assert daikin.max_in_flight == MAX_OPEN_REQUESTS
    assert daikin.in_flight == 0


async def test_token_requests_count_toward_the_open_request_cap(
    client: DaikinOneClient, daikin: FakeDaikin
) -> None:
    daikin.hold = asyncio.Event()
    tasks = [asyncio.create_task(client.get_thermostat(MAIN_ID)) for _ in range(8)]
    for _ in range(20):
        await asyncio.sleep(0)
    assert daikin.in_flight == 1  # only the single token request is open
    daikin.hold.set()
    await asyncio.gather(*tasks)
    assert daikin.max_in_flight <= MAX_OPEN_REQUESTS
    assert daikin.tokens_issued == 1


# ------------------------------------------------------------------ secrets


async def _fail(client: DaikinOneClient, daikin: FakeDaikin, outcome: Any) -> list[DaikinOneError]:
    """Make every kind of request fail with ``outcome`` and collect the errors."""
    routes = (("GET", "/v1/devices"), ("GET", DEVICE), ("PUT", f"{DEVICE}/msp"))
    for method, path in routes:
        daikin.always(method, path, outcome)
    errors: list[DaikinOneError] = []
    for action in (
        client.list_devices(),
        client.get_thermostat(MAIN_ID),
        client.set_mode_setpoints(MAIN_ID, mode=Mode.HEAT, heat_setpoint=20, cool_setpoint=24),
    ):
        try:
            await action
        except DaikinOneError as err:
            errors.append(err)
    return errors


LEAKY_BODY = {
    "messages": (
        f"bad token {ACCESS_TOKEN_PREFIX}1 for {EMAIL} key {API_KEY} {INTEGRATOR_TOKEN} SECRET-BODY"
    ),
}


@pytest.mark.parametrize(
    "outcome",
    [
        Reply(400, LEAKY_BODY),
        Reply(401, LEAKY_BODY),
        Reply(403, LEAKY_BODY),
        Reply(429, LEAKY_BODY),
        Reply(500, LEAKY_BODY),
        Reply(200, raw=f"SECRET-BODY {API_KEY}".encode()),
        TimeoutError(f"SECRET-BODY {API_KEY}"),
        aiohttp.ClientConnectionError(f"SECRET-BODY {API_KEY} {EMAIL}"),
    ],
    ids=["400", "401", "403", "429", "500", "garbage-200", "timeout", "client-error"],
)
async def test_errors_and_logs_never_contain_secrets_or_bodies(
    client: DaikinOneClient,
    daikin: FakeDaikin,
    caplog: pytest.LogCaptureFixture,
    outcome: Any,
) -> None:
    caplog.set_level(logging.DEBUG)
    await client.authenticate()
    errors = await _fail(client, daikin, outcome)
    # A write treats any HTTP 200 as success, so garbage in a 200 fails only the reads.
    assert len(errors) >= 2

    texts = [caplog.text]
    for err in errors:
        texts += [str(err), repr(err), repr(err.args), repr(vars(err))]
        assert err.__cause__ is None
        assert err.__context__ is None or err.__suppress_context__
    for text in texts:
        assert "SECRET-BODY" not in text
        assert MAIN_ID not in text
        for secret in SECRETS:
            assert secret not in text


async def test_client_repr_does_not_expose_credentials(client: DaikinOneClient) -> None:
    await client.authenticate()
    text = repr(client) + str(client)
    for secret in SECRETS:
        assert secret not in text
