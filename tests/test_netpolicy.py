"""Network policy (sqreader.netpolicy): the fork must not depend on any external
service at runtime. Default `local-only`; `allow-central` is opt-in.

Nothing here touches the real network: sockets / DNS are patched to record any
attempt, and the only real connections are to a throw-away 127.0.0.1 server.
"""
from __future__ import annotations

import http.server
import json
import logging
import socket
import threading
import urllib.request
from pathlib import Path

import pytest

from sqreader import ingest_client as ic
from sqreader import netpolicy, offset_client, updater
from sqreader.plugins.notify import DiscordNotifier, replay_url

CREDS = {"SQREADER_AGENT_ID": "a1", "SQREADER_AGENT_SECRET_HEX": "ab" * 32,
         "SQREADER_PUSH_URL": "https://squadreader.com"}


@pytest.fixture(autouse=True)
def _clean_policy(monkeypatch):
    """Undo tests/conftest.py's allow-central: start from the true defaults."""
    monkeypatch.delenv(netpolicy.ENV_POLICY, raising=False)
    monkeypatch.delenv(netpolicy.ENV_ALLOW_HOSTS, raising=False)
    netpolicy.reset()
    yield
    netpolicy.reset()


@pytest.fixture
def net(monkeypatch):
    """Record every attempt to resolve or connect; never really do either."""
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


# -- 1. default ---------------------------------------------------------------

def test_default_policy_is_local_only():
    assert netpolicy.DEFAULT_POLICY == netpolicy.LOCAL_ONLY
    assert netpolicy.effective_policy() == "local-only"
    assert netpolicy.is_local_only()
    assert netpolicy.allowed_hosts() == frozenset()


# -- 2. external targets are refused before any socket / DNS ------------------

@pytest.mark.parametrize("url", [
    "https://squadreader.com/ingest/match",
    "https://discord.com/api/webhooks/1/tok",
    "http://8.8.8.8/",
    "https://cota.squadreplay.com/api/map-config",
    "http://169.254.169.254/latest/meta-data/",   # link-local: cloud metadata
    "http://0.0.0.0/",
    "http://[::ffff:8.8.8.8]/",
    "http://[2001:4860:4860::8888]/",
    "http://[fe80::1]/",
])
def test_external_targets_are_blocked_without_touching_the_network(url, net):
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.check_outbound(url, purpose="t")
    assert net == []


# -- 3. classification --------------------------------------------------------

@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8080/", "http://127.9.9.9/", "http://[::1]:80/",
    "http://localhost/", "http://LOCALHOST:9/", "http://localhost./",
    "localhost:8080", "127.0.0.1", "::1",
    "http://10.1.2.3/", "http://172.16.0.1/", "http://172.31.255.255/",
    "http://192.168.1.10/",
    "http://100.64.0.1/", "http://100.127.255.254/",          # CGNAT
    "http://[fc00::1]/", "http://[fd12:3456::1]/",            # fc00::/7
    "http://2130706433/",                                      # decimal 127.0.0.1
    "http://0x7f.1/",                                          # hex + short form
    "http://0x7f000001/", "http://0177.0.0.1/", "http://127.1/",
    "http://[::ffff:127.0.0.1]/", "http://[::ffff:10.0.0.1]/",  # v4-mapped
])
def test_private_and_loopback_targets_pass(target, net):
    netpolicy.check_outbound(target, purpose="t")
    assert net == []


@pytest.mark.parametrize("target", [
    "http://127.0.0.1.evil.example/",
    "http://localhost.evil.example/",
    "http://10.0.0.1.nip.io/",
    "http://172.32.0.1/", "http://100.128.0.1/", "http://192.169.0.1/",
    "http://11.0.0.1/", "http://[fe00::1]/",
    "http://0x7f.1.evil/", "http://foo.localhost/",
    "http://127.0.0.1\\@evil.example/",       # parser-differential trick
    "http://999.1.1.1/",                      # not an address, not a name
    "http://0x/", "http://",
    "",
])
def test_lookalikes_and_garbage_are_blocked(target, net):
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.check_outbound(target, purpose="t")
    assert net == []


def test_non_string_target_is_blocked():
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.check_outbound(None, purpose="t")


# -- 4. allow list ------------------------------------------------------------

def test_an_allowed_domain_passes_but_its_subdomains_do_not(net):
    netpolicy.check_outbound("https://central.example/x", purpose="t",
                             allow_hosts=["central.example"])
    netpolicy.check_outbound("https://CENTRAL.example./x", purpose="t",
                             allow_hosts="central.example")
    for other in ("https://api.central.example/", "https://central.example.evil/",
                  "https://evilcentral.example/"):
        with pytest.raises(netpolicy.OutboundBlocked):
            netpolicy.check_outbound(other, purpose="t",
                                     allow_hosts=["central.example"])
    assert net == []


def test_an_allowed_public_ip_passes():
    netpolicy.check_outbound("http://203.0.113.5:9000/", purpose="t",
                             allow_hosts=["203.0.113.5"])
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.check_outbound("http://203.0.113.6/", purpose="t",
                                 allow_hosts=["203.0.113.5"])


def test_allow_list_sources_env_and_configure(monkeypatch):
    netpolicy.configure(allow_hosts=["from-config.example"])
    netpolicy.check_outbound("https://from-config.example/", purpose="t")
    monkeypatch.setenv(netpolicy.ENV_ALLOW_HOSTS, " a.example , https://b.example:8443/x ")
    netpolicy.check_outbound("https://a.example/", purpose="t")
    netpolicy.check_outbound("https://b.example/", purpose="t")
    with pytest.raises(netpolicy.OutboundBlocked):     # env replaces config
        netpolicy.check_outbound("https://from-config.example/", purpose="t")


# -- credentials in the URL / logging ----------------------------------------

def test_userinfo_is_refused_even_for_a_private_host():
    for url in ("http://user:pw@127.0.0.1/", "http://user@10.0.0.1/",
                "http://10.0.0.1@evil.example/"):
        with pytest.raises(netpolicy.OutboundBlocked):
            netpolicy.check_outbound(url, purpose="t")


def test_log_line_carries_purpose_and_host_only(caplog):
    caplog.set_level(logging.INFO, logger="sqreader.netpolicy")
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.check_outbound(
            "https://discord.com/api/webhooks/123/SECRETTOKEN?token=abc&x=1",
            purpose="alert-webhook")
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.check_outbound("https://bob:hunter2@example.com/p?q=zzz",
                                 purpose="push")
    lines = [r.getMessage() for r in caplog.records]
    assert len(lines) == 2 and all("\n" not in ln for ln in lines)
    assert "alert-webhook" in lines[0] and "discord.com" in lines[0]
    text = caplog.text
    for secret in ("SECRETTOKEN", "token=abc", "hunter2", "bob", "zzz", "/api/"):
        assert secret not in text


def test_blocked_attempts_are_counted_and_reported():
    assert netpolicy.blocked_attempts() == 0
    for _ in range(3):
        with pytest.raises(netpolicy.OutboundBlocked):
            netpolicy.check_outbound("https://squadreader.com/", purpose="push")
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.check_outbound("https://squadreader.com/", purpose="checkin")
    netpolicy.check_outbound("http://127.0.0.1/", purpose="push")   # allowed: no count
    assert netpolicy.blocked_attempts() == 4
    st = netpolicy.status()
    assert st["policy"] == "local-only"
    assert st["blockedAttempts"] == 4
    assert st["blockedByPurpose"] == {"push": 3, "checkin": 1}
    assert not netpolicy.is_allowed("https://squadreader.com/")
    assert netpolicy.blocked_attempts() == 4          # is_allowed does not count


# -- policy sources -----------------------------------------------------------

def test_policy_precedence_param_over_env_over_config(monkeypatch):
    netpolicy.configure(policy="allow-central")
    assert netpolicy.effective_policy() == "allow-central"
    monkeypatch.setenv(netpolicy.ENV_POLICY, "local-only")
    assert netpolicy.effective_policy() == "local-only"
    assert netpolicy.effective_policy("allow-central") == "allow-central"
    netpolicy.check_outbound("https://squadreader.com/", purpose="t",
                             policy="allow-central")
    monkeypatch.setenv(netpolicy.ENV_POLICY, "Allow_Central")
    assert netpolicy.effective_policy() == "allow-central"


def test_garbage_env_value_fails_closed(monkeypatch):
    netpolicy.configure(policy="allow-central")
    monkeypatch.setenv(netpolicy.ENV_POLICY, "yes-please")
    assert netpolicy.effective_policy() == "local-only"


def test_configure_rejects_an_unknown_policy():
    with pytest.raises(ValueError):
        netpolicy.configure(policy="open")
    with pytest.raises(ValueError):
        netpolicy.check_outbound("http://127.0.0.1/", purpose="t", policy="open")


# -- 9. allow-central is upstream behaviour ----------------------------------

def test_allow_central_lets_everything_through(monkeypatch, net):
    monkeypatch.setenv(netpolicy.ENV_POLICY, "allow-central")
    netpolicy.check_outbound("https://squadreader.com/", purpose="push")
    netpolicy.check_outbound("http://8.8.8.8/", purpose="push")
    assert netpolicy.blocked_attempts() == 0
    seen = []
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: seen.append((req.full_url, timeout)))
    req = urllib.request.Request("https://squadreader.com/x")
    netpolicy.guarded_urlopen(req, timeout=5)
    assert seen == [("https://squadreader.com/x", 5)]


# -- 6. push / ingest path ----------------------------------------------------

def _backlog(tmp_path: Path, n: int = 2) -> Path:
    d = tmp_path / "push_queue"
    for i in range(n):
        ic.enqueue(d, f"match-{i}")
    return d


def test_flush_backlog_in_local_mode_sends_nothing_and_does_not_storm(
        tmp_path, net, monkeypatch):
    sleeps = []
    monkeypatch.setattr(ic.time, "sleep", sleeps.append)
    monkeypatch.setattr(ic, "push_match", lambda *a, **k: pytest.fail("pushed"))
    d = _backlog(tmp_path, 2)
    for i in range(1, 6):
        res = ic.flush_backlog(d, tmp_path / "stats.db", tmp_path, CREDS)
        assert res == {"sent": 0, "pending": 2, "errors": 0, "blocked": 1}
        assert netpolicy.blocked_attempts() == i     # counted, one per flush
    assert sleeps == []                              # no backoff / retry loop
    assert net == []
    assert len(list(d.glob("match-*.json"))) == 2    # markers untouched


def test_push_calls_raise_a_clean_policy_error_not_a_network_error(tmp_path, net):
    bundle = {"schema": ic.SCHEMA_MATCH, "match": {}}
    with pytest.raises(ic.PushBlocked) as ei:
        ic.push_match(CREDS, bundle, seq=1)
    assert isinstance(ei.value, ic.PushError)
    assert "yerel mod" in str(ei.value)
    sqrx = tmp_path / "x.sqrx"
    sqrx.write_bytes(b"x")
    with pytest.raises(ic.PushBlocked):
        ic.push_replay(CREDS, "m", "s", sqrx, seq=1)
    with pytest.raises(ic.PushBlocked):
        ic.enroll("https://squadreader.com", "tok", "fp")
    assert net == []


def test_background_central_calls_are_silent_no_ops(net):
    assert ic.checkin(CREDS, {"v": 1}, seq=1) == {}
    assert ic.fetch_offsets(CREDS, "build", "stable", seq=1) is None
    assert ic.fetch_manifest(CREDS, "linux", "stable", seq=1) is None
    assert ic.download_artifact(CREDS, "sqreader.bin") is None
    assert net == []
    assert netpolicy.blocked_attempts() == 4


def test_push_status_line():
    assert ic.push_status(CREDS) == "merkezi gönderim kapalı (yerel mod)"
    assert ic.push_status(None).startswith("merkezi gönderim kapalı")
    assert ic.push_status({**CREDS, "SQREADER_PUSH_URL": "http://10.0.0.5:8080"}) \
        == "merkezi gönderim etkin"
    assert netpolicy.blocked_attempts() == 0         # asking is not attempting


def test_a_private_central_still_works_in_local_mode(tmp_path, monkeypatch):
    creds = {**CREDS, "SQREADER_PUSH_URL": "http://10.0.0.5:8080"}
    pushed = []
    monkeypatch.setattr(ic, "push_match", lambda c, b, **k: pushed.append(b))
    monkeypatch.setattr(ic, "build_match_bundle", lambda db, m: {"m": m})
    d = _backlog(tmp_path, 1)
    res = ic.flush_backlog(d, tmp_path / "stats.db", None, creds,
                           push_replay_files=False)
    assert res["sent"] == 1 and "blocked" not in res and pushed


# -- 7. offset + update paths -------------------------------------------------

def test_offset_autoheal_never_asks_central_in_local_mode(net, monkeypatch):
    monkeypatch.setattr(ic, "fetch_offsets",
                        lambda *a, **k: pytest.fail("asked central"))
    assert offset_client.fetch_and_accept(CREDS, "b", "stable", 0, seq=1) is None
    assert net == []
    assert netpolicy.blocked_attempts() == 1


def test_update_check_and_download_never_run_in_local_mode(net, monkeypatch):
    monkeypatch.setattr(ic, "fetch_manifest",
                        lambda *a, **k: pytest.fail("asked central"))
    monkeypatch.setattr(ic, "download_artifact",
                        lambda *a, **k: pytest.fail("downloaded"))
    assert updater.fetch_and_accept(CREDS, "stable", seq=1) is None
    assert updater.download_verified(CREDS, "sqreader.bin", "0" * 64) is None
    assert net == []
    assert netpolicy.blocked_attempts() == 2


# -- 8. notify ----------------------------------------------------------------

_ALERT = {"ts": 1.0, "tick": 1, "plugin_id": "p", "alert_type": "speedhack",
          "eos_id": "e", "player_name": "N", "match_id": "m1", "confidence": 0.9,
          "details": {}}


def _notifier(url: str, **kw) -> DiscordNotifier:
    n = DiscordNotifier(url, **kw)
    n.close(0.1)                # stop the worker; the tests drive _post directly
    return n


def test_external_webhook_is_blocked_and_not_retried(net, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("urlopen reached"))
    n = _notifier("https://discord.com/api/webhooks/1/tok")
    n._post([_ALERT])
    n._post([_ALERT])
    assert net == []
    assert netpolicy.blocked_attempts() == 2         # one per batch, no retries


def test_replay_link_is_not_built_for_squadreader_in_local_mode():
    assert replay_url("https://squadreader.com", "m1") is None
    assert replay_url("http://10.0.0.5:8080", "m1") == \
        "http://10.0.0.5:8080/replay/?mode=replay&id=m1"
    netpolicy.configure(allow_hosts=["viewer.example"])
    assert replay_url("https://viewer.example", "m1") == \
        "https://viewer.example/replay/?mode=replay&id=m1"
    assert netpolicy.blocked_attempts() == 0


class _Hook(http.server.BaseHTTPRequestHandler):
    got: list[bytes] = []
    redirect_to: str | None = None

    def do_POST(self):                                # noqa: N802
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.redirect_to:
            self.send_response(307)
            self.send_header("Location", self.redirect_to)
            self.end_headers()
            return
        type(self).got.append(body)
        self.send_response(204)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def hook_server():
    _Hook.got = []
    _Hook.redirect_to = None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Hook)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_a_webhook_on_the_allow_list_is_delivered(hook_server):
    port = hook_server.server_address[1]
    n = _notifier(f"http://127.0.0.1:{port}/hook", replay_base=None)
    n._post([_ALERT])
    assert len(_Hook.got) == 1
    assert json.loads(_Hook.got[0])["embeds"][0]["title"].endswith("N")
    assert netpolicy.blocked_attempts() == 0


def test_an_allow_listed_domain_webhook_is_delivered(monkeypatch):
    netpolicy.configure(allow_hosts=["hooks.example.test"])
    opened = []

    class _Opener:
        def open(self, req, timeout=None):
            opened.append(req.full_url)

            class _R:
                def __enter__(s): return s
                def __exit__(s, *a): return False
            return _R()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *h: _Opener())
    n = _notifier("https://hooks.example.test/api/webhooks/1/tok")
    n._post([_ALERT])
    assert opened == ["https://hooks.example.test/api/webhooks/1/tok"]


def test_a_redirect_out_of_the_allow_list_is_refused(hook_server, monkeypatch):
    resolved: list[object] = []
    real = socket.getaddrinfo

    def spy(host, *a, **k):
        resolved.append(host)
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    _Hook.redirect_to = "https://squadreader.com/steal"
    port = hook_server.server_address[1]
    req = urllib.request.Request(f"http://127.0.0.1:{port}/hook", data=b"{}",
                                 method="POST")
    with pytest.raises(netpolicy.OutboundBlocked):
        netpolicy.guarded_urlopen(req, timeout=5, purpose="alert-webhook")
    assert "squadreader.com" not in resolved
    assert netpolicy.blocked_attempts() == 1
