#!/usr/bin/env python3
import asyncio
import json
import os
import re
import time
from typing import Dict, List, Optional, Set, Tuple

import requests
from evdev import InputDevice, ecodes

OPTIONS_PATH = "/data/options.json"


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
    return {p.strip().lower() for p in s.split(",") if p.strip()}


def key_name(code: int) -> str:
    return ecodes.KEY.get(code, f"KEY_{code}")


def resolve_button(code: int, name: str, scan: str, ignore: Set[str]) -> str:
    # 1) keycode mapping always wins (this restores your dpad/home/back/etc)
    btn = KEY_MAP.get(code)
    if btn:
        return btn

    # 2) scan mapping (app keys / KEY_UNKNOWN etc)
    if scan:
        if scan in ignore:
            return ""  # caller will skip
        btn2 = SCAN_MAP.get(scan)
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


async def fire_event(session: requests.Session, token: str, event_type: str, payload: dict) -> None:
    url = f"http://supervisor/core/api/events/{event_type}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    # Don’t let event posting kill the loop
    try:
        session.post(url, headers=headers, json=payload, timeout=3)
    except Exception:
        pass


async def hold_loop(
    session: requests.Session,
    token: str,
    event_type: str,
    base_payload: dict,
    stop_evt: asyncio.Event,
    delay: float,
    repeat: float,
) -> None:
    await asyncio.sleep(delay)
    while not stop_evt.is_set():
        await fire_event(session, token, event_type, {**base_payload, "type": "key_hold"})
        await asyncio.sleep(repeat)


async def read_device(
    dev: InputDevice,
    session: requests.Session,
    token: str,
    event_type: str,
    ignore_scans: Set[str],
    hold_delay: float,
    hold_repeat: float,
    cfg_level: str,
) -> None:
    last_scan: Optional[str] = None
    holds: Dict[Tuple[int, str], Tuple[asyncio.Event, asyncio.Task]] = {}

    log("INFO", f"Reading device: {dev.path} name='{dev.name}'", cfg_level)

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
        button = resolve_button(code, name, scan, ignore_scans)
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
            await fire_event(session, token, event_type, {**payload, "type": "key_down"})
            log("INFO", f"KEY_DOWN dev={dev.path} code={code} name={name} scan={scan} button={button}", cfg_level)

            # start hold for these
            if button in {"up", "down", "left", "right", "vol_up", "vol_down", "ch_up", "ch_down"}:
                key = (code, button)
                # cancel existing
                old = holds.pop(key, None)
                if old:
                    old[0].set()
                    old[1].cancel()

                stop_evt = asyncio.Event()
                task = asyncio.create_task(
                    hold_loop(session, token, event_type, payload, stop_evt, hold_delay, hold_repeat)
                )
                holds[key] = (stop_evt, task)

        elif val == 0:
            await fire_event(session, token, event_type, {**payload, "type": "key_up"})
            # stop hold if any
            key = (code, button)
            old = holds.pop(key, None)
            if old:
                old[0].set()
                old[1].cancel()


async def main() -> None:
    opts = load_options()
    cfg_level = str(opts.get("log_level", "INFO"))

    target_contains = str(opts.get("target_contains", "Remoter ATV3")).strip()
    event_type = str(opts.get("event_type", "atv3_evdev_bridge_command_received")).strip()
    grab_device = bool(opts.get("grab_device", True))
    ignore_scans = parse_ignore_scans(str(opts.get("ignore_scancodes", "700aa")))
    hold_delay = float(opts.get("hold_delay", 0.25))
    hold_repeat = float(opts.get("hold_repeat", 0.10))

    token = get_supervisor_token()
    session = requests.Session()

    log("INFO", f"Target contains: '{target_contains}'", cfg_level)
    log("INFO", f"Output event_type: '{event_type}'", cfg_level)
    log("INFO", f"Grab device: {grab_device}", cfg_level)
    log("INFO", f"Ignore scancodes: {sorted(ignore_scans)}", cfg_level)
    log("INFO", f"Hold: delay={hold_delay}s repeat={hold_repeat}s", cfg_level)

    while True:
        paths = find_event_paths_for_target(target_contains)
        if not paths:
            log("WARN", "No matching input devices yet. Retrying in 2s...", cfg_level)
            await asyncio.sleep(2)
            continue

        devs: List[InputDevice] = []
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
                log("WARN", f"Failed to open {p}: {e}", cfg_level)

        if not devs:
            await asyncio.sleep(2)
            continue

        tasks = [asyncio.create_task(read_device(d, session, token, event_type, ignore_scans, hold_delay, hold_repeat, cfg_level))
                 for d in devs]

        # If any device task dies (disconnect), close & rediscover
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in pending:
            t.cancel()
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


if __name__ == "__main__":
    asyncio.run(main())
