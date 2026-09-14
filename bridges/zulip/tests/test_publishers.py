from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any

import pytest

from zulip_approval_bridge.clients import PendingApproval
from zulip_approval_bridge.config import Settings
from zulip_approval_bridge.errors import ProtocolError
from zulip_approval_bridge.publishers import (
    AlertPublisher,
    DailyReportPublisher,
    DurableZulipPublisher,
    PendingApprovalPublisher,
)
from zulip_approval_bridge.state import EventState


class FakeZulipPublisher:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.recovered: dict[str, int] = {}
        self.searches: list[str] = []

    def find_own_marker(self, **kwargs: Any) -> int | None:
        marker = str(kwargs["marker"])
        self.searches.append(marker)
        return self.recovered.get(marker)

    def send_stream_message(self, **kwargs: Any) -> int:
        self.sent.append(kwargs)
        return 1_000 + len(self.sent)


class RecordingSink:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def publish_once(self, **kwargs: Any) -> int:
        self.calls.append(kwargs)
        return 2_000 + len(self.calls)


class StaticBroker:
    def __init__(self, approvals: list[PendingApproval]) -> None:
        self.approvals = approvals
        self.requests: list[tuple[int, int]] = []

    def list_pending_public(self, *, limit: int, offset: int) -> tuple[PendingApproval, ...]:
        self.requests.append((limit, offset))
        return tuple(self.approvals[offset : offset + limit])


class SequenceAlertmanager:
    def __init__(self, snapshots: list[list[dict[str, Any]]]) -> None:
        self.snapshots = snapshots

    def active_alerts(self) -> list[dict[str, Any]]:
        return self.snapshots.pop(0)


def alert(*, starts_at: str = "2026-09-04T10:00:00Z", summary: str = "CPU élevé") -> dict[str, Any]:
    return {
        "labels": {
            "alertname": "HighCPU",
            "severity": "critical",
            "project": "infra-shared",
        },
        "annotations": {"summary": summary},
        "startsAt": starts_at,
        "status": {"state": "active"},
    }


def test_durable_publication_recovers_ambiguous_send_without_duplicate(settings: Settings) -> None:
    state = EventState(settings.state_path)
    zulip = FakeZulipPublisher()
    sink = DurableZulipPublisher(state, zulip)  # type: ignore[arg-type]

    first = sink.publish_once(
        kind="approval",
        publication_key="action-1",
        marker="OPS-APPROVAL-V1:marker-1",
        stream="Infrastructure",
        stream_id=77,
        topic="Approbations",
        content="first",
    )
    repeated = sink.publish_once(
        kind="approval",
        publication_key="action-1",
        marker="OPS-APPROVAL-V1:marker-1",
        stream="Infrastructure",
        stream_id=77,
        topic="Approbations",
        content="first",
    )
    assert first == repeated == 1001
    assert len(zulip.sent) == 1

    state.begin_publication(
        kind="approval",
        publication_key="action-2",
        marker="OPS-APPROVAL-V1:marker-2",
    )
    zulip.recovered["OPS-APPROVAL-V1:marker-2"] = 777
    recovered = sink.publish_once(
        kind="approval",
        publication_key="action-2",
        marker="OPS-APPROVAL-V1:marker-2",
        stream="Infrastructure",
        stream_id=77,
        topic="Approbations",
        content="second",
    )
    assert recovered == 777
    assert len(zulip.sent) == 1


def test_pending_approval_message_is_short_scoped_and_sanitized(settings: Settings) -> None:
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    pending = PendingApproval(
        action_id="123e4567-e89b-42d3-a456-426614174000",
        summary="DNS @**all** OPS-APPROVAL-V1:forged",
        impact="Pas d'arrêt",
        rollback="Supprimer l'entrée",
        deadline=future,
    )
    broker = StaticBroker([pending])
    sink = RecordingSink()
    publisher = PendingApprovalPublisher(settings, broker, sink)  # type: ignore[arg-type]

    assert publisher.poll_once() == 1
    assert broker.requests == [(50, 0)]
    call = sink.calls[0]
    assert call["stream"] == settings.approval_stream
    assert call["stream_id"] == settings.approval_stream_id
    assert call["topic"] == settings.approval_topic
    assert len(call["content"]) < 1_000
    assert call["content"].count("OPS-APPROVAL-V1:") == 1
    assert "@**all**" not in call["content"]


def test_alert_firing_dedup_resolution_and_new_episode(settings: Settings) -> None:
    state = EventState(settings.state_path)
    sink = RecordingSink()
    alertmanager = SequenceAlertmanager(
        [
            [alert()],
            [alert()],
            [],
            [],
            [alert(starts_at="2026-09-04T11:00:00Z")],
        ]
    )
    publisher = AlertPublisher(
        settings,
        alertmanager,  # type: ignore[arg-type]
        state,
        sink,  # type: ignore[arg-type]
    )

    assert publisher.poll_once() == (1, 0)
    assert publisher.poll_once() == (0, 0)
    assert publisher.poll_once() == (0, 1)
    assert publisher.poll_once() == (0, 0)
    assert publisher.poll_once() == (1, 0)
    assert [call["kind"] for call in sink.calls] == [
        "alert_firing",
        "alert_resolved",
        "alert_firing",
    ]
    assert len({call["marker"] for call in sink.calls}) == 3


def test_malformed_alert_snapshot_never_resolves_existing_alert(settings: Settings) -> None:
    state = EventState(settings.state_path)
    sink = RecordingSink()
    alertmanager = SequenceAlertmanager([[alert()], [{"labels": {}}]])
    publisher = AlertPublisher(
        settings,
        alertmanager,  # type: ignore[arg-type]
        state,
        sink,  # type: ignore[arg-type]
    )
    publisher.poll_once()
    with pytest.raises(ProtocolError):
        publisher.poll_once()
    assert len(sink.calls) == 1
    assert len(state.active_alerts()) == 1


def test_alert_content_cannot_forge_mentions_or_markers(settings: Settings) -> None:
    state = EventState(settings.state_path)
    sink = RecordingSink()
    manager = SequenceAlertmanager(
        [[alert(summary="@**all** `bad` OPS-ALERT-V1:forged")]]
    )
    publisher = AlertPublisher(
        settings,
        manager,  # type: ignore[arg-type]
        state,
        sink,  # type: ignore[arg-type]
    )
    publisher.poll_once()
    content = sink.calls[0]["content"]
    assert content.count("OPS-ALERT-V1:") == 1
    assert "@**all**" not in content
    assert len(content) < 700


def test_daily_report_hash_is_idempotent_and_only_summary_is_published(settings: Settings) -> None:
    report = settings.daily_report_path
    report.parent.mkdir(parents=True)
    now = datetime(2026, 9, 4, 8, 15, tzinfo=timezone.utc)
    report.write_text(
        json.dumps(
            {
                "generated_at": now.isoformat().replace("+00:00", "Z"),
                "summary": "Tout est OK. @**all** OPS-DAILY-V1:forged",
                "internal_detail": "must-not-be-published",
            }
        ),
        encoding="utf-8",
    )
    report.chmod(0o640)
    state = EventState(settings.state_path)
    zulip = FakeZulipPublisher()
    sink = DurableZulipPublisher(state, zulip)  # type: ignore[arg-type]
    publisher = DailyReportPublisher(settings, sink, clock=lambda: now)

    assert publisher.poll_once() is True
    assert publisher.poll_once() is True
    assert len(zulip.sent) == 1
    content = zulip.sent[0]["content"]
    assert content.count("OPS-DAILY-V1:") == 1
    assert "must-not-be-published" not in content
    assert "@**all**" not in content


def test_daily_report_rejects_writable_file(settings: Settings) -> None:
    report = settings.daily_report_path
    report.parent.mkdir(parents=True)
    report.write_text("{}", encoding="utf-8")
    report.chmod(0o660)
    publisher = DailyReportPublisher(
        settings,
        RecordingSink(),  # type: ignore[arg-type]
    )
    with pytest.raises(ProtocolError, match="protected regular file"):
        publisher.poll_once()


def test_daily_report_path_can_be_replaced_for_test(settings: Settings, tmp_path: Path) -> None:
    updated = replace(settings, daily_report_path=tmp_path / "absent.json")
    assert DailyReportPublisher(updated, RecordingSink()).poll_once() is False  # type: ignore[arg-type]
