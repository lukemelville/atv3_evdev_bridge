# ATV3 Evdev Bridge Addon for Home Assistant

Home Assistant add-on that reads Bluetooth evdev input from the "Remoter ATV3" remote
and emits Home Assistant events with normalized button names. It is designed for
local control via `/dev/input` and the Supervisor event API.

Some app buttons show up as `KEY_UNKNOWN` and only include a scan code. This add-on
captures MSC scan codes so you can map every button reliably.

I created this repo in order to connect a Boxput Remoter ATV3 Lite Bluetooth remote control to my Home Assistant Green via a ASUS BT500 bluetooth controller. I have then created an automation to control my media system (TV, Soundbar, Xbox Series X, lights) using just the remote control.

Thought I'd share in case someone else was looking to do the same thing!

Built with Codex using GPT-5.2 🤖

## Prereqs

- Home Assistant OS with Supervisor (for add-ons and Supervisor API).
- Bluetooth controller that works with Linux (see the [known working adapters list](https://www.home-assistant.io/integrations/bluetooth#known-working-high-performance-adapters))
- A Bluetooth remote that exposes evdev input (tested with [Remoter ATV3](https://www.aliexpress.com/item/1005007402456879.html)).
- Access to `/dev/input` on the host (requires disabling Protection mode).

## What it does

- Discovers matching input devices by name (default: "Remoter ATV3").
- Listens to evdev key + MSC scan codes and normalizes them into a consistent set
  of button names, including app buttons.
- Emits a Home Assistant event for key down/up and synthetic key hold repeats.
- Queues Home Assistant event posts so slow API calls do not block evdev input
  reading. Hold repeats are coalesced, transitions older than two seconds are
  discarded, and releases take priority over queued holds.

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
- `event_type` (string): Event name to fire in Home Assistant.
- `grab_device` (bool): If true, grabs exclusive access to the evdev device.
- `ignore_scancodes` (string): Comma-separated scan codes to ignore.
- `hold_delay` (float): Seconds to wait before emitting `key_hold` repeats
  (`0.05` to `10`).
- `hold_repeat` (float): Seconds between `key_hold` repeats (`0.05` to `10`).
- `log_level` (string): `DEBUG`, `INFO`, `WARN`, or `ERROR`.

The add-on validates options before accessing any devices. Invalid values or an
unreadable `/data/options.json` stop startup with a clear `ERROR` log instead of
silently selecting different defaults. `event_type` accepts letters, numbers,
and underscores; `ignore_scancodes` accepts comma-separated hexadecimal values.

### Device grabbing (exclusive access)

If `grab_device` is enabled, the add-on will attempt to grab the input device.
If you see `Resource busy`, another integration/add-on is consuming the same
device. The add-on closes a partial device set and retries with backoff rather
than running with missing buttons. Disable other keyboard/remote integrations
for that device, or disable `grab_device` if shared access is intentional.

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

The event payload is backward compatible with version 1.0.8. Each `key_hold`
uses the timestamp at which that repeat was generated. Delivery is best-effort:
events are not retried or replayed after an API outage.

## Button mapping

Mappings come from both Linux keycodes and scan codes. Known mappings live in
`run.py` under `KEY_MAP` and `SCAN_MAP`. Unknown keys fall back to `KEY_*` names
(lowercased) to avoid losing buttons. Unknown scan codes are emitted as
`unknown_scan_<scan>` only when Linux reports `KEY_UNKNOWN`; otherwise the Linux
key name is preferred. Scan codes listed in `ignore_scancodes` are skipped and
logged at `DEBUG`.

Known app scan codes from this remote:

- `c0009`: `youtube`
- `c000e`: `netflix`
- `c0005`: `disney_plus`
- `c0007`: `google_play`
- `c000a`: `gear`

## Security notes

This add-on needs raw access to `/dev/input`, so it sets `full_access: true` and
`apparmor: false` in `config.yaml`. You also need to disable Protection mode
for the add-on in the UI. Only install if you trust the code and the host
environment. The add-on enables only the Home Assistant API permission; it does
not request general Supervisor API access.

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
  remote shows up in `/proc/bus/input/devices`. The add-on retries with a capped
  backoff of 2s, 5s, then 10s while waiting for a matching device.
- If buttons are missing, capture scan codes and add them to `SCAN_MAP`.
- If you see `PermissionError` for `/dev/input/event*`, disable Protection mode.
- If you see `Resource busy`, another integration is grabbing the device.
- If you see `Failed to post event`, Home Assistant rejected the event or the
  Supervisor API call timed out. The log includes the HTTP status or exception
  plus success/failure/drop counters.
- If you see `Dropped event`, the Home Assistant API was too slow, the event was
  stale, or the add-on was shutting down. Hold repeats are intentionally lossy;
  the newest down/up transition is preserved when the queue is under pressure.

To confirm Home Assistant is receiving events, listen for
`atv3_evdev_bridge_command_received` in **Developer Tools -> Events** and press a
remote button.

Example automation trigger:

```yaml
trigger:
  - platform: event
    event_type: atv3_evdev_bridge_command_received
    event_data:
      button: youtube
      type: key_down
```

## Local tests

Install the test dependency and run:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m ruff check run.py tests
python3 -m ruff format --check run.py tests
python3 -m pytest -q
```

CI runs the same checks on Python 3.9 and 3.14 and performs a non-publishing
`linux/arm64` container build using the Home Assistant build arguments.

## License

MIT. See `LICENSE`.
