"""Wiring of the live stream into config and the serve loop (cli.py).

* config: the four `live_*` keys, their defaults and validation (fail at start,
  exit code 2, never a silent fallback);
* cli: `_live_publish` is the only thing the tick thread calls (O(1), contained);
  the hub exists only when `export_live_enabled` is true and the Export API is on;
  `/health/deep` carries the live statistics only then;
* the golden `info` shape is untouched while the feature is off.
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from sqreader import cli, config, netpolicy
from sqreader.export_api import ExportApi
from sqreader.livehub import LiveHub

REPO = Path(__file__).resolve().parent.parent
GOLDEN_INFO = REPO / "tests" / "fixtures" / "zz_golden" / "zz-7787" / "info.json"


@pytest.fixture(autouse=True)
def _pristine(monkeypatch, tmp_path):
    monkeypatch.setenv("SQREADER_CONFIG", str(tmp_path / "no-such-config.json"))
    monkeypatch.setattr(config, "_cache", None)
    netpolicy.reset()
    yield
    netpolicy.reset()
    monkeypatch.setattr(config, "_cache", None)


def _config(monkeypatch, tmp_path, **values) -> None:
    path = tmp_path / "sqreader.config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("SQREADER_CONFIG", str(path))
    monkeypatch.setattr(config, "_cache", None)


# -- config ----------------------------------------------------------------------

def test_defaults():
    assert config.DEFAULTS["export_live_enabled"] is False
    assert config.DEFAULTS["live_max_clients"] == 2
    assert config.DEFAULTS["live_ring_seconds"] == 30
    assert config.DEFAULTS["live_ring_max_mb"] == 32
    assert config.live_settings() == {
        "enabled": False, "max_clients": 2, "ring_seconds": 30, "ring_max_mb": 32}


def test_valid_overrides(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path, export_live_enabled=True, live_max_clients=4,
            live_ring_seconds=60, live_ring_max_mb=64)
    assert config.live_settings() == {
        "enabled": True, "max_clients": 4, "ring_seconds": 60, "ring_max_mb": 64}


@pytest.mark.parametrize("bad", [
    {"export_live_enabled": "false"},
    {"export_live_enabled": 1},
    {"export_live_enabled": None},
    {"live_max_clients": 0}, {"live_max_clients": 9}, {"live_max_clients": "2"},
    {"live_max_clients": True}, {"live_max_clients": 2.5},
    {"live_ring_seconds": 4}, {"live_ring_seconds": 121},
    {"live_ring_max_mb": 0}, {"live_ring_max_mb": 257}, {"live_ring_max_mb": None},
])
def test_invalid_values_are_a_config_error(monkeypatch, tmp_path, bad):
    _config(monkeypatch, tmp_path, **bad)
    with pytest.raises(config.ConfigError):
        config.live_settings()


@pytest.mark.parametrize("edge", [
    {"live_max_clients": 1}, {"live_max_clients": 8},
    {"live_ring_seconds": 5}, {"live_ring_seconds": 120},
    {"live_ring_max_mb": 1}, {"live_ring_max_mb": 256},
])
def test_range_edges_are_valid(monkeypatch, tmp_path, edge):
    _config(monkeypatch, tmp_path, **edge)
    config.live_settings()


def test_serve_refuses_a_bad_live_config_with_exit_2(monkeypatch, tmp_path, capsys):
    calls: list[object] = []
    monkeypatch.setattr(cli, "_serve_once", lambda a: calls.append(a) or 0)
    monkeypatch.setattr(cli.time, "sleep", lambda d: None)
    _config(monkeypatch, tmp_path, export_live_enabled="yes")
    assert cli.main(["serve"]) == 2
    assert "export_live_enabled" in capsys.readouterr().err
    assert calls == []
    _config(monkeypatch, tmp_path, live_max_clients=99)
    assert cli.main(["serve"]) == 2
    assert calls == []
    _config(monkeypatch, tmp_path, export_live_enabled=True)
    assert cli.main(["serve"]) == 0
    assert len(calls) == 1


# -- cli wiring --------------------------------------------------------------------

class _Recorder:
    def __init__(self, boom: bool = False) -> None:
        self.calls: list[tuple] = []
        self.boom = boom

    def publish(self, line, kind, match_id=None, match_state=None):
        if self.boom:
            raise RuntimeError("hub exploded")
        self.calls.append((line, kind, match_id, match_state))


def test_live_publish_full_passes_the_line_and_match_fields_verbatim():
    rec = _Recorder()
    line = '{"tick":  5, "gameState": {"matchId": "m", "matchState": "InProgress"}}\n'
    snap = {"gameState": {"matchId": "m", "matchState": "InProgress"}}
    cli._live_publish(rec, line, "full", snap, 5)
    assert rec.calls == [(line, "full", "m", "InProgress")]       # same str object


def test_live_publish_pos_has_no_match_fields():
    rec = _Recorder()
    cli._live_publish(rec, '{"t":"pos"}\n', "pos", None, 6)
    assert rec.calls == [('{"t":"pos"}\n', "pos", None, None)]


def test_live_publish_tolerates_odd_snapshots():
    rec = _Recorder()
    cli._live_publish(rec, "x\n", "full", {"gameState": None}, 1)
    cli._live_publish(rec, "x\n", "full", {}, 2)
    assert [c[2:] for c in rec.calls] == [(None, None), (None, None)]


def test_live_publish_never_raises_into_the_tick(capsys):
    cli._live_publish(_Recorder(boom=True), "x\n", "full", {}, 9)
    assert "live publish error" in capsys.readouterr().err


def test_live_publish_with_a_real_hub():
    hub = LiveHub(server_id="s", hz=1.0)
    snap = {"gameState": {"matchId": "m", "matchState": "InProgress"}}
    for t in range(3):
        cli._live_publish(hub, json.dumps(snap) + "\n", "full", snap, t)
    st = hub.stats()
    assert st["published"] == 3 and st["match"] == {"matchId": "m", "state": "started"}
    hub.close()


def test_serve_loop_wiring_is_conditional_and_only_publishes():
    src = inspect.getsource(cli._serve_once)
    # hub only when enabled; dropped again when the Export API is off
    assert 'if _live_cfg["enabled"]:' in src
    assert "live_hub is not None and export_api is None" in src
    assert "live=live_hub" in src
    # the tick thread's only contact: _live_publish, in both branches
    assert '_live_publish(live_hub, line, "full", snap, tick)' in src
    assert '_live_publish(live_hub, pos_line, "pos", None, tick)' in src
    assert src.count("live_hub.publish(") == 0
    # /health/deep gets the live statistics only when there is a hub
    assert '"live": live_hub.stats()' in src
    # two-tier flag follows the worker that really started
    assert "live_hub.set_two_tier(True)" in src
    # threads are released before the port is
    assert src.index("live_hub.close()") < src.index("srv.server_close()")


def test_tick_order_and_hz_are_untouched():
    src = inspect.getsource(cli._serve_once)
    assert src.index("_live_publish(live_hub, line") > src.index(
        'line = json.dumps(snap, ensure_ascii=False) + "\\n"')
    assert "record_hz" in src and "--hz" not in cli._live_publish.__doc__


# -- golden info stays golden while the feature is off -------------------------------

def test_info_shape_matches_the_golden_file_when_live_is_off(tmp_path):
    golden = json.loads(GOLDEN_INFO.read_text(encoding="utf-8"))
    assert "live" not in golden
    api = ExportApi(token="a" * 40, server_id="zz-7787",
                    stats_db=tmp_path / "stats.db", data_dir=tmp_path,
                    squad_port=7787, container_id="zz-container-7787")
    res = api.handle("GET", "/export/v1/info", "Bearer " + "a" * 40)
    assert res.status == 200
    got = json.loads(res.body)
    assert set(got) == set(golden)


def test_info_gains_live_only_with_a_hub(tmp_path):
    hub = LiveHub(server_id="zz-7787", hz=1.0, two_tier=True, max_clients=3)
    api = ExportApi(token="a" * 40, server_id="zz-7787",
                    stats_db=tmp_path / "stats.db", data_dir=tmp_path, live=hub)
    got = json.loads(api.handle("GET", "/export/v1/info", "Bearer " + "a" * 40).body)
    assert got["live"] == {"enabled": True, "hz": 1.0, "twoTier": True,
                           "clients": 0, "maxClients": 3}
    hub.close()
