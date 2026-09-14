"""Bounded, durable Zulip producers for approvals and Alertmanager state."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import unicodedata
from collections.abc import Callable
from typing import Any

from .clients import AlertmanagerClient, BrokerClient, PendingApproval, ZulipClient
from .config import Settings
from .errors import ProtocolError, RetryableBridgeError
from .state import EventState, StoredAlert


LOG = logging.getLogger("zulip_approval_bridge")
MAX_PENDING_PER_CYCLE = 500
_LABEL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_FORGED_MARKER = re.compile(r"OPS-(?:APPROVAL|ALERT|DAILY)-V1:", re.IGNORECASE)
_MARKDOWN_TRANSLATION = str.maketrans(
    {
        "@": "＠",
        "*": "∗",
        "`": "'",
        "<": "‹",
        ">": "›",
        "[": "(",
        "]": ")",
    }
)


def _safe_text(value: str, *, maximum: int, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    without_controls = "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    collapsed = " ".join(without_controls.split())
    collapsed = _FORGED_MARKER.sub("OPS-MARKER:", collapsed).translate(
        _MARKDOWN_TRANSLATION
    )
    if not collapsed:
        collapsed = fallback
    if len(collapsed) > maximum:
        collapsed = collapsed[: maximum - 1].rstrip() + "…"
    return collapsed


def _parse_timestamp(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise ProtocolError(f"Alertmanager returned an invalid {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProtocolError(f"Alertmanager returned an invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ProtocolError(f"Alertmanager returned a timezone-free {field}")
    return parsed.astimezone(timezone.utc)


def _string_mapping(value: object, field: str) -> dict[str, str]:
    if not isinstance(value, dict) or len(value) > 64:
        raise ProtocolError(f"Alertmanager returned invalid {field}")
    result: dict[str, str] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or not _LABEL_NAME.fullmatch(key)
            or not isinstance(item, str)
            or len(item) > 4_096
        ):
            raise ProtocolError(f"Alertmanager returned invalid {field}")
        result[key] = item
    return result


@dataclass(frozen=True, slots=True)
class AlertNotice:
    fingerprint: str
    episode_key: str
    display_name: str
    severity: str
    project: str
    summary: str

    @classmethod
    def parse(cls, raw: dict[str, Any]) -> "AlertNotice":
        labels = _string_mapping(raw.get("labels"), "labels")
        annotations = _string_mapping(raw.get("annotations", {}), "annotations")
        if not labels.get("alertname", "").strip():
            raise ProtocolError("Alertmanager returned an alert without alertname")
        starts_at = _parse_timestamp(raw.get("startsAt"), "startsAt")
        status = raw.get("status")
        if (
            not isinstance(status, dict)
            or status.get("state") not in {"active", "suppressed", "unprocessed"}
        ):
            raise ProtocolError("Alertmanager returned an invalid active alert status")

        canonical_labels = json.dumps(
            labels,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        fingerprint = hashlib.sha256(canonical_labels).hexdigest()
        episode_key = hashlib.sha256(
            f"{fingerprint}\n{starts_at.isoformat()}".encode("utf-8")
        ).hexdigest()
        project_source = labels.get("project") or labels.get("service") or labels.get("job") or "non renseigné"
        summary_source = annotations.get("summary") or annotations.get("description") or "alerte active"
        return cls(
            fingerprint=fingerprint,
            episode_key=episode_key,
            display_name=_safe_text(labels["alertname"], maximum=120, fallback="Alerte"),
            severity=_safe_text(labels.get("severity", "inconnue"), maximum=40, fallback="inconnue"),
            project=_safe_text(project_source, maximum=120, fallback="non renseigné"),
            summary=_safe_text(summary_source, maximum=240, fallback="alerte active"),
        )


class DurableZulipPublisher:
    """Exactly-once recovery around Zulip's non-idempotent send endpoint."""

    def __init__(self, state: EventState, zulip: ZulipClient) -> None:
        self.state = state
        self.zulip = zulip

    def publish_once(
        self,
        *,
        kind: str,
        publication_key: str,
        marker: str,
        stream: str,
        stream_id: int,
        topic: str,
        content: str,
    ) -> int:
        publication, inserted = self.state.begin_publication(
            kind=kind,
            publication_key=publication_key,
            marker=marker,
        )
        if publication.status == "published":
            assert publication.message_id is not None
            return publication.message_id

        # An existing intent means a previous POST may have reached Zulip even
        # if its response did not reach us. Reconcile by the exact marker before
        # attempting another non-idempotent send.
        if not inserted:
            recovered = self.zulip.find_own_marker(
                stream=stream,
                stream_id=stream_id,
                topic=topic,
                marker=marker,
            )
            if recovered is not None:
                self.state.mark_published(
                    kind=kind,
                    publication_key=publication_key,
                    marker=marker,
                    message_id=recovered,
                )
                return recovered

        message_id = self.zulip.send_stream_message(
            stream=stream,
            topic=topic,
            content=content,
        )
        self.state.mark_published(
            kind=kind,
            publication_key=publication_key,
            marker=marker,
            message_id=message_id,
        )
        return message_id

    def publish_mobile_once(
        self,
        *,
        publication_key: str,
        marker: str,
        stream: str,
        stream_id: int,
        topic: str,
        content: str | Callable[[], str],
    ) -> int:
        """Publish one command reply, reconciling before optional upload work."""

        publication, inserted = self.state.begin_mobile_publication(
            publication_key=publication_key,
            marker=marker,
        )
        if publication.status == "published":
            assert publication.message_id is not None
            return publication.message_id
        if not inserted:
            recovered = self.zulip.find_own_marker(
                stream=stream,
                stream_id=stream_id,
                topic=topic,
                marker=marker,
            )
            if recovered is not None:
                self.state.mark_mobile_published(
                    publication_key=publication_key,
                    marker=marker,
                    message_id=recovered,
                )
                return recovered
        resolved = content() if callable(content) else content
        message_id = self.zulip.send_stream_message(
            stream=stream,
            topic=topic,
            content=resolved,
        )
        self.state.mark_mobile_published(
            publication_key=publication_key,
            marker=marker,
            message_id=message_id,
        )
        return message_id


class PendingApprovalPublisher:
    def __init__(
        self,
        settings: Settings,
        broker: BrokerClient,
        sink: DurableZulipPublisher,
    ) -> None:
        self.settings = settings
        self.broker = broker
        self.sink = sink

    def poll_once(self) -> int:
        published = 0
        offset = 0
        while offset < MAX_PENDING_PER_CYCLE:
            limit = min(self.settings.pending_page_size, MAX_PENDING_PER_CYCLE - offset)
            page = self.broker.list_pending_public(limit=limit, offset=offset)
            for approval in page:
                if self._publish(approval):
                    published += 1
            offset += len(page)
            if len(page) < limit:
                break
        if offset == MAX_PENDING_PER_CYCLE:
            LOG.warning("pending approval publication cycle reached its safety bound")
        return published

    def _publish(self, approval: PendingApproval) -> bool:
        deadline = datetime.fromisoformat(approval.deadline.replace("Z", "+00:00"))
        if deadline <= datetime.now(timezone.utc):
            return False
        marker = f"OPS-APPROVAL-V1:{approval.action_id}"
        summary = _safe_text(approval.summary, maximum=180, fallback="Changement à valider")
        impact = _safe_text(approval.impact, maximum=180, fallback="impact à confirmer")
        rollback = _safe_text(approval.rollback, maximum=180, fallback="rollback à confirmer")
        deadline_text = deadline.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        content = (
            f"Approbation requise : {summary}. Impact : {impact}. "
            f"Rollback : {rollback}. Échéance : {deadline_text}.\n"
            "Réagir ✅ pour approuver ou ❌ pour refuser.\n"
            f"{marker}"
        )
        self.sink.publish_once(
            kind="approval",
            publication_key=approval.action_id,
            marker=marker,
            stream=self.settings.approval_stream,
            stream_id=self.settings.approval_stream_id,
            topic=self.settings.approval_topic,
            content=content,
        )
        return True


class AlertPublisher:
    def __init__(
        self,
        settings: Settings,
        alertmanager: AlertmanagerClient,
        state: EventState,
        sink: DurableZulipPublisher,
    ) -> None:
        self.settings = settings
        self.alertmanager = alertmanager
        self.state = state
        self.sink = sink

    def poll_once(self) -> tuple[int, int]:
        raw_alerts = self.alertmanager.active_alerts()
        notices = [AlertNotice.parse(alert) for alert in raw_alerts]
        by_fingerprint: dict[str, AlertNotice] = {}
        for notice in notices:
            previous = by_fingerprint.get(notice.fingerprint)
            if previous is not None and previous.episode_key != notice.episode_key:
                raise ProtocolError("Alertmanager returned overlapping episodes for one alert")
            by_fingerprint[notice.fingerprint] = notice

        fired = 0
        resolved = 0
        for fingerprint in sorted(by_fingerprint):
            notice = by_fingerprint[fingerprint]
            stored = self.state.get_alert(fingerprint)
            if stored is not None and stored.active and stored.episode_key != notice.episode_key:
                self._publish_resolution(stored)
                self.state.save_alert(
                    fingerprint=stored.fingerprint,
                    episode_key=stored.episode_key,
                    active=False,
                    display_name=stored.display_name,
                    severity=stored.severity,
                    project=stored.project,
                )
                resolved += 1
                stored = None
            if stored is None or not stored.active:
                self._publish_firing(notice)
                self.state.save_alert(
                    fingerprint=notice.fingerprint,
                    episode_key=notice.episode_key,
                    active=True,
                    display_name=notice.display_name,
                    severity=notice.severity,
                    project=notice.project,
                )
                fired += 1

        active_keys = {
            (notice.fingerprint, notice.episode_key) for notice in by_fingerprint.values()
        }
        for stored in self.state.active_alerts():
            if (stored.fingerprint, stored.episode_key) in active_keys:
                continue
            self._publish_resolution(stored)
            self.state.save_alert(
                fingerprint=stored.fingerprint,
                episode_key=stored.episode_key,
                active=False,
                display_name=stored.display_name,
                severity=stored.severity,
                project=stored.project,
            )
            resolved += 1
        return fired, resolved

    def _publish_firing(self, notice: AlertNotice) -> None:
        marker = (
            f"OPS-ALERT-V1:{notice.fingerprint}:{notice.episode_key}:firing"
        )
        content = (
            f"🔴 {notice.display_name} ({notice.severity}) — {notice.summary}. "
            f"Projet : {notice.project}.\n{marker}"
        )
        self.sink.publish_once(
            kind="alert_firing",
            publication_key=f"{notice.fingerprint}:{notice.episode_key}",
            marker=marker,
            stream=self.settings.alert_stream,
            stream_id=self.settings.alert_stream_id,
            topic=self.settings.alert_topic,
            content=content,
        )

    def _publish_resolution(self, alert: StoredAlert) -> None:
        marker = f"OPS-ALERT-V1:{alert.fingerprint}:{alert.episode_key}:resolved"
        content = (
            f"✅ Résolu : {alert.display_name} ({alert.severity}). "
            f"Projet : {alert.project}.\n{marker}"
        )
        self.sink.publish_once(
            kind="alert_resolved",
            publication_key=f"{alert.fingerprint}:{alert.episode_key}",
            marker=marker,
            stream=self.settings.alert_stream,
            stream_id=self.settings.alert_stream_id,
            topic=self.settings.alert_topic,
            content=content,
        )


class DailyReportPublisher:
    """Publish the deterministic daily summary from one bounded local file."""

    def __init__(
        self,
        settings: Settings,
        sink: DurableZulipPublisher,
        *,
        clock: Any = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.settings = settings
        self.sink = sink
        self.clock = clock

    def poll_once(self) -> bool:
        loaded = self._load_report(self.settings.daily_report_path)
        if loaded is None:
            return False
        raw, digest = loaded
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("the daily report is not valid UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise ProtocolError("the daily report must be a JSON object")
        generated = self._report_timestamp(payload.get("generated_at"))
        summary_value = payload.get("summary")
        if not isinstance(summary_value, str) or not 1 <= len(summary_value) <= 500:
            raise ProtocolError("the daily report summary is missing or excessive")
        now = self.clock().astimezone(timezone.utc)
        if generated > now + timedelta(minutes=5) or now - generated > timedelta(hours=36):
            return False
        summary = _safe_text(summary_value, maximum=500, fallback="Rapport indisponible")
        day = generated.date().isoformat()
        marker = f"OPS-DAILY-V1:{day}:{digest}"
        self.sink.publish_once(
            kind="daily",
            publication_key=digest,
            marker=marker,
            stream=self.settings.daily_stream,
            stream_id=self.settings.daily_stream_id,
            topic=self.settings.daily_topic,
            content=f"📋 Rapport quotidien : {summary}\n{marker}",
        )
        return True

    @staticmethod
    def _report_timestamp(value: object) -> datetime:
        if not isinstance(value, str) or not 1 <= len(value) <= 64:
            raise ProtocolError("the daily report has an invalid generation timestamp")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ProtocolError("the daily report has an invalid generation timestamp") from exc
        if parsed.tzinfo is None:
            raise ProtocolError("the daily report generation timestamp lacks a timezone")
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _load_report(path: Path) -> tuple[str, str] | None:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise RetryableBridgeError("the daily report cannot be opened safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise ProtocolError("the daily report is not a protected regular file")
            if metadata.st_size < 2 or metadata.st_size > 65_536:
                raise ProtocolError("the daily report size is outside its safety bound")
            chunks: list[bytes] = []
            remaining = 65_537
            while remaining:
                chunk = os.read(descriptor, min(remaining, 8_192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw_bytes = b"".join(chunks)
            if len(raw_bytes) != metadata.st_size or len(raw_bytes) > 65_536:
                raise ProtocolError("the daily report changed while it was read")
        finally:
            os.close(descriptor)
        digest = hashlib.sha256(raw_bytes).hexdigest()
        try:
            decoded = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProtocolError("the daily report is not valid UTF-8 JSON") from exc
        return decoded, digest
