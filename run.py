#!/usr/bin/env python3
import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

import requests
from evdev import InputDevice, ecodes

OPTIONS_PATH = "/data/options.json"
DEFAULT_TARGET_CONTAINS = "Remoter ATV3"
DEFAULT_EVENT_TYPE = "atv3_evdev_bridge_command_received"
DEFAULT_IGNORE_SCANCODES = "700aa"
DEFAULT_HOLD_DELAY = 0.25
DEFAULT_HOLD_REPEAT = 0.10
DEFAULT_EVENT_QUEUE_SIZE = 256
DEFAULT_EVENT_POST_TIMEOUT = 3.0
EVENT_TYPE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]*$")
BUTTON_RE = re.compile(r"^[a-z0-9_]+$")
SCAN_CODE_RE = re.compile(r"^[0-9a-f]+$")
DEFAULT_HOLD_BUTTONS = ["up", "down", "left", "right", "vol_up", "vol_down", "ch_up", "ch_down"]
DEFAULT_HOLD_BUTTONS_CSV = ",".join(DEFAULT_HOLD_BUTTONS)
DEFAULT_KEY_MAP_OVERRIDES = ""
DEFAULT_SCAN_MAP_OVERRIDES = ""


# === Your original mapping (plus a few adds you’re seeing now) ===
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
    14:  "tv_or_backspace",
}

SCAN_MAP = {
    # App / special buttons (your captures)
    "c000a": "gear",
    "c0009": "youtube",
    "c000e": "netflix",
    "c0005": "disney_plus",
    "c0007": "google_play",

    # Mic behaviour you captured
    "c0221": "mic",
    "700aa": "mic_extra",

    # D-pad scans you’re now seeing (nice to label explicitly)
    "c0041": "ok",
    "c0042": "up",
    "c0043": "down",
    "c0044": "left",
    "c0045": "right",
}

LOG_LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "WARNING": 30, "ERROR": 40}


def log(level: str, msg: str, cfg_level: str = "INFO") -> None:
    want = LOG_LEVELS.get((cfg_level or "INFO").upper(), 20)
    got = LOG_LEVELS.get((level or "INFO").upper(), 20)
    if got >= want:
        print(f"[{level}] {msg}", flush=True)


def load_options() -> dict:
    try:
        with open(OPTIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def read_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return None


def get_supervisor_token() -> str:
    # env first
    for k in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        v = os.environ.get(k)
        if v:
            return v
    # s6 file fallback
    for p in (
        "/run/s6/container_environment/SUPERVISOR_TOKEN",
        "/run/s6/container_environment/HASSIO_TOKEN",
    ):
        v = read_file(p)
        if v:
            return v
    raise RuntimeError("Missing SUPERVISOR_TOKEN/HASSIO_TOKEN")


def norm_scan(v: int) -> str:
    return format(int(v), "x").lower()


def parse_ignore_scans(s: str) -> Set[str]:
    if not s:
        return set()
    return {p.strip().lower().removeprefix("0x") for p in s.split(",") if p.strip()}


def key_name(code: int) -> str:
    return ecodes.KEY.get(code, f"KEY_{code}")


def parse_log_level(raw: object) -> str:
    level = str(raw or "INFO").strip().upper()
    if level in LOG_LEVELS:
        return level
    print(f"[WARN] Invalid log_level '{raw}', using INFO", flush=True)
    return "INFO"


def parse_event_type(raw: object, cfg_level: str) -> str:
    event_type = str(raw or DEFAULT_EVENT_TYPE).strip()
    if not event_type:
        log("WARN", f"event_type is empty, using '{DEFAULT_EVENT_TYPE}'", cfg_level)
        return DEFAULT_EVENT_TYPE
    if not EVENT_TYPE_RE.fullmatch(event_type):
        log("WARN", f"event_type '{event_type}' is invalid, using '{DEFAULT_EVENT_TYPE}'", cfg_level)
        return DEFAULT_EVENT_TYPE
    return event_type


def parse_non_negative_float(name: str, raw: object, default: float, cfg_level: str) -> float:
    try:
        value = float(raw)
    except Exception:
        log("WARN", f"{name}='{raw}' is invalid, using {default}", cfg_level)
        return default
    if value < 0:
        log("WARN", f"{name} must be >= 0, using {default}", cfg_level)
        return default
    return value


def parse_positive_float(name: str, raw: object, default: float, cfg_level: str) -> float:
    try:
        value = float(raw)
    except Exception:
        log("WARN", f"{name}='{raw}' is invalid, using {default}", cfg_level)
        return default
    if value <= 0:
        log("WARN", f"{name} must be > 0, using {default}", cfg_level)
        return default
    return value


def parse_positive_int(name: str, raw: object, default: int, cfg_level: str) -> int:
    try:
        value = int(raw)
    except Exception:
        log("WARN", f"{name}='{raw}' is invalid, using {default}", cfg_level)
        return default
    if value <= 0:
        log("WARN", f"{name} must be > 0, using {default}", cfg_level)
        return default
    return value


def normalize_button_name(raw: object, option_name: str, cfg_level: str) -> Optional[str]:
    button = re.sub(r"\s+", "_", str(raw or "").strip().lower())
    if not button:
        log("WARN", f"Ignoring empty button value in {option_name}", cfg_level)
        return None
    if not BUTTON_RE.fullmatch(button):
        log("WARN", f"Ignoring invalid button value '{raw}' in {option_name}", cfg_level)
        return None
    return button


def parse_button_mapping_input(raw: object, option_name: str, cfg_level: str) -> Dict[str, str]:
    if raw is None:
        return {}

    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}

    text = str(raw).strip()
    if not text:
        return {}

    loaded: Any = None
    if text.startswith("{"):
        try:
            loaded = json.loads(text)
        except Exception as e:
            log("WARN", f"{option_name} JSON parse failed ({e}); falling back to CSV parser", cfg_level)

        if isinstance(loaded, dict):
            return {str(k): str(v) for k, v in loaded.items()}
        if loaded is not None:
            log("WARN", f"{option_name} JSON must be an object; ignoring parsed value", cfg_level)
            return {}

    out: Dict[str, str] = {}
    for part in [p.strip() for p in text.split(",") if p.strip()]:
        if "=" in part:
            k, v = part.split("=", 1)
        elif ":" in part:
            k, v = part.split(":", 1)
        else:
            log("WARN", f"Ignoring invalid mapping '{part}' in {option_name}", cfg_level)
            continue

        key = k.strip()
        value = v.strip()
        if not key or not value:
            log("WARN", f"Ignoring invalid mapping '{part}' in {option_name}", cfg_level)
            continue
        out[key] = value

    return out


def parse_key_map_overrides(raw: object, cfg_level: str) -> Dict[int, str]:
    parsed = parse_button_mapping_input(raw, "key_map_overrides", cfg_level)
    out: Dict[int, str] = {}
    for raw_key, raw_button in parsed.items():
        try:
            key = int(str(raw_key).strip(), 0)
        except Exception:
            log("WARN", f"Ignoring invalid keycode '{raw_key}' in key_map_overrides", cfg_level)
            continue

        button = normalize_button_name(raw_button, "key_map_overrides", cfg_level)
        if not button:
            continue
        out[key] = button
    return out


def parse_scan_map_overrides(raw: object, cfg_level: str) -> Dict[str, str]:
    parsed = parse_button_mapping_input(raw, "scan_map_overrides", cfg_level)
    out: Dict[str, str] = {}
    for raw_key, raw_button in parsed.items():
        key = str(raw_key).strip().lower().removeprefix("0x")
        if not key or not SCAN_CODE_RE.fullmatch(key):
            log("WARN", f"Ignoring invalid scan code '{raw_key}' in scan_map_overrides", cfg_level)
            continue

        button = normalize_button_name(raw_button, "scan_map_overrides", cfg_level)
        if not button:
            continue
        out[key] = button
    return out


def parse_hold_buttons(raw: object, cfg_level: str) -> Set[str]:
    candidates: List[str]
    if isinstance(raw, list):
        candidates = [str(v) for v in raw]
    else:
        text = str(raw or "").strip()
        if not text:
            text = DEFAULT_HOLD_BUTTONS_CSV
        candidates = [p.strip() for p in text.split(",") if p.strip()]

    out: Set[str] = set()
    for c in candidates:
        button = normalize_button_name(c, "hold_buttons", cfg_level)
        if button:
            out.add(button)

    if out:
        return out

    log("WARN", f"hold_buttons resolved to empty; using defaults ({DEFAULT_HOLD_BUTTONS_CSV})", cfg_level)
    return set(DEFAULT_HOLD_BUTTONS)


def resolve_button(
    code: int,
    name: str,
    scan: str,
    ignore: Set[str],
    key_map: Dict[int, str],
    scan_map: Dict[str, str],
) -> str:
    # 1) keycode mapping always wins (this restores your dpad/home/back/etc)
    btn = key_map.get(code)
    if btn:
        return btn

    # 2) scan mapping (app keys / KEY_UNKNOWN etc)
    if scan:
        if scan in ignore:
            return ""  # caller will skip
        btn2 = scan_map.get(scan)
        if btn2:
            return btn2
        if code == 240:
            return f"unknown_scan_{scan}"

    # 3) derive from KEY_* so you never “lose” a button again
    if name.startswith("KEY_"):
        derived = name[4:].lower()
        aliases = {
            "select": "ok",
            "enter": "ok",
            "esc": "back",
            "search": "mic",
        }
        return aliases.get(derived, derived)

    return f"key_{code}"


class EventDispatcher:
    def __init__(
        self,
        session: requests.Session,
        token: str,
        event_type: str,
        cfg_level: str,
        queue_size: int,
        post_timeout: float,
    ) -> None:
        self._session = session
        self._url = f"http://supervisor/core/api/events/{event_type}"
        self._headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self._cfg_level = cfg_level
        self._post_timeout = post_timeout
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=queue_size)
        self._worker_task: Optional[asyncio.Task] = None
        self._dropped = 0
        self._stopping = False

    async def start(self) -> None:
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._worker_task is None:
            self._session.close()
            return

        self._stopping = True
        await self._queue.join()
        self._worker_task.cancel()
        await asyncio.gather(self._worker_task, return_exceptions=True)
        self._worker_task = None
        self._session.close()

    async def emit(self, payload: dict) -> None:
        if self._stopping:
            return
        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 25 == 0:
                log(
                    "WARN",
                    f"Event queue full (size={self._queue.maxsize}); dropped={self._dropped}",
                    self._cfg_level,
                )

    async def _worker(self) -> None:
        while True:
            payload = await self._queue.get()
            try:
                await self._post(payload)
            finally:
                self._queue.task_done()

    async def _post(self, payload: dict) -> None:
        response = None
        try:
            response = await asyncio.to_thread(
                self._session.post,
                self._url,
                headers=self._headers,
                json=payload,
                timeout=self._post_timeout,
            )
            if not response.ok:
                body = (response.text or "").replace("\n", " ").strip()
                if len(body) > 180:
                    body = body[:177] + "..."
                log("WARN", f"Event post failed status={response.status_code} body='{body}'", self._cfg_level)
        except Exception as e:
            log("WARN", f"Event post error: {e}", self._cfg_level)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


def find_event_paths_for_target(target_contains: str) -> List[str]:
    txt = read_file("/proc/bus/input/devices") or ""
    blocks = re.split(r"\n\s*\n", txt.strip(), flags=re.MULTILINE)
    t = target_contains.lower()

    paths: List[str] = []
    for b in blocks:
        nm = re.search(r'^N:\s+Name="([^"]+)"', b, flags=re.MULTILINE)
        if not nm:
            continue
        name = nm.group(1)
        if t not in name.lower():
            continue

        hm = re.search(r"^H:\s+Handlers=(.+)$", b, flags=re.MULTILINE)
        if not hm:
            continue
        handlers = hm.group(1).strip().split()
        for h in handlers:
            if h.startswith("event"):
                paths.append(f"/dev/input/{h}")

    # dedupe preserve order
    out, seen = [], set()
    for p in paths:
        if p not in seen:
            out.append(p)
            seen.add(p)
    return out


async def hold_loop(
    dispatcher: EventDispatcher,
    base_payload: dict,
    stop_evt: asyncio.Event,
    delay: float,
    repeat: float,
) -> None:
    try:
        await asyncio.wait_for(stop_evt.wait(), timeout=delay)
        return
    except asyncio.TimeoutError:
        pass

    while not stop_evt.is_set():
        await dispatcher.emit({**base_payload, "type": "key_hold"})
        try:
            await asyncio.wait_for(stop_evt.wait(), timeout=repeat)
        except asyncio.TimeoutError:
            continue


async def read_device(
    dev: InputDevice,
    dispatcher: EventDispatcher,
    key_map: Dict[int, str],
    scan_map: Dict[str, str],
    ignore_scans: Set[str],
    hold_buttons: Set[str],
    hold_delay: float,
    hold_repeat: float,
    cfg_level: str,
) -> None:
    last_scan: Optional[str] = None
    holds: Dict[Tuple[int, str], Tuple[asyncio.Event, asyncio.Task]] = {}

    log("INFO", f"Reading device: {dev.path} name='{dev.name}'", cfg_level)

    try:
        async for e in dev.async_read_loop():
            if e.type == ecodes.EV_MSC and e.code == ecodes.MSC_SCAN:
                last_scan = norm_scan(e.value)
                continue

            if e.type != ecodes.EV_KEY:
                continue

            code = int(e.code)
            val = int(e.value)  # 1 down, 0 up, 2 repeat
            if val == 2:
                continue  # we generate our own holds

            scan = (last_scan or "").lower()
            last_scan = None  # single-use, avoids “sticky scan” weirdness

            name = key_name(code)
            button = resolve_button(code, name, scan, ignore_scans, key_map, scan_map)
            if not button:
                continue

            payload = {
                "device_name": dev.name or "",
                "device_path": dev.path,
                "key_code": code,
                "key_name": name,
                "scan_code": scan,
                "button": button,
                "ts": time.time(),
            }

            if val == 1:
                await dispatcher.emit({**payload, "type": "key_down"})
                log("INFO", f"KEY_DOWN dev={dev.path} code={code} name={name} scan={scan} button={button}", cfg_level)

                # start hold for these
                if button in hold_buttons:
                    key = (code, button)
                    # cancel existing
                    old = holds.pop(key, None)
                    if old:
                        old[0].set()
                        old[1].cancel()

                    stop_evt = asyncio.Event()
                    task = asyncio.create_task(
                        hold_loop(dispatcher, payload, stop_evt, hold_delay, hold_repeat)
                    )
                    holds[key] = (stop_evt, task)

            elif val == 0:
                await dispatcher.emit({**payload, "type": "key_up"})
                # stop hold if any
                key = (code, button)
                old = holds.pop(key, None)
                if old:
                    old[0].set()
                    old[1].cancel()
    finally:
        pending = [task for _, task in holds.values()]
        for stop_evt, task in holds.values():
            stop_evt.set()
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


async def main() -> None:
    opts = load_options()
    cfg_level = parse_log_level(opts.get("log_level", "INFO"))

    target_contains = str(opts.get("target_contains", DEFAULT_TARGET_CONTAINS)).strip() or DEFAULT_TARGET_CONTAINS
    event_type = parse_event_type(opts.get("event_type", DEFAULT_EVENT_TYPE), cfg_level)
    grab_device = bool(opts.get("grab_device", True))
    ignore_scans = parse_ignore_scans(str(opts.get("ignore_scancodes", DEFAULT_IGNORE_SCANCODES)))
    hold_buttons = parse_hold_buttons(opts.get("hold_buttons", DEFAULT_HOLD_BUTTONS_CSV), cfg_level)
    key_map_overrides = parse_key_map_overrides(opts.get("key_map_overrides", DEFAULT_KEY_MAP_OVERRIDES), cfg_level)
    scan_map_overrides = parse_scan_map_overrides(opts.get("scan_map_overrides", DEFAULT_SCAN_MAP_OVERRIDES), cfg_level)
    hold_delay = parse_non_negative_float("hold_delay", opts.get("hold_delay", DEFAULT_HOLD_DELAY), DEFAULT_HOLD_DELAY, cfg_level)
    hold_repeat = parse_positive_float("hold_repeat", opts.get("hold_repeat", DEFAULT_HOLD_REPEAT), DEFAULT_HOLD_REPEAT, cfg_level)
    event_queue_size = parse_positive_int(
        "event_queue_size",
        opts.get("event_queue_size", DEFAULT_EVENT_QUEUE_SIZE),
        DEFAULT_EVENT_QUEUE_SIZE,
        cfg_level,
    )
    event_post_timeout = parse_positive_float(
        "event_post_timeout",
        opts.get("event_post_timeout", DEFAULT_EVENT_POST_TIMEOUT),
        DEFAULT_EVENT_POST_TIMEOUT,
        cfg_level,
    )

    token = get_supervisor_token()
    session = requests.Session()
    dispatcher = EventDispatcher(
        session,
        token,
        event_type,
        cfg_level,
        queue_size=event_queue_size,
        post_timeout=event_post_timeout,
    )
    await dispatcher.start()
    key_map = dict(KEY_MAP)
    key_map.update(key_map_overrides)
    scan_map = dict(SCAN_MAP)
    scan_map.update(scan_map_overrides)

    log("INFO", f"Target contains: '{target_contains}'", cfg_level)
    log("INFO", f"Output event_type: '{event_type}'", cfg_level)
    log("INFO", f"Grab device: {grab_device}", cfg_level)
    log("INFO", f"Ignore scancodes: {sorted(ignore_scans)}", cfg_level)
    log("INFO", f"Hold buttons: {sorted(hold_buttons)}", cfg_level)
    log("INFO", f"Key map entries: {len(key_map)} (overrides={len(key_map_overrides)})", cfg_level)
    log("INFO", f"Scan map entries: {len(scan_map)} (overrides={len(scan_map_overrides)})", cfg_level)
    log("INFO", f"Hold: delay={hold_delay}s repeat={hold_repeat}s", cfg_level)
    log("INFO", f"Event queue size: {event_queue_size}", cfg_level)
    log("INFO", f"Event post timeout: {event_post_timeout}s", cfg_level)
    open_error_last_log: Dict[str, float] = {}

    try:
        while True:
            paths = find_event_paths_for_target(target_contains)
            if not paths:
                log("WARN", "No matching input devices yet. Retrying in 2s...", cfg_level)
                await asyncio.sleep(2)
                continue

            devs: List[InputDevice] = []
            permission_denied_paths: List[str] = []
            for p in paths:
                try:
                    d = InputDevice(p)
                    devs.append(d)
                    log("INFO", f"Opened {p} name='{d.name}'", cfg_level)
                    if grab_device:
                        try:
                            d.grab()
                            log("INFO", f"Grabbed {p} (exclusive access)", cfg_level)
                        except OSError as e:
                            log("WARN", f"Could not grab {p}: {e} (will still try to read)", cfg_level)
                except Exception as e:
                    now = time.time()
                    err = str(e)
                    msg_key = f"{p}|{err}"
                    last = open_error_last_log.get(msg_key, 0.0)
                    if now - last >= 30.0:
                        open_error_last_log[msg_key] = now
                        log("WARN", f"Failed to open {p}: {e}", cfg_level)
                    if isinstance(e, PermissionError) or "Operation not permitted" in err:
                        permission_denied_paths.append(p)

            if not devs:
                if permission_denied_paths:
                    hint = ",".join(sorted(set(permission_denied_paths)))
                    hint_key = f"perm_hint|{hint}"
                    now = time.time()
                    last = open_error_last_log.get(hint_key, 0.0)
                    if now - last >= 30.0:
                        open_error_last_log[hint_key] = now
                        log(
                            "WARN",
                            f"Permission denied for input devices ({hint}). "
                            "If AppArmor is enabled, disable it or use a custom AppArmor profile.",
                            cfg_level,
                        )
                await asyncio.sleep(2)
                continue

            tasks = [
                asyncio.create_task(
                    read_device(
                        d,
                        dispatcher,
                        key_map,
                        scan_map,
                        ignore_scans,
                        hold_buttons,
                        hold_delay,
                        hold_repeat,
                        cfg_level,
                    )
                )
                for d in devs
            ]

            # If any device task dies (disconnect), close & rediscover
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            for d in devs:
                try:
                    if grab_device:
                        try:
                            d.ungrab()
                        except Exception:
                            pass
                    d.close()
                except Exception:
                    pass

            # log exception if there was one, then retry
            for t in done:
                exc = t.exception()
                if exc:
                    log("WARN", f"Reader ended: {exc}", cfg_level)

            log("WARN", "Rediscovering in 2s...", cfg_level)
            await asyncio.sleep(2)
    finally:
        await dispatcher.stop()


if __name__ == "__main__":
    asyncio.run(main())
