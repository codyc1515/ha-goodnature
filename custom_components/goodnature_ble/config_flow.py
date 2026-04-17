"""Config flow for Goodnature BLE."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import (
    CONF_ADDRESS,
    CONF_NAME,
    DEFAULT_NAME,
    DOMAIN,
)
from .protocol import GOODNATURE_DISCOVERY_UUIDS


def _normalized_service_uuids(service_info: BluetoothServiceInfoBleak) -> set[str]:
    """Return service UUIDs in normalized lower-case forms."""
    normalized: set[str] = set()
    for value in service_info.service_uuids:
        uuid = value.strip().lower()
        if not uuid:
            continue
        normalized.add(uuid)

        compact = uuid.replace("-", "")
        if len(compact) == 4:
            normalized.add(f"0000{compact}-0000-1000-8000-00805f9b34fb")
        elif len(compact) == 8:
            normalized.add(f"{compact}-0000-1000-8000-00805f9b34fb")

    return normalized


def _is_goodnature(service_info: BluetoothServiceInfoBleak) -> bool:
    """Return True if this advertisement looks like a Goodnature trap."""
    local_name = (service_info.name or service_info.device.name or "").strip().lower()
    if local_name == "gn":
        return True

    uuids = _normalized_service_uuids(service_info)
    if not uuids.isdisjoint(GOODNATURE_DISCOVERY_UUIDS):
        return True

    # Some packets arrive with only Nordic manufacturer payload + 600D family UUIDs.
    manufacturer_data = service_info.manufacturer_data or {}
    return 0x0059 in manufacturer_data and (
        "0000600d-0000-1000-8000-00805f9b34fb" in uuids
        or "600d" in uuids
        or "0000de11-1212-efde-1523-785fef13d123" in uuids
        or "0000fade-1212-efde-1523-785fef13d123" in uuids
        or "0000e010-1212-efde-1523-785fef13d123" in uuids
    )


class GoodnatureConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Goodnature BLE."""

    VERSION = 1

    _discovered_name: str | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manual setup by address."""
        errors: dict[str, str] = {}

        if user_input is not None:
            address = user_input[CONF_ADDRESS].upper()
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()

            return self.async_create_entry(
                title=user_input.get(CONF_NAME) or DEFAULT_NAME,
                data={
                    CONF_ADDRESS: address,
                    CONF_NAME: user_input.get(CONF_NAME) or DEFAULT_NAME,
                },
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): str,
                vol.Optional(CONF_NAME, default=DEFAULT_NAME): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle bluetooth discovery."""
        if not _is_goodnature(discovery_info):
            return self.async_abort(reason="not_supported")

        address = discovery_info.address.upper()
        await self.async_set_unique_id(address)
        updates: dict[str, Any] | None = None
        if discovery_info.name:
            updates = {CONF_NAME: discovery_info.name}
        self._abort_if_unique_id_configured(updates=updates)

        self.context["title_placeholders"] = {
            "name": discovery_info.name or discovery_info.address
        }
        self._discovered_name = discovery_info.name or DEFAULT_NAME

        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm bluetooth discovery."""
        if user_input is not None:
            assert self.unique_id is not None
            return self.async_create_entry(
                title=self._discovered_name or DEFAULT_NAME,
                data={
                    CONF_ADDRESS: self.unique_id,
                    CONF_NAME: self._discovered_name or DEFAULT_NAME,
                },
            )

        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": self._discovered_name or "GN"},
            data_schema=vol.Schema({}),
        )
