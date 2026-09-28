"""Safe game-process discovery (sqreader/discovery.py).

These run against a FAKE /proc tree in a temp dir (plus a `ProcFS` subclass to
simulate an exe link or a permission error, which a temp dir cannot on every OS).
They pin down the discovery LOGIC. They are not a real-memory or real-host test:
nothing here proves what a real Docker/Pterodactyl host's cgroup or cmdline
looks like; see the "run on the real host" commands in the task report.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import pytest

from sqreader import cli, config, discovery
from sqreader.discovery import (
    AmbiguousGameProcess, AmbiguousLogPath, ContainerMismatch, GameProcess,
    GameProcessNotFound, ProcFS, ProcUnavailable, ServerIdMismatch, TargetLost,
)

BIN = "/home/container/SquadGame/Binaries/Linux/SquadGameServer"
CID_A = "a" * 64
CID_B = "b1" * 32
UUID_A = "6f0e3c1a-2b4d-4e5f-8a9b-0c1d2e3f4a5b"


def stat_line(pid: int, comm: str, start: int) -> str:
    fields = ["S", "1", str(pid), str(pid), "0", "-1", "4194560", "100", "0",
              "0", "0", "10", "5", "0", "0", "20", "0", "1", "0", str(start),
              "123456", "789"]
    return f"{pid} ({comm}) " + " ".join(fields)


def game_argv(port: int | None = 7787, query: int | None = 27165,
              *, url_style: bool = False) -> list[str]:
    argv = [BIN, "SquadGame"]
    if url_style:
        parts = ["/Game/Maps/Foo"]
        if port is not None:
            parts.append(f"Port={port}")
        if query is not None:
            parts.append(f"QueryPort={query}")
        argv.append("?".join(parts))
    else:
        if port is not None:
            argv.append(f"-Port={port}")
        if query is not None:
            argv.append(f"-QueryPort={query}")
    argv += ["-log", "-NOSTEAM"]
    return argv


def cgroup_v2(cid: str) -> str:
    return f"0::/system.slice/docker-{cid}.scope\n"


def add_proc(root, pid: int, argv: list[str], *, start: int = 5000,
             comm: str = "SquadGameServer", cgroup: str | None = None) -> None:
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "cmdline").write_bytes(("\0".join(argv) + "\0").encode("utf-8"))
    (d / "stat").write_text(stat_line(pid, comm, start), encoding="utf-8")
    if cgroup is not None:
        (d / "cgroup").write_text(cgroup, encoding="utf-8")


class FakeFS(ProcFS):
    """A real temp tree, plus exe links and permission errors by table."""

    def __init__(self, root, exes=None, denied=()):
        super().__init__(root)
        self.exes = dict(exes or {})
        self.denied = set(denied)          # {(pid, "cmdline"|"exe"|"cgroup"|"stat")}

    def read_bytes(self, pid, name):
        if (pid, name) in self.denied:
            raise PermissionError(13, "Permission denied")
        return super().read_bytes(pid, name)

    def readlink(self, pid, name):
        if (pid, name) in self.denied:
            raise PermissionError(13, "Permission denied")
        if pid in self.exes:
            return self.exes[pid]
        raise FileNotFoundError(name)


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / "proc"
    root.mkdir()
    return root


# ---------------------------------------------------------------- criteria 1-4

def test_game_port_selects_only_the_matching_process(proc):
    add_proc(proc, 4101, game_argv(7787, 27165), cgroup=cgroup_v2(CID_A))
    add_proc(proc, 4202, game_argv(7788, 27166), cgroup=cgroup_v2(CID_B))

    a = discovery.discover_game_process(proc_root=proc, game_port=7787)
    b = discovery.discover_game_process(proc_root=proc, game_port=7788)

    assert (a.pid, a.game_port, a.query_port) == (4101, 7787, 27165)
    assert a.container_id == CID_A
    assert (b.pid, b.game_port, b.container_id) == (4202, 7788, CID_B)
    assert a.matched_port_kind == "Port"


def test_no_criteria_and_two_processes_is_an_error_naming_both(proc):
    add_proc(proc, 4101, game_argv(7787), cgroup=cgroup_v2(CID_A))
    add_proc(proc, 4202, game_argv(7788), cgroup=cgroup_v2(CID_B))

    with pytest.raises(AmbiguousGameProcess) as ei:
        discovery.discover_game_process(proc_root=proc)

    msg = str(ei.value)
    assert "4101" in msg and "4202" in msg
    assert "7787" in msg and "7788" in msg
    assert CID_A[:12] in msg and CID_B[:12] in msg
    assert "ilk eşleşme seçilmez" in msg
    assert [c.pid for c in ei.value.candidates] == [4101, 4202]


def test_one_process_without_criteria_is_fine(proc):
    add_proc(proc, 4101, game_argv(7787))
    got = discovery.discover_game_process(proc_root=proc)
    assert got.pid == 4101
    assert got.matched_port_kind is None


def test_same_port_twice_is_ambiguous_not_first_wins(proc):
    add_proc(proc, 4101, game_argv(7787))
    add_proc(proc, 4202, game_argv(7787))
    with pytest.raises(AmbiguousGameProcess) as ei:
        discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert "4101" in str(ei.value) and "4202" in str(ei.value)


def test_ports_match_as_whole_numbers_and_per_key(proc):
    add_proc(proc, 4101, game_argv(77870, 27165))      # Port=77870
    add_proc(proc, 4202, game_argv(7788, 7787))        # QueryPort=7787
    with pytest.raises(GameProcessNotFound) as ei:
        discovery.discover_game_process(proc_root=proc, game_port=7787)
    msg = str(ei.value)
    assert "4101" in msg and "4202" in msg              # both reported as dropped
    # The same number IS found under the key it really is:
    got = discovery.discover_game_process(proc_root=proc, query_port=7787)
    assert (got.pid, got.matched_port_kind) == (4202, "QueryPort")


def test_both_keys_can_be_required_and_are_reported(proc):
    add_proc(proc, 4101, game_argv(7787, 27165))
    add_proc(proc, 4202, game_argv(7787, 27166))
    got = discovery.discover_game_process(proc_root=proc, game_port=7787,
                                          query_port=27166)
    assert (got.pid, got.matched_port_kind) == (4202, "Port+QueryPort")


@pytest.mark.parametrize("args, want", [
    (["-Port=7787"], {"Port": [7787], "QueryPort": []}),
    (["-port=7787", "-QUERYPORT=27165"], {"Port": [7787], "QueryPort": [27165]}),
    (["Port=7787"], {"Port": [7787], "QueryPort": []}),
    (["/Game/Maps/Foo?Port=7787?QueryPort=27165?Foo=1"],
     {"Port": [7787], "QueryPort": [27165]}),
    (["-BeaconPort=7787", "-SteamPort=7787"], {"Port": [], "QueryPort": []}),
    (["-Port=7787abc", "-Port=", "-Port=123456"], {"Port": [], "QueryPort": []}),
    (["-Port=77870"], {"Port": [77870], "QueryPort": []}),
])
def test_command_line_port_forms(args, want):
    assert discovery.parse_ports(args) == want


def test_url_style_arguments_are_matched(proc):
    add_proc(proc, 4101, game_argv(7787, 27165, url_style=True))
    got = discovery.discover_game_process(proc_root=proc, game_port=7787,
                                          query_port=27165)
    assert got.pid == 4101


def test_conflicting_duplicate_port_arguments_are_not_guessed(proc):
    add_proc(proc, 4101, [BIN, "-Port=7787", "-Port=7788"])
    with pytest.raises(GameProcessNotFound) as ei:
        discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert "çelişkili" in str(ei.value)


# ------------------------------------------------------------------ launchers 5

def test_launcher_shell_is_eliminated(proc):
    add_proc(proc, 3000, ["bash", "start.sh", BIN, "SquadGame", "-Port=7787"],
             comm="bash")
    add_proc(proc, 4101, game_argv(7787))

    got = discovery.discover_game_process(proc_root=proc, game_port=7787)

    assert got.pid == 4101


def test_only_a_launcher_is_not_a_match_and_is_counted(proc):
    add_proc(proc, 3000, ["/bin/sh", "-c", f"{BIN} -Port=7787"], comm="sh")
    add_proc(proc, 3001, ["tini", "--", "bash", "start.sh", BIN, "-Port=7787"],
             comm="tini")
    with pytest.raises(GameProcessNotFound) as ei:
        discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert "2 süreç başlatıcı" in str(ei.value)


def test_exec_a_impostor_is_dropped_because_the_exe_link_wins(proc):
    """argv[0] can be set to anything (`exec -a`); the exe link cannot."""
    add_proc(proc, 3000, [BIN, "-Port=7787"], comm="bash")
    add_proc(proc, 4101, game_argv(7787))
    fs = FakeFS(proc, exes={3000: "/usr/bin/bash", 4101: BIN})

    got = discovery.discover_game_process(fs=fs, game_port=7787)

    assert got.pid == 4101 and got.exe == BIN


def test_an_exe_the_reader_may_not_read_is_not_trusted_and_is_reported(proc):
    add_proc(proc, 4101, game_argv(7787))
    fs = FakeFS(proc, denied={(4101, "exe")})
    with pytest.raises(GameProcessNotFound) as ei:
        discovery.discover_game_process(fs=fs, game_port=7787)
    msg = str(ei.value)
    assert "1 süreç izin nedeniyle okunamadı" in msg
    assert "4101" in msg


def test_exe_path_suffix_is_enforced(proc):
    add_proc(proc, 4101, game_argv(7787))
    add_proc(proc, 4202, [BIN.replace("/home/container", "/opt/other"),
                          "-Port=7787"])
    got = discovery.discover_game_process(
        proc_root=proc, expected_exe_path_suffix="/home/container/SquadGame/"
                                                 "Binaries/Linux/SquadGameServer")
    assert got.pid == 4101


# --------------------------------------------------------------- containers 6

CGROUPS = {
    "v1_docker": (f"12:cpu,cpuacct:/docker/{CID_A}\n1:name=systemd:/docker/{CID_A}\n"
                  "0::/system.slice/containerd.service\n"),
    "v2_systemd_scope": f"0::/system.slice/docker-{CID_A}.scope\n",
    "v2_cgroupfs": f"0::/docker/{CID_A}\n",
    "v2_nested_leaf": f"0::/docker/{CID_A}/init.scope\n",
    "containerd_cri": (f"0::/kubepods.slice/kubepods-burstable.slice/"
                       f"cri-containerd-{CID_A}.scope\n"),
    "libpod": f"0::/machine.slice/libpod-{CID_A}.scope\n",
    "pterodactyl_uuid_in_path": f"0::/docker/{CID_A}\n0::/pterodactyl/{UUID_A}\n",
}


@pytest.mark.parametrize("name", [k for k in CGROUPS if k != "pterodactyl_uuid_in_path"])
def test_container_id_is_parsed_from_cgroup_v1_and_v2(name):
    assert discovery.parse_container_id(CGROUPS[name]) == CID_A


@pytest.mark.parametrize("text", [
    "",
    "0::/\n",                                             # private cgroup namespace
    "0::/user.slice/user-1000.slice/session-3.scope\n",
    f"0::/some/place/{CID_A}\n",                          # 64 hex, no runtime marker
    f"0::/docker/{'c' * 63}\n",                           # 63 hex: not an id
    f"0::/docker/{CID_A}\n0::/docker/{CID_B}\n",          # disagreeing lines
    "garbage\n",
])
def test_unrecognised_cgroup_layouts_give_none(text):
    assert discovery.parse_container_id(text) is None


def test_server_uuid_only_when_it_is_really_in_the_path():
    assert discovery.parse_server_uuid(
        f"0::/docker/{CID_A}\n0::/pterodactyl/{UUID_A}\n") == UUID_A
    assert discovery.parse_server_uuid(CGROUPS["v2_systemd_scope"]) is None


def test_container_and_uuid_land_on_the_result(proc):
    add_proc(proc, 4101, game_argv(7787),
             cgroup=CGROUPS["pterodactyl_uuid_in_path"])
    got = discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert got.container_id == CID_A and got.server_uuid == UUID_A


def test_unrecognised_cgroup_gives_container_id_none(proc):
    add_proc(proc, 4101, game_argv(7787), cgroup="0::/\n")
    assert discovery.discover_game_process(
        proc_root=proc, game_port=7787).container_id is None


def test_expected_container_id_full_and_prefix_match(proc):
    add_proc(proc, 4101, game_argv(7787), cgroup=cgroup_v2(CID_A))
    full = discovery.discover_game_process(
        proc_root=proc, game_port=7787, expected_container_id=CID_A)
    pref = discovery.discover_game_process(
        proc_root=proc, game_port=7787, expected_container_id=CID_A[:12].upper())
    assert full.pid == pref.pid == 4101


def test_expected_container_mismatch_is_dropped_and_says_what_to_do(proc):
    add_proc(proc, 4101, game_argv(7787), cgroup=cgroup_v2(CID_B))
    with pytest.raises(ContainerMismatch) as ei:
        discovery.discover_game_process(proc_root=proc, game_port=7787,
                                        expected_container_id=CID_A)
    msg = str(ei.value)
    assert "expected_container_id" in msg and "güncelle" in msg
    assert CID_B[:12] in msg
    assert isinstance(ei.value, GameProcessNotFound)


def test_two_servers_are_told_apart_by_container_alone(proc):
    add_proc(proc, 4101, game_argv(7787), cgroup=cgroup_v2(CID_A))
    add_proc(proc, 4202, game_argv(7787), cgroup=cgroup_v2(CID_B))
    got = discovery.discover_game_process(proc_root=proc, game_port=7787,
                                          expected_container_id=CID_B[:16])
    assert got.pid == 4202


def test_unknown_container_with_an_expected_one_is_refused(proc):
    add_proc(proc, 4101, game_argv(7787), cgroup="0::/\n")
    with pytest.raises(GameProcessNotFound) as ei:
        discovery.discover_game_process(proc_root=proc, game_port=7787,
                                        expected_container_id=CID_A)
    assert "okunamadı/tanınmadı" in str(ei.value)


@pytest.mark.parametrize("bad", ["abc", "zz" * 8, "a" * 65])
def test_unusable_expected_container_id_is_a_config_error(proc, bad):
    add_proc(proc, 4101, game_argv(7787))
    with pytest.raises(discovery.DiscoveryConfigError):
        discovery.discover_game_process(proc_root=proc, expected_container_id=bad)


# ------------------------------------------------ restart / PID reuse / stat 7-9

def test_restart_old_pid_gone_new_pid_same_port_is_rediscovered(proc):
    add_proc(proc, 4101, game_argv(7787), start=5000, cgroup=cgroup_v2(CID_A))
    old = discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert discovery.is_same_process(old)

    import shutil
    shutil.rmtree(proc / "4101")
    assert not discovery.is_same_process(old)          # gone

    add_proc(proc, 4777, game_argv(7787), start=9100, cgroup=cgroup_v2(CID_A))
    new = discovery.discover_game_process(proc_root=proc, game_port=7787)

    assert new.pid == 4777
    assert not discovery.is_same_process(old)          # the old identity is dead
    assert discovery.is_same_process(new)


def test_same_pid_different_start_time_is_a_different_process(proc):
    add_proc(proc, 4101, game_argv(7787), start=5000)
    old = discovery.discover_game_process(proc_root=proc, game_port=7787)
    (proc / "4101" / "stat").write_text(stat_line(4101, "SquadGameServer", 8000),
                                        encoding="utf-8")
    assert not discovery.is_same_process(old)
    again = discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert again.pid == old.pid and again.identity != old.identity


@pytest.mark.parametrize("comm", [
    "SquadGameServer", "Main Thread", "a) b", "(x)", "sh (run) 1 2 3", "x) S 1 2 (",
])
def test_stat_comm_with_spaces_and_parens_is_parsed(comm):
    assert discovery.parse_stat_start_time(stat_line(4101, comm, 424242)) == 424242


def test_stat_garbage_is_a_value_error():
    with pytest.raises(ValueError):
        discovery.parse_stat_start_time("4101 no parens here")
    with pytest.raises(ValueError):
        discovery.parse_stat_start_time("4101 (x) S 1 2")


def test_start_time_is_stored_from_field_22(proc):
    add_proc(proc, 4101, game_argv(7787), start=777001, comm="Squad (Game) Srv")
    got = discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert got.start_time_ticks == 777001


def test_is_same_process_does_not_treat_a_permission_error_as_gone(proc):
    add_proc(proc, 4101, game_argv(7787))
    got = discovery.discover_game_process(proc_root=proc, game_port=7787)
    fs = FakeFS(proc, denied={(4101, "stat")})
    with pytest.raises(PermissionError):
        discovery.is_same_process(got, fs=fs)
    # ...and the loops' wrapper reads "cannot tell" as "still there":
    orig = discovery.is_same_process
    try:
        discovery.is_same_process = lambda p, **k: (_ for _ in ()).throw(PermissionError())
        assert cli._target_alive(4101, got) is True
    finally:
        discovery.is_same_process = orig


# ------------------------------------------------------------- permissions 10

def test_permission_errors_are_counted_and_reported_not_skipped_silently(proc):
    add_proc(proc, 4101, game_argv(7787))                       # readable
    add_proc(proc, 4300, game_argv(7788))
    add_proc(proc, 4301, ["x"])
    add_proc(proc, 4302, ["y"])
    fs = FakeFS(proc, exes={4101: BIN},
                denied={(4300, "cmdline"), (4301, "cmdline"), (4302, "cgroup")})
    with pytest.raises(GameProcessNotFound) as ei:
        discovery.discover_game_process(fs=fs, game_port=9999)
    rep = ei.value.report
    assert sorted(rep.permission_denied) == [4300, 4301]
    assert "2 süreç izin nedeniyle okunamadı" in str(ei.value)
    assert "4300" in str(ei.value)


def test_a_successful_match_still_carries_the_permission_count_in_doctor(proc):
    add_proc(proc, 4101, game_argv(7787), cgroup=cgroup_v2(CID_A))
    add_proc(proc, 4300, ["z"])
    fs = FakeFS(proc, exes={4101: BIN}, denied={(4300, "cmdline")})
    status, lines = discovery.discovery_report(fs=fs, game_port=7787)
    assert status == "ok"
    text = "\n".join(lines)
    assert "1 süreç izin nedeniyle okunamadı" in text and "4300" in text


def test_a_vanished_process_is_churn_not_an_error(proc):
    add_proc(proc, 4101, game_argv(7787))
    (proc / "4400").mkdir()                    # no files at all: exited mid-scan
    got = discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert got.pid == 4101


def test_kernel_threads_with_empty_cmdline_are_ignored(proc):
    add_proc(proc, 2, [], comm="kthreadd")
    (proc / "2" / "cmdline").write_bytes(b"")
    add_proc(proc, 4101, game_argv(7787))
    assert discovery.discover_game_process(proc_root=proc).pid == 4101


# ------------------------------------------------------------------- not found 13

def test_no_process_is_a_not_found_error_naming_the_criteria(proc):
    add_proc(proc, 4101, game_argv(7788))
    add_proc(proc, 100, ["/sbin/init"], comm="systemd")
    with pytest.raises(GameProcessNotFound) as ei:
        discovery.discover_game_process(proc_root=proc, game_port=7787,
                                        expected_container_id=CID_A)
    msg = str(ei.value)
    assert "Port=7787" in msg and CID_A in msg
    assert "taranan süreç: 2" in msg and "aday: 1" in msg
    assert "PID 4101" in msg and "Port=7788" in msg


def test_missing_proc_is_reported_as_not_run_not_as_success(tmp_path):
    with pytest.raises(ProcUnavailable) as ei:
        discovery.discover_game_process(proc_root=tmp_path / "no-proc")
    assert "Linux host gerekli" in str(ei.value)
    status, lines = discovery.discovery_report(proc_root=tmp_path / "no-proc")
    assert status == "skipped"
    assert "ÇALIŞTIRILMADI" in lines[0] and "Linux host gerekli" in lines[0]


def test_doctor_prints_discovery_first_and_a_skip_never_looks_like_a_pass(
        monkeypatch, capsys):
    monkeypatch.setattr(discovery, "discovery_report", lambda **k: (
        "skipped", ["ÇALIŞTIRILMADI: Linux host gerekli (/proc bulunamadı); "
                    "süreç keşfi doğrulanmadı"]))

    def _no_proc(pid=None):
        raise ProcUnavailable("çalıştırılmadı: Linux host gerekli")

    monkeypatch.setattr(config, "resolve_game_process", _no_proc)
    rc = cli.main(["doctor"])
    out = capsys.readouterr()
    assert rc == 2
    assert "== süreç keşfi ==" in out.out
    assert "SKIP" in out.out and "ÇALIŞTIRILMADI" in out.out
    assert "PASS" not in out.out and "OK  " not in out.out
    assert "Linux host gerekli" in out.err


def test_doctor_report_lists_pid_ports_key_container_and_log(proc):
    add_proc(proc, 4101, game_argv(7787, 27165), start=5000,
             cgroup=CGROUPS["pterodactyl_uuid_in_path"])
    log = proc / "4101" / "root" / "home" / "container" / "SquadGame" / "Saved" / "Logs"
    log.mkdir(parents=True)
    (log / "SquadGame.log").write_text("x", encoding="utf-8")
    status, lines = discovery.discovery_report(proc_root=proc, game_port=7787)
    text = "\n".join(lines)
    assert status == "ok"
    assert "eşleşen PID: 4101" in text and "start_time_ticks=5000" in text
    assert "Port=7787" in text and "QueryPort=27165" in text
    assert "eşleşen port anahtarı: Port" in text
    assert CID_A in text and UUID_A in text
    assert "SquadGame.log" in text and "(kaynak: container)" in text
    assert "izin sorunu: yok" in text


# -------------------------------------------------------------- waiting / loop 13

def _nf():
    return GameProcessNotFound("yok")


def test_waiting_for_a_missing_game_backs_off_and_never_raises():
    calls = {"n": 0}
    slept: list[float] = []
    states: list[tuple] = []
    sentinel = object()

    def discover():
        calls["n"] += 1
        if calls["n"] <= 6:
            raise _nf()
        return sentinel

    got = discovery.wait_for_game_process(
        discover, sleep=slept.append, min_delay=5, max_delay=30,
        on_state=lambda st, e, n, d: states.append((st, n, d)))

    assert got is sentinel
    assert slept == [5, 10, 20, 30, 30, 30]            # bounded back-off
    assert [s[0] for s in states] == ["waiting_for_game"] * 6
    assert [s[1] for s in states] == [1, 2, 3, 4, 5, 6]


def test_container_mismatch_is_reported_as_its_own_state_while_waiting():
    seq = [ContainerMismatch("kimlik farklı"), None]
    states: list[str] = []

    def discover():
        e = seq.pop(0)
        if e:
            raise e
        return "proc"

    assert discovery.wait_for_game_process(
        discover, sleep=lambda d: None,
        on_state=lambda st, e, n, d: states.append(st)) == "proc"
    assert states == ["container_mismatch"]


def test_ambiguity_and_config_errors_are_not_waited_out():
    slept: list[float] = []

    def boom():
        raise AmbiguousGameProcess("iki süreç", [])

    with pytest.raises(AmbiguousGameProcess):
        discovery.wait_for_game_process(boom, sleep=slept.append)
    assert slept == []


def test_max_wait_turns_the_wait_into_the_last_error():
    t = {"now": 0.0}

    def sleep(d):
        t["now"] += d

    with pytest.raises(GameProcessNotFound):
        discovery.wait_for_game_process(
            _raise_nf, sleep=sleep, clock=lambda: t["now"],
            min_delay=5, max_delay=5, max_wait=12)


def _raise_nf():
    raise _nf()


def test_reattach_loop_restarts_the_session_after_the_target_is_lost():
    t = {"now": 0.0}
    slept: list[float] = []
    lost: list[tuple] = []
    runs = {"n": 0}

    def sleep(d):
        slept.append(d)
        t["now"] += d

    def session():
        runs["n"] += 1
        t["now"] += 1.0                                  # dies quickly, every time
        if runs["n"] <= 3:
            raise TargetLost(f"kayıp {runs['n']}")
        return 0

    rc = discovery.reattach_loop(
        session, on_lost=lambda e, n, d: lost.append((n, d)), sleep=sleep,
        clock=lambda: t["now"], min_delay=5, max_delay=8)

    assert rc == 0 and runs["n"] == 4
    assert slept == [5, 8, 8]                             # bounded, not a busy loop
    assert [n for n, _ in lost] == [1, 2, 3]


def test_a_session_that_ran_long_resets_the_pause():
    t = {"now": 0.0}
    slept: list[float] = []
    runs = {"n": 0}

    def session():
        runs["n"] += 1
        t["now"] += 100.0 if runs["n"] == 3 else 1.0
        if runs["n"] <= 3:
            raise TargetLost("x")
        return "done"

    discovery.reattach_loop(session, sleep=slept.append,
                            clock=lambda: t["now"], min_delay=5, max_delay=60,
                            stable_after=60)
    assert slept == [5, 10, 5]


def test_a_clean_stop_ends_the_loop_without_rediscovery():
    runs = {"n": 0}

    def session():
        runs["n"] += 1
        return 0

    assert discovery.reattach_loop(session, sleep=lambda d: pytest.fail("slept")) == 0
    assert runs["n"] == 1


def test_serve_survives_the_game_disappearing(monkeypatch, tmp_path):
    """cmd_serve's outer loop, with the session faked: two losses, then a clean
    stop. It must neither crash nor keep the old --pid."""
    sessions = {"n": 0}
    pids_seen: list = []

    def fake_once(a):
        sessions["n"] += 1
        pids_seen.append(a.pid)
        if sessions["n"] <= 2:
            raise TargetLost("oyun süreci kayboldu")
        return 0

    slept: list[float] = []
    monkeypatch.setattr(cli, "_serve_once", fake_once)
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    monkeypatch.setattr(cli, "_reset_signal_handlers", lambda: None)

    args = argparse.Namespace(stats_db=None, server_id="zz-7787", pid=4242)
    assert cli.cmd_serve(args) == 0
    assert sessions["n"] == 3
    assert pids_seen == [4242, None, None]                # the pin is dropped
    assert len(slept) == 2


def test_open_pipeline_or_wait_waits_on_a_missing_game_and_reports_state(
        monkeypatch):
    calls = {"n": 0}
    result = (1, "pm", "arr", "al", "p", "m")

    def fake(pid, *, with_metadata=True):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GameProcessNotFound("Oyun sunucusu süreci bulunamadı")
        if calls["n"] == 2:
            raise ContainerMismatch("konteyner farklı")
        return result

    monkeypatch.setattr(cli, "_open_pipeline", fake)
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    monkeypatch.setattr(cli, "_is_compiled", lambda: False)
    seen: list = []

    class A:
        pid = None
        no_metadata = True
        stats_db = None

    got = cli._open_pipeline_or_wait(
        A(), state_dir=None, channel="stable", started=0.0,
        on_wait=lambda st, why, n, d: seen.append((st, n)))

    assert got == result
    assert seen == [("waiting_for_game", 1), ("container_mismatch", 2)]


def test_open_pipeline_or_wait_refuses_an_ambiguous_target_instead_of_waiting(
        monkeypatch):
    def fake(pid, *, with_metadata=True):
        raise AmbiguousGameProcess("iki süreç: PID 4101, PID 4202", [])

    monkeypatch.setattr(cli, "_open_pipeline", fake)
    monkeypatch.setattr(cli.time, "sleep",
                        lambda s: pytest.fail("must not wait on ambiguity"))

    class A:
        pid = None
        no_metadata = True
        stats_db = None

    with pytest.raises(AmbiguousGameProcess):
        cli._open_pipeline_or_wait(A(), state_dir=None, channel="stable",
                                   started=0.0)


def test_main_turns_a_refusal_into_a_message_and_exit_code_2(monkeypatch, capsys):
    def refuse(a):
        raise AmbiguousGameProcess(
            "Belirsiz oyun sunucusu: PID 4101 ve PID 4202", [])

    monkeypatch.setattr(cli, "cmd_summary", refuse)
    p = cli.build_parser()
    ns = p.parse_args(["summary"])
    ns.func = refuse
    monkeypatch.setattr(cli, "build_parser", lambda: argparse.Namespace(
        parse_args=lambda argv: ns))

    assert cli.main([]) == 2
    assert "HATA: Belirsiz oyun sunucusu" in capsys.readouterr().err


# ------------------------------------------------------------- --pid & config

def test_pid_is_verified_against_the_binary_and_the_ports(proc):
    add_proc(proc, 3000, ["bash", "start.sh", BIN, "-Port=7787"], comm="bash")
    add_proc(proc, 4101, game_argv(7787))
    add_proc(proc, 4202, game_argv(7788))

    ok = discovery.verify_game_process(4101, proc_root=proc, game_port=7787)
    assert ok.pid == 4101
    with pytest.raises(GameProcessNotFound):               # wrong port
        discovery.verify_game_process(4202, proc_root=proc, game_port=7787)
    with pytest.raises(GameProcessNotFound):               # a launcher, not the game
        discovery.verify_game_process(3000, proc_root=proc)
    with pytest.raises(GameProcessNotFound):               # not running
        discovery.verify_game_process(999, proc_root=proc)


def test_resolve_game_process_uses_the_config_criteria(monkeypatch):
    cfg = {"squad_port": 7788, "squad_query_port": None,
           "expected_container_id": CID_A[:12], "log_path_in_container": "/x/y.log",
           "squad_process_name": "SquadGameServer"}
    monkeypatch.setattr(config, "get", lambda k: cfg.get(k, config.DEFAULTS.get(k)))
    seen = {}

    def fake_discover(**kw):
        seen.update(kw)
        return "P"

    monkeypatch.setattr(discovery, "discover_game_process", fake_discover)
    assert config.resolve_game_process() == "P"
    assert seen["game_port"] == 7788 and seen["expected_container_id"] == CID_A[:12]
    assert seen["log_path_in_container"] == "/x/y.log"
    assert "SquadGameServer" in seen["exe_names"]

    monkeypatch.setattr(discovery, "verify_game_process",
                        lambda pid, **kw: ("V", pid, kw["game_port"]))
    assert config.resolve_game_process(4321) == ("V", 4321, 7788)


def test_new_config_keys_default_to_null():
    for key in ("squad_port", "squad_query_port", "expected_container_id",
                "log_path_in_container"):
        assert config.DEFAULTS[key] is None


def test_the_example_config_carries_the_new_keys():
    import json
    example = json.loads((Path(__file__).resolve().parent.parent
                          / "sqreader.config.example.json").read_text(encoding="utf-8"))
    for key in ("squad_port", "squad_query_port", "expected_container_id",
                "log_path_in_container", "log_path"):
        assert example[key] is None


# ------------------------------------------------------------------- log path 11

def test_log_path_is_built_under_the_process_root(proc):
    add_proc(proc, 4101, game_argv(7787))
    got = discovery.discover_game_process(proc_root=proc, game_port=7787)
    assert got.root_path == str(proc / "4101" / "root")
    assert got.log_path == str(
        proc / "4101" / "root" / "home" / "container" / "SquadGame" / "Saved"
        / "Logs" / "SquadGame.log")


def test_log_path_in_container_is_configurable(proc):
    add_proc(proc, 4101, game_argv(7787))
    got = discovery.discover_game_process(
        proc_root=proc, game_port=7787, log_path_in_container="/srv/sq/x.log")
    assert got.log_path == str(proc / "4101" / "root" / "srv" / "sq" / "x.log")


def _proc_obj(**kw) -> GameProcess:
    base = dict(pid=4101, start_time_ticks=1, cmdline=(BIN,), exe=BIN,
                matched_port_kind=None, game_port=7787, query_port=None,
                container_id=None, root_path="/proc/4101/root",
                log_path="/proc/4101/root/home/container/SquadGame/Saved/Logs/"
                         "SquadGame.log")
    base.update(kw)
    return GameProcess(**base)


def test_explicit_log_path_wins():
    res = discovery.resolve_log_path(_proc_obj(), explicit="/var/log/sq.log",
                                     is_readable=lambda p: False)
    assert (res.path, res.source) == ("/var/log/sq.log", "explicit")


def test_log_is_taken_from_the_container_root_when_it_exists(proc):
    add_proc(proc, 4101, game_argv(7787))
    p = discovery.discover_game_process(proc_root=proc, game_port=7787)
    log = proc / "4101" / "root" / "home" / "container" / "SquadGame" / "Saved" / "Logs"
    log.mkdir(parents=True)
    (log / "SquadGame.log").write_text("hi", encoding="utf-8")
    res = discovery.resolve_log_path(p, glob_pattern="/never/*.log")
    assert (res.path, res.source) == (p.log_path, "container")


def test_log_falls_back_to_the_exe_relative_location():
    p = _proc_obj(exe="/opt/squad/SquadGame/Binaries/Linux/SquadGameServer")
    want = os.path.join("/proc/4101/root", "opt", "squad", "SquadGame", "Saved",
                        "Logs", "SquadGame.log")
    want = str(Path(want))
    res = discovery.resolve_log_path(p, is_readable=lambda x: x == want)
    assert (res.path, res.source) == (want, "exe")


def test_glob_fallback_is_used_only_outside_containers_and_once():
    hits = ["/home/a/serverfiles/SquadGame/Saved/Logs/SquadGame.log"]
    res = discovery.resolve_log_path(
        _proc_obj(container_id=None), glob_pattern="/home/*/x.log",
        is_readable=lambda p: p in hits, glob_fn=lambda pat: list(hits))
    assert (res.path, res.source) == (hits[0], "glob")


def test_glob_fallback_with_several_matches_is_an_error_not_the_first():
    hits = ["/home/a/SquadGame.log", "/home/b/SquadGame.log"]
    with pytest.raises(AmbiguousLogPath) as ei:
        discovery.resolve_log_path(
            _proc_obj(container_id=None), glob_pattern="/home/*/SquadGame.log",
            is_readable=lambda p: p in hits, glob_fn=lambda pat: list(hits))
    assert "/home/a/SquadGame.log" in str(ei.value)
    assert "/home/b/SquadGame.log" in str(ei.value)
    assert "log_path" in str(ei.value)


def test_a_containerised_process_never_falls_back_to_the_host_glob():
    hits = ["/home/a/SquadGame.log"]
    res = discovery.resolve_log_path(
        _proc_obj(container_id=CID_A), glob_pattern="/home/*/SquadGame.log",
        is_readable=lambda p: p in hits, glob_fn=lambda pat: list(hits))
    assert res.path is None and res.source == "none"


# ---------------------------------------------------- data directory guard 12

def _make_stats_db(path, server_id):
    from sqreader.stats import StatsStore
    store = StatsStore(path, server_id=server_id)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO matches (match_id, server_id, started_at) "
                 "VALUES (?, ?, 1)", (f"m-{server_id}", server_id))
    conn.commit()
    conn.close()
    store.close()


def test_a_data_dir_of_another_server_is_refused(tmp_path):
    db = tmp_path / "stats.db"
    _make_stats_db(db, "zz-7787")
    with pytest.raises(ServerIdMismatch) as ei:
        discovery.check_stats_db_server_id(db, "zz-7788")
    msg = str(ei.value)
    assert "zz-7787" in msg and "zz-7788" in msg and "ayrı" in msg


def test_the_same_server_id_and_fresh_dirs_pass(tmp_path):
    db = tmp_path / "stats.db"
    discovery.check_stats_db_server_id(db, "zz-7787")          # no file yet
    _make_stats_db(db, "zz-7787")
    discovery.check_stats_db_server_id(db, "zz-7787")
    empty = tmp_path / "empty.db"
    sqlite3.connect(empty).close()                             # no matches table
    discovery.check_stats_db_server_id(empty, "anything")


def test_serve_refuses_to_start_on_the_other_servers_data_dir(
        tmp_path, monkeypatch, capsys):
    db = tmp_path / "stats.db"
    _make_stats_db(db, "zz-7787")
    monkeypatch.setattr(cli, "_serve_once", lambda a: pytest.fail(
        "must not attach before the data dir is checked"))

    rc = cli.main(["serve", "--stats-db", str(db), "--server-id", "zz-7788"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "HATA" in err and "başka bir sunucuya ait" in err


def test_backfill_refuses_the_other_servers_data_dir(tmp_path, capsys):
    db = tmp_path / "stats.db"
    _make_stats_db(db, "zz-7787")
    rec = tmp_path / "rec"
    rec.mkdir()
    old = rec / "a.sqrx"
    old.write_bytes(b"x")
    os.utime(old, (1, 1))
    rc = cli.main(["stats-backfill", "--recordings-dir", str(rec),
                   "--stats-db", str(db), "--server-id", "zz-7788"])
    assert rc == 2
    assert "başka bir sunucuya ait" in capsys.readouterr().err


# ------------------------------------------------------------ OS-dependent

@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="needs real symlinks (the exe link) in a fake /proc")
def test_a_real_exe_symlink_is_followed_and_deleted_suffix_stripped(tmp_path):
    root = tmp_path / "proc"
    add_proc(root, 4101, ["ignored", "-Port=7787"], comm="bash")
    os.symlink("/usr/bin/bash", root / "4101" / "exe")
    add_proc(root, 4202, ["ignored", "-Port=7787"])
    os.symlink(BIN + " (deleted)", root / "4202" / "exe")

    got = discovery.discover_game_process(proc_root=root, game_port=7787)

    assert got.pid == 4202 and got.exe == BIN


@pytest.mark.skipif(sys.platform.startswith("win"),
                    reason="reads the real /proc of this test process")
def test_the_real_proc_of_this_process_is_readable_and_is_not_a_game():
    me = os.getpid()
    fs = ProcFS("/proc")
    if not fs.exists():
        pytest.skip("no /proc on this platform")
    start = discovery.parse_stat_start_time(fs.read_bytes(me, "stat"))
    assert start > 0
    with pytest.raises(GameProcessNotFound):
        discovery.discover_game_process(exe_names=("definitely-not-running-xyz",))
