"""Polling, failure isolation, rate limits, and the write path."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikinone.api import (
    FanCirculate,
    FanCirculateSpeed,
    Mode,
    ThermostatState,
)
from custom_components.daikinone.const import CONF_SCAN_INTERVAL_MINUTES, DOMAIN
from custom_components.daikinone.coordinator import (
    DaikinOneCoordinator,
    scan_interval_from_options,
)
from tests.conftest import advance, setup_entry
from tests.fake_daikin import (
    ACCESS_TOKEN_PREFIX,
    API_KEY,
    EMAIL,
    INTEGRATOR_TOKEN,
    MAIN_ID,
    UPSTAIRS_ID,
    FakeDaikin,
    Reply,
    example_state,
)

MAIN = f"/v1/devices/{MAIN_ID}"
UPSTAIRS = f"/v1/devices/{UPSTAIRS_ID}"
POLL = 180


async def start(hass: HomeAssistant, entry: MockConfigEntry) -> DaikinOneCoordinator:
    await setup_entry(hass, entry)
    coordinator: DaikinOneCoordinator = entry.runtime_data
    # A coordinator only polls while something listens to it, as entities do.
    entry.async_on_unload(coordinator.async_add_listener(lambda: None))
    return coordinator


def reads(daikin: FakeDaikin, since: int = 0) -> list[str]:
    return sorted(c.path for c in daikin.calls[since:] if c.method == "GET")


def puts(daikin: FakeDaikin) -> list[tuple[str, Any]]:
    return [(c.path, c.json) for c in daikin.calls if c.method == "PUT"]


# ---------------------------------------------------------------- intervals


@pytest.mark.parametrize(
    ("options", "minutes"),
    [
        ({}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: 3}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: 10}, 10),
        ({CONF_SCAN_INTERVAL_MINUTES: 60}, 60),
        ({CONF_SCAN_INTERVAL_MINUTES: 2}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: 1}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: 0}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: -30}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: 9999}, 60),
        ({CONF_SCAN_INTERVAL_MINUTES: "1"}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: 0.5}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: None}, 3),
        ({CONF_SCAN_INTERVAL_MINUTES: True}, 3),
    ],
)
def test_polling_interval_never_goes_below_three_minutes(
    options: dict[str, Any], minutes: int
) -> None:
    assert scan_interval_from_options(options) == timedelta(minutes=minutes)


async def test_stored_option_below_the_floor_is_clamped(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(entry, options={CONF_SCAN_INTERVAL_MINUTES: 1})
    coordinator = await start(hass, entry)
    assert coordinator.update_interval == timedelta(minutes=3)


async def test_polls_every_three_minutes_and_lists_hourly(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, entry)
    sent = len(daikin.calls)

    await advance(hass, freezer, POLL - 2)
    assert len(daikin.calls) == sent

    await advance(hass, freezer, 3)
    assert reads(daikin, sent) == sorted([MAIN, UPSTAIRS])

    # Over the next hour: one poll per interval, and the list is fetched once more.
    sent = len(daikin.calls)
    for _ in range(20):
        await advance(hass, freezer, POLL)
    paths = reads(daikin, sent)
    assert paths.count("/v1/devices") == 1
    assert paths.count(MAIN) == 20
    assert paths.count(UPSTAIRS) == 20
    assert daikin.max_in_flight <= 3


# -------------------------------------------------------- failure isolation


@pytest.mark.parametrize(
    ("failure", "online", "problem"),
    [
        (aiohttp.ClientConnectionError(), None, "ConnectionFailedError"),
        (TimeoutError(), None, "ConnectionFailedError"),
        (Reply(500), None, "ServerError"),
        (Reply(400, {"messages": "Invalid request body"}), None, "BadRequestError"),
        (Reply(200, ["wrong", "shape"]), None, "MalformedResponseError"),
        (Reply(200, raw=b"not json"), None, "MalformedResponseError"),
        (Reply(400, {"messages": "DeviceOfflineException"}), False, "offline"),
        (Reply(500, {"messages": "DeviceOfflineException"}), False, "offline"),
    ],
    ids=["connection", "timeout", "500", "400", "wrong-shape", "not-json", "offline", "offline5xx"],
)
async def test_one_bad_thermostat_does_not_affect_the_other(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    failure: Reply | BaseException,
    online: bool | None,
    problem: str,
) -> None:
    coordinator = await start(hass, entry)
    daikin.states[UPSTAIRS_ID]["tempIndoor"] = 24.5
    daikin.always("GET", MAIN, failure)
    await advance(hass, freezer, POLL)

    assert coordinator.last_update_success is True
    main, upstairs = coordinator.data[MAIN_ID], coordinator.data[UPSTAIRS_ID]
    # No stale values survive: the failed thermostat has no state at all.
    assert main.state is None
    assert main.online is online
    assert main.problem == problem
    assert upstairs.state is not None
    assert upstairs.state.temp_indoor == 24.5
    assert upstairs.online is True

    daikin.always("GET", MAIN, None)
    await advance(hass, freezer, POLL)
    assert coordinator.data[MAIN_ID].state is not None
    assert coordinator.data[MAIN_ID].online is True
    assert coordinator.data[MAIN_ID].problem is None


async def test_poll_fails_as_a_whole_when_nothing_can_be_read(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    daikin.always("GET", MAIN, aiohttp.ClientConnectionError())
    daikin.always("GET", UPSTAIRS, Reply(200, raw=b"garbage"))
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is False

    daikin.always("GET", MAIN, None)
    daikin.always("GET", UPSTAIRS, None)
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is True


async def test_all_thermostats_offline_is_a_successful_poll(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    offline = Reply(400, {"messages": "DeviceOfflineException"})
    daikin.always("GET", MAIN, offline)
    daikin.always("GET", UPSTAIRS, offline)
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is True
    assert [d.online for d in coordinator.data.values()] == [False, False]
    assert [d.state for d in coordinator.data.values()] == [None, None]


async def test_thermostat_missing_from_the_list_is_kept_and_unavailable(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    removed = daikin.locations[0]["devices"].pop(1)
    assert removed["id"] == UPSTAIRS_ID

    for _ in range(20):  # past the hourly list refresh
        await advance(hass, freezer, POLL)
    assert set(coordinator.data) == {MAIN_ID, UPSTAIRS_ID}
    assert coordinator.data[UPSTAIRS_ID].state is None
    assert coordinator.data[UPSTAIRS_ID].problem == "not_listed"
    assert coordinator.data[UPSTAIRS_ID].info.name == "Upstairs"
    assert coordinator.data[MAIN_ID].state is not None
    assert coordinator.is_listed(MAIN_ID)
    assert not coordinator.is_listed(UPSTAIRS_ID)

    # While it is missing it is not read, and the list is checked on every poll.
    sent = len(daikin.calls)
    await advance(hass, freezer, POLL)
    assert reads(daikin, sent) == sorted(["/v1/devices", MAIN])

    daikin.locations[0]["devices"].append(removed)
    await advance(hass, freezer, POLL)
    assert coordinator.data[UPSTAIRS_ID].state is not None


async def test_new_thermostat_is_picked_up(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    daikin.locations.append({"locationName": "Cabin", "devices": [{"id": "new-id", "name": "Den"}]})
    daikin.states["new-id"] = example_state(tempIndoor=18.0)
    for _ in range(21):
        await advance(hass, freezer, POLL)
    state = coordinator.data["new-id"].state
    assert state is not None
    assert state.temp_indoor == 18.0


async def test_failed_list_refresh_does_not_stop_polling(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    daikin.always("GET", "/v1/devices", Reply(200, raw=b"garbage"))
    daikin.states[MAIN_ID]["tempIndoor"] = 19.0
    for _ in range(21):
        await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is True
    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert state.temp_indoor == 19.0
    assert coordinator.data[UPSTAIRS_ID].state is not None


async def test_unknown_values_do_not_fail_the_poll(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    daikin.states[MAIN_ID] = {"mode": 42, "equipmentStatus": "??", "tempIndoor": 900, "new": [1]}
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is True
    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert (state.mode, state.equipment_status, state.temp_indoor) == (None, None, None)
    assert coordinator.data[MAIN_ID].online is True


# -------------------------------------------------------------- rate limits


@pytest.mark.parametrize(
    ("headers", "wait"), [({"Retry-After": "600"}, 600), ({}, 180), ({"Retry-After": "x"}, 180)]
)
async def test_429_backs_off_and_honors_retry_after(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    headers: dict[str, str],
    wait: int,
) -> None:
    coordinator = await start(hass, entry)
    daikin.queue("GET", MAIN, Reply(429, {"messages": "Too Many Requests"}, headers))
    await advance(hass, freezer, POLL)
    assert coordinator.last_update_success is False

    sent = len(daikin.calls)
    await advance(hass, freezer, wait - 5)
    assert len(daikin.calls) == sent, "nothing may be sent while backing off"

    await advance(hass, freezer, 10)
    assert coordinator.last_update_success is True
    assert reads(daikin, sent) == sorted([MAIN, UPSTAIRS])

    # Normal cadence resumes afterwards.
    sent = len(daikin.calls)
    await advance(hass, freezer, POLL + 1)
    assert reads(daikin, sent) == sorted([MAIN, UPSTAIRS])


# ------------------------------------------------------------------- writes


async def test_mode_only_write_fills_setpoints_from_last_known_state(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    coordinator = await start(hass, entry)
    await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.COOL)
    assert puts(daikin) == [
        (f"{MAIN}/msp", {"mode": 2, "heatSetpoint": 20.5, "coolSetpoint": 23.5})
    ]


async def test_single_setpoint_write_fills_mode_and_other_setpoint(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.states[MAIN_ID]["mode"] = 1
    coordinator = await start(hass, entry)
    await coordinator.async_set_mode_setpoints(MAIN_ID, heat_setpoint=19.0)
    await coordinator.async_set_mode_setpoints(MAIN_ID, cool_setpoint=25.0)
    assert puts(daikin) == [
        (f"{MAIN}/msp", {"mode": 1, "heatSetpoint": 19.0, "coolSetpoint": 23.5}),
        # The second write builds on the first, not on the last poll.
        (f"{MAIN}/msp", {"mode": 1, "heatSetpoint": 19.0, "coolSetpoint": 25.0}),
    ]


async def test_write_is_reflected_at_once_including_schedule_off(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.states[MAIN_ID]["scheduleEnabled"] = True
    coordinator = await start(hass, entry)
    before = coordinator.data[MAIN_ID].state
    assert before is not None
    assert before.schedule_enabled is True

    updates: list[None] = []
    coordinator.async_add_listener(lambda: updates.append(None))
    sent = len(daikin.calls)
    await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.HEAT, heat_setpoint=21.0)

    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert (state.mode, state.heat_setpoint, state.cool_setpoint) == (Mode.HEAT, 21.0, 23.5)
    assert state.schedule_enabled is False
    assert state.temp_indoor == before.temp_indoor
    assert updates, "entities must be told straight away"
    # Only the PUT was sent; there was no immediate read-back.
    assert [(c.method, c.path) for c in daikin.calls[sent:]] == [("PUT", f"{MAIN}/msp")]
    # The other thermostat is untouched.
    assert coordinator.data[UPSTAIRS_ID].state == ThermostatState.from_api(
        daikin.states[UPSTAIRS_ID]
    )


@pytest.mark.parametrize(
    ("broken", "kwargs"),
    [
        ({"mode": 42}, {"heat_setpoint": 20.0}),
        ({"mode": None}, {"cool_setpoint": 24.0}),
        ({"heatSetpoint": "?"}, {"mode": Mode.COOL}),
        ({"coolSetpoint": None}, {"mode": Mode.HEAT}),
        ({"coolSetpoint": None}, {"heat_setpoint": 20.0}),
    ],
)
async def test_write_is_refused_when_needed_state_is_unknown(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    broken: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    daikin.states[MAIN_ID].update(broken)
    coordinator = await start(hass, entry)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_mode_setpoints(MAIN_ID, **kwargs)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "state_unknown"
    assert err.value.translation_placeholders == {"name": "Main Floor"}
    assert puts(daikin) == []


async def test_fully_specified_write_works_with_unknown_state(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.states[MAIN_ID].update({"mode": 42, "heatSetpoint": None, "coolSetpoint": None})
    coordinator = await start(hass, entry)
    await coordinator.async_set_mode_setpoints(
        MAIN_ID, mode=Mode.AUTO, heat_setpoint=20.0, cool_setpoint=24.0
    )
    assert puts(daikin) == [
        (f"{MAIN}/msp", {"mode": 3, "heatSetpoint": 20.0, "coolSetpoint": 24.0})
    ]


@pytest.mark.parametrize(
    "failure",
    [aiohttp.ClientConnectionError(), Reply(200, raw=b"garbage"), Reply(400, {"messages": "x"})],
)
async def test_write_is_refused_for_an_unavailable_thermostat(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    failure: Reply | BaseException,
) -> None:
    coordinator = await start(hass, entry)
    daikin.always("GET", MAIN, failure)
    await advance(hass, freezer, POLL)

    for write in (
        coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.COOL),
        coordinator.async_set_mode_setpoints(
            MAIN_ID, mode=Mode.COOL, heat_setpoint=20.0, cool_setpoint=24.0
        ),
        coordinator.async_set_schedule(MAIN_ID, enabled=True),
        coordinator.async_set_fan(MAIN_ID, circulate=FanCirculate.OFF),
    ):
        with pytest.raises(HomeAssistantError) as err:
            await write
        assert err.value.translation_key == "thermostat_unavailable"
    assert puts(daikin) == []

    # The healthy thermostat still accepts writes.
    await coordinator.async_set_schedule(UPSTAIRS_ID, enabled=True)
    assert puts(daikin) == [(f"{UPSTAIRS}/schedule", {"scheduleEnabled": True})]


async def test_write_is_refused_after_a_failed_poll(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    daikin.always("GET", MAIN, aiohttp.ClientConnectionError())
    daikin.always("GET", UPSTAIRS, aiohttp.ClientConnectionError())
    await advance(hass, freezer, POLL)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.COOL)
    assert err.value.translation_key == "thermostat_unavailable"
    assert puts(daikin) == []


async def test_write_to_an_unknown_thermostat_is_refused(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    coordinator = await start(hass, entry)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_schedule("no-such-id", enabled=True)
    assert err.value.translation_key == "thermostat_unavailable"
    assert "no-such-id" not in str(err.value.translation_placeholders)


@pytest.mark.parametrize(
    ("kwargs", "key", "placeholders"),
    [
        (
            {"heat_setpoint": 5.0},
            "setpoint_out_of_range",
            {"minimum": "50.0", "maximum": "90.1", "delta": "3.6", "unit": "°F"},
        ),
        (
            {"cool_setpoint": 35.0},
            "setpoint_out_of_range",
            {"minimum": "50.0", "maximum": "90.1", "delta": "3.6", "unit": "°F"},
        ),
        (
            {"heat_setpoint": 22.0, "cool_setpoint": 23.0},
            "setpoints_too_close",
            {"minimum": "50.0", "maximum": "90.1", "delta": "3.6", "unit": "°F"},
        ),
        (
            {"heat_setpoint": 31.5},
            "setpoint_no_room",
            {"minimum": "50.0", "maximum": "90.1", "delta": "3.6", "unit": "°F"},
        ),
    ],
)
async def test_invalid_setpoints_raise_a_translated_error_and_send_nothing(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    kwargs: dict[str, float],
    key: str,
    placeholders: dict[str, str],
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    coordinator = await start(hass, entry)
    before = coordinator.data[MAIN_ID].state
    with pytest.raises(ServiceValidationError) as err:
        await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.AUTO, **kwargs)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key
    assert err.value.translation_placeholders == {"name": "Main Floor", **placeholders}
    assert puts(daikin) == []
    assert coordinator.data[MAIN_ID].state == before


async def test_missing_limits_refuse_the_write(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.states[MAIN_ID]["setpointDelta"] = None
    coordinator = await start(hass, entry)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_mode_setpoints(MAIN_ID, heat_setpoint=20.0, cool_setpoint=25.0)
    assert err.value.translation_key == "limits_unknown"
    assert puts(daikin) == []


async def test_single_setpoint_moves_the_other_to_keep_the_delta(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.states[MAIN_ID]["mode"] = 1
    coordinator = await start(hass, entry)
    await coordinator.async_set_mode_setpoints(MAIN_ID, heat_setpoint=22.5)
    assert puts(daikin) == [
        (f"{MAIN}/msp", {"mode": 1, "heatSetpoint": 22.5, "coolSetpoint": 24.5})
    ]
    assert daikin.states[MAIN_ID]["coolSetpoint"] == 24.5


async def test_fan_write_fills_the_other_value(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    coordinator = await start(hass, entry)
    await coordinator.async_set_fan(MAIN_ID, speed=FanCirculateSpeed.HIGH)
    await coordinator.async_set_fan(MAIN_ID, circulate=FanCirculate.SCHEDULE)
    assert puts(daikin) == [
        (f"{MAIN}/fan", {"fanCirculate": 1, "fanCirculateSpeed": 2}),
        (f"{MAIN}/fan", {"fanCirculate": 2, "fanCirculateSpeed": 2}),
    ]
    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert state.fan_circulate is FanCirculate.SCHEDULE
    assert state.fan_circulate_speed is FanCirculateSpeed.HIGH


@pytest.mark.parametrize(
    ("broken", "kwargs"),
    [
        ({"fanCirculate": 9}, {"speed": FanCirculateSpeed.LOW}),
        ({"fanCirculateSpeed": None}, {"circulate": FanCirculate.OFF}),
    ],
)
async def test_fan_write_is_refused_when_the_other_value_is_unknown(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    broken: dict[str, Any],
    kwargs: dict[str, Any],
) -> None:
    daikin.states[MAIN_ID].update(broken)
    coordinator = await start(hass, entry)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_fan(MAIN_ID, **kwargs)
    assert err.value.translation_key == "state_unknown"
    assert puts(daikin) == []


async def test_schedule_write(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    coordinator = await start(hass, entry)
    await coordinator.async_set_schedule(MAIN_ID, enabled=True)
    assert puts(daikin) == [(f"{MAIN}/schedule", {"scheduleEnabled": True})]
    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert state.schedule_enabled is True


async def test_writes_to_one_thermostat_are_serialized(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    coordinator = await start(hass, entry)
    daikin.max_in_flight = 0
    daikin.hold = asyncio.Event()

    tasks = [
        hass.async_create_task(coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.HEAT)),
        hass.async_create_task(coordinator.async_set_mode_setpoints(MAIN_ID, heat_setpoint=18.0)),
        hass.async_create_task(coordinator.async_set_schedule(MAIN_ID, enabled=True)),
        hass.async_create_task(coordinator.async_set_fan(MAIN_ID, speed=FanCirculateSpeed.LOW)),
        hass.async_create_task(coordinator.async_set_schedule(UPSTAIRS_ID, enabled=True)),
    ]
    for _ in range(20):
        await asyncio.sleep(0)
    # One open request per thermostat, however many writes are queued.
    assert daikin.in_flight == 2
    assert sorted(path for path, _ in puts(daikin)) == [f"{MAIN}/msp", f"{UPSTAIRS}/schedule"]

    daikin.hold.set()
    await asyncio.gather(*tasks)
    assert daikin.max_in_flight == 2
    main_puts = [(p, body) for p, body in puts(daikin) if p.startswith(MAIN)]
    assert main_puts == [
        (f"{MAIN}/msp", {"mode": 1, "heatSetpoint": 20.5, "coolSetpoint": 23.5}),
        # Queued behind the first write, and built on its result (mode 1).
        (f"{MAIN}/msp", {"mode": 1, "heatSetpoint": 18.0, "coolSetpoint": 23.5}),
        (f"{MAIN}/schedule", {"scheduleEnabled": True}),
        (f"{MAIN}/fan", {"fanCirculate": 1, "fanCirculateSpeed": 0}),
    ]


async def test_verification_read_happens_once_15_seconds_after_the_write(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.HEAT, heat_setpoint=21.0)
    # The thermostat settles on a slightly different value than was sent.
    daikin.states[MAIN_ID]["heatSetpoint"] = 21.5
    sent = len(daikin.calls)

    await advance(hass, freezer, 14)
    assert len(daikin.calls) == sent
    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert state.heat_setpoint == 21.0

    await advance(hass, freezer, 2)
    assert reads(daikin, sent) == [MAIN], "only the written thermostat is read back"
    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert state.heat_setpoint == 21.5

    await advance(hass, freezer, 60)
    assert len(daikin.calls) == sent + 1


async def test_several_writes_share_one_verification_read(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.HEAT)
    await advance(hass, freezer, 10)
    await coordinator.async_set_fan(MAIN_ID, speed=FanCirculateSpeed.HIGH)
    await advance(hass, freezer, 10)
    await coordinator.async_set_schedule(MAIN_ID, enabled=True)
    sent = len(daikin.calls)

    await advance(hass, freezer, 14)  # 24 and 14 seconds after the earlier writes
    assert len(daikin.calls) == sent
    await advance(hass, freezer, 2)
    assert reads(daikin, sent) == [MAIN]
    await advance(hass, freezer, 60)
    assert len(daikin.calls) == sent + 1


async def test_poll_inside_the_settling_window_keeps_the_written_state(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    coordinator = await start(hass, entry)
    await advance(hass, freezer, POLL - 5)
    await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.COOL, cool_setpoint=25.0)
    # Daikin has not caught up yet and would still answer with the old values.
    daikin.states[MAIN_ID].update({"mode": 0, "coolSetpoint": 23.5, "scheduleEnabled": True})
    sent = len(daikin.calls)

    await advance(hass, freezer, 6)  # the regular poll, 6 seconds after the write
    assert reads(daikin, sent) == [UPSTAIRS], "the settling thermostat is skipped"
    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert (state.mode, state.cool_setpoint, state.schedule_enabled) == (Mode.COOL, 25.0, False)


async def test_read_already_in_flight_during_a_write_is_discarded(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    coordinator = await start(hass, entry)
    daikin.hold = asyncio.Event()
    daikin.hold_only = ("GET", MAIN)
    refresh = hass.async_create_task(coordinator.async_refresh())
    for _ in range(20):
        await asyncio.sleep(0)
    assert daikin.in_flight == 1  # the read of MAIN is waiting inside Daikin

    await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.COOL, cool_setpoint=25.0)
    # The held read will answer with pre-write values.
    daikin.states[MAIN_ID].update({"mode": 0, "coolSetpoint": 23.5})
    daikin.hold.set()
    await refresh

    state = coordinator.data[MAIN_ID].state
    assert state is not None
    assert (state.mode, state.cool_setpoint) == (Mode.COOL, 25.0)


@pytest.mark.parametrize(
    ("failure", "key", "extra"),
    [
        (Reply(429, None, {"Retry-After": "90"}), "rate_limited", {"seconds": "90"}),
        (Reply(400, {"messages": "Invalid request body"}), "write_rejected", {}),
        (Reply(400, {"messages": "DeviceOfflineException"}), "thermostat_offline", {}),
        (Reply(500), "cannot_connect", {"reason": "ServerError"}),
        (TimeoutError(), "cannot_connect", {"reason": "ConnectionFailedError"}),
        (aiohttp.ClientConnectionError(), "cannot_connect", {"reason": "ConnectionFailedError"}),
    ],
    ids=["429", "400", "offline", "500", "timeout", "connection"],
)
async def test_failed_write_raises_a_translated_error_and_changes_nothing(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    failure: Reply | BaseException,
    key: str,
    extra: dict[str, str],
) -> None:
    coordinator = await start(hass, entry)
    before = coordinator.data[MAIN_ID].state
    daikin.queue("PUT", f"{MAIN}/msp", failure)
    with pytest.raises(HomeAssistantError) as err:
        await coordinator.async_set_mode_setpoints(MAIN_ID, mode=Mode.COOL)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == key
    assert err.value.translation_placeholders == {"name": "Main Floor", **extra}
    assert err.value.__cause__ is None

    after = coordinator.data[MAIN_ID]
    if key == "thermostat_offline":
        assert after.state is None
        assert after.online is False
    else:
        assert after.state == before
    assert coordinator.data[UPSTAIRS_ID].state is not None

    # No write was retried and no verification read was scheduled.
    sent = len(daikin.calls)
    await advance(hass, freezer, 30)
    assert len(daikin.calls) == sent
    assert len(puts(daikin)) == 1


# ------------------------------------------------------------------ secrets


async def test_nothing_sensitive_is_logged_through_failures(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    leak = {"messages": f"SECRET-BODY {API_KEY} {INTEGRATOR_TOKEN} {EMAIL} {ACCESS_TOKEN_PREFIX}1"}
    coordinator = await start(hass, entry)

    for failure in (
        Reply(500, leak),
        Reply(400, leak),
        Reply(200, raw=f"SECRET-BODY {API_KEY}".encode()),
        aiohttp.ClientConnectionError(f"SECRET-BODY {API_KEY}"),
        Reply(429, leak, {"Retry-After": "1"}),
    ):
        daikin.queue("GET", MAIN, failure)
        daikin.queue("GET", UPSTAIRS, failure)
        await advance(hass, freezer, POLL)
        daikin.queue("PUT", f"{MAIN}/schedule", failure)
        try:
            await coordinator.async_set_schedule(MAIN_ID, enabled=True)
        except HomeAssistantError as err:
            caplog.records.append(
                logging.LogRecord("test", logging.ERROR, "", 0, f"{err!s} {err!r}", None, None)
            )
        await advance(hass, freezer, POLL)

    text = caplog.text + " ".join(record.getMessage() for record in caplog.records)
    assert "daikinone" in text  # the integration did log something
    for secret in (
        "SECRET-BODY",
        API_KEY,
        INTEGRATOR_TOKEN,
        ACCESS_TOKEN_PREFIX,
        EMAIL,
        EMAIL.lower(),
        EMAIL.split("@", maxsplit=1)[0],
        MAIN_ID,
        UPSTAIRS_ID,
    ):
        assert secret not in text
