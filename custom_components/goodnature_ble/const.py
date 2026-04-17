"""Constants for the Goodnature BLE integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "goodnature_ble"

PLATFORMS = ["binary_sensor", "sensor", "button"]

CONF_ADDRESS = "address"
CONF_NAME = "name"

DEFAULT_NAME = "Goodnature Trap"
DEFAULT_COOLDOWN_SECONDS = 23
DEFAULT_ACTIVE_WINDOW_SECONDS = 120
DEFAULT_MIN_PACKETS_FOR_ACTIVATION = 3
DEFAULT_BURST_GAP_SECONDS = 8
DEFAULT_ENABLE_GATT = True
DEFAULT_GATT_MIN_POLL_SECONDS = 120
DEFAULT_WRITE_TIME_ON_CONNECT = True
DEFAULT_SEND_CONTROL_ZERO = False

COORDINATOR_UPDATE_INTERVAL = timedelta(seconds=30)

ATTR_ESTIMATED_ACTIVATIONS = "estimated_activations"
