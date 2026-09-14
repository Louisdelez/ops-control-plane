"""Service entry point."""

from __future__ import annotations

import logging

from .bridge import ApprovalProcessor, BridgeRunner
from .clients import AlertmanagerClient, BrokerClient, ZulipClient
from .config import Settings
from .errors import BridgeError
from .publishers import (
    AlertPublisher,
    DailyReportPublisher,
    DurableZulipPublisher,
    PendingApprovalPublisher,
)
from .runtime import ProducerRunner, ServiceSupervisor
from .state import EventState
from .mobile import MobileCommandProcessor


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        settings = Settings.from_env()
        state = EventState(settings.state_path)
        with (
            ZulipClient(settings) as reaction_zulip,
            ZulipClient(settings) as producer_zulip,
            BrokerClient(settings.broker_socket) as approval_broker,
            BrokerClient(settings.broker_socket) as producer_broker,
            AlertmanagerClient(settings.alertmanager_socket) as alertmanager,
        ):
            sink = DurableZulipPublisher(state, producer_zulip)
            mobile = MobileCommandProcessor(
                settings,
                reaction_zulip,
                approval_broker,
                # Mobile replies share the reaction client so queue/message
                # validation and publication use the same verified identity.
                DurableZulipPublisher(state, reaction_zulip),
            )
            processor = ApprovalProcessor(
                settings, reaction_zulip, approval_broker, mobile
            )
            reactions = BridgeRunner(
                settings,
                state,
                reaction_zulip,
                processor,
            )
            pending = PendingApprovalPublisher(settings, producer_broker, sink)
            alerts = AlertPublisher(settings, alertmanager, state, sink)
            daily = DailyReportPublisher(settings, sink)
            producers = ProducerRunner(
                settings,
                producer_zulip,
                producer_broker,
                alertmanager,
                pending,
                alerts,
                daily,
            )
            ServiceSupervisor(processor, reactions, producers).run_forever()
    except BridgeError as exc:
        logging.getLogger("zulip_approval_bridge").critical("startup stopped: %s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        logging.getLogger("zulip_approval_bridge").info("shutdown requested")


if __name__ == "__main__":
    main()
