#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from collections import deque
import json
import math
import os
import re
import signal
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Set, Tuple

import requests

try:
    from evdev import InputDevice, ecodes

    EVDEV_AVAILABLE = True
except ImportError:  # pragma: no cover - local unit tests do not require evdev.
    EVDEV_AVAILABLE = False

    class _MissingInputDevice:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("python-evdev is required to read input devices")

    class _MissingEcodes:
        EV_MSC = 4
        EV_KEY = 1
        MSC_SCAN = 4
        KEY_UNKNOWN = 240
        KEY = {
            14: "KEY_BACKSPACE",
            103: "KEY_UP",
            104: "KEY_PAGEUP",
            105: "KEY_LEFT",
            106: "KEY_RIGHT",
            108: "KEY_DOWN",
            109: "KEY_PAGEDOWN",
            113: "KEY_MUTE",
            114: "KEY_VOLUMEDOWN",
            115: "KEY_VOLUMEUP",
            116: "KEY_POWER",
            139: "KEY_MENU",
            158: "KEY_BACK",
            172: "KEY_HOMEPAGE",
            217: "KEY_SEARCH",
            240: "KEY_UNKNOWN",
            353: "KEY_SELECT",
        }

    InputDevice = _MissingInputDevice
    ecodes = _MissingEcodes()


OPTIONS_PATH = "/data/options.json"
DEFAULT_TARGET_CONTAINS = "Remoter ATV3"
DEFAULT_EVENT_TYPE = "atv3_evdev_bridge_command_received"
DEFAULT_HOLD_DELAY = 0.25
DEFAULT_HOLD_REPEAT = 0.10
DEFAULT_IGNORE_SCANCODES = "700aa"
DEFAULT_GRAB_DEVICE = True
DEFAULT_LOG_LEVEL = "INFO"
MIN_HOLD_SECONDS = 0.05
MAX_HOLD_SECONDS = 10.0
POST_TIMEOUT_SECONDS = 1.0
EVENT_QUEUE_SIZE = 100
MAX_QUEUE_AGE_SECONDS = 2.0
SHUTDOWN_TIMEOUT_SECONDS = 2.0
NO_DEVICE_BACKOFFS = (2.0, 5.0, 10.0)
HOLD_BUTTONS = {
    "up",
    "down",
    "left",
    "right",
    "vol_up",
    "vol_down",
    "ch_up",
    "ch_down",
}


KEY_MAP = {
    116: "power",
    139: "menu",
    217: "mic",
    103: "up",
    108: "down",
    105: "left",
    106: "right",
    353: "ok",
    158: "back",
    172: "home",
    113: "mute",
    115: "vol_up",
    114: "vol_down",
    104: "ch_up",
    109: "ch_down",
    14: "tv_or_backspace",
}

SCAN_MAP = {
    "c000a": "gear",
    "c0009": "youtube",
    "c000e": "netflix",
    "c0005": "disney_plus",
    "c0007": "google_play",
    "c0221": "mic",
    "700aa": "mic_extra",
    "c0041": "ok",
    "c0042": "up",
    "c0043": "down",
    "c0044": "left",
    "c0045": "right",
}

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
EVENT_TYPE_RE = re.compile(r"^[A-Za-z0-9_]+$")
SCAN_CODE_RE = re.compile(r"^[0-9a-f]+$")

HoldKey = Tuple[str, int, str]
HoldTask = Tuple[asyncio.Event, asyncio.Task[None]]


class ConfigurationError(ValueError):
    """Raised when add-on options are missing or invalid."""


@dataclass(frozen=True)
class Config:
    target_contains: str
    event_type: str
    grab_device: bool
    ignore_scans: Set[str]
    hold_delay: float
    hold_repeat: float
    log_level: str


@dataclass
class EventPostResult:
    ok: bool
    status_code: Optional[int] = None
    error: Optional[str] = None


@dataclass
class QueuedEvent:
    payload: dict
    queued_at: float
    hold_key: Optional[HoldKey] = None


@dataclass(frozen=True)
class ActivePress:
    button: Optional[str]


def log(level: str, msg: str, cfg_level: str = DEFAULT_LOG_LEVEL) -> None:
    want = LOG_LEVELS.get((cfg_level or DEFAULT_LOG_LEVEL).upper(), 20)
    got = LOG_LEVELS.get((level or DEFAULT_LOG_LEVEL).upper(), 20)
    if got >= want:
        print(f"[{level}] {msg}", flush=True)


def load_options(path: str = OPTIONS_PATH) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            options = json.load(file_handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"Could not read options from {path}: {exc}") from exc

    if not isinstance(options, dict):
        raise ConfigurationError(f"Options in {path} must be a JSON object")
    return options


def read_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as file_handle:
            return file_handle.read().strip()
    except OSError:
        return None


def get_supervisor_token() -> str:
    for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        value = os.environ.get(key)
        if value:
            return value

    for path in (
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6/container_environment/HASSIO_TOKEN",
    ):
        value = read_file(path)
        if value:
            return value
    raise RuntimeError("Missing SUPERVISOR_TOKEN/HASSIO_TOKEN")


def parse_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    raise ConfigurationError(f"{name} must be a boolean")


def parse_ignore_scans(value: Any) -> Set[str]:
    if value in (None, ""):
        return set()
    if not isinstance(value, str):
        raise ConfigurationError("ignore_scancodes must be a comma-separated string")

    scans = {part.strip().lower() for part in value.split(",") if part.strip()}
    invalid = sorted(scan for scan in scans if not SCAN_CODE_RE.fullmatch(scan))
    if invalid:
        raise ConfigurationError(
            f"ignore_scancodes contains non-hexadecimal value(s): {', '.join(invalid)}"
        )
    return scans


def parse_hold_timing(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or not MIN_HOLD_SECONDS <= parsed <= MAX_HOLD_SECONDS:
        raise ConfigurationError(
            f"{name} must be between {MIN_HOLD_SECONDS:g} and {MAX_HOLD_SECONDS:g} seconds"
        )
    return parsed


def validate_event_type(value: Any) -> str:
    event_type = str(value or "").strip()
    if not EVENT_TYPE_RE.fullmatch(event_type):
        raise ConfigurationError(
            "event_type must contain only ASCII letters, numbers, and underscores"
        )
    return event_type


def validate_target_contains(value: Any) -> str:
    target = str(value or "").strip()
    if not target:
        raise ConfigurationError("target_contains must not be empty")
    return target


def validate_log_level(value: Any) -> str:
    level = str(value or "").strip().upper()
    if level == "WARNING":
        level = "WARN"
    if level not in LOG_LEVELS:
        raise ConfigurationError("log_level must be DEBUG, INFO, WARN, or ERROR")
    return level


def load_config(options: dict) -> Config:
    return Config(
        target_contains=validate_target_contains(
            options.get("target_contains", DEFAULT_TARGET_CONTAINS)
        ),
        event_type=validate_event_type(options.get("event_type", DEFAULT_EVENT_TYPE)),
        grab_device=parse_bool(
            options.get("grab_device", DEFAULT_GRAB_DEVICE), "grab_device"
        ),
        ignore_scans=parse_ignore_scans(
            options.get("ignore_scancodes", DEFAULT_IGNORE_SCANCODES)
        ),
        hold_delay=parse_hold_timing(
            options.get("hold_delay", DEFAULT_HOLD_DELAY), "hold_delay"
        ),
        hold_repeat=parse_hold_timing(
            options.get("hold_repeat", DEFAULT_HOLD_REPEAT), "hold_repeat"
        ),
        log_level=validate_log_level(options.get("log_level", DEFAULT_LOG_LEVEL)),
    )


def norm_scan(value: int) -> str:
    return format(int(value), "x").lower()


def key_name(code: int) -> str:
    name = ecodes.KEY.get(code, f"KEY_{code}")
    if isinstance(name, (list, tuple)):
        return str(name[0])
    return str(name)


def is_key_unknown(code: int, name: str) -> bool:
    return code == getattr(ecodes, "KEY_UNKNOWN", 240) or name == "KEY_UNKNOWN"


def resolve_button(
    code: int,
    name: str,
    scan: str,
    ignore: Set[str],
    cfg_level: str = DEFAULT_LOG_LEVEL,
) -> str:
    button = KEY_MAP.get(code)
    if button:
        return button

    scan = (scan or "").lower()
    if scan:
        if scan in ignore:
            log(
                "DEBUG",
                f"Ignoring scan code {scan} for code={code} name={name}",
                cfg_level,
            )
            return ""

        button = SCAN_MAP.get(scan)
        if button:
            return button

    if name.startswith("KEY_") and not is_key_unknown(code, name):
        derived = name[4:].lower()
        aliases = {
            "select": "ok",
            "enter": "ok",
            "esc": "back",
            "search": "mic",
            "homepage": "home",
            "volumeup": "vol_up",
            "volumedown": "vol_down",
            "pageup": "ch_up",
            "pagedown": "ch_down",
        }
        return aliases.get(derived, derived)

    if scan and is_key_unknown(code, name):
        return f"unknown_scan_{scan}"

    return f"key_{code}"


def find_event_paths_in_text(devices_text: str, target_contains: str) -> List[str]:
    blocks = re.split(r"\n\s*\n", (devices_text or "").strip(), flags=re.MULTILINE)
    target = target_contains.lower()

    paths: List[str] = []
    for block in blocks:
        name_match = re.search(r'^N:\s+Name="([^"]+)"', block, flags=re.MULTILINE)
        if not name_match or target not in name_match.group(1).lower():
            continue

        handlers_match = re.search(r"^H:\s+Handlers=(.+)$", block, flags=re.MULTILINE)
        if not handlers_match:
            continue
        for handler in handlers_match.group(1).strip().split():
            if handler.startswith("event"):
                paths.append(f"/dev/input/{handler}")

    output: List[str] = []
    seen: Set[str] = set()
    for path in paths:
        if path not in seen:
            output.append(path)
            seen.add(path)
    return output


def find_event_paths_for_target(target_contains: str) -> List[str]:
    return find_event_paths_in_text(
        read_file("/proc/bus/input/devices") or "", target_contains
    )


def post_event_sync(
    session: requests.Session,
    token: str,
    event_type: str,
    payload: dict,
    timeout: float = POST_TIMEOUT_SECONDS,
) -> EventPostResult:
    url = f"http://supervisor/core/api/events/{event_type}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        response = session.post(url, headers=headers, json=payload, timeout=timeout)
    except Exception as exc:  # requests can surface adapter-specific exception types.
        return EventPostResult(ok=False, error=str(exc))

    if 200 <= response.status_code < 300:
        return EventPostResult(ok=True, status_code=response.status_code)
    error = str(getattr(response, "text", ""))[:200]
    return EventPostResult(
        ok=False, status_code=response.status_code, error=error or None
    )


class EventSender:
    def __init__(
        self,
        session: requests.Session,
        token: str,
        event_type: str,
        cfg_level: str,
        queue_size: int = EVENT_QUEUE_SIZE,
        max_event_age: float = MAX_QUEUE_AGE_SECONDS,
        shutdown_timeout: float = SHUTDOWN_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.token = token
        self.event_type = event_type
        self.cfg_level = cfg_level
        self.queue_size = max(1, queue_size)
        self.max_event_age = max_event_age
        self.shutdown_timeout = shutdown_timeout
        self._clock = clock
        self._queue: Deque[QueuedEvent] = deque()
        self._pending_holds: Dict[HoldKey, QueuedEvent] = {}
        self._wakeup = asyncio.Event()
        self._worker: Optional[asyncio.Task[None]] = None
        self._stopping = False
        self._in_flight = False
        self.success_count = 0
        self.failure_count = 0
        self.dropped_count = 0

    @property
    def pending_count(self) -> int:
        return len(self._queue)

    @staticmethod
    def event_key(payload: dict) -> HoldKey:
        return (
            str(payload.get("device_path", "")),
            int(payload.get("key_code", -1)),
            str(payload.get("button", "")),
        )

    def start(self) -> None:
        if self._stopping:
            raise RuntimeError("Cannot restart a stopped event sender")
        if not self._worker or self._worker.done():
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if not self._worker:
            return

        self._stopping = True
        while self._queue:
            item = self._queue.popleft()
            self._forget_hold(item)
            self._record_drop(item, "shutdown", warn=False)
        self._pending_holds.clear()
        self._wakeup.set()

        try:
            await asyncio.wait_for(self._worker, timeout=self.shutdown_timeout)
        except asyncio.TimeoutError:
            log(
                "WARN",
                f"Event sender did not stop within {self.shutdown_timeout:g}s; cancelling worker",
                self.cfg_level,
            )
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
        finally:
            self._worker = None

    def emit(self, payload: dict) -> bool:
        if self._stopping:
            self._record_drop(
                QueuedEvent(payload, self._clock()), "sender stopping", warn=False
            )
            return False

        event_type = payload.get("type")
        hold_key = self.event_key(payload) if event_type == "key_hold" else None

        if hold_key is not None:
            existing = self._pending_holds.get(hold_key)
            if existing is not None:
                existing.payload = payload
                existing.queued_at = self._clock()
                self.dropped_count += 1
                log(
                    "DEBUG",
                    f"Coalesced pending hold button={payload.get('button')} dropped={self.dropped_count}",
                    self.cfg_level,
                )
                return True
        elif event_type == "key_up":
            self.discard_pending_hold(self.event_key(payload))

        if len(self._queue) >= self.queue_size:
            if hold_key is not None:
                self._record_drop(
                    QueuedEvent(payload, self._clock(), hold_key),
                    "queue full",
                    warn=True,
                )
                return False
            self._evict_for_transition()

        item = QueuedEvent(payload, self._clock(), hold_key)
        self._queue.append(item)
        if hold_key is not None:
            self._pending_holds[hold_key] = item
        self._wakeup.set()
        return True

    def discard_pending_hold(self, hold_key: HoldKey) -> None:
        item = self._pending_holds.pop(hold_key, None)
        if item is None:
            return
        try:
            self._queue.remove(item)
        except ValueError:
            return
        self._record_drop(item, "released before delivery", warn=False)

    async def wait_until_idle(self, timeout: float = 2.0) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while self._queue or self._in_flight:
            if self._worker and self._worker.done():
                self._worker.result()
            if asyncio.get_running_loop().time() >= deadline:
                raise asyncio.TimeoutError("Event sender did not become idle")
            await asyncio.sleep(0.001)

    def _evict_for_transition(self) -> None:
        item = next(
            (candidate for candidate in self._queue if candidate.hold_key is not None),
            self._queue[0],
        )
        self._queue.remove(item)
        self._forget_hold(item)
        self._record_drop(item, "queue full; preserving latest transition", warn=True)

    def _forget_hold(self, item: QueuedEvent) -> None:
        if item.hold_key is not None and self._pending_holds.get(item.hold_key) is item:
            self._pending_holds.pop(item.hold_key, None)

    def _record_drop(self, item: QueuedEvent, reason: str, warn: bool) -> None:
        self.dropped_count += 1
        log(
            "WARN" if warn else "DEBUG",
            f"Dropped event button={item.payload.get('button')} type={item.payload.get('type')} "
            f"reason={reason} dropped={self.dropped_count}",
            self.cfg_level,
        )

    async def _run(self) -> None:
        while True:
            while self._queue:
                item = self._queue.popleft()
                self._forget_hold(item)
                age = self._clock() - item.queued_at
                if age > self.max_event_age:
                    self._record_drop(item, f"stale after {age:.3f}s", warn=True)
                    continue

                self._in_flight = True
                try:
                    result = await asyncio.to_thread(
                        post_event_sync,
                        self.session,
                        self.token,
                        self.event_type,
                        item.payload,
                    )
                finally:
                    self._in_flight = False

                if result.ok:
                    self.success_count += 1
                    log(
                        "DEBUG",
                        f"Posted event button={item.payload.get('button')} "
                        f"type={item.payload.get('type')} success={self.success_count} "
                        f"failure={self.failure_count} dropped={self.dropped_count}",
                        self.cfg_level,
                    )
                else:
                    self.failure_count += 1
                    detail = (
                        f"status={result.status_code}"
                        if result.status_code is not None
                        else f"error={result.error}"
                    )
                    log(
                        "WARN",
                        f"Failed to post event button={item.payload.get('button')} "
                        f"type={item.payload.get('type')} {detail} "
                        f"success={self.success_count} failure={self.failure_count} "
                        f"dropped={self.dropped_count}",
                        self.cfg_level,
                    )

            if self._stopping:
                return
            self._wakeup.clear()
            if self._queue:
                continue
            await self._wakeup.wait()

    def log_summary(self, context: str) -> None:
        log(
            "INFO",
            f"Event sender {context}: success={self.success_count} "
            f"failure={self.failure_count} dropped={self.dropped_count}",
            self.cfg_level,
        )


async def hold_loop(
    sender: EventSender,
    base_payload: dict,
    stop_event: asyncio.Event,
    delay: float,
    repeat: float,
) -> None:
    await asyncio.sleep(delay)
    while not stop_event.is_set():
        sender.emit({**base_payload, "type": "key_hold", "ts": time.time()})
        await asyncio.sleep(repeat)


async def cancel_hold(task_info: HoldTask) -> None:
    stop_event, task = task_info
    stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def build_payload(
    device: InputDevice, code: int, name: str, scan: str, button: str
) -> dict:
    return {
        "device_name": device.name or "",
        "device_path": device.path,
        "key_code": code,
        "key_name": name,
        "scan_code": scan,
        "button": button,
        "ts": time.time(),
    }


async def read_device(
    device: InputDevice,
    sender: EventSender,
    ignore_scans: Set[str],
    hold_delay: float,
    hold_repeat: float,
    cfg_level: str,
) -> None:
    last_scan: Optional[str] = None
    holds: Dict[Tuple[int, str], HoldTask] = {}
    active_presses: Dict[int, ActivePress] = {}

    log("INFO", f"Reading device: {device.path} name='{device.name}'", cfg_level)
    try:
        async for event in device.async_read_loop():
            if event.type == ecodes.EV_MSC and event.code == ecodes.MSC_SCAN:
                last_scan = norm_scan(event.value)
                continue

            if event.type != ecodes.EV_KEY:
                continue

            code = int(event.code)
            value = int(event.value)  # 1 down, 0 up, 2 kernel repeat
            scan = (last_scan or "").lower()
            last_scan = None

            if value == 2:
                continue

            name = key_name(code)
            if value == 1:
                previous = active_presses.pop(code, None)
                if previous and previous.button:
                    old_hold = holds.pop((code, previous.button), None)
                    if old_hold:
                        await cancel_hold(old_hold)
                    sender.discard_pending_hold(
                        (str(device.path), code, previous.button)
                    )

                button = resolve_button(code, name, scan, ignore_scans, cfg_level)
                active_presses[code] = ActivePress(button=button or None)
                if not button:
                    continue

                payload = build_payload(device, code, name, scan, button)
                sender.emit({**payload, "type": "key_down"})
                log(
                    "INFO",
                    f"KEY_DOWN dev={device.path} code={code} name={name} "
                    f"scan={scan} button={button}",
                    cfg_level,
                )

                if button in HOLD_BUTTONS:
                    stop_event = asyncio.Event()
                    task = asyncio.create_task(
                        hold_loop(
                            sender,
                            payload,
                            stop_event,
                            hold_delay,
                            hold_repeat,
                        )
                    )
                    holds[(code, button)] = (stop_event, task)

            elif value == 0:
                active_press = active_presses.pop(code, None)
                if active_press is not None:
                    if not active_press.button:
                        continue
                    button = active_press.button
                else:
                    button = resolve_button(code, name, scan, ignore_scans, cfg_level)
                    if not button:
                        continue

                old_hold = holds.pop((code, button), None)
                if old_hold:
                    await cancel_hold(old_hold)

                payload = build_payload(device, code, name, scan, button)
                sender.emit({**payload, "type": "key_up"})
    finally:
        if holds:
            log(
                "DEBUG",
                f"Cleaning up {len(holds)} hold task(s) for {device.path}",
                cfg_level,
            )
        await asyncio.gather(
            *(cancel_hold(task_info) for task_info in holds.values()),
            return_exceptions=True,
        )
        for code, button in holds:
            sender.discard_pending_hold((str(device.path), code, button))
        holds.clear()
        active_presses.clear()


def close_device(device: InputDevice) -> None:
    try:
        device.close()
    except Exception:
        pass


def open_devices(
    paths: Iterable[str], grab_device: bool, cfg_level: str
) -> List[InputDevice]:
    devices: List[InputDevice] = []
    failed = False

    for path in paths:
        try:
            device = InputDevice(path)
        except Exception as exc:
            failed = True
            log("WARN", f"Failed to open {path}: {exc}", cfg_level)
            continue

        try:
            capabilities = device.capabilities()
        except Exception as exc:
            failed = True
            log("WARN", f"Failed to inspect {path}: {exc}", cfg_level)
            close_device(device)
            continue

        if ecodes.EV_KEY not in capabilities:
            log("DEBUG", f"Skipping {path}; device exposes no key events", cfg_level)
            close_device(device)
            continue

        log("INFO", f"Opened {path} name='{device.name}'", cfg_level)
        if grab_device:
            try:
                device.grab()
            except OSError as exc:
                failed = True
                log("WARN", f"Could not grab {path}: {exc}", cfg_level)
                close_device(device)
                continue
            log("INFO", f"Grabbed {path} (exclusive access)", cfg_level)
        devices.append(device)

    if failed:
        log(
            "WARN",
            "Could not open every matching key device; closing partial set before retry",
            cfg_level,
        )
        close_devices(devices, grab_device)
        return []
    return devices


def close_devices(devices: Iterable[InputDevice], grab_device: bool) -> None:
    for device in devices:
        if grab_device:
            try:
                device.ungrab()
            except Exception:
                pass
        close_device(device)


async def run_readers(
    devices: List[InputDevice],
    sender: EventSender,
    config: Config,
    stop_event: asyncio.Event,
) -> bool:
    reader_tasks = [
        asyncio.create_task(
            read_device(
                device,
                sender,
                config.ignore_scans,
                config.hold_delay,
                config.hold_repeat,
                config.log_level,
            )
        )
        for device in devices
    ]
    stop_task = asyncio.create_task(stop_event.wait())

    try:
        done, _ = await asyncio.wait(
            [*reader_tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
        )
        stopping = stop_task in done and stop_event.is_set()
        if not stopping:
            for task in done:
                if task is stop_task:
                    continue
                if task.cancelled():
                    log(
                        "WARN",
                        "Reader was cancelled; rediscovering devices",
                        config.log_level,
                    )
                elif task.exception():
                    log(
                        "WARN",
                        f"Reader ended: {task.exception()}",
                        config.log_level,
                    )
                else:
                    log(
                        "WARN",
                        "Reader ended without error; rediscovering devices",
                        config.log_level,
                    )
        return not stopping
    finally:
        for task in [*reader_tasks, stop_task]:
            if not task.done():
                task.cancel()
        await asyncio.gather(*reader_tasks, stop_task, return_exceptions=True)


async def wait_or_stop(delay: float, stop_event: asyncio.Event) -> bool:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=delay)
    except asyncio.TimeoutError:
        return False
    return True


async def run_bridge(
    config: Config, sender: EventSender, stop_event: asyncio.Event
) -> None:
    no_device_attempt = 0
    while not stop_event.is_set():
        paths = find_event_paths_for_target(config.target_contains)
        devices = (
            open_devices(paths, config.grab_device, config.log_level) if paths else []
        )

        if not devices:
            delay = NO_DEVICE_BACKOFFS[
                min(no_device_attempt, len(NO_DEVICE_BACKOFFS) - 1)
            ]
            no_device_attempt += 1
            reason = (
                "No matching input devices yet"
                if not paths
                else "No usable matching key devices"
            )
            log("WARN", f"{reason}. Retrying in {delay:g}s...", config.log_level)
            if await wait_or_stop(delay, stop_event):
                break
            continue

        no_device_attempt = 0
        try:
            should_rediscover = await run_readers(devices, sender, config, stop_event)
        finally:
            close_devices(devices, config.grab_device)
            sender.log_summary("before rediscovery")

        if not should_rediscover or stop_event.is_set():
            break

        delay = NO_DEVICE_BACKOFFS[0]
        log("WARN", f"Rediscovering in {delay:g}s...", config.log_level)
        if await wait_or_stop(delay, stop_event):
            break


def install_signal_handlers(stop_event: asyncio.Event) -> List[signal.Signals]:
    loop = asyncio.get_running_loop()
    installed: List[signal.Signals] = []
    for signal_number in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signal_number, stop_event.set)
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(signal_number)
    return installed


def remove_signal_handlers(installed: Iterable[signal.Signals]) -> None:
    loop = asyncio.get_running_loop()
    for signal_number in installed:
        loop.remove_signal_handler(signal_number)


async def main() -> None:
    if not EVDEV_AVAILABLE:
        raise RuntimeError("python-evdev is required to run the bridge")

    config = load_config(load_options())
    token = get_supervisor_token()
    session = requests.Session()
    sender = EventSender(session, token, config.event_type, config.log_level)
    stop_event = asyncio.Event()
    installed_signals = install_signal_handlers(stop_event)
    sender.start()

    log("INFO", f"Target contains: '{config.target_contains}'", config.log_level)
    log("INFO", f"Output event_type: '{config.event_type}'", config.log_level)
    log("INFO", f"Grab device: {config.grab_device}", config.log_level)
    log("INFO", f"Ignore scancodes: {sorted(config.ignore_scans)}", config.log_level)
    log(
        "INFO",
        f"Hold: delay={config.hold_delay}s repeat={config.hold_repeat}s",
        config.log_level,
    )

    try:
        await run_bridge(config, sender, stop_event)
    finally:
        stop_event.set()
        await sender.stop()
        session.close()
        remove_signal_handlers(installed_signals)
        sender.log_summary("at shutdown")


def cli() -> None:
    try:
        asyncio.run(main())
    except (ConfigurationError, RuntimeError) as exc:
        log("ERROR", str(exc))
        raise SystemExit(1) from exc


if __name__ == "__main__":
    cli()
