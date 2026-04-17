"""Coordinator for Goodnature BLE trap data."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from homeassistant.components.bluetooth import (
    BluetoothChange,
    BluetoothCallbackMatcher,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
    async_ble_device_from_address,
    async_register_callback,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ADDRESS,
    CONF_NAME,
    COORDINATOR_UPDATE_INTERVAL,
    DEFAULT_ACTIVE_WINDOW_SECONDS,
    DEFAULT_BURST_GAP_SECONDS,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_ENABLE_GATT,
    DEFAULT_GATT_MIN_POLL_SECONDS,
    DEFAULT_MIN_PACKETS_FOR_ACTIVATION,
    DEFAULT_NAME,
    DEFAULT_SEND_CONTROL_ZERO,
    DEFAULT_WRITE_TIME_ON_CONNECT,
)
from .protocol import (
    UUIDS,
    decode_text,
    gn_uuid,
    parse_d30d,
    parse_u16_le,
    parse_u8,
)

try:
    from bleak import BleakClient
except ImportError:  # pragma: no cover - Home Assistant normally provides bleak
    BleakClient = None

try:
    from bleak_retry_connector import establish_connection
except ImportError:  # pragma: no cover - local dev env may not include HA deps
    establish_connection = None

_LOGGER = logging.getLogger(__name__)
U = UUIDS
MODEL_UNKNOWN = "unknown"
MODEL_A24 = "a24"
MODEL_C20 = "c20"
NORDIC_UART_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_UART_RX_UUID = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NORDIC_UART_TX_UUID = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
MEMFAULT_DIAG_SERVICE_UUID = "54220000-f6a5-4007-a371-722f4ebd8436"
MFG_GOODNATURE = 0x0059
C20_COMMAND_FIRE = 1049702590
C20_TYPE_SET_TIME = 0x04
C20_TYPE_FIRMWARE = 0x08
C20_TYPE_DEVICE_STATE = 0x10
C20_TYPE_BATTERY_LEVEL = 0x11
C20_TYPE_SET_COMMAND = 0x12
C20_TYPE_KILL_HISTORY = 0x14
C20_TYPE_STRIKER_EVENT = 0x31
C20_REQUEST_SUBTYPE = 0x00
C20_RESPONSE_SUBTYPE = 0x01
C20_SET_TIME_SUBTYPE = 0x02
C20_UART_TIMEOUT_SECONDS = 4.0
C20_DEVICE_STATE = ("DEACTIVATED", "ACTIVATED", "ERROR", "NO_DATA")
C20_KILL_STATE = ("CLEARED", "DETECTED", "NO_DATA")
C20_TRAY_STATE = ("OPEN", "CLOSED", "UNKNOWN", "NO_DATA")
C20_BATTERY_STATE = ("STARTUP", "NOT_CONNECTED", "CRITICAL", "LOW", "NORMAL", "NO_DATA")
C20_CHARGE_STATE = ("NOT_CHARGING", "CHARGING", "NO_DATA")
C20_USB_STATE = ("DISCONNECTED", "CONNECTED", "NO_DATA")
C20_STRIKER_SOURCE = ("TRIGGER", "UNUSED", "USER", "NO_DATA")


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


@dataclass(slots=True)
class TrapState:
    """Current observed state from BLE advertisements and GATT reads."""

    address: str
    name: str
    last_seen: datetime | None = None
    last_activation: datetime | None = None
    burst_started: datetime | None = None
    last_packet: datetime | None = None
    packets_in_burst: int = 0
    last_rssi: int | None = None
    estimated_activations: int = 0
    activation_counted_for_burst: bool = False
    model_family: str = MODEL_UNKNOWN
    model_source: str | None = None

    # GATT-backed values
    serial_number: str | None = None
    firmware_version: str | None = None
    device_control: int | None = None
    device_state_raw: int | None = None
    kill_displayed: int | None = None
    kill_read: int | None = None
    kill_state_raw: int | None = None
    event_displayed: int | None = None
    event_read: int | None = None
    event_data_raw: str | None = None
    dead_raw: str | None = None
    fade_raw: str | None = None
    fad1_raw: int | None = None
    fad2_raw: int | None = None
    fad3_raw: int | None = None
    e771_raw: str | None = None
    e772_raw: str | None = None
    e773_raw: str | None = None
    d30d_raw: str | None = None
    last_strike_id: int | None = None
    last_strike_at: datetime | None = None
    last_strike_flags: int | None = None
    c20_battery_percent: int | None = None
    c20_device_time: datetime | None = None
    c20_last_kill_at: datetime | None = None
    c20_strike_count: int | None = None
    c20_device_state: str | None = None
    c20_kill_state: str | None = None
    c20_tray_state: str | None = None
    c20_battery_state: str | None = None
    c20_charge_state: str | None = None
    c20_usb_state: str | None = None
    c20_kill_history_raw: str | None = None
    c20_last_striker_source: str | None = None
    c20_last_trigger_number: int | None = None
    c20_last_fire_time_ms: int | None = None
    c20_last_rewind_time_ms: int | None = None
    c20_last_backdrive_time_ms: int | None = None

    last_gatt_poll: datetime | None = None
    gatt_failures: int = 0
    last_gatt_error: str | None = None


class GoodnatureCoordinator(DataUpdateCoordinator[None]):
    """Track BLE advertisements and infer trap activations."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize coordinator."""
        self.config_entry = config_entry

        self.address: str = config_entry.data[CONF_ADDRESS].upper()
        self.name: str = config_entry.data.get(CONF_NAME, DEFAULT_NAME)

        self.cooldown = timedelta(seconds=DEFAULT_COOLDOWN_SECONDS)
        self.active_window = timedelta(seconds=DEFAULT_ACTIVE_WINDOW_SECONDS)
        self.burst_gap = timedelta(seconds=DEFAULT_BURST_GAP_SECONDS)
        self.min_packets_for_activation: int = DEFAULT_MIN_PACKETS_FOR_ACTIVATION
        self.enable_gatt: bool = DEFAULT_ENABLE_GATT
        self.gatt_min_poll = timedelta(seconds=DEFAULT_GATT_MIN_POLL_SECONDS)
        self.write_time_on_connect: bool = DEFAULT_WRITE_TIME_ON_CONNECT
        self.send_control_zero: bool = DEFAULT_SEND_CONTROL_ZERO

        self.state = TrapState(address=self.address, name=self.name)
        self._unsubscribe_ble: CALLBACK_TYPE | None = None
        self._gatt_lock = asyncio.Lock()
        self._gatt_task: asyncio.Task | None = None

        super().__init__(
            hass,
            logger=_LOGGER,
            name=self.name,
            update_interval=COORDINATOR_UPDATE_INTERVAL,
        )

    async def _async_update_data(self) -> None:
        """No polling endpoint; BLE callbacks update state in near real-time."""
        if self._unsubscribe_ble is None:
            self._unsubscribe_ble = async_register_callback(
                self.hass,
                self._async_handle_ble,
                BluetoothCallbackMatcher(address=self.address),
                BluetoothScanningMode.ACTIVE,
            )

        if self._should_poll_gatt(dt_util.utcnow()):
            self._schedule_gatt_refresh(reason="periodic")

    @callback
    def _async_handle_ble(
        self,
        service_info: BluetoothServiceInfoBleak,
        change: BluetoothChange,
    ) -> None:
        """Handle incoming BLE advertisements for this device."""
        del change
        now = dt_util.utcnow()
        state = self.state

        self._update_model_from_advertisement(service_info)

        state.last_seen = now
        state.last_rssi = service_info.rssi

        if state.last_packet is None or (now - state.last_packet) > self.burst_gap:
            state.burst_started = now
            state.packets_in_burst = 1
            state.activation_counted_for_burst = False
        else:
            state.packets_in_burst += 1

        state.last_packet = now

        if (
            not state.activation_counted_for_burst
            and state.packets_in_burst >= self.min_packets_for_activation
            and (
                state.last_activation is None
                or (now - state.last_activation) >= self.cooldown
            )
        ):
            state.last_activation = now
            state.estimated_activations += 1
            state.activation_counted_for_burst = True
            if self._should_poll_gatt(now):
                self._schedule_gatt_refresh(reason="activation")

        self.async_set_updated_data(None)

    def _update_model_from_advertisement(
        self, service_info: BluetoothServiceInfoBleak
    ) -> None:
        """Infer model family from BLE advertisement shape."""
        service_uuids = _normalized_service_uuids(service_info)
        has_a24_markers = gn_uuid("D00D") in service_uuids or gn_uuid("D2ED") in service_uuids

        # C20-like profile from observed service patterns and on-device captures.
        if (
            gn_uuid("E010") in service_uuids
            or NORDIC_UART_SERVICE_UUID in service_uuids
            or MEMFAULT_DIAG_SERVICE_UUID in service_uuids
        ):
            self._set_model_family(MODEL_C20, source="service_profile")
            return

        # Only mark A24 when A24-specific channels are visible.
        if has_a24_markers:
            self._set_model_family(MODEL_A24, source="service_profile")
            return

        # Heuristic fallback: C20-like payload shape seen on GN advertisements.
        manufacturer_data = service_info.manufacturer_data or {}
        mfg_payload = manufacturer_data.get(MFG_GOODNATURE)
        if mfg_payload and len(mfg_payload) >= 4 and not self.state.serial_number:
            # C20 scanner heuristic derives serial from the first 4 bytes as LE uint32.
            self.state.serial_number = f"{int.from_bytes(mfg_payload[:4], 'little'):08X}"

        if (
            mfg_payload
            and len(mfg_payload) == 9
            and not has_a24_markers
            and "0000600d-0000-1000-8000-00805f9b34fb" in service_uuids
        ):
            self._set_model_family(MODEL_C20, source="manufacturer_shape")
            return

        # Keep unknown unless explicit A24/C20 markers are observed.

    def _set_model_family(self, family: str, *, source: str) -> None:
        """Set model family with conservative precedence.

        We only overwrite unknown with a known family. Once known, keep stable.
        """
        if self.state.model_family == family:
            self.state.model_source = source
            return

        if self.state.model_family != MODEL_UNKNOWN:
            return

        self.state.model_family = family
        self.state.model_source = source

    def _should_poll_gatt(self, now: datetime) -> bool:
        """Return True when we should refresh GATT values."""
        if not self.enable_gatt or BleakClient is None:
            return False

        if self._gatt_lock.locked():
            return False

        if self.state.last_seen is None:
            return False

        # Prefer polling while device is actively advertising.
        if now - self.state.last_seen > self.active_window:
            return False

        if self.state.last_gatt_poll is None:
            return True

        return now - self.state.last_gatt_poll >= self.gatt_min_poll

    @callback
    def _schedule_gatt_refresh(self, reason: str) -> None:
        """Start a GATT refresh task if one is not already running."""
        if self._gatt_task and not self._gatt_task.done():
            return

        self._gatt_task = self.hass.async_create_task(self._async_refresh_gatt(reason))

    async def _async_refresh_gatt(self, reason: str) -> None:
        """Connect to the trap and read known characteristics."""
        if BleakClient is None:
            return

        async with self._gatt_lock:
            ble_device = self._async_resolve_ble_device()
            if ble_device is None:
                self.state.gatt_failures += 1
                self.state.last_gatt_error = "BLE device not available"
                self.async_set_updated_data(None)
                return

            client: BleakClient | None = None
            try:
                client = await self._async_connect_client(ble_device)

                if self.state.model_family == MODEL_C20:
                    await self._async_refresh_c20_uart(client)
                else:
                    # A24 app behavior writes current time in minutes to F1AF.
                    if self.write_time_on_connect:
                        minutes = int(dt_util.utcnow().timestamp() // 60)
                        await self._async_write_char(
                            client,
                            U["F1AF"],
                            minutes.to_bytes(4, byteorder="little", signed=False),
                        )

                    if self.send_control_zero:
                        await self._async_write_char(client, U["DE13"], b"\x00")

                    await self._async_read_known_chars(client)

                self.state.last_gatt_poll = dt_util.utcnow()
                self.state.last_gatt_error = None
                self.logger.debug("GATT refresh successful for %s (%s)", self.address, reason)
            except Exception as err:  # noqa: BLE001
                self.state.gatt_failures += 1
                self.state.last_gatt_error = str(err)
                self.logger.debug(
                    "GATT refresh failed for %s (%s): %s",
                    self.address,
                    reason,
                    err,
                )
            finally:
                if client and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:  # noqa: BLE001
                        self.logger.debug(
                            "Disconnect failed for %s (%s): %s",
                            self.address,
                            reason,
                            err,
                        )
                self.async_set_updated_data(None)

    async def async_manual_poll(self) -> bool:
        """Manually poll GATT once."""
        if BleakClient is None:
            return False
        await self._async_refresh_gatt("manual")
        return self.state.last_gatt_error is None

    async def async_acknowledge_kills(self) -> bool:
        """Set kill read pointer to the current displayed count."""
        return await self._async_acknowledge_alerts(ack_kills=True, ack_events=False)

    async def async_acknowledge_events(self) -> bool:
        """Set event read pointer to the current displayed count."""
        return await self._async_acknowledge_alerts(ack_kills=False, ack_events=True)

    async def async_reset_alert(self) -> bool:
        """Acknowledge both kill and event alerts using the known write flow."""
        return await self._async_acknowledge_alerts(ack_kills=True, ack_events=True)

    async def async_test_fire(self) -> bool:
        """Trigger trap test fire using known command paths."""
        if BleakClient is None:
            return False

        if self.state.model_family != MODEL_C20:
            self.state.last_gatt_error = "Test fire is only supported for C20"
            self.async_set_updated_data(None)
            return False

        async with self._gatt_lock:
            ble_device = self._async_resolve_ble_device()
            if ble_device is None:
                self.state.gatt_failures += 1
                self.state.last_gatt_error = "BLE device not available"
                self.async_set_updated_data(None)
                return False

            client: BleakClient | None = None
            try:
                client = await self._async_connect_client(ble_device)
                queue: asyncio.Queue[bytes] = asyncio.Queue()

                def _on_notify(_: int, data: bytearray) -> None:
                    queue.put_nowait(bytes(data))

                await client.start_notify(NORDIC_UART_TX_UUID, _on_notify)
                command_response = await self._async_c20_request_response(
                    client,
                    queue,
                    C20_TYPE_SET_COMMAND,
                    payload=C20_COMMAND_FIRE.to_bytes(4, byteorder="little", signed=False),
                    request_subtype=C20_SET_TIME_SUBTYPE,
                )
                ok = command_response is not None

                if not ok:
                    self.state.gatt_failures += 1
                    self.state.last_gatt_error = "Test fire command failed"
                    self.async_set_updated_data(None)
                    return False

                # Allow immediate async event notifications (state/striker event) to land.
                end_at = self.hass.loop.time() + 2.0
                while True:
                    remaining = end_at - self.hass.loop.time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(queue.get(), timeout=remaining)
                    except asyncio.TimeoutError:
                        break
                    decoded = self._decode_c20_frame(raw)
                    if decoded is None:
                        continue
                    message_type, subtype, payload = decoded
                    if subtype != C20_RESPONSE_SUBTYPE:
                        continue
                    if message_type == C20_TYPE_DEVICE_STATE:
                        self._apply_c20_device_state(payload)
                    elif message_type == C20_TYPE_STRIKER_EVENT:
                        self._apply_c20_striker_event(payload)

                self.state.last_gatt_poll = dt_util.utcnow()
                self.state.last_gatt_error = None
                self.async_set_updated_data(None)
                return True
            except Exception as err:  # noqa: BLE001
                self.state.gatt_failures += 1
                self.state.last_gatt_error = str(err)
                self.async_set_updated_data(None)
                return False
            finally:
                if client and client.is_connected:
                    try:
                        await client.stop_notify(NORDIC_UART_TX_UUID)
                    except Exception:  # noqa: BLE001
                        pass
                if client and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:  # noqa: BLE001
                        self.logger.debug(
                            "Disconnect failed for %s (test_fire): %s",
                            self.address,
                            err,
                        )

    async def _async_acknowledge_alerts(
        self,
        *,
        ack_kills: bool,
        ack_events: bool,
    ) -> bool:
        """Acknowledge alert channels and write control 0x02."""
        if BleakClient is None:
            return False

        async with self._gatt_lock:
            ble_device = self._async_resolve_ble_device()
            if ble_device is None:
                self.state.gatt_failures += 1
                self.state.last_gatt_error = "BLE device not available"
                self.async_set_updated_data(None)
                return False

            client: BleakClient | None = None
            try:
                client = await self._async_connect_client(ble_device)

                kill_displayed = parse_u16_le(await self._async_read_char(client, U["D20D"]))
                event_displayed = parse_u16_le(await self._async_read_char(client, U["D2ED"]))

                if ack_kills and kill_displayed is not None:
                    await self._async_write_char(
                        client,
                        U["D60D"],
                        kill_displayed.to_bytes(2, byteorder="little", signed=False),
                    )
                    self.state.kill_read = kill_displayed

                if ack_events and event_displayed is not None:
                    await self._async_write_char(
                        client,
                        U["D3ED"],
                        event_displayed.to_bytes(2, byteorder="little", signed=False),
                    )
                    self.state.event_read = event_displayed

                await self._async_write_char(client, U["DE13"], b"\x02")
                await self._async_read_known_chars(client)

                self.state.last_gatt_poll = dt_util.utcnow()
                self.state.last_gatt_error = None
                self.async_set_updated_data(None)
                return True
            except Exception as err:  # noqa: BLE001
                self.state.gatt_failures += 1
                self.state.last_gatt_error = str(err)
                self.async_set_updated_data(None)
                return False
            finally:
                if client and client.is_connected:
                    try:
                        await client.disconnect()
                    except Exception as err:  # noqa: BLE001
                        self.logger.debug(
                            "Disconnect failed for %s (acknowledge): %s",
                            self.address,
                            err,
                        )

    async def _async_connect_client(self, ble_device):
        """Establish a BLE connection using HA-recommended retry connector."""
        if BleakClient is None:
            raise RuntimeError("bleak is not available")

        if establish_connection is not None:
            return await establish_connection(
                BleakClient,
                ble_device,
                self.name,
                max_attempts=3,
            )

        client = BleakClient(ble_device, timeout=10.0)
        await client.connect()
        return client

    async def _async_read_known_chars(self, client: BleakClient) -> None:
        """Read and parse known characteristics."""
        de12 = await self._async_read_char(client, U["DE12"])
        de13 = await self._async_read_char(client, U["DE13"])
        de14 = await self._async_read_char(client, U["DE14"])
        de15 = await self._async_read_char(client, U["DE15"])
        de18 = await self._async_read_char(client, U["DE18"])

        d20d = await self._async_read_char(client, U["D20D"])
        d30d = await self._async_read_char(client, U["D30D"])
        d50d = await self._async_read_char(client, U["D50D"])
        d60d = await self._async_read_char(client, U["D60D"])

        d2ed = await self._async_read_char(client, U["D2ED"])
        deed = await self._async_read_char(client, U["DEED"])
        d3ed = await self._async_read_char(client, U["D3ED"])

        fad1 = await self._async_read_char(client, U["FAD1"])
        fad2 = await self._async_read_char(client, U["FAD2"])
        fad3 = await self._async_read_char(client, U["FAD3"])
        fade = await self._async_read_char(client, U["FADE"])

        e771 = await self._async_read_char(client, U["E771"])
        e772 = await self._async_read_char(client, U["E772"])
        e773 = await self._async_read_char(client, U["E773"])

        self.state.serial_number = decode_text(de12)
        self.state.device_control = parse_u8(de13)
        self.state.device_state_raw = parse_u16_le(de14)
        self.state.firmware_version = decode_text(de15)
        self.state.dead_raw = de18.hex() if de18 else None

        self.state.kill_displayed = parse_u16_le(d20d)
        self.state.kill_state_raw = parse_u16_le(d50d)
        self.state.kill_read = parse_u16_le(d60d)

        self.state.event_displayed = parse_u16_le(d2ed)
        self.state.event_data_raw = deed.hex() if deed else None
        self.state.event_read = parse_u16_le(d3ed)

        self.state.fad1_raw = parse_u16_le(fad1)
        self.state.fad2_raw = parse_u16_le(fad2)
        self.state.fad3_raw = parse_u16_le(fad3)
        self.state.fade_raw = fade.hex() if fade else None

        self.state.e771_raw = e771.hex() if e771 else None
        self.state.e772_raw = e772.hex() if e772 else None
        self.state.e773_raw = e773.hex() if e773 else None

        d30d_raw, strike_id, strike_at, strike_flags = parse_d30d(d30d)
        self.state.d30d_raw = d30d_raw
        self.state.last_strike_id = strike_id
        self.state.last_strike_at = strike_at
        self.state.last_strike_flags = strike_flags

        if d20d is not None or d30d is not None or d60d is not None:
            self._set_model_family(MODEL_A24, source="gatt_poll")

    async def _async_read_char(self, client: BleakClient, uuid: str) -> bytes | None:
        """Read a characteristic safely."""
        try:
            return await client.read_gatt_char(uuid)
        except Exception:  # noqa: BLE001
            return None

    def _decode_c20_frame(self, frame: bytes) -> tuple[int, int, bytes] | None:
        """Decode a single C20 UART frame into (message_type, subtype, payload)."""
        if len(frame) < 6 or frame[0] != 0xB0 or frame[-1] != 0xB1:
            return None

        escaped = frame[1:-1]
        body = bytearray()
        index = 0
        while index < len(escaped):
            value = escaped[index]
            if value == 0xB2:
                index += 1
                if index >= len(escaped):
                    return None
                body.append(escaped[index] ^ 0x04)
            else:
                body.append(value)
            index += 1

        if len(body) < 4:
            return None

        payload_and_header = bytes(body[:-2])
        crc_expected = int.from_bytes(body[-2:], byteorder="little", signed=False)
        crc_calculated = self._crc16_ccitt(payload_and_header)
        if crc_expected != crc_calculated:
            return None

        message_type = payload_and_header[0]
        subtype = payload_and_header[1]
        payload = payload_and_header[2:]
        return message_type, subtype, payload

    def _encode_c20_message(self, message_type: int, subtype: int, payload: bytes = b"") -> bytes:
        """Encode a C20 UART message frame."""
        body = bytes([message_type, subtype]) + payload
        crc = self._crc16_ccitt(body).to_bytes(2, byteorder="little", signed=False)
        escaped = bytearray()
        for value in body + crc:
            if value in (0xB0, 0xB1):
                escaped.append(0xB2)
                escaped.append(value ^ 0x04)
            else:
                escaped.append(value)
        return bytes([0xB0]) + bytes(escaped) + bytes([0xB1])

    async def _async_c20_request_response(
        self,
        client: BleakClient,
        queue: asyncio.Queue[bytes],
        message_type: int,
        *,
        payload: bytes = b"",
        request_subtype: int = C20_REQUEST_SUBTYPE,
        timeout: float = C20_UART_TIMEOUT_SECONDS,
    ) -> bytes | None:
        """Send one C20 UART request and wait for matching response payload."""
        request = self._encode_c20_message(message_type, request_subtype, payload)
        if not await self._async_write_char(client, NORDIC_UART_RX_UUID, request):
            return None

        end_at = self.hass.loop.time() + timeout
        while True:
            remaining = end_at - self.hass.loop.time()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

            decoded = self._decode_c20_frame(raw)
            if decoded is None:
                continue
            response_type, response_subtype, response_payload = decoded
            if response_type == message_type and response_subtype == C20_RESPONSE_SUBTYPE:
                return response_payload

            # Keep state in sync when asynchronous striker events arrive.
            if response_type == C20_TYPE_DEVICE_STATE and response_subtype == C20_RESPONSE_SUBTYPE:
                self._apply_c20_device_state(response_payload)
            elif response_type == C20_TYPE_STRIKER_EVENT and response_subtype == C20_RESPONSE_SUBTYPE:
                self._apply_c20_striker_event(response_payload)

    def _enum_at(self, values: tuple[str, ...], index: int) -> str:
        """Return enum label for index with NO_DATA fallback."""
        if 0 <= index < len(values):
            return values[index]
        return values[-1]

    def _parse_u32_le(self, payload: bytes, offset: int) -> int | None:
        """Parse little-endian uint32 from payload offset."""
        end = offset + 4
        if len(payload) < end:
            return None
        return int.from_bytes(payload[offset:end], byteorder="little", signed=False)

    def _u32_to_datetime(self, value: int | None) -> datetime | None:
        """Convert unix seconds to UTC datetime."""
        if value is None or value <= 0:
            return None
        return dt_util.utc_from_timestamp(value)

    def _apply_c20_device_state(self, payload: bytes) -> None:
        """Apply DeviceStateResponse payload (type 0x10)."""
        if len(payload) < 14:
            return
        state_time = self._parse_u32_le(payload, 0)
        self.state.c20_device_time = self._u32_to_datetime(state_time)
        self.state.c20_device_state = self._enum_at(C20_DEVICE_STATE, payload[4])
        self.state.c20_kill_state = self._enum_at(C20_KILL_STATE, payload[5])
        self.state.c20_tray_state = self._enum_at(C20_TRAY_STATE, payload[6])
        self.state.c20_battery_state = self._enum_at(C20_BATTERY_STATE, payload[7])
        self.state.c20_charge_state = self._enum_at(C20_CHARGE_STATE, payload[8])
        self.state.c20_usb_state = self._enum_at(C20_USB_STATE, payload[9])
        self.state.c20_strike_count = self._parse_u32_le(payload, 10)

    def _apply_c20_battery_level(self, payload: bytes) -> None:
        """Apply BatteryLevelResponse payload (type 0x11)."""
        if len(payload) < 5:
            return
        state_time = self._parse_u32_le(payload, 0)
        self.state.c20_device_time = self._u32_to_datetime(state_time)
        self.state.c20_battery_percent = payload[4]

    def _apply_c20_kill_history(self, payload: bytes) -> None:
        """Apply KillHistoryResponse payload (type 0x14)."""
        if len(payload) < 40:
            return
        times: list[int] = []
        for offset in range(0, 40, 4):
            value = self._parse_u32_le(payload, offset)
            if value:
                times.append(value)
        self.state.c20_kill_history_raw = payload.hex()
        self.state.c20_last_kill_at = self._u32_to_datetime(max(times) if times else None)

    def _apply_c20_striker_event(self, payload: bytes) -> None:
        """Apply StrikerEvent payload (type 0x31)."""
        if len(payload) < 21:
            return
        event_time = self._parse_u32_le(payload, 0)
        strike_count = self._parse_u32_le(payload, 4)
        source_idx = payload[8]
        trigger_number = self._parse_u32_le(payload, 9)
        fire_time_ms = self._parse_u32_le(payload, 13)
        rewind_time_ms = self._parse_u32_le(payload, 17)
        backdrive_time_ms = self._parse_u32_le(payload, 25) if len(payload) >= 29 else None

        event_dt = self._u32_to_datetime(event_time)
        self.state.c20_device_time = event_dt or self.state.c20_device_time
        self.state.c20_strike_count = strike_count or self.state.c20_strike_count
        self.state.c20_last_kill_at = event_dt or self.state.c20_last_kill_at
        self.state.last_activation = event_dt or self.state.last_activation
        self.state.last_strike_at = event_dt or self.state.last_strike_at
        self.state.c20_last_striker_source = self._enum_at(C20_STRIKER_SOURCE, source_idx)
        self.state.c20_last_trigger_number = trigger_number
        self.state.c20_last_fire_time_ms = fire_time_ms
        self.state.c20_last_rewind_time_ms = rewind_time_ms
        self.state.c20_last_backdrive_time_ms = backdrive_time_ms

    async def _async_refresh_c20_uart(self, client: BleakClient) -> None:
        """Refresh C20 telemetry using the observed UART request/response flow."""
        queue: asyncio.Queue[bytes] = asyncio.Queue()

        def _on_notify(_: int, data: bytearray) -> None:
            queue.put_nowait(bytes(data))

        await client.start_notify(NORDIC_UART_TX_UUID, _on_notify)
        try:
            if self.write_time_on_connect:
                now_seconds = int(dt_util.utcnow().timestamp())
                await self._async_c20_request_response(
                    client,
                    queue,
                    C20_TYPE_SET_TIME,
                    payload=now_seconds.to_bytes(4, byteorder="little", signed=False),
                    request_subtype=C20_SET_TIME_SUBTYPE,
                )

            firmware = await self._async_c20_request_response(client, queue, C20_TYPE_FIRMWARE)
            if firmware is not None:
                self.state.firmware_version = firmware.split(b"\x00", 1)[0].decode(
                    "utf-8",
                    errors="ignore",
                ) or self.state.firmware_version

            battery = await self._async_c20_request_response(client, queue, C20_TYPE_BATTERY_LEVEL)
            if battery is not None:
                self._apply_c20_battery_level(battery)

            device_state = await self._async_c20_request_response(client, queue, C20_TYPE_DEVICE_STATE)
            if device_state is not None:
                self._apply_c20_device_state(device_state)

            kill_history = await self._async_c20_request_response(client, queue, C20_TYPE_KILL_HISTORY)
            if kill_history is not None:
                self._apply_c20_kill_history(kill_history)
        finally:
            try:
                await client.stop_notify(NORDIC_UART_TX_UUID)
            except Exception:  # noqa: BLE001
                pass

    async def _async_write_char(self, client: BleakClient, uuid: str, value: bytes) -> bool:
        """Write a characteristic safely."""
        try:
            await client.write_gatt_char(uuid, value, response=True)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _encode_c20_set_command(self, command_u32: int) -> bytes:
        """Encode C20 Nordic UART SetCommandRequest frame from the current protocol."""
        return self._encode_c20_message(
            0x12,
            0x02,
            command_u32.to_bytes(4, byteorder="little", signed=False),
        )

    def _crc16_ccitt(self, data: bytes) -> int:
        """CRC16-CCITT (poly 0x1021, init 0xFFFF)."""
        crc = 0xFFFF
        for value in data:
            work = (value & 0xFF) << 8
            for _ in range(8):
                bit = (crc ^ work) & 0x8000
                crc = (crc << 1) & 0xFFFF
                if bit:
                    crc ^= 0x1021
                work = (work << 1) & 0xFFFF
        return crc & 0xFFFF

    def _async_resolve_ble_device(self):
        """Resolve current BLE device record by address."""
        ble_device = async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=True,
        )
        if ble_device is not None:
            return ble_device

        return async_ble_device_from_address(
            self.hass,
            self.address,
            connectable=False,
        )

    @property
    def ble_device_name(self) -> str | None:
        """Try to resolve the live BLE device name."""
        device = async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device and device.name:
            return device.name
        return None

    @property
    def model_name(self) -> str:
        """User-facing model label."""
        if self.state.model_family == MODEL_C20:
            return "C20"
        if self.state.model_family == MODEL_A24:
            return "A24 Chirp"
        return "Unknown"

    def async_shutdown(self) -> None:
        """Unregister callbacks."""
        if self._unsubscribe_ble:
            self._unsubscribe_ble()
            self._unsubscribe_ble = None

        if self._gatt_task and not self._gatt_task.done():
            self._gatt_task.cancel()
            self._gatt_task = None
