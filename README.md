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
- `daemon` service: event-driven call light (mic changes react instantly) with saved settings, a JSON state file, hooks, and an [Omarchy](https://omarchy.org/) bar widget

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
pipx install --force .
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
  3. **Camera**: any process is streaming from a `/dev/video*` device (an open fd in `/proc/<pid>/fd` with its buffers mapped in `/proc/<pid>/maps`, so apps that only probe the camera for a permission check or device list don't count; only your own processes are visible, PipeWire's daemons are ignored), or a PipeWire `Video/Source` node is `running` (`pw-dump`) — the latter catches apps that use the camera through PipeWire/the portal.

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

## Daemon and Omarchy bar widget
`lctl daemon` is the always-on version of `auto`, meant to run as a systemd user service. It owns the BLE connection and reads its settings from `~/.config/lctl/config.json`:

> **Unlike the other commands, `lctl daemon` sends real BLE writes by default.** Use `lctl daemon --dry-run` to only log them.

- **Mode** `auto` turns the light on during a call and off once nothing has been detected for `off_delay_seconds` (default 8). `on`/`off` pin the light and pause call detection.
- **Mic** changes are event driven (`pactl subscribe`), so opening a mic in Slack/Meet/Zoom switches the light within about a second. Camera use and meeting processes have no change events and are re-checked every `poll_seconds` (default 5).
- **State** is published to `$XDG_RUNTIME_DIR/lctl/state.json` (rewritten only on change) for front ends; `lctl status` prints it.
- **Hooks**: executables at `~/.config/lctl/hooks/light-on` and `light-off` run, detached, after the light switches (e.g. re-apply webcam white balance).
- Stopping the daemon turns the light off, and so does switching to another light (the old one goes off).
- If the light is unreachable the daemon keeps retrying, backing off to once a minute.

`config.json` also accepts keys that `lctl set` doesn't cover (edit the file, then run `lctl set` with no flags to check it and have the daemon re-read it): `camera` / `mic` (true/false, default true), `mic_apps` (extra app names to watch, e.g. `["discord"]`), `poll_seconds` (default 5) and `off_delay_seconds` (default 8). A bad value is reported in `lctl status` and the daemon keeps its last good settings.

Change settings with `lctl set`; a running daemon picks them up immediately (SIGHUP):
```bash
lctl set --light AA:BB:CC:DD:EE:FF        # the light to control
lctl set --mode auto                      # auto | on | off
lctl set --brightness 40 --color-temp 3400
lctl status
```
Once a light is saved, `on`/`off`/`color-temp`/`effect`/`auto` use it when `--address` is omitted.

Install and start the service:
```bash
mkdir -p ~/.config/systemd/user
cp contrib/lctl.service ~/.config/systemd/user/
systemctl --user enable --now lctl.service
```

### Omarchy bar widget
`omarchy/kevin.light` is an Omarchy shell plugin that mirrors the built-in panels: a bulb in the bar (lit while the light is on, red during a call, dimmed while the daemon is stopped) and a panel with

- a switch that starts/stops (and enables/disables) `lctl.service`, so detection and Bluetooth stop entirely while you're away;
- On / Auto / Off mode;
- brightness and warmth sliders;
- a light picker listing Ulanzi lights BlueZ knows about, with a scan button.

Right-click the bulb to start/stop the service. In the panel, `a`/`o`/`f` pick a mode and `l` toggles the service. Install (Omarchy rejects symlinked plugins, so copy):
```bash
cp -r omarchy/kevin.light ~/.config/omarchy/plugins/
omarchy plugin enable kevin.light --section right
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
