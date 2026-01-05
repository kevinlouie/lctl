# LightController

Control Ulanzi panel lights over BLE from the command line.

Based on the official Ulanzi app, the following lights may be supported:
- VL-200Bi
- VL-120C
- VL-120Bi
- EC65
- AL60
- AL120
- L149

## Features
- BLE control with power, brightness (0–100), color temperature (2700–6500K), and FX effects (flash/tv/candle/strobe1-3)
- Dry-run by default; add `--execute` to perform real BLE writes
- Built-in scanner to discover nearby Ulanzi lights

## Requirements
- Python 3.10+
- Bluetooth stack that works with [`bleak`](https://github.com/hbldh/bleak) (BlueZ on Linux with `bluetoothd` running)
- BLE adapter with permission to scan/connect

## Installation
### pipx (recommended)
```bash
pipx install .
```

Reinstall after local changes:
```bash
pipx reinstall --force .
```

### Virtualenv
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage
The CLI defaults to dry-run logging. Add `--execute` to send real BLE commands.

- Scan for lights (prints address, name, RSSI):
  ```bash
  lctl scan --timeout 15
  ```

- Turn on/off (on defaults to 80% brightness unless overridden):
  ```bash
  lctl --address AA:BB:CC:DD:EE:FF on
  lctl --address AA:BB:CC:DD:EE:FF --brightness 40 off
  ```

- Set color temperature (optionally set brightness too):
  ```bash
  lctl --address AA:BB:CC:DD:EE:FF --brightness 60 color-temp 3400
  ```

- FX effects (flash, tv, candle, strobe1-3):
  ```bash
  lctl --address AA:BB:CC:DD:EE:FF --execute effect candle
  ```

## BLE protocol (observed)
- Service `0xFFF0`
  - `0xFFF2` write-without-response (control channel)
  - `0xFFF1` read/notify (status; subscription required for 2-way communication)
- Commands used by the controller:
  - Power ON: `FF A1 01 00 00 AA` (single command)
  - Power OFF: `FF A1 01 30 30 AA` then `FF A1 01 01 01 AA` (two-step required)
  - Brightness: `FF A2 01 <level> <level> AA` where `<level>` is decimal 0–100
  - Color temperature: `FF A3 02 <hi> <lo> <checksum> AA` with Kelvin encoded big-endian and checksum = `<hi> + <lo> (mod 256)`
  - FX effects: `FF A1 01 <fx> <fx> AA` with fx codes {flash:0x04, tv:0x05, candle:0x06, strobe1:0x0C, strobe2:0x0D, strobe3:0x0E}

## Notes
- The service keeps a cached on/off state and lazily reconnects through `bleak` when needed.

## Disclaimer
This project is not affiliated with or endorsed by Ulanzi. Use at your own risk.
