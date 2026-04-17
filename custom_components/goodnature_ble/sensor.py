"""Sensors for Goodnature BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, SIGNAL_STRENGTH_DECIBELS_MILLIWATT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import GoodnatureRuntimeData
from .const import ATTR_ESTIMATED_ACTIVATIONS
from .entity import GoodnatureEntity
from .coordinator import TrapState


ACTIVATIONS_DESCRIPTION = SensorEntityDescription(
    key="estimated_activations",
    name="Activations",
    icon="mdi:counter",
    state_class=SensorStateClass.TOTAL,
)

RSSI_DESCRIPTION = SensorEntityDescription(
    key="rssi",
    name="Signal Strength",
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    entity_category=EntityCategory.DIAGNOSTIC,
)

@dataclass(frozen=True, slots=True)
class GoodnatureSensorSpec:
    """Describes a dynamic coordinator-backed sensor."""

    key: str
    name: str
    value_fn: Callable[[TrapState], Any]
    icon: str | None = None
    device_class: SensorDeviceClass | None = None
    entity_category: EntityCategory | None = None
    native_unit_of_measurement: str | None = None


SENSOR_SPECS: tuple[GoodnatureSensorSpec, ...] = (
    GoodnatureSensorSpec(
        key="c20_battery_percent",
        name="Battery",
        value_fn=lambda s: s.c20_battery_percent,
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement="%",
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GoodnatureSensorSpec(
        key="c20_last_kill_at",
        name="Last Kill At",
        value_fn=lambda s: s.c20_last_kill_at,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    GoodnatureSensorSpec(
        key="c20_device_state",
        name="Device",
        value_fn=lambda s: s.c20_device_state,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Goodnature sensors."""
    runtime: GoodnatureRuntimeData = entry.runtime_data

    dynamic_sensors = [
        GoodnatureStateSensor(runtime.coordinator, spec)
        for spec in SENSOR_SPECS
    ]

    async_add_entities(
        [
            GoodnatureEstimatedActivationsSensor(runtime.coordinator),
            GoodnatureRssiSensor(runtime.coordinator),
            *dynamic_sensors,
        ]
    )


class GoodnatureEstimatedActivationsSensor(GoodnatureEntity, RestoreEntity, SensorEntity):
    """Activation count, preferring trap-reported values over inferred bursts."""

    entity_description = ACTIVATIONS_DESCRIPTION

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_estimated_activations"
        self._restored_base = 0

    async def async_added_to_hass(self) -> None:
        """Restore previous total after Home Assistant restart."""
        await super().async_added_to_hass()
        if last_state := await self.async_get_last_state():
            try:
                self._restored_base = int(float(last_state.state))
            except (TypeError, ValueError):
                self._restored_base = 0

    @property
    def native_value(self) -> int:
        """Return activation total.

        Prefer trap counters from GATT and only fall back
        to inferred burst activations when no reliable trap counter is available.
        """
        state = self.coordinator.state

        if state.c20_strike_count is not None:
            return state.c20_strike_count

        if state.kill_displayed is not None:
            return state.kill_displayed

        if state.kill_read is not None:
            return state.kill_read

        # Fallback for passive/BLE-only mode.
        return self._restored_base + state.estimated_activations

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return compatibility and provenance metadata."""
        state = self.coordinator.state
        source = (
            "c20_strike_count"
            if state.c20_strike_count is not None
            else
            "kill_displayed"
            if state.kill_displayed is not None
            else "kill_read"
            if state.kill_read is not None
            else "inferred_ble_burst"
        )
        return {
            ATTR_ESTIMATED_ACTIVATIONS: self.native_value,
            "source": source,
        }


class GoodnatureRssiSensor(GoodnatureEntity, SensorEntity):
    """RSSI from the most recent packet."""

    entity_description = RSSI_DESCRIPTION
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.address}_rssi"

    @property
    def native_value(self) -> int | None:
        """Return most recent RSSI."""
        return self.coordinator.state.last_rssi


class GoodnatureStateSensor(GoodnatureEntity, SensorEntity):
    """Generic sensor backed by coordinator trap state."""

    def __init__(self, coordinator, spec: GoodnatureSensorSpec) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._attr_unique_id = f"{coordinator.address}_{spec.key}"
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_device_class = spec.device_class
        self._attr_entity_category = spec.entity_category
        self._attr_native_unit_of_measurement = spec.native_unit_of_measurement

    @property
    def native_value(self) -> Any:
        """Return dynamic value."""
        return self._spec.value_fn(self.coordinator.state)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Attach raw protocol payloads on a single sensor to aid debugging."""
        return None
