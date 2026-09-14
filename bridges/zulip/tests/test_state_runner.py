from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from zulip_approval_bridge.bridge import BridgeRunner
from zulip_approval_bridge.clients import QueueCursor
from zulip_approval_bridge.config import Settings
from zulip_approval_bridge.errors import ConfigurationError, RetryableBridgeError
from zulip_approval_bridge.state import EventState


class StaticZulip:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.register_count = 0

    def register_queue(self) -> QueueCursor:
        self.register_count += 1
        return QueueCursor("queue-1", -1)

    def get_events(self, _: QueueCursor) -> list[dict[str, Any]]:
        return deepcopy(self.events)


class CountingProcessor:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.calls = 0
        self.fail_once = fail_once

    def process(self, _: dict[str, Any], __: str) -> str:
        self.calls += 1
        if self.fail_once and self.calls == 1:
            raise RetryableBridgeError("simulated transient failure")
        return "approved"


def runner(
    settings: Settings,
    state: EventState,
    zulip: StaticZulip,
    processor: CountingProcessor,
) -> BridgeRunner:
    # The runner is deliberately structural here; no real HTTP method is used.
    return BridgeRunner(settings, state, zulip, processor)  # type: ignore[arg-type]


def test_event_and_cursor_commit_atomically_and_skip_duplicate(
    settings: Settings, approval_event: dict[str, object]
) -> None:
    state = EventState(settings.state_path)
    zulip = StaticZulip([approval_event])
    processor = CountingProcessor()
    service = runner(settings, state, zulip, processor)
    initial = service.cursor()

    current = service.poll_once(initial)
    assert current.last_event_id == 12
    assert processor.calls == 1
    assert state.is_processed(settings.realm_fingerprint, "queue-1", 12)

    # Supplying the stale cursor simulates an event redelivery after a crash.
    repeated = service.poll_once(initial)
    assert repeated.last_event_id == 12
    assert processor.calls == 1


def test_transient_failure_does_not_acknowledge_event(
    settings: Settings, approval_event: dict[str, object]
) -> None:
    state = EventState(settings.state_path)
    zulip = StaticZulip([approval_event])
    processor = CountingProcessor(fail_once=True)
    service = runner(settings, state, zulip, processor)
    initial = service.cursor()

    with pytest.raises(RetryableBridgeError):
        service.poll_once(initial)
    stored = state.load_cursor(settings.realm_fingerprint)
    assert stored is not None
    assert stored.last_event_id == -1
    assert not state.is_processed(settings.realm_fingerprint, "queue-1", 12)

    current = service.poll_once(initial)
    assert current.last_event_id == 12
    assert processor.calls == 2


def test_state_file_is_private(settings: Settings) -> None:
    EventState(settings.state_path)
    assert settings.state_path.stat().st_mode & 0o077 == 0


def test_state_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("not a database", encoding="utf-8")
    link = tmp_path / "state.db"
    link.symlink_to(target)
    with pytest.raises(ConfigurationError, match="non-symlink"):
        EventState(link)
