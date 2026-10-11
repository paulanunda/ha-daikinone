"""Base class shared by every Daikin One entity."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import ThermostatState
from .const import DOMAIN, MANUFACTURER
from .coordinator import DaikinOneConfigEntry, DaikinOneCoordinator, ThermostatData


class DaikinOneEntity(CoordinatorEntity[DaikinOneCoordinator]):
    """An entity that belongs to one thermostat."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: DaikinOneCoordinator, device_id: str, key: str) -> None:
        """Attach the entity to its thermostat's device."""
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_{key}"
        info = coordinator.data[device_id].info
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            manufacturer=MANUFACTURER,
            name=info.name or "Daikin One thermostat",
            model=info.model,
            sw_version=info.firmware_version,
        )

    @property
    def thermostat(self) -> ThermostatData | None:
        """Everything currently known about this entity's thermostat."""
        return self.coordinator.data.get(self._device_id)

    @property
    def thermostat_state(self) -> ThermostatState | None:
        """The thermostat's fresh state, or None when there is none."""
        thermostat = self.thermostat
        return thermostat.state if thermostat is not None else None

    @property
    def available(self) -> bool:
        """Unavailable unless the latest poll produced fresh state for this thermostat."""
        return super().available and self.thermostat_state is not None


@callback
def async_add_thermostat_entities(
    entry: DaikinOneConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
    create: Callable[[DaikinOneCoordinator, str], Iterable[Entity]],
) -> None:
    """Create entities for every thermostat now, and for any that appear later.

    Entities are only ever added. A thermostat that disappears keeps its
    entities, which become unavailable.
    """
    coordinator = entry.runtime_data
    known: set[str] = set()

    @callback
    def _add_new() -> None:
        new = [device_id for device_id in coordinator.data if device_id not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(entity for device_id in new for entity in create(coordinator, device_id))

    _add_new()
    entry.async_on_unload(coordinator.async_add_listener(_add_new))
