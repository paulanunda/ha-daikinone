"""Temperature, humidity, setpoint, equipment status, and system fan sensors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import EquipmentStatus, FanState, ThermostatState
from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator
from .entity import DaikinOneEntity, async_add_thermostat_entities

# Read-only; all data comes from the coordinator.
PARALLEL_UPDATES = 0

# Overcooling to dehumidify keeps its own state here; the Cooling binary sensor
# and the climate entity fold it into cooling and drying.
EQUIPMENT_STATUS_OPTIONS: Final = {
    EquipmentStatus.COOL: "cooling",
    EquipmentStatus.OVERCOOL_FOR_DEHUM: "dehumidifying",
    EquipmentStatus.HEAT: "heating",
    EquipmentStatus.FAN: "fan",
    EquipmentStatus.IDLE: "idle",
}
SYSTEM_FAN_OPTIONS: Final = {
    FanState.AUTO: "auto",
    FanState.ON: "on",
}


@dataclass(frozen=True, kw_only=True)
class DaikinOneSensorDescription(SensorEntityDescription):
    """A sensor and where its value comes from."""

    value: Callable[[ThermostatState], float | str | None]


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
    # Daikin reports both setpoints in every mode. The climate entity shows only
    # the ones the current mode uses, so these keep an unbroken history of each.
    DaikinOneSensorDescription(
        key="heating_setpoint",
        translation_key="heating_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state: state.heat_setpoint,
    ),
    DaikinOneSensorDescription(
        key="cooling_setpoint",
        translation_key="cooling_setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda state: state.cool_setpoint,
    ),
    DaikinOneSensorDescription(
        key="equipment_status",
        translation_key="equipment_status",
        device_class=SensorDeviceClass.ENUM,
        options=list(EQUIPMENT_STATUS_OPTIONS.values()),
        value=lambda state: (
            EQUIPMENT_STATUS_OPTIONS[state.equipment_status]
            if state.equipment_status is not None
            else None
        ),
    ),
    # The fan setting made at the thermostat. Daikin's API cannot change it.
    DaikinOneSensorDescription(
        key="system_fan",
        translation_key="system_fan",
        device_class=SensorDeviceClass.ENUM,
        options=list(SYSTEM_FAN_OPTIONS.values()),
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda state: SYSTEM_FAN_OPTIONS[state.fan] if state.fan is not None else None,
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
    def native_value(self) -> float | str | None:
        """The reading, or None (shown as unknown) if Daikin sent nothing usable."""
        state = self.thermostat_state
        return self.entity_description.value(state) if state is not None else None
