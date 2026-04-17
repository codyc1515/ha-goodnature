"""Binary sensors for Goodnature BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoodnatureRuntimeData
from .entity import GoodnatureEntity


@dataclass(frozen=True, slots=True)
class GoodnatureBinaryStateSpec:
    """Describes a C20 state-derived binary sensor."""

    key: str
    name: str
    device_class: BinarySensorDeviceClass
    state_fn: Callable[[str | None], bool | None]
    raw_attr: str
    entity_category: EntityCategory | None = EntityCategory.DIAGNOSTIC


STATE_SPECS: tuple[GoodnatureBinaryStateSpec, ...] = (
    GoodnatureBinaryStateSpec(
        key="charge_state",
        name="Charge",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        state_fn=lambda raw: None if raw in (None, "NO_DATA") else raw == "CHARGING",
        raw_attr="c20_charge_state",
    ),
    GoodnatureBinaryStateSpec(
        key="battery_state_problem",
        name="Battery",
        device_class=BinarySensorDeviceClass.PROBLEM,
        state_fn=lambda raw: (
            None
            if raw in (None, "NO_DATA")
            else raw in ("CRITICAL", "LOW", "NOT_CONNECTED")
        ),
        raw_attr="c20_battery_state",
    ),
    GoodnatureBinaryStateSpec(
        key="kill_state",
        name="Trap Occupancy",
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        state_fn=lambda raw: None if raw in (None, "NO_DATA") else raw == "DETECTED",
        raw_attr="c20_kill_state",
        entity_category=None,
    ),
    GoodnatureBinaryStateSpec(
        key="usb_state",
        name="USB",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        state_fn=lambda raw: None if raw in (None, "NO_DATA") else raw == "CONNECTED",
        raw_attr="c20_usb_state",
    ),
    GoodnatureBinaryStateSpec(
        key="tray_state",
        name="Tray",
        device_class=BinarySensorDeviceClass.OPENING,
        state_fn=lambda raw: None if raw in (None, "NO_DATA", "UNKNOWN") else raw == "OPEN",
        raw_attr="c20_tray_state",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Goodnature binary sensors."""
    runtime: GoodnatureRuntimeData = entry.runtime_data

    async_add_entities(
        [GoodnatureStateBinarySensor(runtime.coordinator, spec) for spec in STATE_SPECS]
    )


class GoodnatureStateBinarySensor(GoodnatureEntity, BinarySensorEntity):
    """Binary sensor derived from C20 state values."""

    def __init__(self, coordinator, spec: GoodnatureBinaryStateSpec) -> None:
        super().__init__(coordinator)
        self._spec = spec
        self._attr_unique_id = f"{coordinator.address}_{spec.key}"
        self._attr_name = spec.name
        self._attr_device_class = spec.device_class
        self._attr_entity_category = spec.entity_category

    @property
    def is_on(self) -> bool | None:
        """Return mapped binary state from raw C20 enum text."""
        raw = getattr(self.coordinator.state, self._spec.raw_attr)
        return self._spec.state_fn(raw)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose raw enum text for debugging and automations."""
        return {"raw_state": getattr(self.coordinator.state, self._spec.raw_attr)}
