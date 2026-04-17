"""The Goodnature BLE integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import PLATFORMS
from .coordinator import GoodnatureCoordinator


@dataclass(slots=True)
class GoodnatureRuntimeData:
    """Runtime data for a config entry."""

    coordinator: GoodnatureCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Goodnature BLE from a config entry."""
    coordinator = GoodnatureCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GoodnatureRuntimeData(
        coordinator=coordinator,
    )
    await hass.config_entries.async_forward_entry_setups(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(
        entry, [Platform(platform) for platform in PLATFORMS]
    )
    if unloaded and entry.runtime_data:
        entry.runtime_data.coordinator.async_shutdown()
    return unloaded
