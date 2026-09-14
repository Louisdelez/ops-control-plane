"""Deterministic, human-authenticated mobile mission commands."""

from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import stat
from typing import Any
import unicodedata
import uuid

from .clients import BrokerClient, MobileMission, ZulipClient
from .config import Settings
from .errors import BrokerRejected, ProtocolError, ResourceNotFound
from .publishers import DurableZulipPublisher, _safe_text


_PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_HELP = (
    "Commandes : `!ops mission <projet> <titre>`, `!ops statut <uuid>`, "
    "`!ops reprendre <uuid>`, `!ops répondre <uuid> <texte>`, `!ops rapport`, "
    "`!ops capture <uuid>`."
)


class _CommandHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.valid = True

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"p", "br"} or attrs:
            self.valid = False
        if tag == "br":
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag != "p":
            self.valid = False
        else:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_comment(self, data: str) -> None:
        self.valid = False


def _plain_command(rendered: object) -> str | None:
    if not isinstance(rendered, str) or not 1 <= len(rendered) <= 2_000:
        raise ProtocolError("Zulip returned invalid mobile command content")
    parser = _CommandHTML()
    try:
        parser.feed(rendered)
        parser.close()
    except Exception as exc:
        raise ProtocolError("Zulip returned malformed mobile command HTML") from exc
    text = "".join(parser.parts).strip()
    if not parser.valid or "\n" in text or len(text) > 500:
        return None
    return unicodedata.normalize("NFKC", text)


def _read_protected(path: Path, maximum: int) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProtocolError("the requested mobile artifact cannot be opened safely") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not 1 <= metadata.st_size <= maximum
        ):
            raise ProtocolError("the requested mobile artifact is not a protected bounded file")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65_536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        if len(content) != metadata.st_size or len(content) > maximum:
            raise ProtocolError("the requested mobile artifact changed while it was read")
        return content
    finally:
        os.close(descriptor)


class MobileCommandProcessor:
    """Accept only explicit commands from freshly verified human Zulip IDs."""

    def __init__(
        self,
        settings: Settings,
        zulip: ZulipClient,
        broker: BrokerClient,
        sink: DurableZulipPublisher,
    ) -> None:
        self.settings = settings
        self.zulip = zulip
        self.broker = broker
        self.sink = sink

    def process(self, event: dict[str, Any], queue_id: str) -> str:
        raw_message = event.get("message")
        if not isinstance(raw_message, dict):
            raise ProtocolError("a Zulip message event lacks its message object")
        message_id = raw_message.get("id")
        if type(message_id) is not int or message_id <= 0:
            raise ProtocolError("a Zulip message event has an invalid message ID")
        try:
            message = self.zulip.get_message(message_id)
        except ResourceNotFound:
            return "ignored_missing_message"
        if not self._is_mobile_message(message, message_id):
            return "ignored_wrong_message_scope"

        sender_id = message["sender_id"]
        assert type(sender_id) is int
        if sender_id not in self.settings.approver_user_ids:
            return "ignored_unauthorized_actor"
        try:
            user = self.zulip.get_user(sender_id)
        except ResourceNotFound:
            return "ignored_missing_actor"
        if (
            type(user.get("user_id")) is not int
            or user["user_id"] != sender_id
            or user.get("is_bot") is not False
            or user.get("is_active") is not True
            or user.get("is_deleted") is True
            or user.get("is_imported_stub") is True
        ):
            return "ignored_non_human_actor"

        command = _plain_command(message.get("content"))
        if command is None or not command.startswith("!ops"):
            return "ignored_non_command_message"
        event_id = event.get("id")
        if type(event_id) is not int or event_id < 0:
            raise ProtocolError("a Zulip event contains an invalid id")
        queue_hash = hashlib.sha256(queue_id.encode("utf-8")).hexdigest()[:16]
        source = f"zulip-mobile:{self.settings.realm_fingerprint}:{queue_hash}:{event_id}"
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, source))
        marker = f"OPS-MOBILE-V1:{request_id}"
        topic = message["subject"]
        assert isinstance(topic, str)

        content: str | Any
        outcome = "mobile_invalid_command"
        try:
            content, outcome = self._execute(command, request_id, sender_id)
        except BrokerRejected:
            content = "Demande refusée par la politique Ops. Consulte le Dell pour le détail."
            outcome = "mobile_broker_rejected"
        content_factory = content if callable(content) else f"{content}\n{marker}"
        if callable(content):
            original = content

            def with_marker() -> str:
                return f"{original()}\n{marker}"

            content_factory = with_marker
        self.sink.publish_mobile_once(
            publication_key=source,
            marker=marker,
            stream=self.settings.approval_stream,
            stream_id=self.settings.approval_stream_id,
            topic=topic,
            content=content_factory,
        )
        return outcome

    def _is_mobile_message(self, message: dict[str, Any], message_id: int) -> bool:
        topic = message.get("subject")
        return (
            type(message.get("id")) is int
            and message["id"] == message_id
            and message.get("type") == "stream"
            and type(message.get("stream_id")) is int
            and message["stream_id"] == self.settings.approval_stream_id
            and message.get("display_recipient") == self.settings.approval_stream
            and type(message.get("sender_id")) is int
            and message["sender_id"] != self.settings.bot_user_id
            and isinstance(topic, str)
            and 1 <= len(topic) <= 200
            and not any(ord(character) < 32 or ord(character) == 127 for character in topic)
        )

    def _execute(
        self, command: str, request_id: str, sender_id: int
    ) -> tuple[str | Any, str]:
        mission_match = re.fullmatch(r"!ops mission ([a-z0-9][a-z0-9-]{1,63}) (.+)", command)
        if mission_match:
            project, title = mission_match.groups()
            title = " ".join(title.split())
            if (
                not _PROJECT.fullmatch(project)
                or not 1 <= len(title) <= 240
                or any(unicodedata.category(character).startswith("C") for character in title)
            ):
                return _HELP, "mobile_invalid_command"
            mission = self.broker.create_mobile_mission(
                request_id=request_id, project_id=project, title=title
            )
            safe_title = _safe_text(mission.title, maximum=180, fallback="Mission")
            return (
                f"Mission lancée : {safe_title} ({mission.project_id}). ID : `{mission.id}`.",
                "mobile_mission_created",
            )

        status_match = re.fullmatch(r"!ops statut ([0-9a-f-]{36})", command)
        if status_match and _UUID.fullmatch(status_match.group(1)):
            mission = self.broker.get_mobile_mission(status_match.group(1))
            return self._status_text(mission), "mobile_status_returned"

        resume_match = re.fullmatch(r"!ops reprendre ([0-9a-f-]{36})", command)
        if resume_match and _UUID.fullmatch(resume_match.group(1)):
            mission = self.broker.resume_mobile_mission(
                mission_id=resume_match.group(1), request_id=request_id
            )
            return f"Reprise demandée. {self._status_text(mission)}", "mobile_mission_resumed"

        answer_match = re.fullmatch(
            r"!ops r(?:é|e)pondre ([0-9a-f-]{36}) (.+)", command
        )
        if answer_match and _UUID.fullmatch(answer_match.group(1)):
            answer = " ".join(answer_match.group(2).split())
            if (
                not 1 <= len(answer) <= 500
                or any(unicodedata.category(character).startswith("C") for character in answer)
            ):
                return _HELP, "mobile_invalid_command"
            self.broker.answer_mobile_mission(
                mission_id=answer_match.group(1),
                request_id=request_id,
                source_user_id=sender_id,
                answer=answer,
            )
            return "Réponse transmise à l’équipe Ops.", "mobile_answer_recorded"

        if command == "!ops rapport":
            def report_link() -> str:
                content = _read_protected(self.settings.daily_report_path, 65_536)
                if content is None:
                    return "Aucun rapport quotidien n’est disponible."
                uri = self.zulip.upload_file(
                    filename="rapport-ops-latest.json",
                    content=content,
                    content_type="application/json",
                )
                return f"Rapport complet : [ouvrir le JSON]({uri})."

            return report_link, "mobile_report_returned"

        capture_match = re.fullmatch(r"!ops capture ([0-9a-f-]{36})", command)
        if capture_match and _UUID.fullmatch(capture_match.group(1)):
            mission_id = capture_match.group(1)
            # Confirm that the fixed mobile broker role can read this mission
            # before touching its correspondingly named local artifact.
            self.broker.get_mobile_mission(mission_id)

            def capture_link() -> str:
                content = _read_protected(self.settings.capture_dir / f"{mission_id}.png", 5_242_880)
                if content is None:
                    return "Aucune capture validée n’est disponible pour cette mission."
                if not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    raise ProtocolError("the requested capture is not a PNG file")
                uri = self.zulip.upload_file(
                    filename=f"capture-{mission_id}.png",
                    content=content,
                    content_type="image/png",
                )
                return f"Capture : [ouvrir l’image]({uri})."

            return capture_link, "mobile_capture_returned"

        return _HELP, "mobile_invalid_command"

    @staticmethod
    def _status_text(mission: MobileMission) -> str:
        title = _safe_text(mission.title, maximum=160, fallback="Mission")
        queue = mission.queue_state or "nouvelle"
        return (
            f"{title} ({mission.project_id}) : statut `{mission.status}`, "
            f"file `{queue}`. ID : `{mission.id}`."
        )
