import asyncio
import json
import threading
import time
from types import SimpleNamespace

import pytest

import run


def input_event(event_type, code, value):
    return SimpleNamespace(type=event_type, code=code, value=value)


def event_payload(
    event_type="key_down",
    *,
    button="up",
    code=103,
    path="/dev/input/event-test",
    timestamp=1.0,
):
    return {
        "device_name": "Remoter ATV3 Keyboard",
        "device_path": path,
        "key_code": code,
        "key_name": "KEY_UP",
        "scan_code": "c0042",
        "button": button,
        "ts": timestamp,
        "type": event_type,
    }


def default_config():
    return run.load_config({})


def test_load_config_uses_manifest_defaults():
    config = default_config()

    assert config.target_contains == run.DEFAULT_TARGET_CONTAINS
    assert config.event_type == run.DEFAULT_EVENT_TYPE
    assert config.grab_device is True
    assert config.ignore_scans == {"700aa"}
    assert config.hold_delay == run.DEFAULT_HOLD_DELAY
    assert config.hold_repeat == run.DEFAULT_HOLD_REPEAT
    assert config.log_level == "INFO"


def test_load_config_normalizes_valid_values():
    config = run.load_config(
        {
            "target_contains": "  custom remote ",
            "event_type": "CUSTOM_event_2",
            "grab_device": "false",
            "ignore_scancodes": "700AA, c0001",
            "hold_delay": "0.05",
            "hold_repeat": 10,
            "log_level": "warning",
        }
    )

    assert config.target_contains == "custom remote"
    assert config.event_type == "CUSTOM_event_2"
    assert config.grab_device is False
    assert config.ignore_scans == {"700aa", "c0001"}
    assert config.hold_delay == 0.05
    assert config.hold_repeat == 10.0
    assert config.log_level == "WARN"


@pytest.mark.parametrize(
    ("option", "value", "message"),
    [
        ("target_contains", "", "must not be empty"),
        ("event_type", "bad event", "letters, numbers, and underscores"),
        ("event_type", "../events", "letters, numbers, and underscores"),
        ("grab_device", "sometimes", "must be a boolean"),
        ("ignore_scancodes", "700aa, nope", "non-hexadecimal"),
        ("hold_delay", 0.049, "between 0.05 and 10"),
        ("hold_repeat", 10.01, "between 0.05 and 10"),
        ("hold_repeat", "nan", "between 0.05 and 10"),
        ("hold_repeat", "inf", "between 0.05 and 10"),
        ("log_level", "TRACE", "DEBUG, INFO, WARN, or ERROR"),
    ],
)
def test_load_config_rejects_invalid_values(option, value, message):
    with pytest.raises(run.ConfigurationError, match=message):
        run.load_config({option: value})


def test_load_options_rejects_invalid_json(tmp_path):
    options_path = tmp_path / "options.json"
    options_path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(run.ConfigurationError, match="Could not read options"):
        run.load_options(str(options_path))


def test_load_options_rejects_non_object(tmp_path):
    options_path = tmp_path / "options.json"
    options_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")

    with pytest.raises(run.ConfigurationError, match="must be a JSON object"):
        run.load_options(str(options_path))


def test_resolve_button_key_map_wins_over_scan_map():
    assert run.resolve_button(103, "KEY_UP", "c0009", set()) == "up"


def test_resolve_button_scan_map_handles_app_buttons():
    assert run.resolve_button(240, "KEY_UNKNOWN", "c0009", set()) == "youtube"


def test_resolve_button_ignored_scan_skips_event():
    assert run.resolve_button(240, "KEY_UNKNOWN", "700aa", {"700aa"}) == ""


def test_resolve_button_unknown_scan_only_for_key_unknown():
    assert (
        run.resolve_button(240, "KEY_UNKNOWN", "abc123", set()) == "unknown_scan_abc123"
    )
    assert run.resolve_button(999, "KEY_CUSTOM", "abc123", set()) == "custom"


def test_key_name_normalizes_alias_lists(monkeypatch):
    monkeypatch.setitem(run.ecodes.KEY, 999, ["KEY_PRIMARY", "KEY_ALIAS"])

    assert run.key_name(999) == "KEY_PRIMARY"


def test_find_event_paths_in_text_matches_and_dedupes():
    devices = """
I: Bus=0005 Vendor=0000 Product=0000 Version=0000
N: Name="Remoter ATV3 Keyboard"
H: Handlers=sysrq kbd event2 event2

I: Bus=0005 Vendor=0000 Product=0000 Version=0000
N: Name="Other Keyboard"
H: Handlers=sysrq kbd event3

I: Bus=0005 Vendor=0000 Product=0000 Version=0000
N: Name="Remoter ATV3 Mouse"
H: Handlers=mouse0 event4
"""

    assert run.find_event_paths_in_text(devices, "remoter atv3") == [
        "/dev/input/event2",
        "/dev/input/event4",
    ]


class CaptureSender:
    def __init__(self):
        self.payloads = []

    def emit(self, payload):
        self.payloads.append(payload)
        return True

    def discard_pending_hold(self, hold_key):
        return None


class FiniteDevice:
    path = "/dev/input/event-test"
    name = "Remoter ATV3 Keyboard"

    def __init__(self, events):
        self.events = events

    async def async_read_loop(self):
        for event in self.events:
            yield event


class BlockingDevice(FiniteDevice):
    def __init__(self, events):
        super().__init__(events)
        self.cancelled = False

    async def async_read_loop(self):
        try:
            for event in self.events:
                yield event
            await asyncio.Event().wait()
        finally:
            self.cancelled = True


def test_read_device_reuses_active_mapping_for_scanless_release():
    asyncio.run(_read_device_reuses_active_mapping_for_scanless_release())


async def _read_device_reuses_active_mapping_for_scanless_release():
    sender = CaptureSender()
    device = FiniteDevice(
        [
            input_event(run.ecodes.EV_MSC, run.ecodes.MSC_SCAN, int("c0009", 16)),
            input_event(run.ecodes.EV_KEY, 240, 1),
            input_event(run.ecodes.EV_KEY, 240, 0),
        ]
    )

    await run.read_device(device, sender, set(), 0.25, 0.1, "ERROR")

    assert [(item["type"], item["button"]) for item in sender.payloads] == [
        ("key_down", "youtube"),
        ("key_up", "youtube"),
    ]
    assert set(sender.payloads[0]) == {
        "device_name",
        "device_path",
        "key_code",
        "key_name",
        "scan_code",
        "button",
        "ts",
        "type",
    }


def test_read_device_suppresses_scanless_release_for_ignored_press():
    asyncio.run(_read_device_suppresses_scanless_release_for_ignored_press())


async def _read_device_suppresses_scanless_release_for_ignored_press():
    sender = CaptureSender()
    device = FiniteDevice(
        [
            input_event(run.ecodes.EV_MSC, run.ecodes.MSC_SCAN, int("700aa", 16)),
            input_event(run.ecodes.EV_KEY, 240, 1),
            input_event(run.ecodes.EV_KEY, 240, 0),
        ]
    )

    await run.read_device(device, sender, {"700aa"}, 0.25, 0.1, "ERROR")

    assert sender.payloads == []


def test_kernel_repeat_clears_scan_state():
    asyncio.run(_kernel_repeat_clears_scan_state())


async def _kernel_repeat_clears_scan_state():
    sender = CaptureSender()
    device = FiniteDevice(
        [
            input_event(run.ecodes.EV_MSC, run.ecodes.MSC_SCAN, int("c0009", 16)),
            input_event(run.ecodes.EV_KEY, 240, 2),
            input_event(run.ecodes.EV_KEY, 240, 1),
            input_event(run.ecodes.EV_KEY, 240, 0),
        ]
    )

    await run.read_device(device, sender, set(), 0.25, 0.1, "ERROR")

    assert [item["button"] for item in sender.payloads] == ["key_240", "key_240"]


def test_input_overrun_clears_holds_and_skips_damaged_frame(monkeypatch):
    async def scenario():
        sender = run.EventSender(FakeSession(), "token", "event_name", "ERROR")
        hold_started = asyncio.Event()
        hold_stopped = asyncio.Event()

        async def hold_loop(sender, payload, stop_event, delay, repeat):
            sender.emit({**payload, "type": "key_hold"})
            hold_started.set()
            try:
                await stop_event.wait()
            finally:
                hold_stopped.set()

        monkeypatch.setattr(run, "hold_loop", hold_loop)

        class OverrunDevice(FiniteDevice):
            def active_keys(self):
                return []

            async def async_read_loop(self):
                yield input_event(run.ecodes.EV_KEY, 115, 1)
                await asyncio.wait_for(hold_started.wait(), 1)
                assert sender.pending_count == 2

                yield input_event(run.ecodes.EV_SYN, run.ecodes.SYN_DROPPED, 0)
                assert hold_stopped.is_set()
                assert sender.pending_count == 1

                yield input_event(run.ecodes.EV_KEY, 116, 1)
                yield input_event(run.ecodes.EV_MSC, run.ecodes.MSC_SCAN, 0xC0009)
                yield input_event(run.ecodes.EV_SYN, run.ecodes.SYN_REPORT, 0)
                yield input_event(run.ecodes.EV_KEY, 240, 1)
                yield input_event(run.ecodes.EV_KEY, 240, 0)

                hold_started.clear()
                yield input_event(run.ecodes.EV_KEY, 115, 1)
                await asyncio.wait_for(hold_started.wait(), 1)
                yield input_event(run.ecodes.EV_KEY, 115, 0)

        await run.read_device(OverrunDevice([]), sender, set(), 0.25, 0.1, "ERROR")

        assert [
            (item.payload["type"], item.payload["button"]) for item in sender._queue
        ] == [
            ("key_down", "vol_up"),
            ("key_down", "key_240"),
            ("key_up", "key_240"),
            ("key_down", "vol_up"),
            ("key_up", "vol_up"),
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("keys_down", "released_button"), [([240], "youtube"), ([], "key_240")]
)
def test_input_overrun_resyncs_release_mapping(keys_down, released_button):
    class OverrunDevice(FiniteDevice):
        def active_keys(self):
            return keys_down

    sender = CaptureSender()
    device = OverrunDevice(
        [
            input_event(run.ecodes.EV_MSC, run.ecodes.MSC_SCAN, 0xC0009),
            input_event(run.ecodes.EV_KEY, 240, 1),
            input_event(run.ecodes.EV_SYN, run.ecodes.SYN_DROPPED, 0),
            input_event(run.ecodes.EV_SYN, run.ecodes.SYN_REPORT, 0),
            input_event(run.ecodes.EV_KEY, 240, 0),
        ]
    )

    asyncio.run(run.read_device(device, sender, set(), 0.25, 0.1, "ERROR"))

    assert [(item["type"], item["button"]) for item in sender.payloads] == [
        ("key_down", "youtube"),
        ("key_up", released_button),
    ]


def test_read_device_cleans_up_hold_task_on_cancellation():
    asyncio.run(_read_device_cleans_up_hold_task_on_cancellation())


async def _read_device_cleans_up_hold_task_on_cancellation():
    sender = CaptureSender()
    device = BlockingDevice([input_event(run.ecodes.EV_KEY, 103, 1)])
    task = asyncio.create_task(
        run.read_device(device, sender, set(), 0.01, 0.01, "ERROR")
    )

    await asyncio.sleep(0.035)
    holds = [payload for payload in sender.payloads if payload["type"] == "key_hold"]
    assert holds
    assert all(hold["ts"] > sender.payloads[0]["ts"] for hold in holds)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    count_after_cancel = len(sender.payloads)
    await asyncio.sleep(0.035)
    assert len(sender.payloads) == count_after_cancel


def test_reader_disconnect_removes_queued_hold():
    asyncio.run(_reader_disconnect_removes_queued_hold())


async def _reader_disconnect_removes_queued_hold():
    sender = run.EventSender(FakeSession(), "token", "event_name", "ERROR")
    device = BlockingDevice([input_event(run.ecodes.EV_KEY, 103, 1)])
    task = asyncio.create_task(
        run.read_device(device, sender, set(), 0.01, 0.01, "ERROR")
    )
    await asyncio.sleep(0.035)
    assert any(item.payload["type"] == "key_hold" for item in sender._queue)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert all(item.payload["type"] != "key_hold" for item in sender._queue)


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response or FakeResponse(201)
        self.exc = exc
        self.calls = []

    def post(self, url, headers, json, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "json": dict(json), "timeout": timeout}
        )
        if self.exc:
            raise self.exc
        return self.response


class BlockingSession(FakeSession):
    def __init__(self, release, wait_timeout=0.5):
        super().__init__()
        self.release = release
        self.wait_timeout = wait_timeout

    def post(self, url, headers, json, timeout):
        self.calls.append(
            {"url": url, "headers": headers, "json": dict(json), "timeout": timeout}
        )
        self.release.wait(self.wait_timeout)
        return self.response


def test_post_event_sync_success():
    session = FakeSession(FakeResponse(201))

    result = run.post_event_sync(session, "token", "event_name", {"button": "up"})

    assert result.ok is True
    assert result.status_code == 201
    assert session.calls[0]["url"] == "http://supervisor/core/api/events/event_name"
    assert session.calls[0]["timeout"] == run.POST_TIMEOUT_SECONDS


def test_post_event_sync_exception():
    result = run.post_event_sync(
        FakeSession(exc=TimeoutError("slow")), "token", "event_name", {}
    )

    assert result.ok is False
    assert "slow" in result.error


def test_post_event_sync_non_2xx():
    result = run.post_event_sync(
        FakeSession(FakeResponse(500, "server error")),
        "token",
        "event_name",
        {},
    )

    assert result.ok is False
    assert result.status_code == 500
    assert result.error == "server error"


def test_event_sender_posts_off_the_event_loop():
    asyncio.run(_event_sender_posts_off_the_event_loop())


async def _event_sender_posts_off_the_event_loop():
    release = threading.Event()
    session = BlockingSession(release)
    sender = run.EventSender(session, "token", "event_name", "ERROR")
    sender.start()
    sender.emit(event_payload())

    await asyncio.sleep(0.01)
    assert sender._in_flight is True

    release.set()
    await sender.wait_until_idle()
    await sender.stop()
    assert sender.success_count == 1


def test_event_sender_coalesces_pending_holds():
    asyncio.run(_event_sender_coalesces_pending_holds())


async def _event_sender_coalesces_pending_holds():
    session = FakeSession()
    sender = run.EventSender(session, "token", "event_name", "ERROR")

    assert sender.emit(event_payload("key_hold", timestamp=1.0)) is True
    assert sender.emit(event_payload("key_hold", timestamp=2.0)) is True
    assert sender.pending_count == 1
    assert sender.dropped_count == 1

    sender.start()
    await sender.wait_until_idle()
    await sender.stop()

    assert [call["json"]["ts"] for call in session.calls] == [2.0]


def test_event_sender_release_removes_queued_hold_and_is_preserved():
    asyncio.run(_event_sender_release_removes_queued_hold_and_is_preserved())


async def _event_sender_release_removes_queued_hold_and_is_preserved():
    session = FakeSession()
    sender = run.EventSender(session, "token", "event_name", "ERROR", queue_size=2)

    sender.emit(event_payload("key_hold"))
    sender.emit(event_payload("key_down", button="left", code=105))
    assert sender.emit(event_payload("key_up")) is True
    assert sender.pending_count == 2

    sender.start()
    await sender.wait_until_idle()
    await sender.stop()

    assert [call["json"]["type"] for call in session.calls] == [
        "key_down",
        "key_up",
    ]


def test_event_sender_evicts_oldest_transition_when_full():
    asyncio.run(_event_sender_evicts_oldest_transition_when_full())


async def _event_sender_evicts_oldest_transition_when_full():
    session = FakeSession()
    sender = run.EventSender(session, "token", "event_name", "ERROR", queue_size=2)

    sender.emit(event_payload(button="up", code=103))
    sender.emit(event_payload(button="left", code=105))
    sender.emit(event_payload(button="right", code=106))

    sender.start()
    await sender.wait_until_idle()
    await sender.stop()

    assert [call["json"]["button"] for call in session.calls] == ["left", "right"]
    assert sender.dropped_count == 1


def test_event_sender_discards_stale_events_without_retry():
    asyncio.run(_event_sender_discards_stale_events_without_retry())


async def _event_sender_discards_stale_events_without_retry():
    session = FakeSession()
    sender = run.EventSender(
        session, "token", "event_name", "ERROR", max_event_age=0.01
    )
    sender.emit(event_payload())
    await asyncio.sleep(0.02)

    sender.start()
    await sender.wait_until_idle()
    await sender.stop()

    assert session.calls == []
    assert sender.dropped_count == 1


def test_event_sender_counts_http_failures():
    asyncio.run(_event_sender_counts_http_failures())


async def _event_sender_counts_http_failures():
    sender = run.EventSender(
        FakeSession(FakeResponse(503, "unavailable")),
        "token",
        "event_name",
        "ERROR",
    )
    sender.start()
    sender.emit(event_payload())

    await sender.wait_until_idle()
    await sender.stop()

    assert sender.success_count == 0
    assert sender.failure_count == 1


def test_event_sender_shutdown_is_bounded():
    asyncio.run(_event_sender_shutdown_is_bounded())


async def _event_sender_shutdown_is_bounded():
    release = threading.Event()
    sender = run.EventSender(
        BlockingSession(release, wait_timeout=0.1),
        "token",
        "event_name",
        "ERROR",
        shutdown_timeout=0.01,
    )
    sender.start()
    sender.emit(event_payload())
    await asyncio.sleep(0.005)

    started = time.monotonic()
    await sender.stop()
    elapsed = time.monotonic() - started
    release.set()

    assert elapsed < 0.08


class OpenDevice:
    def __init__(self, path, *, has_keys=True, grab_error=None):
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.has_keys = has_keys
        self.grab_error = grab_error
        self.grabbed = False
        self.ungrabbed = False
        self.closed = False

    def capabilities(self):
        return {run.ecodes.EV_KEY: []} if self.has_keys else {}

    def grab(self):
        if self.grab_error:
            raise self.grab_error
        self.grabbed = True

    def ungrab(self):
        self.ungrabbed = True

    def close(self):
        self.closed = True


def test_open_devices_skips_non_key_devices_and_closes_cleanly(monkeypatch):
    keyboard = OpenDevice("/dev/input/event1")
    mouse = OpenDevice("/dev/input/event2", has_keys=False)
    devices_by_path = {keyboard.path: keyboard, mouse.path: mouse}
    monkeypatch.setattr(run, "InputDevice", devices_by_path.__getitem__)

    opened = run.open_devices(devices_by_path, True, "ERROR")

    assert opened == [keyboard]
    assert keyboard.grabbed is True
    assert mouse.closed is True

    run.close_devices(opened, True)
    assert keyboard.ungrabbed is True
    assert keyboard.closed is True


def test_open_devices_rolls_back_partial_set_when_grab_fails(monkeypatch):
    first = OpenDevice("/dev/input/event1")
    second = OpenDevice("/dev/input/event2", grab_error=OSError("Resource busy"))
    devices_by_path = {first.path: first, second.path: second}
    monkeypatch.setattr(run, "InputDevice", devices_by_path.__getitem__)

    opened = run.open_devices(devices_by_path, True, "ERROR")

    assert opened == []
    assert first.ungrabbed is True
    assert first.closed is True
    assert second.closed is True


def test_run_readers_cancels_siblings_when_one_reader_finishes():
    asyncio.run(_run_readers_cancels_siblings_when_one_reader_finishes())


async def _run_readers_cancels_siblings_when_one_reader_finishes():
    finished = FiniteDevice([])
    blocked = BlockingDevice([])
    should_rediscover = await run.run_readers(
        [finished, blocked], CaptureSender(), default_config(), asyncio.Event()
    )

    assert should_rediscover is True
    assert blocked.cancelled is True


def test_run_readers_stops_without_requesting_rediscovery():
    asyncio.run(_run_readers_stops_without_requesting_rediscovery())


async def _run_readers_stops_without_requesting_rediscovery():
    first = BlockingDevice([])
    second = BlockingDevice([])
    stop_event = asyncio.Event()
    task = asyncio.create_task(
        run.run_readers([first, second], CaptureSender(), default_config(), stop_event)
    )
    await asyncio.sleep(0)
    stop_event.set()

    should_rediscover = await task

    assert should_rediscover is False
    assert first.cancelled is True
    assert second.cancelled is True
