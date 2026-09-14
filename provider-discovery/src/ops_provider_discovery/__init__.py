"""Observation-only model discovery for the provider integration registry."""

from .collector import (
    Collector,
    CollectorLimits,
    DiscoveryFailure,
    TransportResponse,
    UrlLibTransport,
    collect_inventory,
    load_credential_map,
    load_registry,
)

__all__ = [
    "Collector",
    "CollectorLimits",
    "DiscoveryFailure",
    "TransportResponse",
    "UrlLibTransport",
    "collect_inventory",
    "load_credential_map",
    "load_registry",
]
