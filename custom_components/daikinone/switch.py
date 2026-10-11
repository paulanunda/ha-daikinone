"""Switch for the thermostat's own schedule."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator
from .entity import DaikinOneEntity, async_add_thermostat_entities

# Writes are queued per thermostat by the coordinator, so no extra limit here.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the schedule switch for every thermostat."""
    async_add_thermostat_entities(
        entry,
        async_add_entities,
        lambda coordinator, device_id: [ScheduleSwitch(coordinator, device_id)],
    )


class ScheduleSwitch(DaikinOneEntity, SwitchEntity):
    """Whether the thermostat follows its own schedule.

    Any mode or setpoint change made through the API turns the schedule off, so
    this switch goes off as soon as the climate entity is used.
    """

    _attr_translation_key = "schedule"

    def __init__(self, coordinator: DaikinOneCoordinator, device_id: str) -> None:
        """Create the switch."""
        super().__init__(coordinator, device_id, "schedule")

    @property
    def is_on(self) -> bool | None:
        """Whether the schedule is enabled."""
        state = self.thermostat_state
        return state.schedule_enabled if state is not None else None

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the thermostat's schedule."""
        await self.coordinator.async_set_schedule(self._device_id, enabled=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the thermostat's schedule."""
        await self.coordinator.async_set_schedule(self._device_id, enabled=False)
