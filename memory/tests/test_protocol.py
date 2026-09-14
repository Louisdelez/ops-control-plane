from __future__ import annotations

import threading

from ops_memory.protocol import Dispatcher, MemorySocketServer, socket_request
from ops_memory.service import MemoryService


def envelope(operation: str, payload: dict | None = None) -> dict:
    return {
        "version": 1,
        "request_id": "request-1",
        "operation": operation,
        "actor": "operator",
        "payload": payload or {},
    }


def test_dispatcher_returns_stable_response(service: MemoryService, uid: int) -> None:
    response = Dispatcher(service).dispatch(envelope("health"), uid)
    assert response["ok"] is True
    assert response["request_id"] == "request-1"
    assert response["result"]["sqlite"] == "ok"


def test_dispatcher_rejects_unknown_operation_without_traceback(
    service: MemoryService, uid: int
) -> None:
    response = Dispatcher(service).dispatch(envelope("destroy_everything"), uid)
    assert response == {
        "version": 1,
        "request_id": "request-1",
        "ok": False,
        "error": {"code": "protocol_error", "message": "unknown operation"},
    }


def test_dispatcher_rejects_extra_envelope_field(service: MemoryService, uid: int) -> None:
    request = envelope("health")
    request["roles"] = ["admin"]
    response = Dispatcher(service).dispatch(request, uid)
    assert response["error"]["code"] == "protocol_error"


def test_dispatcher_rejects_unknown_payload_field(service: MemoryService, uid: int) -> None:
    response = Dispatcher(service).dispatch(envelope("health", {"roles": ["admin"]}), uid)
    assert response["error"]["code"] == "validation_error"


def test_unix_socket_protocol_uses_real_peer_credentials(
    service: MemoryService, uid: int
) -> None:
    with MemorySocketServer(service) as socket_server:
        thread = threading.Thread(target=socket_server.serve_forever)
        thread.start()
        try:
            response = socket_request(service.config.socket_path, envelope("health"))
            assert response["ok"] is True
            assert response["result"]["status"] == "ok"
            assert socket_server.healthy() is True
        finally:
            assert socket_server.server is not None
            socket_server.server.shutdown()
            thread.join(timeout=2)
    assert not service.config.socket_path.exists()
