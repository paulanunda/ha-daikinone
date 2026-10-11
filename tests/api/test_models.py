"""Parsing of documented response shapes, and graceful degradation of bad ones."""

from __future__ import annotations

import dataclasses
import json
from enum import IntEnum
from pathlib import Path
from typing import Any

import pytest

from custom_components.daikinone.api import (
    DeviceInfo,
    EquipmentStatus,
    FanCirculate,
    FanCirculateSpeed,
    FanState,
    MalformedResponseError,
    Mode,
    ModeLimit,
    ThermostatState,
)
from custom_components.daikinone.api.models import THERMOSTAT_FIELDS, parse_device_list
from tests.fake_daikin import CONTRACT, example_state

FIXTURES = Path(__file__).parent.parent / "fixtures"

FIELD_TO_ATTR = {
    "equipmentStatus": "equipment_status",
    "mode": "mode",
    "modeLimit": "mode_limit",
    "modeEmHeatAvailable": "emergency_heat_available",
    "fan": "fan",
    "fanCirculate": "fan_circulate",
    "fanCirculateSpeed": "fan_circulate_speed",
    "heatSetpoint": "heat_setpoint",
    "coolSetpoint": "cool_setpoint",
    "setpointDelta": "setpoint_delta",
    "setpointMinimum": "setpoint_minimum",
    "setpointMaximum": "setpoint_maximum",
    "tempIndoor": "temp_indoor",
    "humIndoor": "hum_indoor",
    "tempOutdoor": "temp_outdoor",
    "humOutdoor": "hum_outdoor",
    "scheduleEnabled": "schedule_enabled",
    "geofencingEnabled": "geofencing_enabled",
}

ENUMS: dict[str, type[IntEnum]] = {
    "equipmentStatus": EquipmentStatus,
    "mode": Mode,
    "modeLimit": ModeLimit,
    "fan": FanState,
    "fanCirculate": FanCirculate,
    "fanCirculateSpeed": FanCirculateSpeed,
}

BOOLEAN_FIELDS = ("scheduleEnabled", "geofencingEnabled")
NUMBER_FIELDS = tuple(
    name
    for name in THERMOSTAT_FIELDS
    if name not in ENUMS and name not in BOOLEAN_FIELDS and name != "modeEmHeatAvailable"
)

BAD_FOR_ALL: list[Any] = [None, "banana", "", [], {}, [1], {"a": 1}]
BAD_VALUES: dict[str, list[Any]] = {
    **{name: [*BAD_FOR_ALL, True, -1, 99, 1.5, 1e300] for name in ENUMS},
    **{
        name: [*BAD_FOR_ALL, True, -1000, 1000, 1e300, float("nan"), float("inf")]
        for name in NUMBER_FIELDS
    },
    **{name: [*BAD_FOR_ALL, 0, 1, "true", 0.0] for name in BOOLEAN_FIELDS},
    "modeEmHeatAvailable": [*BAD_FOR_ALL, 2, -1, 0.5],
}
CORRUPTIONS = [(name, value) for name, values in BAD_VALUES.items() for value in values]


def _without_raw(state: ThermostatState) -> dict[str, Any]:
    values = dataclasses.asdict(state)
    values.pop("raw")
    return values


def test_models_cover_every_documented_field() -> None:
    documented = set(CONTRACT["endpoints"]["get_device"]["response_example"])
    assert set(THERMOSTAT_FIELDS) == documented
    assert set(FIELD_TO_ATTR) == documented
    assert set(BAD_VALUES) == documented


@pytest.mark.parametrize("name", sorted(ENUMS))
def test_enums_match_the_contract(name: str) -> None:
    documented = {int(value) for value in CONTRACT["fields"][name]["enum"]}
    assert {member.value for member in ENUMS[name]} == documented


def test_parses_the_documented_example() -> None:
    state = ThermostatState.from_api(example_state())
    assert state.equipment_status is EquipmentStatus.IDLE
    assert state.mode is Mode.OFF
    assert state.mode_limit is ModeLimit.ALL
    assert state.emergency_heat_available is False
    assert state.fan is FanState.AUTO
    assert state.fan_circulate is FanCirculate.ALWAYS_ON
    assert state.fan_circulate_speed is FanCirculateSpeed.MEDIUM
    assert state.heat_setpoint == 20.5
    assert state.cool_setpoint == 23.5
    assert state.setpoint_delta == 2.0
    assert state.setpoint_minimum == 10.0
    assert state.setpoint_maximum == 32.3
    assert state.temp_indoor == 21.5
    assert state.hum_indoor == 50.0
    assert state.temp_outdoor == 20.5
    assert state.hum_outdoor == 50.0
    assert state.schedule_enabled is False
    assert state.geofencing_enabled is False


def test_documented_example_equipment_status_zero_is_unknown() -> None:
    """The published example uses a value the published enum does not define."""
    raw = CONTRACT["endpoints"]["get_device"]["response_example"]
    assert raw["equipmentStatus"] == 0
    assert ThermostatState.from_api(raw).equipment_status is None


@pytest.mark.parametrize("value", [6, 7, 8, 9])
def test_undocumented_equipment_status_is_unknown(value: int) -> None:
    assert ThermostatState.from_api(example_state(equipmentStatus=value)).equipment_status is None


def test_whole_numbers_sent_as_floats_are_accepted_for_enums() -> None:
    assert ThermostatState.from_api(example_state(mode=2.0)).mode is Mode.COOL


@pytest.mark.parametrize(("name", "value"), CORRUPTIONS, ids=repr)
def test_bad_value_degrades_only_its_own_field(name: str, value: Any) -> None:
    baseline = _without_raw(ThermostatState.from_api(example_state()))
    parsed = _without_raw(ThermostatState.from_api(example_state(**{name: value})))
    attr = FIELD_TO_ATTR[name]
    assert parsed.pop(attr) is None
    baseline.pop(attr)
    assert parsed == baseline


@pytest.mark.parametrize("name", THERMOSTAT_FIELDS)
def test_missing_field_degrades_only_its_own_field(name: str) -> None:
    payload = example_state()
    del payload[name]
    baseline = _without_raw(ThermostatState.from_api(example_state()))
    parsed = _without_raw(ThermostatState.from_api(payload))
    attr = FIELD_TO_ATTR[name]
    assert parsed.pop(attr) is None
    baseline.pop(attr)
    assert parsed == baseline


def test_empty_object_parses_to_all_unknown() -> None:
    assert all(value is None for value in _without_raw(ThermostatState.from_api({})).values())


def test_unknown_extra_fields_are_ignored() -> None:
    state = ThermostatState.from_api(example_state(brandNewField={"x": 1}))
    assert state.mode is Mode.OFF
    assert state.raw["brandNewField"] == {"x": 1}


@pytest.mark.parametrize("payload", [None, [], "text", 5, True, [example_state()]])
def test_non_object_thermostat_response_is_rejected(payload: Any) -> None:
    with pytest.raises(MalformedResponseError):
        ThermostatState.from_api(payload)


def test_parses_the_documented_device_list() -> None:
    payload = [
        {
            "locationName": "Home",
            "devices": [
                {"id": "a", "name": "Main", "model": "ONETOUCH", "firmwareVersion": "1.2"},
                {"id": "b", "name": "Up", "model": "ONETOUCH", "firmwareVersion": "1.3"},
            ],
        },
        {"locationName": "Cabin", "devices": [{"id": "c", "name": "Cabin"}]},
    ]
    assert parse_device_list(payload) == [
        DeviceInfo(
            id="a", name="Main", model="ONETOUCH", firmware_version="1.2", location_name="Home"
        ),
        DeviceInfo(
            id="b", name="Up", model="ONETOUCH", firmware_version="1.3", location_name="Home"
        ),
        DeviceInfo(id="c", name="Cabin", location_name="Cabin"),
    ]


def test_device_list_field_names_match_the_contract() -> None:
    location = CONTRACT["endpoints"]["list_devices"]["response_body"][0]
    assert set(location) == {"locationName", "devices"}
    assert set(location["devices"][0]) == {"id", "name", "model", "firmwareVersion"}


def test_malformed_device_list_entries_are_skipped_individually() -> None:
    payload: list[Any] = [
        "not a location",
        {"locationName": "No devices key"},
        {"locationName": "Bad devices", "devices": "nope"},
        {
            "locationName": 7,
            "devices": [
                None,
                {"name": "no id"},
                {"id": 12345},
                {"id": ""},
                {"id": "good", "name": 5, "model": None, "firmwareVersion": []},
                {"id": "good", "name": "duplicate id"},
            ],
        },
    ]
    assert parse_device_list(payload) == [DeviceInfo(id="good")]


@pytest.mark.parametrize("payload", [None, {}, "text", 5, {"devices": []}])
def test_non_array_device_list_is_rejected(payload: Any) -> None:
    with pytest.raises(MalformedResponseError):
        parse_device_list(payload)


@pytest.mark.parametrize(
    ("value", "expected"), [(0, False), (1, True), (False, False), (True, True), (1.0, True)]
)
def test_emergency_heat_flag_accepts_documented_integers_and_live_booleans(
    value: Any, expected: bool
) -> None:
    state = ThermostatState.from_api(example_state(modeEmHeatAvailable=value))
    assert state.emergency_heat_available is expected


def test_parses_the_response_shape_observed_live() -> None:
    """A real response differs from the documentation in two ways; neither loses data."""
    live = json.loads((FIXTURES / "live_thermostat.json").read_text(encoding="utf-8"))
    live.pop("_about")
    documented = set(CONTRACT["endpoints"]["get_device"]["response_example"])
    # One field more than documented, and none fewer.
    assert set(live) - documented == {"equipmentCommunication"}
    assert documented <= set(live)
    # Documented as an integer, sent as a boolean.
    assert live["modeEmHeatAvailable"] is False

    state = ThermostatState.from_api(live)
    parsed = _without_raw(state)
    assert None not in parsed.values(), "every documented field parsed"
    assert state.emergency_heat_available is False
    assert state.mode is Mode.HEAT
    assert state.equipment_status is EquipmentStatus.IDLE
    assert (state.heat_setpoint, state.cool_setpoint) == (23.3, 25.6)
    # Whole numbers arrive as JSON integers and are still read as floats.
    assert (state.temp_indoor, state.setpoint_minimum, state.setpoint_delta) == (24.0, 10.0, 2.0)
    assert state.geofencing_enabled is True
