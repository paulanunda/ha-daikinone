"""Temperature and humidity sensors for a Daikin One thermostat."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ThermostatState
from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator
from .entity import DaikinOneEntity, async_add_thermostat_entities

# Read-only; all data comes from the coordinator.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class DaikinOneSensorDescription(SensorEntityDescription):
    """A sensor and where its value comes from."""

    value: Callable[[ThermostatState], float | None]


SENSORS: tuple[DaikinOneSensorDescription, ...] = (
    DaikinOneSensorDescription(
        key="indoor_temperature",
        translation_key="indoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state: state.temp_indoor,
    ),
    DaikinOneSensorDescription(
        key="indoor_humidity",
        translation_key="indoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state: state.hum_indoor,
    ),
    DaikinOneSensorDescription(
        key="outdoor_temperature",
        translation_key="outdoor_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state: state.temp_outdoor,
    ),
    DaikinOneSensorDescription(
        key="outdoor_humidity",
        translation_key="outdoor_humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state: state.hum_outdoor,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors for every thermostat."""
    async_add_thermostat_entities(
        entry,
        async_add_entities,
        lambda coordinator, device_id: [
            DaikinOneSensor(coordinator, device_id, description) for description in SENSORS
        ],
    )


class DaikinOneSensor(DaikinOneEntity, SensorEntity):
    """One reading from the thermostat."""

    entity_description: DaikinOneSensorDescription

    def __init__(
        self,
        coordinator: DaikinOneCoordinator,
        device_id: str,
        description: DaikinOneSensorDescription,
    ) -> None:
        """Create the sensor."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """The reading, or None (shown as unknown) if Daikin sent nothing usable."""
        state = self.thermostat_state
        return self.entity_description.value(state) if state is not None else None
