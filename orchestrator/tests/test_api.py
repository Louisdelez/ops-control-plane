from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time

from ops_orchestrator.api import APIHandler, ThreadingUnixHTTPServer
from ops_orchestrator.cli import _request
from ops_orchestrator.service import OrchestratorService

from conftest import FakeProvider, make_config, result


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition did not become true before timeout")


def _partial_unix_request(socket_path):
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(2)
    client.connect(str(socket_path))
    client.sendall(
        b"POST /v1/route HTTP/1.1\r\n"
        b"Host: localhost\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 4096\r\n"
        b"Connection: close\r\n\r\n"
        b"{"
    )
    return client


def _assert_peer_closes(client):
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            data = client.recv(4096)
        except (ConnectionResetError, BrokenPipeError):
            return
        except socket.timeout:
            continue
        if not data:
            return
    raise AssertionError("server did not close the client socket")


def _raw_request(socket_path, method, path, payload):
    from ops_orchestrator.cli import UnixHTTPConnection

    connection = UnixHTTPConnection(str(socket_path), timeout=5)
    body = json.dumps(payload).encode("utf-8")
    connection.request(
        method,
        path,
        body=body,
        headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
    )
    response = connection.getresponse()
    data = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, data


def test_unix_socket_api_routes_without_tcp(tmp_path, payload):
    payload["deterministic_available"] = True
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        health = _request(str(config.socket_path), "GET", "/v1/health")
        catalogue = _request(
            str(config.socket_path), "GET", "/v1/catalogue?offset=0&limit=10"
        )
        metrics = _request(str(config.socket_path), "GET", "/v1/metrics")
        decision = _request(str(config.socket_path), "POST", "/v1/route", payload)
        assert health["database"] == "ok"
        assert health["catalogue"]["cards"] == 58
        assert catalogue["pagination"]["returned"] == 10
        assert catalogue["pagination"]["total"] == 58
        assert "ops_orchestrator_up 1" in metrics["metrics"]
        assert "ops_orchestrator_database_readable 1" in metrics["metrics"]
        assert decision["disposition"] == "deterministic_tool_required"
        assert server.socket.family == socket.AF_UNIX
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_catalogue_preview_is_project_scoped_and_makes_no_provider_call(
    tmp_path, payload
):
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, result = _raw_request(
            config.socket_path,
            "POST",
            "/v1/catalogue/route-preview",
            {
                "project_id": "minecraft",
                "requirements": {"code": 8},
                "runtime_only": False,
                "limit": 3,
            },
        )
        assert status == 200
        assert result["advisory_only"] is True
        assert result["candidates"][0]["card_id"] == "qwen3-coder-next"

        denied_status, denied = _raw_request(
            config.socket_path,
            "POST",
            "/v1/catalogue/route-preview",
            {
                "project_id": "not-granted",
                "requirements": {},
                "runtime_only": False,
            },
        )
        assert denied_status == 403
        assert denied == {"error": "access_denied"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_catalogue_pagination_rejects_duplicates_unknowns_and_oversize(
    tmp_path
):
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from ops_orchestrator.cli import UnixHTTPConnection

        for path in (
            "/v1/catalogue?limit=10&limit=20",
            "/v1/catalogue?unknown=1",
            "/v1/catalogue?limit=201",
            "/v1/catalogue?offset=-1",
        ):
            connection = UnixHTTPConnection(str(config.socket_path), timeout=5)
            connection.request("GET", path)
            response = connection.getresponse()
            document = json.loads(response.read().decode("utf-8"))
            connection.close()
            assert response.status == 400
            assert document["error"] == "ValidationError"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_unix_peer_policy_blocks_project_spoofing(tmp_path, payload):
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload["project_id"] = "not-granted"
        status, response = _raw_request(config.socket_path, "POST", "/v1/route", payload)
        assert status == 403
        assert response == {"error": "access_denied"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_unix_peer_policy_can_only_reduce_remote_egress(tmp_path, payload):
    config = make_config(tmp_path)
    username = next(iter(config.route_grants))
    grant = config.route_grants[username]
    config.route_grants[username] = type(grant)(grant.username, grant.projects, False)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        payload["remote_allowed"] = True
        decision = _request(str(config.socket_path), "POST", "/v1/route", payload)
        assert decision["disposition"] == "human_escalation_required"
        assert "remote context egress" in decision["reason"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_human_approval_shaped_signal_is_not_an_api_capability(tmp_path, payload):
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    payload["deterministic_available"] = True
    service.route(payload)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, response = _raw_request(
            config.socket_path,
            "POST",
            "/v1/signals",
            {
                "signal": "human_validation",
                "project_id": payload["project_id"],
            },
        )
        assert status == 400
        assert response["error"] == "ValidationError"
        assert len(service.mission_trace(payload["mission_id"])["events"]) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_signal_requires_unix_peer_project_grant_before_mutation(tmp_path):
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        denied_status, denied = _raw_request(
            config.socket_path,
            "POST",
            "/v1/signals",
            {"signal": "tool_error", "project_id": "not-granted"},
        )
        assert denied_status == 403
        assert denied == {"error": "access_denied"}
        with sqlite3.connect(config.database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM metric_counters WHERE metric_key = 'tool_errors'"
            ).fetchone()[0] == 0

        allowed_status, allowed = _raw_request(
            config.socket_path,
            "POST",
            "/v1/signals",
            {"signal": "tool_error", "project_id": "minecraft"},
        )
        assert allowed_status == 200
        assert allowed == {
            "status": "recorded",
            "signal": "tool_error",
            "project_id": "minecraft",
        }
        with sqlite3.connect(config.database_path) as connection:
            row = connection.execute(
                "SELECT labels_json, value FROM metric_counters "
                "WHERE metric_key = 'tool_errors'"
            ).fetchone()
        assert row == ('{"project":"minecraft"}', 1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_signal_payload_requires_exact_signal_and_project_fields(tmp_path):
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for invalid_payload in (
            {"signal": "memory_search"},
            {"signal": "memory_search", "project_id": "minecraft", "extra": "x"},
        ):
            status, response = _raw_request(
                config.socket_path,
                "POST",
                "/v1/signals",
                invalid_payload,
            )
            assert status == 400
            assert response["error"] == "ValidationError"
        with sqlite3.connect(config.database_path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM metric_counters WHERE metric_key = 'memory_searches'"
            ).fetchone()[0] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_model_outcome_api_binds_route_and_performance_is_read_only(
    tmp_path, payload
):
    config = make_config(tmp_path, ["qwen-utility-api"])
    provider = FakeProvider(config.provider("qwen-utility-api"), [result()])
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(
        config, provider_overrides={"qwen-utility-api": provider}
    )
    decision = service.route(payload)
    server = ThreadingUnixHTTPServer(str(config.socket_path), APIHandler, service)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        outcome = {
            "route_id": decision["route_id"],
            "mission_id": payload["mission_id"],
            "project_id": payload["project_id"],
            "outcome": "succeeded",
            "quality": 0.92,
            "corrections_required": 0,
            "evidence_kind": "deterministic_test",
        }
        status, recorded = _raw_request(
            config.socket_path, "POST", "/v1/model-outcomes", outcome
        )
        assert status == 200
        assert recorded["provider_id"] == "qwen-utility-api"

        performance = _request(
            str(config.socket_path), "GET", "/v1/model-performance", timeout=10
        )
        assert performance["adaptive_policy"]["catalogue_mutation"] is False
        assert performance["provider_account_caps"][0]["hard_shared_cap"] is True
        assert performance["validated_outcomes"] == [
            {
                "task_type": "ANALYZE",
                "provider_id": "qwen-utility-api",
                "provider_account_id": "alibaba",
                "model": "test-model",
                "validation_count": 1,
                "successful_validations": 1,
                "average_quality_milli": 920,
                "average_corrections_required": 0,
                "average_attempts": 1,
                "average_uncertain_attempts": 0,
                "average_cost_microusd": 4,
                "average_duration_ms": performance["validated_outcomes"][0][
                    "average_duration_ms"
                ],
                "first_recorded_at": performance["validated_outcomes"][0][
                    "first_recorded_at"
                ],
                "last_recorded_at": performance["validated_outcomes"][0][
                    "last_recorded_at"
                ],
            }
        ]

        denied = dict(outcome)
        denied["project_id"] = "not-granted"
        denied_status, denied_result = _raw_request(
            config.socket_path, "POST", "/v1/model-outcomes", denied
        )
        assert denied_status == 403
        assert denied_result == {"error": "access_denied"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_partial_requests_expire_while_health_and_route_keep_spare_capacity(
    tmp_path, payload
):
    payload["deterministic_available"] = True
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(
        str(config.socket_path),
        APIHandler,
        service,
        client_timeout_seconds=0.5,
        max_request_workers=4,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    slow_clients = []
    try:
        slow_clients = [
            _partial_unix_request(config.socket_path),
            _partial_unix_request(config.socket_path),
        ]
        _wait_until(lambda: server.active_request_count == 2)

        health = _request(str(config.socket_path), "GET", "/v1/health")
        decision = _request(str(config.socket_path), "POST", "/v1/route", payload)

        assert health["database"] == "ok"
        # A successful scoped route on the original accepted Unix socket also
        # proves that adding the deadline did not discard SO_PEERCRED.
        assert decision["disposition"] == "deterministic_tool_required"
        for client in slow_clients:
            _assert_peer_closes(client)
        _wait_until(lambda: server.active_request_count == 0)
    finally:
        for client in slow_clients:
            client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_worker_saturation_rejects_without_creating_an_unbounded_thread(tmp_path):
    config = make_config(tmp_path)
    config.socket_path.parent.mkdir(parents=True)
    service = OrchestratorService(config)
    server = ThreadingUnixHTTPServer(
        str(config.socket_path),
        APIHandler,
        service,
        client_timeout_seconds=0.5,
        max_request_workers=1,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    slow_client = _partial_unix_request(config.socket_path)
    overflow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    overflow.settimeout(2)
    try:
        _wait_until(lambda: server.active_request_count == 1)
        overflow.connect(str(config.socket_path))
        try:
            overflow.sendall(b"GET /v1/health HTTP/1.1\r\nHost: localhost\r\n\r\n")
        except BrokenPipeError:
            # The admission path may close before the local write is scheduled.
            pass
        _wait_until(lambda: server.rejected_request_count == 1)

        assert server.active_request_count == 1
        _assert_peer_closes(overflow)
        _assert_peer_closes(slow_client)
        _wait_until(lambda: server.active_request_count == 0)
        assert _request(str(config.socket_path), "GET", "/v1/health")["database"] == "ok"
    finally:
        overflow.close()
        slow_client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
