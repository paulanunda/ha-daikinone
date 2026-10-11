"""Typed models for the documented Daikin One Open API responses.

Parsing is deliberately forgiving at the field level: a value that is missing,
has the wrong type, is not a documented enum member, or is physically
implausible becomes ``None`` for that one field. Only a response that is not a
JSON object at all is rejected as a whole.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Final

from .errors import MalformedResponseError


class Mode(IntEnum):
    """Thermostat operating mode (``mode``)."""

    OFF = 0
    HEAT = 1
    COOL = 2
    AUTO = 3
    EMERGENCY_HEAT = 4


class ModeLimit(IntEnum):
    """Which modes the thermostat allows (``modeLimit``)."""

    NONE = 0
    ALL = 1
    HEAT_ONLY = 2
    COOL_ONLY = 3


class EquipmentStatus(IntEnum):
    """What the HVAC equipment is doing right now (``equipmentStatus``)."""

    COOL = 1
    OVERCOOL_FOR_DEHUM = 2
    HEAT = 3
    FAN = 4
    IDLE = 5


class FanState(IntEnum):
    """System fan (``fan``)."""

    AUTO = 0
    ON = 1


class FanCirculate(IntEnum):
    """Fan circulation mode (``fanCirculate``)."""

    OFF = 0
    ALWAYS_ON = 1
    SCHEDULE = 2


class FanCirculateSpeed(IntEnum):
    """Fan circulation speed (``fanCirculateSpeed``)."""

    LOW = 0
    MEDIUM = 1
    HIGH = 2


# Plausibility bounds, in the API's units (degrees Celsius and percent). A value
# outside them is treated as unknown instead of being shown as a real reading.
TEMPERATURE_RANGE: Final = (-60.0, 70.0)
SETPOINT_RANGE: Final = (0.0, 50.0)
SETPOINT_DELTA_RANGE: Final = (0.0, 20.0)
HUMIDITY_RANGE: Final = (0.0, 100.0)

# Every field documented for GET /v1/devices/<id>.
THERMOSTAT_FIELDS: Final = (
    "equipmentStatus",
    "mode",
    "modeLimit",
    "modeEmHeatAvailable",
    "fan",
    "fanCirculate",
    "fanCirculateSpeed",
    "heatSetpoint",
    "coolSetpoint",
    "setpointDelta",
    "setpointMinimum",
    "setpointMaximum",
    "tempIndoor",
    "humIndoor",
    "tempOutdoor",
    "humOutdoor",
    "scheduleEnabled",
    "geofencingEnabled",
)


def _as_int(value: object) -> int | None:
    """Return ``value`` as an int, or None if it is not a whole number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _as_enum[E: IntEnum](enum: type[E], value: object) -> E | None:
    """Return the enum member for ``value``, or None if it is not a documented one."""
    number = _as_int(value)
    if number is None:
        return None
    try:
        return enum(number)
    except ValueError:
        return None


def _as_number(value: object, bounds: tuple[float, float]) -> float | None:
    """Return ``value`` as a float, or None if it is not a finite number within bounds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number):
        return None
    low, high = bounds
    if number < low or number > high:
        return None
    return number


def _as_bool(value: object) -> bool | None:
    """Return ``value`` if it is a JSON boolean, otherwise None."""
    return value if isinstance(value, bool) else None


def _as_flag(value: object) -> bool | None:
    """Return an on/off flag as a bool, otherwise None.

    Daikin documents ``modeEmHeatAvailable`` as the integer 0 or 1, but the live
    service sends a JSON boolean. Both forms are accepted.
    """
    if isinstance(value, bool):
        return value
    number = _as_int(value)
    if number == 0:
        return False
    if number == 1:
        return True
    return None


def _as_text(value: object) -> str | None:
    """Return ``value`` if it is a non-empty string, otherwise None."""
    return value if isinstance(value, str) and value else None


@dataclass(frozen=True, slots=True, kw_only=True)
class ThermostatState:
    """Parsed ``GET /v1/devices/<id>`` response. Temperatures are in Celsius.

    Every field is None when Daikin did not send a usable value for it.
    """

    equipment_status: EquipmentStatus | None = None
    mode: Mode | None = None
    mode_limit: ModeLimit | None = None
    emergency_heat_available: bool | None = None
    fan: FanState | None = None
    fan_circulate: FanCirculate | None = None
    fan_circulate_speed: FanCirculateSpeed | None = None
    heat_setpoint: float | None = None
    cool_setpoint: float | None = None
    setpoint_delta: float | None = None
    setpoint_minimum: float | None = None
    setpoint_maximum: float | None = None
    temp_indoor: float | None = None
    hum_indoor: float | None = None
    temp_outdoor: float | None = None
    hum_outdoor: float | None = None
    schedule_enabled: bool | None = None
    geofencing_enabled: bool | None = None
    # The response exactly as received, kept for the diagnostics download only.
    raw: Mapping[str, object] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_api(cls, data: object) -> ThermostatState:
        """Parse a thermostat response, degrading unusable fields to None."""
        if not isinstance(data, dict):
            raise MalformedResponseError("Thermostat response is not a JSON object")
        return cls(
            equipment_status=_as_enum(EquipmentStatus, data.get("equipmentStatus")),
            mode=_as_enum(Mode, data.get("mode")),
            mode_limit=_as_enum(ModeLimit, data.get("modeLimit")),
            emergency_heat_available=_as_flag(data.get("modeEmHeatAvailable")),
            fan=_as_enum(FanState, data.get("fan")),
            fan_circulate=_as_enum(FanCirculate, data.get("fanCirculate")),
            fan_circulate_speed=_as_enum(FanCirculateSpeed, data.get("fanCirculateSpeed")),
            heat_setpoint=_as_number(data.get("heatSetpoint"), SETPOINT_RANGE),
            cool_setpoint=_as_number(data.get("coolSetpoint"), SETPOINT_RANGE),
            setpoint_delta=_as_number(data.get("setpointDelta"), SETPOINT_DELTA_RANGE),
            setpoint_minimum=_as_number(data.get("setpointMinimum"), SETPOINT_RANGE),
            setpoint_maximum=_as_number(data.get("setpointMaximum"), SETPOINT_RANGE),
            temp_indoor=_as_number(data.get("tempIndoor"), TEMPERATURE_RANGE),
            hum_indoor=_as_number(data.get("humIndoor"), HUMIDITY_RANGE),
            temp_outdoor=_as_number(data.get("tempOutdoor"), TEMPERATURE_RANGE),
            hum_outdoor=_as_number(data.get("humOutdoor"), HUMIDITY_RANGE),
            schedule_enabled=_as_bool(data.get("scheduleEnabled")),
            geofencing_enabled=_as_bool(data.get("geofencingEnabled")),
            raw=dict(data),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class DeviceInfo:
    """One thermostat from ``GET /v1/devices``."""

    id: str
    name: str | None = None
    model: str | None = None
    firmware_version: str | None = None
    location_name: str | None = None


def parse_device_list(data: object) -> list[DeviceInfo]:
    """Parse ``GET /v1/devices`` into a flat list of thermostats.

    A location or thermostat entry that does not have the documented shape is
    skipped on its own; the rest of the list is still returned. Thermostats
    without a usable id cannot be addressed and are dropped.
    """
    if not isinstance(data, list):
        raise MalformedResponseError("Device list response is not a JSON array")
    devices: dict[str, DeviceInfo] = {}
    for location in data:
        if not isinstance(location, dict):
            continue
        entries = location.get("devices")
        if not isinstance(entries, list):
            continue
        location_name = _as_text(location.get("locationName"))
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            device_id = _as_text(entry.get("id"))
            if device_id is None or device_id in devices:
                continue
            devices[device_id] = DeviceInfo(
                id=device_id,
                name=_as_text(entry.get("name")),
                model=_as_text(entry.get("model")),
                firmware_version=_as_text(entry.get("firmwareVersion")),
                location_name=location_name,
            )
    return list(devices.values())
