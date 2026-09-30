"""Zero Zone deployment guards that keep a reader off the WRONG game server.

* `serve` refuses to start (exit code 2) in Zero Zone / Export-API mode when the
  config names no target criterion (game port, query port, container id).
  Stock upstream behaviour (no Export API) is unchanged: see
  tests/test_discovery.py::test_one_process_without_criteria_is_fine for the
  library-level rule, which this file deliberately does not touch.
* A config file that EXISTS but cannot be parsed is an error (exit code 2), not
  "all defaults". A file that does not exist keeps the zero-config behaviour.
* `sqreader check-instance <port>` (the unit's ExecStartPre) accepts only an
  instance name equal to the game port in the config.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from sqreader import cli, config, discovery, netpolicy

REPO = Path(__file__).resolve().parent.parent
UNIT = REPO / "deploy" / "zerozone" / "sqreader@.service"


@pytest.fixture(autouse=True)
def _pristine(monkeypatch, tmp_path):
    monkeypatch.setenv("SQREADER_CONFIG", str(tmp_path / "no-such-config.json"))
    monkeypatch.setattr(config, "_cache", None)
    netpolicy.reset()
    yield
    netpolicy.reset()
    monkeypatch.setattr(config, "_cache", None)


@pytest.fixture
def served(monkeypatch):
    """Record whether a serve session was started."""
    calls: list[object] = []

    def fake_once(args):
        calls.append(args)
        return 0

    monkeypatch.setattr(cli, "_serve_once", fake_once)
    monkeypatch.setattr(cli.time, "sleep", lambda d: None)
    return calls


def _config(monkeypatch, tmp_path, **values) -> Path:
    path = tmp_path / "sqreader.config.json"
    path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("SQREADER_CONFIG", str(path))
    monkeypatch.setattr(config, "_cache", None)
    return path


def _raw_config(monkeypatch, tmp_path, data: bytes) -> Path:
    path = tmp_path / "sqreader.config.json"
    path.write_bytes(data)
    monkeypatch.setenv("SQREADER_CONFIG", str(path))
    monkeypatch.setattr(config, "_cache", None)
    return path


TOKEN = "export_token_file"


# -- 1. serve needs a target criterion in Zero Zone mode ----------------------

def test_export_mode_without_any_criterion_refuses_to_start(
        monkeypatch, tmp_path, capsys, served):
    _config(monkeypatch, tmp_path, **{TOKEN: "/etc/sqreader/7787.export-token"})
    assert cli.main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "HATA" in err and "hedef ölçütü yok" in err
    assert "no target criterion" in err
    assert served == []                                   # never attached


def test_the_token_file_flag_alone_also_counts_as_export_mode(
        monkeypatch, tmp_path, capsys, served):
    assert cli.main(["serve", "--export-token-file", str(tmp_path / "t")]) == 2
    assert "hedef ölçütü yok" in capsys.readouterr().err
    assert served == []


@pytest.mark.parametrize("crit", [
    {"squad_port": 7787},
    {"squad_query_port": 27165},
    {"expected_container_id": "a" * 64},
    {"squad_port": "7787"},
])
def test_export_mode_with_a_criterion_starts(monkeypatch, tmp_path, served, crit):
    _config(monkeypatch, tmp_path, **{TOKEN: "/etc/sqreader/7787.export-token"},
            **crit)
    assert cli.main(["serve"]) == 0
    assert len(served) == 1


@pytest.mark.parametrize("crit", [
    {"squad_port": None},
    {"squad_port": ""},
    {"squad_port": None, "squad_query_port": "", "expected_container_id": ""},
])
def test_empty_criteria_do_not_count(monkeypatch, tmp_path, served, capsys, crit):
    _config(monkeypatch, tmp_path, **{TOKEN: "/x"}, **crit)
    assert cli.main(["serve"]) == 2
    assert served == []


def test_an_unusable_criterion_is_an_error_too(monkeypatch, tmp_path, served,
                                               capsys):
    _config(monkeypatch, tmp_path, **{TOKEN: "/x"}, squad_port="abc")
    assert cli.main(["serve"]) == 2
    assert "squad_port" in capsys.readouterr().err
    assert served == []


def test_without_export_the_stock_behaviour_is_unchanged(
        monkeypatch, tmp_path, served):
    """No Export API and no criteria: upstream's "the one process there is"."""
    assert config.get("require_target") is None
    assert cli.main(["serve"]) == 0
    assert len(served) == 1


def test_require_target_forces_and_disables(monkeypatch, tmp_path, served,
                                            capsys):
    _config(monkeypatch, tmp_path, require_target=True)
    assert cli.main(["serve"]) == 2
    assert served == []

    _config(monkeypatch, tmp_path, require_target=False, **{TOKEN: "/x"})
    assert cli.main(["serve"]) == 0
    assert len(served) == 1

    _config(monkeypatch, tmp_path, require_target=True, squad_port=7787)
    assert cli.main(["serve"]) == 0
    assert len(served) == 2


def test_the_library_rule_still_allows_one_process_without_criteria():
    """The guard lives in `serve`, not in discovery: the general helper accepts
    'no criteria' (test_discovery.py pins that) and only the explicit
    require_target_criteria() refuses."""
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.require_target_criteria()
    discovery.require_target_criteria(game_port=7787)
    discovery.require_target_criteria(query_port="27165")
    discovery.require_target_criteria(expected_container_id="c" * 12)
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.require_target_criteria(game_port=0)


# -- 2. a broken config file is an error, a missing one is not ----------------

@pytest.mark.parametrize("data", [
    b"{ not json",
    b"",
    b"[1, 2]",
    b"null",
    b'{"squad_port": 7787,}',
    b"\xff\xfe\x00garbage",
])
def test_an_unusable_config_file_stops_every_command(
        monkeypatch, tmp_path, capsys, served, data):
    _raw_config(monkeypatch, tmp_path, data)
    assert cli.main(["serve"]) == 2
    err = capsys.readouterr().err
    assert "HATA" in err and "yapılandırma dosyası" in err
    assert "sqreader.config.json" in err
    assert served == []
    monkeypatch.setattr(config, "_cache", None)
    assert cli.main(["version"]) == 2                   # not only serve


def test_a_config_that_cannot_be_read_is_an_error(monkeypatch, tmp_path, capsys):
    """Unreadable (e.g. wrong owner / mode) must not silently become defaults."""
    path = _config(monkeypatch, tmp_path, squad_port=7787)
    real = Path.read_text

    def deny(self, *a, **k):
        if self == path:
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", deny)
    with pytest.raises(config.ConfigError):
        config.get("squad_port")
    assert cli.main(["version"]) == 2
    assert "Permission denied" in capsys.readouterr().err


def test_a_missing_config_file_keeps_the_zero_config_behaviour(monkeypatch,
                                                               tmp_path):
    monkeypatch.setenv("SQREADER_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setattr(config, "_cache", None)
    assert config.get("squad_port") is None
    assert cli.main(["version"]) == 0


def test_a_valid_config_is_still_read(monkeypatch, tmp_path):
    _config(monkeypatch, tmp_path, squad_port=7787, _comment="ignored")
    assert config.get("squad_port") == 7787
    assert config.get("_comment") is None


# -- 3. check-instance (ExecStartPre) -----------------------------------------

def test_check_instance_accepts_the_matching_port(monkeypatch, tmp_path, capsys):
    _config(monkeypatch, tmp_path, squad_port=7787)
    assert cli.main(["check-instance", "7787"]) == 0
    _config(monkeypatch, tmp_path, squad_port="7787")
    assert cli.main(["check-instance", "7787"]) == 0


def test_check_instance_refuses_another_servers_config(monkeypatch, tmp_path,
                                                        capsys):
    _config(monkeypatch, tmp_path, squad_port=7788)
    assert cli.main(["check-instance", "7787"]) == 2
    err = capsys.readouterr().err
    assert "UYUŞMUYOR" in err and "7787" in err and "7788" in err


def test_check_instance_refuses_a_config_without_a_port(monkeypatch, tmp_path,
                                                         capsys):
    _config(monkeypatch, tmp_path, squad_query_port=27165)
    assert cli.main(["check-instance", "7787"]) == 2
    assert "squad_port" in capsys.readouterr().err
    monkeypatch.setenv("SQREADER_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setattr(config, "_cache", None)
    assert cli.main(["check-instance", "7787"]) == 2


@pytest.mark.parametrize("bad", ["", "abc", "7787;id", "$(id)", "7787 ", "-1",
                                 "٧٧٨٧", "0777777", "1e3"])
def test_check_instance_refuses_a_non_numeric_instance(monkeypatch, tmp_path,
                                                        capsys, bad):
    _config(monkeypatch, tmp_path, squad_port=7787)
    assert cli.main(["check-instance", bad]) == 2
    assert "rakam" in capsys.readouterr().err


def test_check_instance_refuses_a_bad_config_and_a_bad_port_value(
        monkeypatch, tmp_path, capsys):
    _raw_config(monkeypatch, tmp_path, b"{ broken")
    assert cli.main(["check-instance", "7787"]) == 2
    _config(monkeypatch, tmp_path, squad_port="seven")
    assert cli.main(["check-instance", "7787"]) == 2


def test_the_unit_runs_the_check_before_starting():
    text = UNIT.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]
    pre = [ln for ln in lines if ln.startswith("ExecStartPre=")]
    assert "ExecStartPre=/opt/sqreader/venv/bin/sqreader check-instance %i" in pre
    start = next(i for i, ln in enumerate(lines) if ln.startswith("ExecStart="))
    check = lines.index("ExecStartPre=/opt/sqreader/venv/bin/sqreader "
                        "check-instance %i")
    assert check < start
    # the instance name reaches the check as ONE argv word, never through a shell
    assert not any("check-instance" in ln and "sh -c" in ln for ln in lines)


def test_backup_script_state_file_names_match_the_code():
    """yedek-geri-al.sh copies exactly the files export_state.py writes."""
    from sqreader import export_state
    import re

    text = (REPO / "deploy" / "zerozone" / "yedek-geri-al.sh").read_text(
        encoding="utf-8")
    m = re.search(r'^DURUM_DOSYALARI="([^"]*)"', text, re.M)
    assert m is not None
    assert set(m.group(1).split()) == {
        export_state.STATE_FILE, export_state.AUDIT_FILE,
        export_state.RETENTION_STATE_FILE}
    assert "paralel görev" not in text
