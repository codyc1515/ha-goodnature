# Goodnature BLE (Home Assistant Custom Integration)

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=codyc1515&repository=ha-goodnature&category=integration)

This integration supports Goodnature BLE traps (A24 Chirp and C20) via:

- passive BLE advertisement tracking, and
- optional active GATT polling/writes.

The goal is to use trap-reported counters when available and only infer from BLE bursts when needed.

## Current status

- Discovery by:
  - local name `GN`
  - `D00D`, `DE11`, `FADE`, `E010` service UUIDs
  - legacy `1234` and `600D` service UUID advertisements (short or expanded form)
- Passive mode works without connecting to the trap.
- GATT/UART mode reads trap counters and metadata.
- Acknowledge/reset actions are implemented from observed device write flows.
- C20 UART session uses a consistent polling flow (version, battery percent, device state, kill history).

## Key terminology

These are the most important semantics.

### Displayed / Read / Pending

For both kills and events the trap exposes two counters:

- `displayed`: how many items the trap says are currently raised/displayed as new.
- `read`: how many items the client has acknowledged/read.
- `pending`: computed by this integration as `max(0, displayed - read)`.

So yes, this creates three related sensors per channel. They are not duplicates:

- `displayed` is trap-side surfaced count.
- `read` is client-side acknowledged pointer.
- `pending` is actionable delta.

### Kill vs Event

- `kill`: strike/kill channel (`D20D` displayed, `D60D` read pointer, `D30D` strike payload).
- `event`: broader event/alert channel (`D2ED` displayed, `D3ED` read pointer, `DEED` payload).

In practice:

- Kills are the trap-hit stream users usually care about first.
- Events appear to include other noteworthy trap/device events.

## Activation counting model

The `Activations` sensor prefers trap counters over inferred bursts:

1. `kill_displayed` if available
2. else `kill_read` if available
3. else fallback to inferred BLE burst count

That means in GATT mode activation total is no longer purely estimated.

## BLE/GATT protocol map (known)

All Goodnature custom UUIDs use base suffix:

- `-1212-efde-1523-785fef13d123`

### Device info/service area

- `DE11` service root (device information service area)
- `DE12` serial number (string)
- `DE13` device control (u8 observed; writable)
- `DE14` device state raw
- `DE15` firmware/version text
- `DE18` unknown raw (captured)

### Kill channel

- `D00D` service root
- `D20D` kill displayed counter
- `D30D` kill/strike payload
- `D50D` kill state raw
- `D60D` kill read pointer (writable)

### Event channel

- `D2ED` event displayed counter
- `DEED` event payload raw
- `D3ED` event read pointer (writable)

### Battery / telemetry

- `FADE` unknown raw
- `FAD1` battery voltage raw
- `FAD2` battery internal resistance raw
- `FAD3` unknown battery-related raw

### Time

- `F1AE` time service
- `F1AF` time characteristic (writable, little-endian minutes)

### Unknown/diagnostic

- `E010` unknown service root (observed on C20-like profiles)
- `E771`, `E772`, `E773` unknown raw (E771 appears writable in protocol docs)

## Observed write flows (action perspective)

These flows are based on observed device/client behavior.

### A24

- On connect/sync start:
  - writes current time to `F1AF` (`write_time_on_connect` option)
- Control write path:
  - `DE13 = 0x00` exists as a control action path
- Acknowledge/reset path:
  - optional write to `D60D` (kill read pointer)
  - optional write to `D3ED` (event read pointer)
  - then `DE13 = 0x02`

### C20

C20 uses Nordic UART writes (`6E400002`) with framed command packets, including:

- set time
- get version/state/battery/history
- set command (`FIRE`, `CLEAR`, etc)

Memfault diagnostics also write to a separate characteristic (`54220005...`).

This integration now follows the same C20 connect/poll sequence seen in `btsnoop` captures:

- subscribe notify on `6E400003`
- set time (`type 0x04`, subtype `0x02`)
- request firmware (`0x08`), battery (`0x11`), device state (`0x10`), kill history (`0x14`)
- parse responses (`subtype 0x01`)

From `btsnoop2` (triggering a strike/test-fire action), the flow is:

- write `SetCommandRequest(FIRE)` (`type 0x12`, `subtype 0x02`, payload `0x3E9130BE`)
- receive command response (`type 0x12`, `subtype 0x01`)
- receive `DeviceStateResponse` (`0x10`) and `StrikerEvent` (`0x31`)

The `StrikerEvent` includes source (`TRIGGER` vs `USER`) and timing metrics; `USER` indicates manual test fire.

## Entities exposed

### Binary sensor

- `Charge` (battery_charging class)
- `Battery` (problem class)
- `Trap Occupancy` (occupancy class)
- `USB` (connectivity class)
- `Tray` (opening class)

### Core sensors

- `Activations` (counter with source precedence above)
- `Signal Strength`

### Trap-state sensors (GATT-backed when available)

- `Serial Number`
- `Firmware Version`
- `Battery` (C20 percent, trap-reported)
- `Last Kill At` (C20 kill history-derived, diagnostic)
- `Device` (C20, diagnostic)

### Buttons

- `Poll Now`
- `Test Fire`

`Test Fire` uses model-specific write paths:

- C20:
  - Nordic UART write to `6E400002...`
  - framed `SetCommandRequest(FIRE)` packet (command id `1049702590`)

`Test Fire` is currently exposed only for C20 devices. A24 manual test-fire behavior has not been confirmed sufficiently to expose safely.

## Options

No user-configurable runtime options are currently exposed. The integration uses built-in defaults.

## Known caveats

- Chirp is a sleepy BLE device; proximity matters.
- Manual wake/shake can resemble real strikes in passive mode.
- Some fields remain unknown/partially understood; unknown raws are exposed for analysis.
- Not all protocol paths are guaranteed stable across firmware versions.
- App UI items like donut life and full activity timeline likely include cloud/server data; BLE alone may not provide all of those derived values.

## Installation

1. Copy `custom_components/goodnature_ble` into your Home Assistant config directory.
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration**.
4. Add **Goodnature BLE**.
5. If not auto-discovered, enter trap Bluetooth MAC manually.

## Recommended topology

Use ESPHome Bluetooth proxies near remote traps for reliable range and better packet capture.
