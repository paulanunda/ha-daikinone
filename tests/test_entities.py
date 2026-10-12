"""Sensors, binary sensors, selects, and the schedule switch."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from tests.conftest import advance, setup_entry
from tests.fake_daikin import MAIN_ID, UPSTAIRS_ID, FakeDaikin, Reply, example_state

MAIN = f"/v1/devices/{MAIN_ID}"

# entity id suffix -> unique id suffix, for each thermostat
ENTITIES = {
    "climate.{}": "climate",
    "sensor.{}_indoor_temperature": "indoor_temperature",
    "sensor.{}_indoor_humidity": "indoor_humidity",
    "sensor.{}_outdoor_temperature": "outdoor_temperature",
    "sensor.{}_outdoor_humidity": "outdoor_humidity",
    "sensor.{}_heating_setpoint": "heating_setpoint",
    "sensor.{}_cooling_setpoint": "cooling_setpoint",
    "sensor.{}_equipment_status": "equipment_status",
    "sensor.{}_system_fan": "system_fan",
    "binary_sensor.{}_online": "online",
    "binary_sensor.{}_heating": "heating",
    "binary_sensor.{}_cooling": "cooling",
    "binary_sensor.{}_fan": "fan",
    "binary_sensor.{}_geofencing": "geofencing",
    "select.{}_fan_circulation_mode": "fan_circulation_mode",
    "select.{}_fan_circulation_speed": "fan_circulation_speed",
    "switch.{}_schedule": "schedule",
}
MAIN_ENTITIES = [template.format("main_floor") for template in ENTITIES]


@pytest.fixture(autouse=True)
def fahrenheit(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM


async def start(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, **state: Any
) -> None:
    daikin.states[MAIN_ID].update(state)
    await setup_entry(hass, entry)


def value(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} does not exist"
    return state.state


def all_values(hass: HomeAssistant, slug: str = "main_floor") -> dict[str, str]:
    return {template: value(hass, template.format(slug)) for template in ENTITIES}


def puts(daikin: FakeDaikin) -> list[tuple[str, Any]]:
    return [(c.path, c.json) for c in daikin.calls if c.method == "PUT"]


# ----------------------------------------------------------------- registry


async def test_every_entity_exists_with_a_stable_unique_id(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
) -> None:
    await start(hass, daikin, entry)
    registered = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    assert len(registered) == 2 * len(ENTITIES)
    for slug, device_id in (("main_floor", MAIN_ID), ("upstairs", UPSTAIRS_ID)):
        for template, key in ENTITIES.items():
            item = entity_registry.async_get(template.format(slug))
            assert item is not None, template.format(slug)
            assert item.unique_id == f"{device_id}_{key}"
            assert item.has_entity_name is True
    assert len({item.device_id for item in registered}) == 2


async def test_entity_names(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry)
    names = {
        entity_id: hass.states.get(entity_id).name  # type: ignore[union-attr]
        for entity_id in MAIN_ENTITIES
    }
    assert names == {
        "climate.main_floor": "Main Floor",
        "sensor.main_floor_indoor_temperature": "Main Floor Indoor temperature",
        "sensor.main_floor_indoor_humidity": "Main Floor Indoor humidity",
        "sensor.main_floor_outdoor_temperature": "Main Floor Outdoor temperature",
        "sensor.main_floor_outdoor_humidity": "Main Floor Outdoor humidity",
        "sensor.main_floor_heating_setpoint": "Main Floor Heating setpoint",
        "sensor.main_floor_cooling_setpoint": "Main Floor Cooling setpoint",
        "sensor.main_floor_equipment_status": "Main Floor Equipment status",
        "sensor.main_floor_system_fan": "Main Floor System fan",
        "binary_sensor.main_floor_online": "Main Floor Online",
        "binary_sensor.main_floor_heating": "Main Floor Heating",
        "binary_sensor.main_floor_cooling": "Main Floor Cooling",
        "binary_sensor.main_floor_fan": "Main Floor Fan",
        "binary_sensor.main_floor_geofencing": "Main Floor Geofencing",
        "select.main_floor_fan_circulation_mode": "Main Floor Fan circulation mode",
        "select.main_floor_fan_circulation_speed": "Main Floor Fan circulation speed",
        "switch.main_floor_schedule": "Main Floor Schedule",
    }


# ------------------------------------------------------------------- values


async def test_values_from_the_documented_example(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, tempOutdoor=-5.0, humOutdoor=81)
    assert all_values(hass) == {
        "climate.{}": "off",
        "sensor.{}_indoor_temperature": "70.7",  # 21.5 C
        "sensor.{}_indoor_humidity": "50.0",
        "sensor.{}_outdoor_temperature": "23.0",  # -5.0 C
        "sensor.{}_outdoor_humidity": "81.0",
        "sensor.{}_heating_setpoint": "68.9",  # 20.5 C
        "sensor.{}_cooling_setpoint": "74.3",  # 23.5 C
        "sensor.{}_equipment_status": "idle",
        "sensor.{}_system_fan": "auto",
        "binary_sensor.{}_online": "on",
        "binary_sensor.{}_heating": "off",
        "binary_sensor.{}_cooling": "off",
        "binary_sensor.{}_fan": "off",
        "binary_sensor.{}_geofencing": "off",
        "select.{}_fan_circulation_mode": "always_on",
        "select.{}_fan_circulation_speed": "medium",
        "switch.{}_schedule": "off",
    }
    sensor = hass.states.get("sensor.main_floor_indoor_temperature")
    assert sensor is not None
    assert sensor.attributes["unit_of_measurement"] == "°F"
    assert sensor.attributes["device_class"] == "temperature"
    assert sensor.attributes["state_class"] == "measurement"
    status = hass.states.get("sensor.main_floor_equipment_status")
    assert status is not None
    assert status.attributes["device_class"] == "enum"
    assert status.attributes["options"] == ["cooling", "dehumidifying", "heating", "fan", "idle"]


@pytest.mark.parametrize(
    ("status", "sensor", "heating", "cooling", "fan"),
    [
        (1, "cooling", "off", "on", "on"),
        (2, "dehumidifying", "off", "on", "on"),
        (3, "heating", "on", "off", "on"),
        (4, "fan", "off", "off", "on"),
        (5, "idle", "off", "off", "off"),
        (0, "unknown", "unknown", "unknown", "unknown"),
        (6, "unknown", "unknown", "unknown", "unknown"),
        (99, "unknown", "unknown", "unknown", "unknown"),
        (None, "unknown", "unknown", "unknown", "unknown"),
        ("heat", "unknown", "unknown", "unknown", "unknown"),
    ],
)
async def test_equipment_status_sensors(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    status: Any,
    sensor: str,
    heating: str,
    cooling: str,
    fan: str,
) -> None:
    await start(hass, daikin, entry, equipmentStatus=status)
    assert value(hass, "sensor.main_floor_equipment_status") == sensor
    assert value(hass, "binary_sensor.main_floor_heating") == heating
    assert value(hass, "binary_sensor.main_floor_cooling") == cooling
    assert value(hass, "binary_sensor.main_floor_fan") == fan
    # The other thermostat, and unrelated entities of this one, are untouched.
    assert value(hass, "binary_sensor.upstairs_heating") == "off"
    assert value(hass, "sensor.main_floor_indoor_temperature") == "70.7"
    assert value(hass, "binary_sensor.main_floor_online") == "on"


@pytest.mark.parametrize("mode", [0, 1, 2, 3, 4])
async def test_setpoint_sensors_show_both_setpoints_in_every_mode(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, mode: int
) -> None:
    await start(hass, daikin, entry, mode=mode, modeEmHeatAvailable=1)
    assert value(hass, "sensor.main_floor_heating_setpoint") == "68.9"
    assert value(hass, "sensor.main_floor_cooling_setpoint") == "74.3"


@pytest.mark.parametrize(("fan", "expected"), [(0, "auto"), (1, "on"), (2, "unknown")])
async def test_system_fan_sensor(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    fan: int,
    expected: str,
) -> None:
    await start(hass, daikin, entry, fan=fan)
    assert value(hass, "sensor.main_floor_system_fan") == expected
    item = entity_registry.async_get("sensor.main_floor_system_fan")
    assert item is not None
    assert item.entity_category == "diagnostic"


# Values that are wrong for every field, whatever its documented type.
BAD: list[Any] = [None, "banana", 9999, -9999, [], {"a": 1}]


@pytest.mark.parametrize("bad", BAD, ids=repr)
@pytest.mark.parametrize(
    ("field", "template"),
    [
        ("tempIndoor", "sensor.{}_indoor_temperature"),
        ("humIndoor", "sensor.{}_indoor_humidity"),
        ("tempOutdoor", "sensor.{}_outdoor_temperature"),
        ("humOutdoor", "sensor.{}_outdoor_humidity"),
        ("heatSetpoint", "sensor.{}_heating_setpoint"),
        ("coolSetpoint", "sensor.{}_cooling_setpoint"),
        ("fan", "sensor.{}_system_fan"),
        ("geofencingEnabled", "binary_sensor.{}_geofencing"),
        ("fanCirculate", "select.{}_fan_circulation_mode"),
        ("fanCirculateSpeed", "select.{}_fan_circulation_speed"),
        ("scheduleEnabled", "switch.{}_schedule"),
        ("mode", "climate.{}"),
    ],
)
async def test_bad_value_makes_only_its_own_entity_unknown(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    field: str,
    template: str,
    bad: Any,
) -> None:
    await setup_entry(hass, entry)
    expected = all_values(hass)
    other_before = all_values(hass, "upstairs")

    daikin.states[MAIN_ID][field] = bad
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    expected[template] = "unknown"
    assert all_values(hass) == expected
    assert all_values(hass, "upstairs") == other_before


@pytest.mark.parametrize("field", sorted(example_state()))
async def test_missing_field_never_breaks_setup(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, field: str
) -> None:
    del daikin.states[MAIN_ID][field]
    await setup_entry(hass, entry)
    values = all_values(hass)
    assert "unavailable" not in values.values()
    assert values["binary_sensor.{}_online"] == "on"
    assert "unknown" not in all_values(hass, "upstairs").values()


async def test_empty_response_makes_everything_unknown_but_online(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.states[MAIN_ID] = {}
    await setup_entry(hass, entry)
    values = all_values(hass)
    assert values.pop("binary_sensor.{}_online") == "on"
    assert set(values.values()) == {"unknown"}


# ------------------------------------------------------------- availability


@pytest.mark.parametrize(
    ("failure", "online"),
    [
        (aiohttp.ClientConnectionError(), "unavailable"),
        (TimeoutError(), "unavailable"),
        (Reply(500), "unavailable"),
        (Reply(200, ["wrong"]), "unavailable"),
        (Reply(200, raw=b"not json"), "unavailable"),
        (Reply(400, {"messages": "DeviceOfflineException"}), "off"),
    ],
    ids=["connection", "timeout", "500", "wrong-shape", "not-json", "offline"],
)
async def test_unreadable_thermostat_goes_unavailable_alone(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    failure: Reply | BaseException,
    online: str,
) -> None:
    await start(hass, daikin, entry)
    other_before = all_values(hass, "upstairs")
    healthy = all_values(hass)

    daikin.always("GET", MAIN, failure)
    await advance(hass, freezer, 180)
    values = all_values(hass)
    assert values.pop("binary_sensor.{}_online") == online
    # Never zero, never the previous reading: unavailable.
    assert set(values.values()) == {"unavailable"}
    assert all_values(hass, "upstairs") == other_before

    daikin.always("GET", MAIN, None)
    await advance(hass, freezer, 180)
    assert all_values(hass) == healthy


async def test_thermostat_missing_from_the_account_goes_unavailable(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
) -> None:
    await start(hass, daikin, entry)
    del daikin.locations[0]["devices"][0]
    for _ in range(21):
        await advance(hass, freezer, 180)
    assert set(all_values(hass).values()) == {"unavailable"}
    assert "unavailable" not in all_values(hass, "upstairs").values()
    for entity_id in MAIN_ENTITIES:
        assert entity_registry.async_get(entity_id) is not None


async def test_new_thermostat_gets_entities_without_a_reload(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, daikin, entry)
    daikin.locations[0]["devices"].append({"id": "third-id", "name": "Basement"})
    daikin.states["third-id"] = example_state(tempIndoor=18.0)
    for _ in range(21):
        await advance(hass, freezer, 180)
    values = all_values(hass, "basement")
    assert values["sensor.{}_indoor_temperature"] == "64.4"
    assert values["binary_sensor.{}_online"] == "on"


# ------------------------------------------------------------------ selects


async def test_fan_circulation_selects(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry)
    mode = "select.main_floor_fan_circulation_mode"
    speed = "select.main_floor_fan_circulation_speed"
    state = hass.states.get(mode)
    assert state is not None
    assert state.attributes["options"] == ["off", "always_on", "schedule"]
    state = hass.states.get(speed)
    assert state is not None
    assert state.attributes["options"] == ["low", "medium", "high"]

    await hass.services.async_call(
        "select", "select_option", {"entity_id": mode, "option": "schedule"}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option", {"entity_id": speed, "option": "high"}, blocking=True
    )
    await hass.services.async_call(
        "select", "select_option", {"entity_id": mode, "option": "off"}, blocking=True
    )
    # Each request carries both values; the one not chosen is the last known one.
    assert puts(daikin) == [
        (f"{MAIN}/fan", {"fanCirculate": 2, "fanCirculateSpeed": 1}),
        (f"{MAIN}/fan", {"fanCirculate": 2, "fanCirculateSpeed": 2}),
        (f"{MAIN}/fan", {"fanCirculate": 0, "fanCirculateSpeed": 2}),
    ]
    assert value(hass, mode) == "off"
    assert value(hass, speed) == "high"
    assert value(hass, "select.upstairs_fan_circulation_mode") == "always_on"


async def test_select_refuses_unknown_options_and_unknown_state(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, fanCirculateSpeed=7)
    mode = "select.main_floor_fan_circulation_mode"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": mode, "option": "turbo"}, blocking=True
        )
    # The speed is unknown, so a mode change cannot be completed safely.
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "select", "select_option", {"entity_id": mode, "option": "off"}, blocking=True
        )
    assert err.value.translation_key == "state_unknown"
    assert puts(daikin) == []


# ------------------------------------------------------------------- switch


async def test_schedule_switch(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry)
    switch = "switch.main_floor_schedule"
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch}, blocking=True)
    assert value(hass, switch) == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch}, blocking=True)
    assert value(hass, switch) == "off"
    assert puts(daikin) == [
        (f"{MAIN}/schedule", {"scheduleEnabled": True}),
        (f"{MAIN}/schedule", {"scheduleEnabled": False}),
    ]


@pytest.mark.parametrize(
    ("service", "data"),
    [
        ("set_hvac_mode", {"hvac_mode": "cool"}),
        ("set_temperature", {"hvac_mode": "heat", "temperature": 68}),
        ("turn_on", {}),
    ],
)
async def test_any_climate_write_shows_the_schedule_off_at_once(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    service: str,
    data: dict[str, Any],
) -> None:
    await start(hass, daikin, entry, scheduleEnabled=True)
    switch = "switch.main_floor_schedule"
    assert value(hass, switch) == "on"
    sent = len(daikin.calls)

    await hass.services.async_call(
        "climate", service, {"entity_id": "climate.main_floor", **data}, blocking=True
    )
    assert value(hass, switch) == "off"
    assert [c.method for c in daikin.calls[sent:]] == ["PUT"], "shown before any read-back"
    assert value(hass, "switch.upstairs_schedule") == "off"

    # The verification read 15 seconds later agrees.
    await advance(hass, freezer, 16)
    assert [c.method for c in daikin.calls[sent:]] == ["PUT", "GET"]
    assert value(hass, switch) == "off"


async def test_fan_and_schedule_writes_do_not_touch_the_schedule_state(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, scheduleEnabled=True)
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.main_floor_fan_circulation_speed", "option": "low"},
        blocking=True,
    )
    assert value(hass, "switch.main_floor_schedule") == "on"
