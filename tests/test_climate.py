"""The climate entity, exercised the way the house uses it: in Fahrenheit."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.climate.const import ClimateEntityFeature
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daikinone import async_remove_config_entry_device
from custom_components.daikinone.const import DOMAIN
from tests.conftest import advance, setup_entry
from tests.fake_daikin import MAIN_ID, UPSTAIRS_ID, FakeDaikin, Reply

MAIN = f"/v1/devices/{MAIN_ID}"
ENTITY = "climate.main_floor"
OTHER = "climate.upstairs"


@pytest.fixture(autouse=True)
def fahrenheit(hass: HomeAssistant) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM


async def start(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, **state: Any
) -> State:
    daikin.states[MAIN_ID].update(state)
    await setup_entry(hass, entry)
    return get(hass)


def get(hass: HomeAssistant, entity_id: str = ENTITY) -> State:
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def call(hass: HomeAssistant, service: str, **data: Any) -> None:
    await hass.services.async_call("climate", service, {"entity_id": ENTITY, **data}, blocking=True)


def msp(daikin: FakeDaikin) -> list[Any]:
    return [c.json for c in daikin.calls if c.method == "PUT" and c.path == f"{MAIN}/msp"]


def puts(daikin: FakeDaikin) -> list[str]:
    return [c.path for c in daikin.calls if c.method == "PUT"]


# -------------------------------------------------------------------- state


async def test_registry_entries(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    await start(hass, daikin, entry)
    registered = entity_registry.async_get(ENTITY)
    assert registered is not None
    assert registered.unique_id == f"{MAIN_ID}_climate"
    other = entity_registry.async_get(OTHER)
    assert other is not None
    assert other.unique_id == f"{UPSTAIRS_ID}_climate"

    device = device_registry.async_get_device_by_identifier((DOMAIN, MAIN_ID), entry.entry_id)
    assert device is not None
    assert registered.device_id == device.id
    assert (device.name, device.manufacturer, device.model, device.sw_version) == (
        "Main Floor",
        "Daikin",
        "ONETOUCH",
        "3.2.19",
    )
    assert get(hass).name == "Main Floor"


@pytest.mark.parametrize(
    ("mode", "state", "single", "low", "high"),
    [
        (0, "off", None, None, None),
        (1, "heat", 69, None, None),
        (2, "cool", 74, None, None),
        (3, "heat_cool", None, 69, 74),
        (4, "heat", 69, None, None),
    ],
)
async def test_mode_and_setpoints(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    mode: int,
    state: str,
    single: int | None,
    low: int | None,
    high: int | None,
) -> None:
    """The documented example: heat 20.5 C (68.9 F), cool 23.5 C (74.3 F)."""
    climate = await start(hass, daikin, entry, mode=mode)
    assert climate.state == state
    attrs = climate.attributes
    assert attrs["temperature"] == single
    assert attrs["target_temp_low"] == low
    assert attrs["target_temp_high"] == high
    assert attrs["current_temperature"] == 71  # 21.5 C
    assert attrs["current_humidity"] == 50
    assert attrs["min_temp"] == 50  # 10.0 C
    assert attrs["max_temp"] == 90  # 32.3 C


@pytest.mark.parametrize(
    ("status", "mode", "action"),
    [
        (1, 2, "cooling"),
        (2, 2, "drying"),
        (3, 1, "heating"),
        (4, 0, "fan"),
        (4, 1, "fan"),
        (5, 1, "idle"),
        (5, 3, "idle"),
        (5, 0, "off"),
        (5, 42, "idle"),
    ],
)
async def test_current_action(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    status: int,
    mode: int,
    action: str,
) -> None:
    climate = await start(hass, daikin, entry, equipmentStatus=status, mode=mode)
    assert climate.attributes["hvac_action"] == action


@pytest.mark.parametrize("status", [0, 6, 7, 8, 9, 99, -1, None, "heat", 1.5])
async def test_unknown_equipment_status_gives_no_action(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, status: Any
) -> None:
    climate = await start(hass, daikin, entry, equipmentStatus=status, mode=1)
    assert climate.state == "heat"
    assert climate.attributes.get("hvac_action") is None
    assert get(hass, OTHER).attributes["hvac_action"] == "off"


@pytest.mark.parametrize(
    ("limit", "mode", "modes"),
    [
        (1, 0, ["off", "heat", "cool", "heat_cool"]),
        (2, 0, ["off", "heat"]),
        (3, 0, ["off", "cool"]),
        (0, 0, ["off"]),
        (99, 0, ["off"]),
        (None, 0, ["off"]),
        # Whatever mode the thermostat is actually in is always listed.
        (0, 3, ["off", "heat_cool"]),
        (2, 2, ["off", "heat", "cool"]),
        (None, 1, ["off", "heat"]),
        (3, 4, ["off", "cool", "heat"]),
    ],
)
async def test_only_modes_the_mode_limit_allows_are_offered(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    limit: Any,
    mode: int,
    modes: list[str],
) -> None:
    climate = await start(hass, daikin, entry, modeLimit=limit, mode=mode)
    assert climate.attributes["hvac_modes"] == modes


@pytest.mark.parametrize(
    ("limit", "single", "ranged"),
    [(1, True, True), (2, True, False), (3, True, False), (0, False, False)],
)
async def test_features_follow_the_allowed_modes_not_the_current_one(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    limit: int,
    single: bool,
    ranged: bool,
) -> None:
    climate = await start(hass, daikin, entry, modeLimit=limit, mode=0)
    features = ClimateEntityFeature(climate.attributes["supported_features"])
    assert bool(features & ClimateEntityFeature.TARGET_TEMPERATURE) is single
    assert bool(features & ClimateEntityFeature.TARGET_TEMPERATURE_RANGE) is ranged
    assert features & ClimateEntityFeature.TURN_OFF
    assert features & ClimateEntityFeature.TURN_ON
    assert not features & ClimateEntityFeature.PRESET_MODE
    assert features & ClimateEntityFeature.FAN_MODE


@pytest.mark.parametrize(
    ("circulate", "fan_mode"),
    [(0, "off"), (1, "on"), (2, "schedule"), (3, None), (None, None), ("on", None)],
)
async def test_fan_mode_is_the_fan_circulation_setting(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    circulate: Any,
    fan_mode: str | None,
) -> None:
    climate = await start(hass, daikin, entry, fanCirculate=circulate)
    assert climate.attributes["fan_modes"] == ["off", "on", "schedule"]
    assert climate.attributes["fan_mode"] == fan_mode
    # An unknown fan setting does not affect the rest of the entity.
    assert climate.state == "off"
    assert get(hass, OTHER).attributes["fan_mode"] == "on"


@pytest.mark.parametrize("available", [0, False, None, 2, "yes"])
async def test_emergency_heat_is_hidden_unless_reported_available(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, available: Any
) -> None:
    climate = await start(hass, daikin, entry, modeEmHeatAvailable=available, mode=1)
    assert "preset_modes" not in climate.attributes
    assert "preset_mode" not in climate.attributes
    features = ClimateEntityFeature(climate.attributes["supported_features"])
    assert not features & ClimateEntityFeature.PRESET_MODE
    with pytest.raises(HomeAssistantError):
        await call(hass, "set_preset_mode", preset_mode="emergency_heat")
    assert puts(daikin) == []


@pytest.mark.parametrize("available", [1, True])
async def test_emergency_heat_when_available(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, available: Any
) -> None:
    climate = await start(hass, daikin, entry, modeEmHeatAvailable=available, mode=1)
    assert climate.attributes["preset_modes"] == ["none", "emergency_heat"]
    assert climate.attributes["preset_mode"] == "none"
    assert ClimateEntityFeature(climate.attributes["supported_features"]) & (
        ClimateEntityFeature.PRESET_MODE
    )

    await call(hass, "set_preset_mode", preset_mode="emergency_heat")
    assert msp(daikin) == [{"mode": 4, "heatSetpoint": 20.5, "coolSetpoint": 23.5}]
    climate = get(hass)
    assert climate.state == "heat"
    assert climate.attributes["preset_mode"] == "emergency_heat"
    assert climate.attributes["temperature"] == 69

    await call(hass, "set_preset_mode", preset_mode="none")
    assert msp(daikin)[-1] == {"mode": 1, "heatSetpoint": 20.5, "coolSetpoint": 23.5}
    assert get(hass).attributes["preset_mode"] == "none"

    # Choosing "none" when emergency heat is not active changes nothing.
    await call(hass, "set_preset_mode", preset_mode="none")
    assert len(msp(daikin)) == 2


@pytest.mark.parametrize("mode", [5, 42, -1, None, "heat", 1.5, True])
async def test_unknown_mode_is_unknown_for_this_entity_only(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, mode: Any
) -> None:
    climate = await start(hass, daikin, entry, mode=mode)
    assert climate.state == "unknown"
    assert climate.attributes["current_temperature"] == 71
    assert climate.attributes["temperature"] is None
    assert get(hass, OTHER).state == "off"


async def test_missing_values_are_unknown_not_zero(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    daikin.states[MAIN_ID] = {"mode": 1, "modeLimit": 1}
    await setup_entry(hass, entry)
    climate = get(hass)
    assert climate.state == "heat"
    for name in ("current_temperature", "temperature"):
        assert climate.attributes[name] is None
    assert "current_humidity" not in climate.attributes
    assert climate.attributes.get("hvac_action") is None
    # Default limits are shown when the thermostat reports none.
    assert climate.attributes["min_temp"] == 45
    assert climate.attributes["max_temp"] == 95


@pytest.mark.parametrize(
    "failure",
    [
        aiohttp.ClientConnectionError(),
        Reply(200, ["wrong", "shape"]),
        Reply(400, {"messages": "DeviceOfflineException"}),
    ],
    ids=["connection", "malformed", "offline"],
)
async def test_unreadable_thermostat_is_unavailable_and_the_other_is_not(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    failure: Reply | BaseException,
) -> None:
    await start(hass, daikin, entry, mode=1)
    daikin.always("GET", MAIN, failure)
    await advance(hass, freezer, 180)
    assert get(hass).state == "unavailable"
    assert "current_temperature" not in get(hass).attributes
    assert get(hass, OTHER).state == "off"
    assert get(hass, OTHER).attributes["current_temperature"] == 73

    daikin.always("GET", MAIN, None)
    await advance(hass, freezer, 180)
    assert get(hass).state == "heat"


async def test_everything_is_unavailable_when_daikin_cannot_be_reached(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, daikin, entry)
    daikin.always("GET", MAIN, aiohttp.ClientConnectionError())
    daikin.always("GET", f"/v1/devices/{UPSTAIRS_ID}", TimeoutError())
    await advance(hass, freezer, 180)
    assert get(hass).state == "unavailable"
    assert get(hass, OTHER).state == "unavailable"


# ---------------------------------------------------------------- registry


async def test_missing_thermostat_is_never_removed_from_the_registry(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
) -> None:
    await start(hass, daikin, entry)

    def registry() -> tuple[set[str], set[str]]:
        entities = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
        devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
        return {e.entity_id for e in entities}, {d.id for d in devices}

    entities_before, devices_before = registry()
    assert OTHER in entities_before
    assert len(devices_before) == 2

    # Upstairs disappears from the account.
    del daikin.locations[0]["devices"][1]
    for _ in range(21):
        await advance(hass, freezer, 180)
    assert get(hass, OTHER).state == "unavailable"
    assert get(hass).state == "off"
    assert registry() == (entities_before, devices_before)

    # Still nothing is removed across a reload, when it is absent from the start.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry() == (entities_before, devices_before)
    assert get(hass, OTHER).state == "unavailable"

    # Nor when the whole account comes back empty.
    daikin.locations = []
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert registry() == (entities_before, devices_before)


async def test_manual_device_removal_is_allowed_only_for_unlisted_thermostats(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    del daikin.locations[0]["devices"][1]
    # Upstairs is known to the registry from an earlier run but no longer listed.
    gone = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, UPSTAIRS_ID)}
    )
    await start(hass, daikin, entry)
    present = device_registry.async_get_device_by_identifier((DOMAIN, MAIN_ID), entry.entry_id)
    assert present is not None
    assert await async_remove_config_entry_device(hass, entry, present) is False
    assert await async_remove_config_entry_device(hass, entry, gone) is True


async def test_manual_device_removal_is_allowed_when_the_entry_never_loaded(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    device_registry: dr.DeviceRegistry,
) -> None:
    device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={(DOMAIN, MAIN_ID)}
    )
    daikin.always("POST", "/v1/token", aiohttp.ClientConnectionError())
    await setup_entry(hass, entry)
    assert await async_remove_config_entry_device(hass, entry, device) is True


# ----------------------------------------------------------------- commands


async def test_set_hvac_mode_keeps_both_setpoints(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry)
    await call(hass, "set_hvac_mode", hvac_mode="cool")
    assert msp(daikin) == [{"mode": 2, "heatSetpoint": 20.5, "coolSetpoint": 23.5}]
    climate = get(hass)
    assert climate.state == "cool"
    assert climate.attributes["temperature"] == 74

    await call(hass, "set_hvac_mode", hvac_mode="heat_cool")
    assert msp(daikin)[-1] == {"mode": 3, "heatSetpoint": 20.5, "coolSetpoint": 23.5}
    assert get(hass).state == "heat_cool"


@pytest.mark.parametrize(
    ("limit", "mode"),
    [
        (2, "cool"),
        (2, "heat_cool"),
        (3, "heat"),
        (0, "heat"),
        (1, "auto"),
        (1, "dry"),
        (1, "fan_only"),
    ],
)
async def test_mode_outside_the_limit_is_refused(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, limit: int, mode: str
) -> None:
    await start(hass, daikin, entry, modeLimit=limit)
    with pytest.raises(ServiceValidationError):
        await call(hass, "set_hvac_mode", hvac_mode=mode)
    with pytest.raises(ServiceValidationError):
        await call(hass, "set_temperature", hvac_mode=mode, temperature=70)
    assert puts(daikin) == []


async def test_turn_off_and_on(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, mode=1)
    await call(hass, "turn_off")
    assert msp(daikin) == [{"mode": 0, "heatSetpoint": 20.5, "coolSetpoint": 23.5}]
    assert get(hass).state == "off"
    await call(hass, "turn_on")
    assert msp(daikin)[-1]["mode"] == 3  # Home Assistant's default: heat_cool when offered
    assert get(hass).state == "heat_cool"


async def test_turn_on_with_a_heat_only_limit_selects_heat(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, modeLimit=2)
    await call(hass, "turn_on")
    assert msp(daikin) == [{"mode": 1, "heatSetpoint": 20.5, "coolSetpoint": 23.5}]


async def test_single_setpoint_in_heat_and_cool(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, mode=1)
    await call(hass, "set_temperature", temperature=67)  # 19.4 C
    assert msp(daikin) == [{"mode": 1, "heatSetpoint": 19.4, "coolSetpoint": 23.5}]
    assert get(hass).attributes["temperature"] == 67

    await call(hass, "set_hvac_mode", hvac_mode="cool")
    await call(hass, "set_temperature", temperature=76)  # 24.4 C
    assert msp(daikin)[-1] == {"mode": 2, "heatSetpoint": 19.4, "coolSetpoint": 24.4}
    assert get(hass).attributes["temperature"] == 76


async def test_mode_and_setpoint_together_while_off(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    """What the outdoor-temperature automation does: one call, one request."""
    await start(hass, daikin, entry, mode=0)
    await call(hass, "set_temperature", hvac_mode="heat", temperature=68)
    assert msp(daikin) == [{"mode": 1, "heatSetpoint": 20.0, "coolSetpoint": 23.5}]
    climate = get(hass)
    assert climate.state == "heat"
    assert climate.attributes["temperature"] == 68

    await call(hass, "set_temperature", hvac_mode="cool", temperature=75)
    assert msp(daikin)[-1] == {"mode": 2, "heatSetpoint": 20.0, "coolSetpoint": 23.9}
    assert len(puts(daikin)) == 2


async def test_range_in_heat_cool(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, mode=3)
    await call(hass, "set_temperature", target_temp_low=66, target_temp_high=77)
    assert msp(daikin) == [{"mode": 3, "heatSetpoint": 18.9, "coolSetpoint": 25.0}]
    attrs = get(hass).attributes
    assert (attrs["target_temp_low"], attrs["target_temp_high"]) == (66, 77)

    await call(
        hass, "set_temperature", hvac_mode="heat_cool", target_temp_low=68, target_temp_high=74
    )
    assert msp(daikin)[-1] == {"mode": 3, "heatSetpoint": 20.0, "coolSetpoint": 23.3}


async def test_single_setpoint_pushes_the_hidden_one_to_keep_the_delta(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, mode=1)
    await call(hass, "set_temperature", temperature=73)  # 22.8 C; cool was 23.5, delta is 2
    assert msp(daikin) == [{"mode": 1, "heatSetpoint": 22.8, "coolSetpoint": 24.8}]


@pytest.mark.parametrize("mode", [0, 3])
async def test_single_temperature_without_heat_or_cool_is_refused(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, mode: int
) -> None:
    await start(hass, daikin, entry, mode=mode)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "set_temperature", temperature=70)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "single_setpoint_needs_heat_or_cool"
    assert err.value.translation_placeholders == {"name": "Main Floor"}
    assert puts(daikin) == []


async def test_range_closer_than_the_delta_is_refused_with_a_clear_error(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, mode=3)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "set_temperature", target_temp_low=70, target_temp_high=72)
    assert err.value.translation_domain == DOMAIN
    assert err.value.translation_key == "setpoints_too_close"
    assert err.value.translation_placeholders == {
        "name": "Main Floor",
        "minimum": "50.0",
        "maximum": "90.1",
        "delta": "3.6",
        "unit": "°F",
    }
    assert puts(daikin) == []
    attrs = get(hass).attributes
    assert (attrs["target_temp_low"], attrs["target_temp_high"]) == (69, 74)


@pytest.mark.parametrize("temperature", [40, 49, 91, 120])
async def test_setpoint_outside_the_limits_is_refused(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, temperature: int
) -> None:
    await start(hass, daikin, entry, mode=1)
    with pytest.raises(ServiceValidationError):
        await call(hass, "set_temperature", temperature=temperature)
    assert puts(daikin) == []
    assert get(hass).attributes["temperature"] == 69


async def test_setpoint_with_no_room_for_the_delta_is_refused(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, mode=1)
    with pytest.raises(ServiceValidationError) as err:
        await call(hass, "set_temperature", temperature=89)  # 31.7 C + 2 > 32.3 C
    assert err.value.translation_key == "setpoint_no_room"
    assert puts(daikin) == []


@pytest.mark.parametrize("mode", [0, 1])
async def test_set_fan_mode_changes_circulation_and_keeps_the_speed(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, mode: int
) -> None:
    await start(hass, daikin, entry, mode=mode, fanCirculateSpeed=2, scheduleEnabled=True)
    await call(hass, "set_fan_mode", fan_mode="schedule")
    await call(hass, "set_fan_mode", fan_mode="off")
    await call(hass, "set_fan_mode", fan_mode="on")
    assert [(c.path, c.json) for c in daikin.calls if c.method == "PUT"] == [
        (f"{MAIN}/fan", {"fanCirculate": 2, "fanCirculateSpeed": 2}),
        (f"{MAIN}/fan", {"fanCirculate": 0, "fanCirculateSpeed": 2}),
        (f"{MAIN}/fan", {"fanCirculate": 1, "fanCirculateSpeed": 2}),
    ]
    assert get(hass).attributes["fan_mode"] == "on"
    # The select shows the same setting, and the schedule is left alone.
    assert get(hass, "select.main_floor_fan_circulation_mode").state == "always_on"
    assert get(hass, "switch.main_floor_schedule").state == "on"
    assert get(hass, OTHER).attributes["fan_mode"] == "on"


async def test_fan_mode_follows_the_select(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry)
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.main_floor_fan_circulation_mode", "option": "schedule"},
        blocking=True,
    )
    assert get(hass).attributes["fan_mode"] == "schedule"


@pytest.mark.parametrize("fan_mode", ["auto", "always_on", "low"])
async def test_fan_mode_that_is_not_offered_is_refused(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry, fan_mode: str
) -> None:
    await start(hass, daikin, entry)
    with pytest.raises(ServiceValidationError):
        await call(hass, "set_fan_mode", fan_mode=fan_mode)
    assert puts(daikin) == []


async def test_set_fan_mode_is_refused_when_the_speed_is_unknown(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, fanCirculateSpeed=None)
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "set_fan_mode", fan_mode="off")
    assert err.value.translation_key == "state_unknown"
    assert puts(daikin) == []


async def test_command_to_an_unavailable_thermostat_sends_nothing(
    hass: HomeAssistant,
    daikin: FakeDaikin,
    entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    await start(hass, daikin, entry, mode=1)
    daikin.always("GET", MAIN, aiohttp.ClientConnectionError())
    await advance(hass, freezer, 180)
    for service, data in (
        ("set_hvac_mode", {"hvac_mode": "cool"}),
        ("set_temperature", {"temperature": 70}),
        ("turn_off", {}),
        ("set_fan_mode", {"fan_mode": "on"}),
    ):
        try:
            await call(hass, service, **data)
        except HomeAssistantError as err:
            assert err.translation_key in ("thermostat_unavailable", "entity_not_available")  # noqa: PT017
    assert puts(daikin) == []


async def test_failed_write_leaves_the_state_alone(
    hass: HomeAssistant, daikin: FakeDaikin, entry: MockConfigEntry
) -> None:
    await start(hass, daikin, entry, mode=1)
    daikin.queue("PUT", f"{MAIN}/msp", Reply(400, {"messages": "Invalid request body"}))
    with pytest.raises(HomeAssistantError) as err:
        await call(hass, "set_temperature", temperature=67)
    assert err.value.translation_key == "write_rejected"
    assert get(hass).attributes["temperature"] == 69
    assert get(hass).state == "heat"
