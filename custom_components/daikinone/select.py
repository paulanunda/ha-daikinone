"""Fan circulation selects for a Daikin One thermostat."""

from __future__ import annotations

from typing import Final

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import FanCirculate, FanCirculateSpeed
from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator
from .entity import DaikinOneEntity, async_add_thermostat_entities

# Writes are queued per thermostat by the coordinator, so no extra limit here.
PARALLEL_UPDATES = 0

CIRCULATE_OPTIONS: Final = {
    FanCirculate.OFF: "off",
    FanCirculate.ALWAYS_ON: "always_on",
    FanCirculate.SCHEDULE: "schedule",
}
SPEED_OPTIONS: Final = {
    FanCirculateSpeed.LOW: "low",
    FanCirculateSpeed.MEDIUM: "medium",
    FanCirculateSpeed.HIGH: "high",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the fan circulation selects for every thermostat."""
    async_add_thermostat_entities(
        entry,
        async_add_entities,
        lambda coordinator, device_id: [
            FanCirculationModeSelect(coordinator, device_id),
            FanCirculationSpeedSelect(coordinator, device_id),
        ],
    )


class FanCirculationModeSelect(DaikinOneEntity, SelectEntity):
    """Whether the fan circulates air: off, always on, or on a schedule."""

    _attr_translation_key = "fan_circulation_mode"

    def __init__(self, coordinator: DaikinOneCoordinator, device_id: str) -> None:
        """Create the select."""
        super().__init__(coordinator, device_id, "fan_circulation_mode")
        self._attr_options = list(CIRCULATE_OPTIONS.values())

    @property
    def current_option(self) -> str | None:
        """The current setting, or None if Daikin sent one this integration does not know."""
        state = self.thermostat_state
        if state is None or state.fan_circulate is None:
            return None
        return CIRCULATE_OPTIONS[state.fan_circulate]

    async def async_select_option(self, option: str) -> None:
        """Change the circulation mode, keeping the speed."""
        circulate = next(value for value, name in CIRCULATE_OPTIONS.items() if name == option)
        await self.coordinator.async_set_fan(self._device_id, circulate=circulate)


class FanCirculationSpeedSelect(DaikinOneEntity, SelectEntity):
    """How fast the fan runs while circulating."""

    _attr_translation_key = "fan_circulation_speed"

    def __init__(self, coordinator: DaikinOneCoordinator, device_id: str) -> None:
        """Create the select."""
        super().__init__(coordinator, device_id, "fan_circulation_speed")
        self._attr_options = list(SPEED_OPTIONS.values())

    @property
    def current_option(self) -> str | None:
        """The current setting, or None if Daikin sent one this integration does not know."""
        state = self.thermostat_state
        if state is None or state.fan_circulate_speed is None:
            return None
        return SPEED_OPTIONS[state.fan_circulate_speed]

    async def async_select_option(self, option: str) -> None:
        """Change the circulation speed, keeping the mode."""
        speed = next(value for value, name in SPEED_OPTIONS.items() if name == option)
        await self.coordinator.async_set_fan(self._device_id, speed=speed)
