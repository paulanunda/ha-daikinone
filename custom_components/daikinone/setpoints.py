"""Work out the full mode/setpoint payload for a write, and validate it.

Daikin's ``/msp`` endpoint needs the mode and both setpoints on every call, the
setpoints must lie within the thermostat's reported limits, and the cool
setpoint must exceed the heat setpoint by at least the reported delta. This
module turns a partial request ("set heat to 21") into a complete, valid pair,
or explains why it cannot. It is pure logic with no Home Assistant imports and
works entirely in Celsius, the API's unit.
"""

from __future__ import annotations

from dataclasses import dataclass

from .api import Mode, ThermostatState

# Setpoints are exchanged in 0.1 degree steps; this absorbs float rounding noise.
_EPSILON = 1e-6


class SetpointError(Exception):
    """A requested write cannot be turned into a request Daikin would accept.

    ``key`` names the translated error message; the numeric attributes are the
    values (Celsius) that the message needs.
    """

    def __init__(
        self,
        key: str,
        *,
        minimum: float | None = None,
        maximum: float | None = None,
        delta: float | None = None,
    ) -> None:
        super().__init__(key)
        self.key = key
        self.minimum = minimum
        self.maximum = maximum
        self.delta = delta


@dataclass(frozen=True, slots=True)
class Setpoints:
    """A complete, validated pair of setpoints in Celsius."""

    heat: float
    cool: float


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(max(value, minimum), maximum)


def resolve_setpoints(
    state: ThermostatState,
    mode: Mode,
    *,
    heat: float | None = None,
    cool: float | None = None,
) -> Setpoints:
    """Return the setpoints to send for ``mode``.

    ``heat`` and ``cool`` are the values the caller explicitly asked for; either
    may be None, in which case the thermostat's last known value is used.

    Rules:
    * A requested value outside the thermostat's limits is an error.
    * If the pair is closer together than the thermostat's delta:
      - when both were requested, it is an error;
      - otherwise the value that was *not* requested moves just far enough
        away (in cool mode with nothing requested the heat setpoint moves; in
        every other mode the cool setpoint moves), which is what the thermostat
        itself does. If the limits leave no room for that, it is an error.
    """
    minimum, maximum, delta = state.setpoint_minimum, state.setpoint_maximum, state.setpoint_delta
    if minimum is None or maximum is None or delta is None or minimum > maximum:
        raise SetpointError("limits_unknown")
    limits = {"minimum": minimum, "maximum": maximum, "delta": delta}

    for requested in (heat, cool):
        if requested is not None and not (
            minimum - _EPSILON <= round(requested, 1) <= maximum + _EPSILON
        ):
            raise SetpointError("setpoint_out_of_range", **limits)

    # Fill whatever was not requested from the last known state. Those values
    # came from the thermostat, so pull them inside the limits instead of failing.
    new_heat = round(heat, 1) if heat is not None else state.heat_setpoint
    new_cool = round(cool, 1) if cool is not None else state.cool_setpoint
    if new_heat is None or new_cool is None:
        raise SetpointError("state_unknown")
    if heat is None:
        new_heat = round(_clamp(new_heat, minimum, maximum), 1)
    if cool is None:
        new_cool = round(_clamp(new_cool, minimum, maximum), 1)

    if new_cool - new_heat >= delta - _EPSILON:
        return Setpoints(heat=new_heat, cool=new_cool)

    if heat is not None and cool is not None:
        raise SetpointError("setpoints_too_close", **limits)

    keep_cool = cool is not None or (heat is None and mode is Mode.COOL)
    if keep_cool:
        new_heat = round(new_cool - delta, 1)
        if new_heat < minimum - _EPSILON:
            raise SetpointError("setpoint_no_room", **limits)
    else:
        new_cool = round(new_heat + delta, 1)
        if new_cool > maximum + _EPSILON:
            raise SetpointError("setpoint_no_room", **limits)
    return Setpoints(heat=new_heat, cool=new_cool)
