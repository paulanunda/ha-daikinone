"""Completing and validating a mode/setpoint write."""

from __future__ import annotations

import pytest

from custom_components.daikinone.api import Mode, ThermostatState
from custom_components.daikinone.setpoints import SetpointError, Setpoints, resolve_setpoints
from tests.fake_daikin import example_state

# The documented example: heat 20.5, cool 23.5, delta 2, limits 10.0 to 32.3.
STATE = ThermostatState.from_api(example_state())


def state(**overrides: object) -> ThermostatState:
    return ThermostatState.from_api(example_state(**overrides))


def test_nothing_requested_keeps_the_known_setpoints() -> None:
    assert resolve_setpoints(STATE, Mode.HEAT) == Setpoints(heat=20.5, cool=23.5)


def test_requested_values_replace_known_ones() -> None:
    assert resolve_setpoints(STATE, Mode.HEAT, heat=18) == Setpoints(heat=18.0, cool=23.5)
    assert resolve_setpoints(STATE, Mode.COOL, cool=26) == Setpoints(heat=20.5, cool=26.0)
    assert resolve_setpoints(STATE, Mode.AUTO, heat=19, cool=25) == Setpoints(heat=19.0, cool=25.0)


def test_requested_values_are_rounded_to_a_tenth() -> None:
    # 72 and 76 Fahrenheit.
    result = resolve_setpoints(STATE, Mode.AUTO, heat=22.2222, cool=24.4444)
    assert result == Setpoints(heat=22.2, cool=24.4)


def test_limits_are_inclusive() -> None:
    assert resolve_setpoints(STATE, Mode.AUTO, heat=10.0, cool=32.3) == Setpoints(10.0, 32.3)
    # Values that round onto a limit are accepted.
    assert resolve_setpoints(STATE, Mode.AUTO, heat=9.96, cool=32.34) == Setpoints(10.0, 32.3)


@pytest.mark.parametrize(
    "request_",
    [{"heat": 9.9}, {"heat": 32.4}, {"cool": 9.9}, {"cool": 32.4}, {"heat": 5, "cool": 40}],
)
def test_requested_value_outside_the_limits_is_refused(request_: dict[str, float]) -> None:
    with pytest.raises(SetpointError) as err:
        resolve_setpoints(STATE, Mode.AUTO, **request_)
    assert err.value.key == "setpoint_out_of_range"
    assert (err.value.minimum, err.value.maximum, err.value.delta) == (10.0, 32.3, 2.0)


def test_exactly_the_delta_apart_is_accepted() -> None:
    assert resolve_setpoints(STATE, Mode.AUTO, heat=21.1, cool=23.1) == Setpoints(21.1, 23.1)


def test_both_requested_too_close_is_refused() -> None:
    with pytest.raises(SetpointError) as err:
        resolve_setpoints(STATE, Mode.AUTO, heat=22, cool=23.9)
    assert err.value.key == "setpoints_too_close"
    assert err.value.delta == 2.0


def test_raising_heat_pushes_the_unrequested_cool_setpoint_up() -> None:
    assert resolve_setpoints(STATE, Mode.HEAT, heat=22.5) == Setpoints(heat=22.5, cool=24.5)


def test_lowering_cool_pushes_the_unrequested_heat_setpoint_down() -> None:
    assert resolve_setpoints(STATE, Mode.COOL, cool=21.0) == Setpoints(heat=19.0, cool=21.0)


def test_the_requested_value_wins_whatever_the_mode() -> None:
    assert resolve_setpoints(STATE, Mode.COOL, heat=22.5) == Setpoints(heat=22.5, cool=24.5)
    assert resolve_setpoints(STATE, Mode.HEAT, cool=21.0) == Setpoints(heat=19.0, cool=21.0)


def test_no_room_to_move_the_other_setpoint_is_refused() -> None:
    with pytest.raises(SetpointError) as err:
        resolve_setpoints(STATE, Mode.HEAT, heat=31.0)  # cool would need 33.0 > 32.3
    assert err.value.key == "setpoint_no_room"
    with pytest.raises(SetpointError) as err:
        resolve_setpoints(STATE, Mode.COOL, cool=11.0)  # heat would need 9.0 < 10.0
    assert err.value.key == "setpoint_no_room"


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (Mode.OFF, Setpoints(heat=22.0, cool=24.0)),
        (Mode.HEAT, Setpoints(heat=22.0, cool=24.0)),
        (Mode.AUTO, Setpoints(heat=22.0, cool=24.0)),
        (Mode.EMERGENCY_HEAT, Setpoints(heat=22.0, cool=24.0)),
        (Mode.COOL, Setpoints(heat=21.0, cool=23.0)),
    ],
)
def test_mode_only_write_repairs_a_known_pair_that_is_too_close(
    mode: Mode, expected: Setpoints
) -> None:
    """If the thermostat itself reports a pair closer than its delta, keep the active one."""
    assert resolve_setpoints(state(heatSetpoint=22.0, coolSetpoint=23.0), mode) == expected


def test_known_values_outside_the_limits_are_pulled_inside() -> None:
    odd = state(heatSetpoint=5.0, coolSetpoint=40.0)
    assert resolve_setpoints(odd, Mode.OFF) == Setpoints(heat=10.0, cool=32.3)


@pytest.mark.parametrize("missing", ["setpointMinimum", "setpointMaximum", "setpointDelta"])
def test_unknown_limits_refuse_the_write(missing: str) -> None:
    with pytest.raises(SetpointError) as err:
        resolve_setpoints(state(**{missing: None}), Mode.HEAT, heat=20, cool=24)
    assert err.value.key == "limits_unknown"


def test_inverted_limits_refuse_the_write() -> None:
    with pytest.raises(SetpointError) as err:
        resolve_setpoints(state(setpointMinimum=30.0, setpointMaximum=20.0), Mode.HEAT)
    assert err.value.key == "limits_unknown"


@pytest.mark.parametrize("missing", ["heatSetpoint", "coolSetpoint"])
def test_unknown_setpoint_refuses_a_write_that_needs_it(missing: str) -> None:
    with pytest.raises(SetpointError) as err:
        resolve_setpoints(state(**{missing: "garbage"}), Mode.HEAT)
    assert err.value.key == "state_unknown"


def test_unknown_setpoint_is_fine_when_both_are_requested() -> None:
    unknown = state(heatSetpoint=None, coolSetpoint=None)
    assert resolve_setpoints(unknown, Mode.AUTO, heat=20, cool=24) == Setpoints(20.0, 24.0)
