from __future__ import annotations

from pathlib import Path
import subprocess


CONTROL_PLANE = Path(__file__).resolve().parents[3]
BRIDGE = CONTROL_PLANE / "bridges" / "zulip"


def test_no_bridge_tcp_listener_and_private_alertmanager_socket() -> None:
    main = (BRIDGE / "systemd" / "zulip-approval-bridge.service").read_text(
        encoding="utf-8"
    )
    query_socket = (BRIDGE / "systemd" / "zulip-alertmanager-query.socket").read_text(
        encoding="utf-8"
    )
    query_service = (BRIDGE / "systemd" / "zulip-alertmanager-query.service").read_text(
        encoding="utf-8"
    )
    assert "SupplementaryGroups=opsbroker-api ops-readers" in main
    assert "ExecStart=/usr/local/libexec/zulip-openbao-launcher" in main
    assert "EnvironmentFile=" not in main
    assert "LoadCredentialEncrypted=zulip-openbao-role-id:" in main
    assert "LoadCredentialEncrypted=zulip-openbao-secret-id:" in main
    assert "ListenStream=" not in main
    assert "ListenStream=/run/zulip-alertmanager-query/api.sock" in query_socket
    assert "SocketGroup=zulipbridge" in query_socket
    assert "SocketMode=0660" in query_socket
    assert "ListenStream=127." not in query_socket
    assert "systemd-socket-proxyd" in query_service
    assert "127.0.0.1:9093" in query_service
    assert "IPAddressDeny=any" in query_service
    assert "IPAddressAllow=localhost" in query_service


def test_example_configuration_contains_no_credentials() -> None:
    example = (BRIDGE / "config" / "example.env").read_text(encoding="utf-8")
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in example.splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    assert values["ZULIP_BOT_EMAIL"] == ""
    assert values["ZULIP_API_KEY"] == ""
    assert values["ZULIP_APPROVER_USER_IDS"] == ""


def test_installer_syntax_and_dormant_default() -> None:
    installer = CONTROL_PLANE / "scripts" / "install-zulip-bridge.sh"
    completed = subprocess.run(
        ["/usr/bin/bash", "-n", str(installer)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    source = installer.read_text(encoding="utf-8")
    assert 'systemctl disable --now "$service_name"' in source
    assert 'systemctl disable --now "$query_socket_name"' in source
    assert 'usermod --append --groups "$required_group" zulipbridge' in source
    assert 'install -o root -g root -m 0755 "$launcher_source" "$launcher_target"' in source
    assert 'install -o root -g root -m 0755 "$provisioner_source" "$provisioner_target"' in source


def test_system_identity_declares_daily_report_read_group() -> None:
    sysusers = (CONTROL_PLANE / "systemd" / "ops-users.conf").read_text(
        encoding="utf-8"
    )
    assert "m zulipbridge ops-readers" in sysusers.splitlines()
