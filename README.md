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
- `auto` mode: turns the light on while you're in a call (camera in use, a meeting app's mic open, or a Zoom/Google Meet process) and off afterwards
- `detect` command: prints what call detection currently sees, without touching the light

## Requirements
- Python 3.10+
- Bluetooth stack that works with [`bleak`](https://github.com/hbldh/bleak) (BlueZ on Linux with `bluetoothd` running)
- BLE adapter with permission to scan/connect
- For camera/mic call detection (optional): Linux with `/proc`, and PipeWire with `pactl` (`pipewire-pulse`/`libpulse`, 16+ for `-f json`) and `pw-dump`. Each missing piece just disables that signal.

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
> **Dry-run is the default.** Without `--execute` the CLI only logs the BLE commands it *would* send and the light does nothing. Global options (`--address`, `--execute`, `--brightness`, `-v`) go before the subcommand.

- Scan for lights (prints address, name, RSSI):
  ```bash
  lctl scan --timeout 15
  ```

- Turn on/off (on defaults to 80% brightness unless overridden; brightness is ignored for off):
  ```bash
  lctl --address AA:BB:CC:DD:EE:FF --execute on
  lctl --address AA:BB:CC:DD:EE:FF --execute --brightness 40 on
  lctl --address AA:BB:CC:DD:EE:FF --execute off
  ```

- Set color temperature (optionally set brightness too):
  ```bash
  lctl --address AA:BB:CC:DD:EE:FF --execute --brightness 60 color-temp 3400
  ```

- FX effects (flash, tv, candle, strobe1-3):
  ```bash
  lctl --address AA:BB:CC:DD:EE:FF --execute effect candle
  ```

- Meeting-aware auto mode (runs until Ctrl-C):
  ```bash
  lctl --address AA:BB:CC:DD:EE:FF --execute --brightness 70 auto --poll-seconds 5
  ```
  Every `--poll-seconds` (default 5, minimum 0.5) it checks whether you're in a call and switches the light on/off when that changes. BLE errors (light unplugged or out of range) are logged and retried with backoff instead of stopping the loop.

  You count as in a call if **any** of these is true (checked cheapest first, stopping at the first hit):

  1. **Meeting process**: the Zoom desktop app (`zoom` / `zoom.us`), or a browser started with a meeting URL on its command line (e.g. a Google Meet app window: `chromium --app=https://meet.google.com/...`). The Zoom app counts whenever it is running.
  2. **Microphone** (`pactl -f json list source-outputs`): a meeting app has a capture stream open — **muted or not**, since muting in Slack/Meet usually keeps the stream. Default apps: `slack`, `zoom`, `zoom.us`, `teams`, `teams-for-linux`, and the common browsers (Chrome/Chromium, Brave, Edge, Vivaldi, Firefox). Matched exactly (case-insensitive) on the stream's process binary or application name. Server-internal streams (loopbacks, monitors) and pavucontrol's level meters are ignored.
  3. **Camera**: any process has a `/dev/video*` device open (via `/proc/<pid>/fd`; only your own processes are visible, PipeWire's daemons are ignored), or a PipeWire `Video/Source` node is `running` (`pw-dump`) — the latter catches apps that use the camera through PipeWire/the portal.

  This catches Slack huddles and Meet/Zoom/Teams tabs in an already-open browser, which the process check alone can't see.

  Options (also accepted by `detect`):
  - `--no-camera`: ignore camera use.
  - `--no-mic`: ignore microphone streams.
  - `--mic-app NAME`: also count mic streams from `NAME` (repeatable), e.g. `--mic-app discord --mic-app lark`. Run `pactl list source-outputs` during a call to find an app's `application.process.binary`.

  False positives to expect:
  - Any app using the camera turns the light on (e.g. a webcam test or Cheese) — intended.
  - A browser counts whenever any page holds the mic: voice typing, a site's voice search, a web recorder.
  - Discord isn't in the default list since people idle in voice channels for hours; add it with `--mic-app discord` if you want it.

- Check what detection sees right now (no BLE; exits 0 when in a call, 1 otherwise):
  ```bash
  lctl detect
  lctl -v detect --mic-app discord   # -v also logs which signals are unavailable
  ```
  Example output while Slack has the mic open (e.g. in a huddle):
  ```
  mic: slack
      Slack (pid 29545)
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
- Run the tests with `PYTHONPATH=src python3 -m unittest discover -s tests`.

## Disclaimer
This project is not affiliated with or endorsed by Ulanzi. Use at your own risk.
