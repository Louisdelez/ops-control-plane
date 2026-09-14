"""Supervise independent reaction and producer loops in one confined service."""

from __future__ import annotations

import logging
from queue import Empty, SimpleQueue
import random
from threading import Event, Thread

from .bridge import ApprovalProcessor, BridgeRunner
from .clients import AlertmanagerClient, BrokerClient, ZulipClient
from .config import Settings
from .errors import BridgeError, RetryableBridgeError
from .publishers import AlertPublisher, DailyReportPublisher, PendingApprovalPublisher


LOG = logging.getLogger("zulip_approval_bridge")


class ProducerRunner:
    def __init__(
        self,
        settings: Settings,
        zulip: ZulipClient,
        broker: BrokerClient,
        alertmanager: AlertmanagerClient,
        approvals: PendingApprovalPublisher,
        alerts: AlertPublisher,
        daily: DailyReportPublisher,
    ) -> None:
        self.settings = settings
        self.zulip = zulip
        self.broker = broker
        self.alertmanager = alertmanager
        self.approvals = approvals
        self.alerts = alerts
        self.daily = daily

    def verify_startup(self) -> None:
        self.zulip.verify_bot_identity()
        self.zulip.verify_stream_identity()
        self.zulip.verify_alert_stream_identity()
        self.zulip.verify_stream(
            self.settings.daily_stream,
            self.settings.daily_stream_id,
        )
        self.broker.verify_health()
        self.alertmanager.verify_ready()

    def run_forever(
        self,
        *,
        stop: Event,
        worker_errors: SimpleQueue[Exception],
    ) -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                worker_error = worker_errors.get_nowait()
            except Empty:
                worker_error = None
            if worker_error is not None:
                raise BridgeError("the reaction worker stopped unexpectedly") from None

            had_transient_failure = False
            for label, operation in (
                ("approval publisher", self.approvals.poll_once),
                ("alert publisher", self.alerts.poll_once),
                ("daily report publisher", self.daily.poll_once),
            ):
                try:
                    operation()
                except RetryableBridgeError as exc:
                    had_transient_failure = True
                    LOG.warning("transient %s failure: %s", label, exc)
            if had_transient_failure:
                delay = min(backoff, self.settings.retry_max_seconds)
                backoff = min(backoff * 2.0, self.settings.retry_max_seconds)
                delay += random.uniform(0.0, min(delay * 0.2, 1.0))
            else:
                backoff = 1.0
                delay = self.settings.producer_poll_seconds
            stop.wait(delay)


class ServiceSupervisor:
    def __init__(
        self,
        processor: ApprovalProcessor,
        reactions: BridgeRunner,
        producers: ProducerRunner,
    ) -> None:
        self.processor = processor
        self.reactions = reactions
        self.producers = producers

    def run_forever(self) -> None:
        # Verify every remote identity and both local UDS peers before starting
        # either loop. A partially configured bridge therefore fails closed.
        self.processor.verify_startup()
        self.producers.verify_startup()
        stop = Event()
        worker_errors: SimpleQueue[Exception] = SimpleQueue()

        def reaction_worker() -> None:
            try:
                self.reactions.run_forever(verify_startup=False, stop_event=stop)
            except Exception as exc:  # the supervisor emits only a generic failure
                worker_errors.put(exc)
                stop.set()

        worker = Thread(
            target=reaction_worker,
            name="zulip-reaction-consumer",
            daemon=True,
        )
        worker.start()
        try:
            self.producers.run_forever(stop=stop, worker_errors=worker_errors)
            try:
                worker_errors.get_nowait()
            except Empty:
                pass
            else:
                raise BridgeError("the reaction worker stopped unexpectedly") from None
        finally:
            stop.set()
