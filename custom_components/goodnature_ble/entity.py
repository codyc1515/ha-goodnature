"""Entity helpers for Goodnature BLE."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GoodnatureCoordinator


class GoodnatureEntity(CoordinatorEntity[GoodnatureCoordinator]):
    """Base entity for Goodnature BLE."""

    _attr_has_entity_name = True

    @property
    def device_info(self) -> DeviceInfo:
        """Return shared device info."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, self.coordinator.address)},
            manufacturer="Goodnature",
            model=self.coordinator.model_name,
            name=self.coordinator.name,
            serial_number=self.coordinator.state.serial_number,
            sw_version=self.coordinator.state.firmware_version,
        )
