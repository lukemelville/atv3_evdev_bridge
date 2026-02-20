# ATV3 Evdev Bridge

Home Assistant add-on that reads Bluetooth evdev input from the "Remoter ATV3" remote
and emits Home Assistant events with normalized button names. It is designed for
local control via `/dev/input` and the Supervisor event API.

Some app buttons show up as `KEY_UNKNOWN` and only include a scan code. This add-on
captures MSC scan codes so you can map every button reliably.

I created this repo in order to connect a Boxput Remoter ATV3 Lite Bluetooth remote control to my Home Assistant Green via a ASUS BT500 bluetooth controller. I have then created an automation to control my media system (TV, Soundbar, Xbox Series X, lights) using just the remote control.

Thought I'd share in case someone else was looking to do the same thing!

Built with Codex.

## Prereqs

- Home Assistant OS with Supervisor (for add-ons and Supervisor API).
- Bluetooth controller that works with Linux (see the [known working adapters list](https://www.home-assistant.io/integrations/bluetooth#known-working-high-performance-adapters))
- A Bluetooth remote that exposes evdev input (tested with Remoter ATV3).
- Access to `/dev/input` on the host.

## What it does

- Discovers matching input devices by name (default: "Remoter ATV3").
- Listens to evdev key + MSC scan codes and normalizes them into a consistent set
  of button names, including app buttons.
- Emits a Home Assistant event for key down/up and synthetic key hold repeats.
- Uses a bounded async event queue so slow API responses do not block evdev reads.

## Tested hardware / environment

- Home Assistant Green (HAOS + Supervisor)
- Architecture: aarch64 / arm64
- Bluetooth controller: ASUS BT500
- Remote: Remoter ATV3 (Bluetooth)
  - Devices show up as "Remoter ATV3 Keyboard" and "Remoter ATV3 Mouse"

## Installation (local add-on)

1. Copy this folder into your Home Assistant `addons/local/` directory.
2. In Home Assistant, go to **Settings -> Add-ons -> Add-on Store** and refresh
   (menu -> Check for updates).
3. Install **ATV3 Evdev Bridge** and start it.

## Updating

Home Assistant only detects updates for local add-ons when the version changes.
If modifying the repo locally, bump `version` in `config.yaml`, then use Add-on Store -> Check for updates.

## Configuration

All options live in the add-on config UI.

- `target_contains` (string): Substring match for input device name.
- `event_type` (string): Event name to fire in Home Assistant (`[A-Za-z][A-Za-z0-9_]*`).
- `grab_device` (bool): If true, grabs exclusive access to the evdev device.
- `ignore_scancodes` (string): Comma-separated scan codes to ignore (hex, optional `0x` prefix).
- `hold_buttons` (string): Comma-separated buttons that should emit `key_hold` (or a list value in JSON).
- `key_map_overrides` (string): Button overrides for Linux key codes (JSON object, or CSV `key=value` / `key:value`).
- `scan_map_overrides` (string): Button overrides for scan codes (JSON object, or CSV `key=value` / `key:value`).
- `hold_delay` (float): Seconds to wait before emitting `key_hold` repeats.
- `hold_repeat` (float): Seconds between `key_hold` repeats.
- `event_queue_size` (int): Max queued events before new events are dropped.
- `event_post_timeout` (float): Timeout in seconds for posting each event.
- `log_level` (string): `DEBUG`, `INFO`, `WARN`/`WARNING`, or `ERROR`.

### Validation and fallback behavior

The add-on validates option values at runtime. Invalid values are ignored and replaced with defaults, with a warning in logs.

- `target_contains`: default `Remoter ATV3`
- `event_type`: default `atv3_evdev_bridge_command_received`
- `ignore_scancodes`: default `700aa`
- `hold_buttons`: defaults to `up,down,left,right,vol_up,vol_down,ch_up,ch_down`
- `hold_delay`: default `0.25` (must be `>= 0`)
- `hold_repeat`: default `0.10` (must be `> 0`)
- `event_queue_size`: default `256` (must be `> 0`)
- `event_post_timeout`: default `3.0` (must be `> 0`)

### Device grabbing (exclusive access)

If `grab_device` is enabled, the add-on will attempt to grab the input device.
If you see `Resource busy`, another integration/add-on is consuming the same
device. Disable other keyboard/remote integrations for that device.

## Events

The add-on fires events to `event_type` with a payload like:

```json
{
  "device_name": "Remoter ATV3",
  "device_path": "/dev/input/event2",
  "key_code": 103,
  "key_name": "KEY_UP",
  "scan_code": "c0042",
  "button": "up",
  "ts": 1700000000.0,
  "type": "key_down"
}
```

`type` is one of:

- `key_down`
- `key_up`
- `key_hold`

## Button mapping

Mappings come from both Linux keycodes and scan codes. Known mappings live in
`run.py` under `KEY_MAP` and `SCAN_MAP`.

Resolution order is:

1. `KEY_MAP` (plus `key_map_overrides`)
2. `SCAN_MAP` (plus `scan_map_overrides`)
3. Derived key name fallback (e.g. `KEY_UP` -> `up`)

Derived key names also include aliases like `select`/`enter` -> `ok`, `esc` -> `back`, and `search` -> `mic`.

You can override mappings from the add-on config without editing code.

- `key_map_overrides` supports either JSON object or comma-separated pairs (`=` or `:`):
  - JSON: `{"353":"ok","172":"home"}`
  - CSV: `353=ok,172=home` or `0x161:ok,172:home`
- `scan_map_overrides` supports either JSON object or comma-separated pairs (`=` or `:`):
  - JSON: `{"c0009":"youtube","c000a":"gear"}`
  - CSV: `c0009=youtube,c000a=gear` or `0xc0009:youtube,c000a:gear`

`hold_buttons` controls which normalized buttons emit repeated `key_hold` events.
Example: `up,down,left,right,vol_up,vol_down`

Known app scan codes from this remote:

- `c0009`: `youtube`
- `c000e`: `netflix`
- `c0005`: `disney_plus`
- `c0007`: `google_play`
- `c000a`: `gear`
- `c0221`: `mic`
- `700aa`: `mic_extra`

## Security notes

This add-on needs raw access to `/dev/input` event nodes. On Home Assistant OS +
Supervisor (tested on HAOS 17.1 / Core 2026.2.2), those devices are blocked when
the add-on is in Protection mode. For this add-on, you should:

- Disable Protection mode in the add-on UI.
- Keep `apparmor: false` and `full_access: true` in `config.yaml`.
- If the Protection toggle is missing in the UI, check `ha apps info local_atv3_evdev_bridge`:
  if `full_access` is `false`, Supervisor will hide that toggle.
- The add-on logs current protection status at startup and warns if it is still enabled.

## Bluetooth setup (optional)

Pair, trust, and connect with bluetoothctl:

```bash
bluetoothctl
power on
agent on
default-agent
scan on
pair AA:BB:CC:DD:EE:FF
trust AA:BB:CC:DD:EE:FF
connect AA:BB:CC:DD:EE:FF
scan off
quit
```

Verify the input devices exist:

```bash
cat /proc/bus/input/devices | grep -i -n "remoter\\|atv3"
```

## Troubleshooting

- If no device is found, increase `log_level` to `DEBUG` and confirm the
  remote shows up in `/proc/bus/input/devices`.
- If buttons are missing, capture scan codes and add them to `scan_map_overrides` (or `SCAN_MAP` in code).
- If you see `PermissionError` / `Operation not permitted` for `/dev/input/event*`, disable Protection mode for this add-on and restart it. Permission warnings are throttled to once per 30 seconds per error signature.
- If you see `Resource busy`, another integration is grabbing the device.
- If an override mapping entry is malformed, it is skipped and a warning is logged.

## License

MIT. See `LICENSE`.
