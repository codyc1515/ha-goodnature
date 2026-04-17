"""Buttons for Goodnature BLE."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GoodnatureRuntimeData
from .entity import GoodnatureEntity

MODEL_C20 = "c20"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Goodnature button entities."""
    runtime: GoodnatureRuntimeData = entry.runtime_data
    coordinator = runtime.coordinator

    async_add_entities(
        [
            GoodnatureActionButton(
                coordinator,
                key="poll_now",
                name="Poll Now",
                icon="mdi:refresh",
                action=coordinator.async_manual_poll,
                entity_category=EntityCategory.DIAGNOSTIC,
            ),
            GoodnatureActionButton(
                coordinator,
                key="test_fire",
                name="Test Fire",
                icon="mdi:flash",
                action=coordinator.async_test_fire,
                available_fn=lambda: coordinator.state.model_family == MODEL_C20,
            ),
        ]
    )


class GoodnatureActionButton(GoodnatureEntity, ButtonEntity):
    """A button that executes a coordinator action."""

    def __init__(
        self,
        coordinator,
        *,
        key: str,
        name: str,
        icon: str,
        action: Callable[[], Awaitable[bool]],
        entity_category: EntityCategory = EntityCategory.CONFIG,
        available_fn: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._action = action
        self._available_fn = available_fn
        self._attr_unique_id = f"{coordinator.address}_{key}"
        self._attr_name = name
        self._attr_icon = icon
        self._attr_entity_category = entity_category

    @property
    def available(self) -> bool:
        """Return True when this action is supported for the current trap."""
        if self._available_fn is None:
            return True
        return self._available_fn()

    async def async_press(self) -> None:
        """Handle the button press."""
        await self._action()
