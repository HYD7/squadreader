"""Local-mode defaults of the config and the CLI (Zero Zone fork).

With NO config file and NO environment override a reader must not reach any
external service: `net_policy` is `local-only`, the remote-update switches are
off, `sqreader enroll` refuses (exit code 2) before it touches the network, and
the status output says so. `allow-central` is opt-in and brings the upstream
behaviour back.

No test here opens a real connection: sockets and DNS are patched to record any
attempt, so "no outbound traffic" is asserted, not assumed.
"""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from sqreader import agent_creds, cli, config, hwfp, ingest_client, netpolicy

REPO = Path(__file__).resolve().parent.parent
CENTRAL = "https://central.example"


@pytest.fixture(autouse=True)
def _pristine(monkeypatch, tmp_path):
    """The true defaults: undo tests/conftest.py's allow-central, no config file."""
    monkeypatch.delenv(netpolicy.ENV_POLICY, raising=False)
    monkeypatch.delenv(netpolicy.ENV_ALLOW_HOSTS, raising=False)
    monkeypatch.setenv("SQREADER_CONFIG", str(tmp_path / "no-such-config.json"))
    monkeypatch.setattr(config, "_cache", None)
    netpolicy.reset()
    yield
    netpolicy.reset()
    monkeypatch.setattr(config, "_cache", None)


@pytest.fixture
def net(monkeypatch):
    """Record (and refuse) every socket / DNS attempt."""
    calls: list[tuple[str, object]] = []

    def connect(self, *a, **k):
        calls.append(("connect", a))
        raise AssertionError("socket.connect must not be reached")

    def create_connection(*a, **k):
        calls.append(("create_connection", a))
        raise AssertionError("socket.create_connection must not be reached")

    def getaddrinfo(*a, **k):
        calls.append(("getaddrinfo", a))
        raise AssertionError("DNS resolution must not be reached")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return calls


def _write_config(monkeypatch, tmp_path, **values):
    path = tmp_path / "sqreader.config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("SQREADER_CONFIG", str(path))
    monkeypatch.setattr(config, "_cache", None)


# -- 1. the defaults themselves ------------------------------------------------

def test_config_defaults_are_local_mode():
    assert config.get("net_policy") == "local-only"
    assert config.get("net_allow_hosts") == []
    assert config.get("update_enabled") is False
    assert config.get("offset_autoheal") is False
    assert config.get("push_enabled") is False
    assert config.get("central_url") is None
    assert config.get("alert_replay_base") is None


def test_example_config_matches_the_defaults():
    ex = json.loads((REPO / "sqreader.config.example.json").read_text(encoding="utf-8"))
    assert ex["net_policy"] == "local-only"
    assert ex["net_allow_hosts"] == []
    assert ex["alert_replay_base"] is None
    assert ex["update_enabled"] is False and ex["offset_autoheal"] is False
    assert ex["push_enabled"] is False and ex["central_url"] is None
    # the retention placeholders exist and equal the built-in defaults
    for key in ("retention_max_age_days", "retention_max_total_gb",
                "retention_min_free_gb", "retention_in_progress_min",
                "retention_min_keep", "retention_require_ack",
                "retention_include_unverified", "retention_unverified_max_age_days",
                "export_state_dir"):
        assert key in ex, key
        assert ex[key] == config.DEFAULTS[key], key
    # only the comment keys may mention a hostname; no value points at a site
    for key, value in ex.items():
        if key.startswith("_"):
            continue
        assert not (isinstance(value, str) and "://" in value), key
    # the example never carries an external URL as a value
    assert "squadreader.com" not in json.dumps(
        {k: v for k, v in ex.items() if not k.startswith("_")})


def test_main_applies_the_config_policy_before_any_command(net, capsys):
    assert cli.main(["version"]) == 0
    assert netpolicy.effective_policy() == "local-only"
    assert netpolicy.allowed_hosts() == frozenset()
    assert net == []


# -- 2. enroll refuses in local mode -------------------------------------------

def test_enroll_refuses_in_local_mode_without_touching_the_network(
        net, capsys, monkeypatch):
    def boom(*a, **k):  # pragma: no cover - reaching it fails the test
        raise AssertionError("enroll must not be attempted in local mode")

    monkeypatch.setattr(ingest_client, "enroll", boom)
    monkeypatch.setattr(agent_creds, "write", boom)
    monkeypatch.setattr(hwfp, "compute", boom)
    rc = cli.main(["enroll", "tok-123", "--central-url", CENTRAL])
    err = capsys.readouterr().err
    assert rc == 2
    assert "enroll kapalı: yerel modda merkezi kayıt yapılmaz" in err
    assert net == []
    assert netpolicy.blocked_attempts() == 0     # decided from policy, not by a probe


def test_enroll_refuses_without_any_central_url_too(net, capsys):
    assert cli.main(["enroll", "tok-123"]) == 2
    assert "yerel modda" in capsys.readouterr().err
    assert net == []


def test_enroll_allowed_host_still_works_in_local_mode(net, capsys, monkeypatch, tmp_path):
    """Listing the central verbatim in net_allow_hosts is the explicit opt-in."""
    _write_config(monkeypatch, tmp_path, net_allow_hosts=["central.example"])
    seen = {}

    def fake_enroll(central, token, fp, **k):
        seen["central"] = central
        return {"SQREADER_AGENT_ID": "agent-1", "SQREADER_COMMUNITY": "c"}

    monkeypatch.setattr(ingest_client, "enroll", fake_enroll)
    monkeypatch.setattr(hwfp, "compute", lambda: "fp")
    monkeypatch.setattr(agent_creds, "write", lambda fields: tmp_path / ".env.agent")
    assert cli.main(["enroll", "tok-123", "--central-url", CENTRAL]) == 0
    assert seen["central"] == CENTRAL


def test_allow_central_restores_the_upstream_enroll(net, capsys, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, net_policy="allow-central")
    seen = {}

    def fake_enroll(central, token, fp, **k):
        seen["args"] = (central, token, fp)
        return {"SQREADER_AGENT_ID": "agent-1", "SQREADER_COMMUNITY": "comm"}

    monkeypatch.setattr(ingest_client, "enroll", fake_enroll)
    monkeypatch.setattr(hwfp, "compute", lambda: "fp-1")
    monkeypatch.setattr(agent_creds, "write", lambda fields: tmp_path / ".env.agent")
    rc = cli.main(["enroll", "tok-123", "--central-url", CENTRAL])
    out = capsys.readouterr()
    assert rc == 0
    assert seen["args"] == (CENTRAL, "tok-123", "fp-1")
    assert netpolicy.effective_policy() == "allow-central"
    assert "enrolled as agent_id=agent-1" in out.out
    assert "yerel mod" not in out.err


def test_environment_can_also_choose_allow_central(monkeypatch, tmp_path):
    monkeypatch.setenv(netpolicy.ENV_POLICY, "allow-central")
    seen = {}
    monkeypatch.setattr(ingest_client, "enroll",
                        lambda c, t, f, **k: seen.setdefault("c", c) and {
                            "SQREADER_AGENT_ID": "a"})
    monkeypatch.setattr(hwfp, "compute", lambda: "fp")
    monkeypatch.setattr(agent_creds, "write", lambda fields: tmp_path / "x")
    assert cli.main(["enroll", "tok", "--central-url", CENTRAL]) == 0
    assert seen["c"] == CENTRAL


# -- 3. a bad policy value stops the command -----------------------------------

def test_invalid_net_policy_is_a_clear_error(net, capsys, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, net_policy="open-sesame")
    assert cli.main(["version"]) == 2
    err = capsys.readouterr().err
    assert "net_policy" in err and "open-sesame" in err
    assert "Traceback" not in err


def test_invalid_allow_hosts_type_is_a_clear_error(capsys, monkeypatch, tmp_path):
    _write_config(monkeypatch, tmp_path, net_allow_hosts=42)
    assert cli.main(["version"]) == 2
    assert "net_allow_hosts" in capsys.readouterr().err


# -- 4. status output ----------------------------------------------------------

def test_enroll_status_prints_the_policy(net, capsys, monkeypatch):
    monkeypatch.setattr(agent_creds, "load", lambda: None)
    assert cli.main(["enroll", "--status"]) == 0
    out = capsys.readouterr().out
    assert "not enrolled" in out
    assert "ağ politikası: local-only" in out
    assert net == []


def test_enroll_status_enrolled_says_central_push_is_off(net, capsys, monkeypatch):
    creds = {"SQREADER_AGENT_ID": "a1", "SQREADER_AGENT_SECRET_HEX": "ab" * 32,
             "SQREADER_PUSH_URL": "https://squadreader.example",
             "SQREADER_COMMUNITY": "c"}
    monkeypatch.setattr(agent_creds, "load", lambda: creds)
    assert cli.main(["enroll", "--status"]) == 0
    out = capsys.readouterr().out
    assert "ağ politikası: local-only" in out
    assert ingest_client.LOCAL_MODE_MESSAGE in out
    assert net == []


def test_push_status_line_in_local_mode():
    creds = {"SQREADER_AGENT_ID": "a1", "SQREADER_AGENT_SECRET_HEX": "ab" * 32,
             "SQREADER_PUSH_URL": "https://squadreader.example"}
    assert ingest_client.push_status(creds) == "merkezi gönderim kapalı (yerel mod)"
    assert ingest_client.push_status(None) == "merkezi gönderim kapalı (kayıtlı değil)"


# -- 5. no command reaches the network with the default config -----------------

def test_default_config_commands_never_open_a_socket(net, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(agent_creds, "load", lambda: None)
    rec = tmp_path / "recordings"
    rec.mkdir()
    for argv in (
        ["version"],
        ["enroll", "--status"],
        ["enroll", "tok", "--central-url", CENTRAL],
        ["retention", "--recordings-dir", str(rec), "--dry-run"],
        ["selftest"],
    ):
        cli.main(argv)
    capsys.readouterr()
    assert net == [], net


def test_flush_backlog_is_blocked_in_local_mode(net, tmp_path):
    """The serve loop's `_flush_push` relies on this answer to print its single
    'merkezi gönderim kapalı (yerel mod)' line instead of pushing."""
    creds = {"SQREADER_AGENT_ID": "a1", "SQREADER_AGENT_SECRET_HEX": "ab" * 32,
             "SQREADER_PUSH_URL": "https://squadreader.example"}
    ingest_client.enqueue(tmp_path, "match-1")
    res = ingest_client.flush_backlog(tmp_path, tmp_path / "s.db", None, creds)
    assert res["blocked"] == 1 and res["sent"] == 0 and res["pending"] == 1
    assert net == []


def test_update_and_autoheal_are_off_without_a_config_file():
    assert not config.get("update_enabled")
    assert not config.get("offset_autoheal")
