"""Climate entity for a Daikin One thermostat.

Mapping between Daikin and Home Assistant:

* Daikin "auto" is Home Assistant ``heat_cool``: the thermostat heats or cools
  to stay between a low and a high setpoint. (Home Assistant's own ``auto``
  means "following a schedule", which is the separate schedule switch here.)
* Daikin "emergency heat" is the ``heat`` mode with the ``emergency_heat``
  preset, offered only when the thermostat reports it available.
* Daikin's fan circulation setting is the fan mode: ``off``, ``on`` (always
  on), or ``schedule``. The fan circulation mode select changes the same
  setting. The thermostat's own auto/on fan setting is read-only and is the
  separate system fan sensor.
* All temperatures are exchanged with Daikin in Celsius. Home Assistant
  converts to and from the unit it is configured to display.
"""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_OFF,
    FAN_ON,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import EquipmentStatus, FanCirculate, Mode, ModeLimit
from .const import DOMAIN
from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator
from .entity import DaikinOneEntity, async_add_thermostat_entities

# Writes are queued per thermostat by the coordinator, so no extra limit here.
PARALLEL_UPDATES = 0

PRESET_EMERGENCY_HEAT: Final = "emergency_heat"
FAN_SCHEDULE: Final = "schedule"

MODE_TO_HVAC: Final = {
    Mode.OFF: HVACMode.OFF,
    Mode.HEAT: HVACMode.HEAT,
    Mode.COOL: HVACMode.COOL,
    Mode.AUTO: HVACMode.HEAT_COOL,
    Mode.EMERGENCY_HEAT: HVACMode.HEAT,
}
HVAC_TO_MODE: Final = {
    HVACMode.OFF: Mode.OFF,
    HVACMode.HEAT: Mode.HEAT,
    HVACMode.COOL: Mode.COOL,
    HVACMode.HEAT_COOL: Mode.AUTO,
}
MODES_FOR_LIMIT: Final = {
    ModeLimit.ALL: [HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL, HVACMode.HEAT_COOL],
    ModeLimit.HEAT_ONLY: [HVACMode.OFF, HVACMode.HEAT],
    ModeLimit.COOL_ONLY: [HVACMode.OFF, HVACMode.COOL],
    # "none" is not explained by Daikin. Until it is, offer only off.
    ModeLimit.NONE: [HVACMode.OFF],
}
STATUS_TO_ACTION: Final = {
    EquipmentStatus.COOL: HVACAction.COOLING,
    EquipmentStatus.OVERCOOL_FOR_DEHUM: HVACAction.DRYING,
    EquipmentStatus.HEAT: HVACAction.HEATING,
    EquipmentStatus.FAN: HVACAction.FAN,
    EquipmentStatus.IDLE: HVACAction.IDLE,
}
HEATING_MODES: Final = (Mode.HEAT, Mode.EMERGENCY_HEAT)
CIRCULATE_TO_FAN_MODE: Final = {
    FanCirculate.OFF: FAN_OFF,
    FanCirculate.ALWAYS_ON: FAN_ON,
    FanCirculate.SCHEDULE: FAN_SCHEDULE,
}
FAN_MODE_TO_CIRCULATE: Final = {name: value for value, name in CIRCULATE_TO_FAN_MODE.items()}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one climate entity per thermostat."""
    async_add_thermostat_entities(
        entry,
        async_add_entities,
        lambda coordinator, device_id: [DaikinOneClimate(coordinator, device_id)],
    )


class DaikinOneClimate(DaikinOneEntity, ClimateEntity):
    """The thermostat itself."""

    _attr_name = None  # the entity is the device's main feature and takes its name
    _attr_translation_key = "thermostat"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS

    def __init__(self, coordinator: DaikinOneCoordinator, device_id: str) -> None:
        """Create the climate entity."""
        super().__init__(coordinator, device_id, "climate")
        self._attr_fan_modes = list(FAN_MODE_TO_CIRCULATE)

    # ------------------------------------------------------------------ state

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Modes the thermostat's mode limit allows, plus whatever mode it is in."""
        state = self.thermostat_state
        if state is None or state.mode_limit is None:
            modes = [HVACMode.OFF]
        else:
            modes = list(MODES_FOR_LIMIT[state.mode_limit])
        current = self.hvac_mode
        if current is not None and current not in modes:
            modes.append(current)
        return modes

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Current mode, or None if Daikin sent one this integration does not know."""
        state = self.thermostat_state
        if state is None or state.mode is None:
            return None
        return MODE_TO_HVAC[state.mode]

    @property
    def hvac_action(self) -> HVACAction | None:
        """What the equipment is doing right now."""
        state = self.thermostat_state
        if state is None or state.equipment_status is None:
            return None
        if state.equipment_status is EquipmentStatus.IDLE and state.mode is Mode.OFF:
            return HVACAction.OFF
        return STATUS_TO_ACTION[state.equipment_status]

    @property
    def supported_features(self) -> ClimateEntityFeature:
        """Features follow the allowed modes, not the current one.

        Keeping them independent of the current mode lets one service call set
        a mode and its setpoint together even while the thermostat is off.
        """
        features = (
            ClimateEntityFeature.TURN_OFF
            | ClimateEntityFeature.TURN_ON
            | ClimateEntityFeature.FAN_MODE
        )
        modes = self.hvac_modes
        if HVACMode.HEAT in modes or HVACMode.COOL in modes:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if HVACMode.HEAT_COOL in modes:
            features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
        if self.preset_modes:
            features |= ClimateEntityFeature.PRESET_MODE
        return features

    @property
    def current_temperature(self) -> float | None:
        """Indoor temperature."""
        state = self.thermostat_state
        return state.temp_indoor if state else None

    @property
    def current_humidity(self) -> float | None:
        """Indoor humidity."""
        state = self.thermostat_state
        return state.hum_indoor if state else None

    @property
    def target_temperature(self) -> float | None:
        """The single setpoint, in heat or cool mode only."""
        state = self.thermostat_state
        if state is None:
            return None
        if state.mode in HEATING_MODES:
            return state.heat_setpoint
        if state.mode is Mode.COOL:
            return state.cool_setpoint
        return None

    @property
    def target_temperature_low(self) -> float | None:
        """The heating setpoint, in heat_cool mode only."""
        state = self.thermostat_state
        return state.heat_setpoint if state and state.mode is Mode.AUTO else None

    @property
    def target_temperature_high(self) -> float | None:
        """The cooling setpoint, in heat_cool mode only."""
        state = self.thermostat_state
        return state.cool_setpoint if state and state.mode is Mode.AUTO else None

    @property
    def min_temp(self) -> float:
        """Lowest setpoint the thermostat reports it accepts."""
        state = self.thermostat_state
        if state is None or state.setpoint_minimum is None:
            return super().min_temp
        return state.setpoint_minimum

    @property
    def max_temp(self) -> float:
        """Highest setpoint the thermostat reports it accepts."""
        state = self.thermostat_state
        if state is None or state.setpoint_maximum is None:
            return super().max_temp
        return state.setpoint_maximum

    @property
    def fan_mode(self) -> str | None:
        """The fan circulation setting."""
        state = self.thermostat_state
        if state is None or state.fan_circulate is None:
            return None
        return CIRCULATE_TO_FAN_MODE[state.fan_circulate]

    @property
    def preset_modes(self) -> list[str] | None:
        """Emergency heat, only when the thermostat says it has it."""
        state = self.thermostat_state
        if state is None:
            return None
        if state.emergency_heat_available or state.mode is Mode.EMERGENCY_HEAT:
            return [PRESET_NONE, PRESET_EMERGENCY_HEAT]
        return None

    @property
    def preset_mode(self) -> str | None:
        """Whether emergency heat is the active mode."""
        state = self.thermostat_state
        if state is None or not self.preset_modes:
            return None
        return PRESET_EMERGENCY_HEAT if state.mode is Mode.EMERGENCY_HEAT else PRESET_NONE

    # --------------------------------------------------------------- commands

    def _invalid(self, key: str, **placeholders: str) -> ServiceValidationError:
        name = self.device_info["name"] if self.device_info else None
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"name": str(name), **placeholders},
        )

    def _mode_for(self, hvac_mode: HVACMode | str) -> Mode:
        """Translate a requested Home Assistant mode, refusing ones not allowed."""
        if hvac_mode not in self.hvac_modes or hvac_mode not in HVAC_TO_MODE:
            raise self._invalid("mode_not_supported", mode=str(hvac_mode))
        return HVAC_TO_MODE[HVACMode(hvac_mode)]

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Change the mode, keeping both setpoints."""
        await self.coordinator.async_set_mode_setpoints(
            self._device_id, mode=self._mode_for(hvac_mode)
        )

    async def async_turn_off(self) -> None:
        """Turn the thermostat off."""
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Change one or both setpoints, optionally with a mode in the same request."""
        requested_mode: HVACMode | None = kwargs.get(ATTR_HVAC_MODE)
        mode = self._mode_for(requested_mode) if requested_mode is not None else None
        heat: float | None = kwargs.get(ATTR_TARGET_TEMP_LOW)
        cool: float | None = kwargs.get(ATTR_TARGET_TEMP_HIGH)

        single: float | None = kwargs.get(ATTR_TEMPERATURE)
        if single is not None:
            # A single temperature belongs to whichever setpoint the mode uses.
            state = self.thermostat_state
            target = mode if mode is not None else (state.mode if state else None)
            if target in HEATING_MODES:
                heat = single
            elif target is Mode.COOL:
                cool = single
            else:
                raise self._invalid("single_setpoint_needs_heat_or_cool")

        await self.coordinator.async_set_mode_setpoints(
            self._device_id, mode=mode, heat_setpoint=heat, cool_setpoint=cool
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Switch emergency heat on or off."""
        state = self.thermostat_state
        if preset_mode == PRESET_EMERGENCY_HEAT:
            if state is None or not state.emergency_heat_available:
                raise self._invalid("mode_not_supported", mode=preset_mode)
            await self.coordinator.async_set_mode_setpoints(
                self._device_id, mode=Mode.EMERGENCY_HEAT
            )
        elif state is not None and state.mode is Mode.EMERGENCY_HEAT:
            # Leaving emergency heat returns to ordinary heat.
            await self.coordinator.async_set_mode_setpoints(self._device_id, mode=Mode.HEAT)

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Change the fan circulation setting, keeping its speed."""
        await self.coordinator.async_set_fan(
            self._device_id, circulate=FAN_MODE_TO_CIRCULATE[fan_mode]
        )
