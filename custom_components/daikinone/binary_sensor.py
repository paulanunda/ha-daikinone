"""Binary sensors for a Daikin One thermostat."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.components.binary_sensor.const import BinarySensorDeviceClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import EquipmentStatus, ThermostatState
from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator
from .entity import DaikinOneEntity, async_add_thermostat_entities

# Read-only; all data comes from the coordinator.
PARALLEL_UPDATES = 0

# Daikin reports one equipment status at a time. The blower moves air in every
# status except idle, so "fan" is on for all of these.
_FAN_RUNNING = (
    EquipmentStatus.COOL,
    EquipmentStatus.OVERCOOL_FOR_DEHUM,
    EquipmentStatus.HEAT,
    EquipmentStatus.FAN,
)
_COOLING = (EquipmentStatus.COOL, EquipmentStatus.OVERCOOL_FOR_DEHUM)


def _status_in(
    statuses: tuple[EquipmentStatus, ...],
) -> Callable[[ThermostatState], bool | None]:
    """Return a function telling whether the equipment status is one of ``statuses``."""

    def value(state: ThermostatState) -> bool | None:
        if state.equipment_status is None:
            return None
        return state.equipment_status in statuses

    return value


@dataclass(frozen=True, kw_only=True)
class DaikinOneBinarySensorDescription(BinarySensorEntityDescription):
    """A binary sensor and where its value comes from."""

    value: Callable[[ThermostatState], bool | None]


BINARY_SENSORS: tuple[DaikinOneBinarySensorDescription, ...] = (
    DaikinOneBinarySensorDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.RUNNING,
        value=_status_in((EquipmentStatus.HEAT,)),
    ),
    DaikinOneBinarySensorDescription(
        key="cooling",
        translation_key="cooling",
        device_class=BinarySensorDeviceClass.RUNNING,
        value=_status_in(_COOLING),
    ),
    DaikinOneBinarySensorDescription(
        key="fan",
        translation_key="fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        value=_status_in(_FAN_RUNNING),
    ),
    DaikinOneBinarySensorDescription(
        key="geofencing",
        translation_key="geofencing",
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda state: state.geofencing_enabled,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors for every thermostat."""
    async_add_thermostat_entities(
        entry,
        async_add_entities,
        lambda coordinator, device_id: [
            DaikinOneOnlineSensor(coordinator, device_id),
            *(
                DaikinOneBinarySensor(coordinator, device_id, description)
                for description in BINARY_SENSORS
            ),
        ],
    )


class DaikinOneBinarySensor(DaikinOneEntity, BinarySensorEntity):
    """A yes/no fact from the thermostat's state."""

    entity_description: DaikinOneBinarySensorDescription

    def __init__(
        self,
        coordinator: DaikinOneCoordinator,
        device_id: str,
        description: DaikinOneBinarySensorDescription,
    ) -> None:
        """Create the binary sensor."""
        super().__init__(coordinator, device_id, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """The value, or None (shown as unknown) if Daikin sent nothing usable."""
        state = self.thermostat_state
        return self.entity_description.value(state) if state is not None else None


class DaikinOneOnlineSensor(DaikinOneEntity, BinarySensorEntity):
    """Whether Daikin can currently reach the thermostat.

    The API has no online field, so this is derived from the last read: on when
    it returned state, off when Daikin answered that the thermostat is offline.
    When Daikin itself could not be reached, or the answer was unusable, the
    thermostat's status is not known and this sensor is unavailable.
    """

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DaikinOneCoordinator, device_id: str) -> None:
        """Create the online sensor."""
        super().__init__(coordinator, device_id, "online")

    @property
    def available(self) -> bool:
        """Available whenever the thermostat's online status is known."""
        thermostat = self.thermostat
        return (
            self.coordinator.last_update_success
            and thermostat is not None
            and thermostat.online is not None
        )

    @property
    def is_on(self) -> bool | None:
        """Whether the thermostat is online."""
        thermostat = self.thermostat
        return thermostat.online if thermostat is not None else None
