"""Recording retention — the only code path that deletes user data.

The rule that matters most is the in-progress guard: the match being recorded
right now must never be touched, because deleting it corrupts a live capture.
Everything else here exists so a full disk cannot take the game server down
with the agent.
"""
from __future__ import annotations

import argparse
import json
import time

import pytest

from sqreader.cli import cmd_retention

GB = 1024 ** 3


def mk(d, name, *, age_days=0.0, size=1024, state="finalized"):
    """Write a .sqrx plus its sidecar, backdated by age_days.

    The sidecar carries the recording lifecycle state, which is what retention
    reads: only ``finalized`` recordings are ever candidates."""
    p = d / f"{name}.sqrx"
    with open(p, "wb") as fh:          # sized without writing gigabytes of data
        fh.truncate(size)
    side = p.with_suffix(".meta.json")
    meta = {"id": name, "serverId": "s", "matchId": f"m-{name}",
            "recordingState": state, "inProgress": state != "finalized"}
    side.write_text(json.dumps(meta), encoding="utf-8")
    if age_days:
        when = time.time() - age_days * 86400
        for f in (p, side):
            import os
            os.utime(f, (when, when))
    return p


def run(d, **kw):
    args = argparse.Namespace(
        recordings_dir=d, max_age_days=0, max_total_gb=0, min_free_gb=0,
        in_progress_min=60, dry_run=False, min_keep=0)
    for k, v in kw.items():
        setattr(args, k, v)
    return cmd_retention(args)


def test_missing_dir_is_an_error(tmp_path):
    assert run(tmp_path / "nope") == 1


def test_nothing_to_do_still_succeeds(tmp_path):
    """The timer must stay green when no policy matches."""
    mk(tmp_path, "recent")
    assert run(tmp_path, max_age_days=90) == 0
    assert (tmp_path / "recent.sqrx").exists()


def test_in_progress_recording_is_never_deleted(tmp_path):
    """A freshly-written file is the live capture — off limits to every policy."""
    live = mk(tmp_path, "live")                       # mtime = now
    old = mk(tmp_path, "old", age_days=400)
    # Aggressive limits that would otherwise sweep everything.
    assert run(tmp_path, max_age_days=1, max_total_gb=0.0000001) == 0
    assert live.exists(), "deleted the in-flight recording"
    assert not old.exists()


def test_age_policy_removes_sidecar_too(tmp_path):
    p = mk(tmp_path, "ancient", age_days=200)
    assert run(tmp_path, max_age_days=90) == 0
    assert not p.exists()
    assert not p.with_suffix(".meta.json").exists(), "orphaned sidecar left behind"


def test_age_policy_keeps_files_inside_the_window(tmp_path):
    keep = mk(tmp_path, "keep", age_days=10)
    drop = mk(tmp_path, "drop", age_days=120)
    assert run(tmp_path, max_age_days=90) == 0
    assert keep.exists() and not drop.exists()


def test_total_size_policy_drops_oldest_first(tmp_path):
    """Oldest go first so the newest history survives."""
    a = mk(tmp_path, "a", age_days=30, size=GB // 2)
    b = mk(tmp_path, "b", age_days=20, size=GB // 2)
    c = mk(tmp_path, "c", age_days=10, size=GB // 2)
    assert run(tmp_path, max_total_gb=1.0) == 0
    assert not a.exists(), "oldest should go first"
    assert b.exists() and c.exists()


def test_dry_run_deletes_nothing(tmp_path, capsys):
    p = mk(tmp_path, "old", age_days=500)
    assert run(tmp_path, max_age_days=1, dry_run=True) == 0
    assert p.exists()
    assert "DRY RUN" in capsys.readouterr().out


def test_free_space_policy_frees_until_the_floor(tmp_path, monkeypatch):
    """Simulate a nearly-full disk: prune until min-free is satisfied."""
    import shutil

    a = mk(tmp_path, "a", age_days=30, size=GB)
    b = mk(tmp_path, "b", age_days=20, size=GB)
    keep = mk(tmp_path, "c", age_days=10, size=GB)
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: type("U", (), {"total": 100 * GB, "used": 98 * GB,
                                  "free": 2 * GB})())
    assert run(tmp_path, min_free_gb=4, min_keep=0) == 0
    assert not a.exists() and not b.exists()
    assert keep.exists(), "freed more than the floor required"


@pytest.mark.parametrize("policy", [
    {"max_age_days": 90}, {"max_total_gb": 150}, {"min_free_gb": 50},
])
def test_policies_are_independent(tmp_path, policy):
    """Each policy is opt-in; an unset one must not delete anything."""
    p = mk(tmp_path, "small", age_days=5, size=1024)
    assert run(tmp_path, **policy) == 0
    assert p.exists()


# ------------------------------------------------- unreachable free-space ----

def test_unreachable_free_space_target_deletes_nothing(tmp_path, monkeypatch,
                                                       capsys):
    """The disk is full because of something else — do not wipe the archive.

    Free space is a property of the whole filesystem, but this can only delete
    recordings. Chasing a target the recordings cannot satisfy used to delete
    every match ever recorded and still miss it.
    """
    import shutil

    keep = [mk(tmp_path, f"r{i}", age_days=30 + i, size=GB) for i in range(4)]
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: type("U", (), {"total": 900 * GB, "used": 899 * GB,
                                  "free": 1 * GB})())
    assert run(tmp_path, min_free_gb=50, min_keep=0) == 0
    assert all(p.exists() for p in keep), "wiped the archive for nothing"
    assert "cannot reach" in capsys.readouterr().err


def test_keep_floor_survives_the_free_space_policy(tmp_path, monkeypatch):
    """Even when the target IS reachable, never leave the operator with zero."""
    import shutil

    files = [mk(tmp_path, f"r{i}", age_days=40 - i, size=GB) for i in range(6)]
    monkeypatch.setattr(
        shutil, "disk_usage",
        lambda _p: type("U", (), {"total": 100 * GB, "used": 98 * GB,
                                  "free": 2 * GB})())
    assert run(tmp_path, min_free_gb=5, min_keep=3) == 0
    survivors = [p for p in files if p.exists()]
    assert len(survivors) >= 3, "keep-floor was ignored"
    # the newest are the ones kept
    assert files[-1].exists() and files[-2].exists()


# =============================================================================
# Zero Zone retention rules: finalized-only, ack-aware, emergency, crash-safe.
# The engine is sqreader.export_state.run_retention; cmd_retention is its CLI.
# =============================================================================

import logging  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from sqreader.export_state import (  # noqa: E402
    AUDIT_FILE,
    RETENTION_STATE_FILE,
    ExportState,
    RecordingGate,
    RetentionPolicy,
    read_retention_state,
    run_retention,
)

MB = 1024 * 1024


@pytest.fixture
def rec(tmp_path):
    d = tmp_path / "data" / "recordings"
    d.mkdir(parents=True)
    return d


def data_dir_of(rec):
    return rec.parent


def exists(p) -> bool:
    return p.exists() and p.with_suffix(".meta.json").exists()


def gone(p) -> bool:
    return not p.exists() and not p.with_suffix(".meta.json").exists()


def fake_disk(monkeypatch, free):
    """`free` is an int or a callable returning the free bytes right now."""
    def usage(_p):
        n = free() if callable(free) else free
        return SimpleNamespace(total=1000 * MB, used=1000 * MB - n, free=n)
    monkeypatch.setattr(shutil, "disk_usage", usage)


def audit(rec):
    p = data_dir_of(rec) / AUDIT_FILE
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x]


def ack(rec, *names):
    ExportState(data_dir_of(rec)).ack(list(names), "127.0.0.1")


# -- 7. what is never touched ---------------------------------------------------

def test_active_and_non_finalized_recordings_are_never_touched(rec, monkeypatch):
    old = 400
    active = mk(rec, "active", age_days=old, state="active")
    unverified = mk(rec, "unverified", age_days=old, state="unverified")
    legacy = mk(rec, "legacy", age_days=old)
    legacy.with_suffix(".meta.json").write_text("{}", encoding="utf-8")   # no state
    broken = mk(rec, "broken", age_days=old)
    broken.with_suffix(".meta.json").write_text("{ not json", encoding="utf-8")
    nosidecar = mk(rec, "nosidecar", age_days=old)
    nosidecar.with_suffix(".meta.json").unlink()
    notdone = mk(rec, "notdone", age_days=old)                 # finalized but inProgress
    notdone.with_suffix(".meta.json").write_text(json.dumps(
        {"recordingState": "finalized", "inProgress": True}), encoding="utf-8")
    victim = mk(rec, "victim", age_days=old)
    keep = [active, unverified, legacy, notdone]
    # every policy at once, emergency included
    fake_disk(monkeypatch, 1 * MB)
    policy = RetentionPolicy(max_age_days=1, max_total_bytes=1, min_free_bytes=50 * MB,
                             min_keep=0, require_ack=False)
    res = run_retention(rec, policy, data_dir=data_dir_of(rec))
    assert [v.rec_id for v in res.deleted] == ["victim"]
    assert gone(victim)
    for p in keep:
        assert exists(p), f"{p.name} must never be touched"
    assert broken.exists() and nosidecar.exists()              # unreadable / no sidecar


def test_recent_files_are_protected_even_when_finalized(rec):
    fresh = mk(rec, "fresh")                                    # finalized, mtime = now
    assert run_retention(rec, RetentionPolicy(max_age_days=0.0001, max_total_bytes=1),
                         data_dir=data_dir_of(rec)).deleted == []
    assert exists(fresh)


def test_unverified_go_only_on_explicit_request_and_their_own_age(rec):
    u_old = mk(rec, "u_old", age_days=300, state="unverified")
    u_mid = mk(rec, "u_mid", age_days=150, state="unverified")
    legacy = mk(rec, "legacy", age_days=300)
    legacy.with_suffix(".meta.json").write_text("{}", encoding="utf-8")
    active = mk(rec, "active", age_days=300, state="active")
    fin = mk(rec, "fin", age_days=120)                          # inside its own 200 d
    d = data_dir_of(rec)
    base = dict(max_age_days=200, unverified_max_age_days=180)
    # not asked for: nothing goes
    assert run_retention(rec, RetentionPolicy(**base), data_dir=d).deleted == []
    res = run_retention(rec, RetentionPolicy(include_unverified=True, **base), data_dir=d)
    assert sorted(v.rec_id for v in res.deleted) == ["legacy", "u_old"]
    assert {v.reason for v in res.deleted} == {"unverified_age"}
    assert gone(u_old) and gone(legacy)
    assert exists(u_mid) and exists(active) and exists(fin)     # younger / active / final


# -- 8. order: age, then quota; un-acked normally stay --------------------------

def test_unacked_recordings_are_not_deleted_normally(rec):
    a = mk(rec, "a_old_acked", age_days=200, size=MB)
    b = mk(rec, "b_old_unacked", age_days=180, size=MB)
    c = mk(rec, "c_new_acked", age_days=10, size=MB)
    ack(rec, "a_old_acked", "c_new_acked")
    policy = RetentionPolicy(max_age_days=90, require_ack=True)
    res = run_retention(rec, policy, data_dir=data_dir_of(rec))
    assert [v.rec_id for v in res.deleted] == ["a_old_acked"]    # c is young, b un-acked
    assert res.unacked_deleted == 0
    assert gone(a) and exists(b) and exists(c)
    # quota alone cannot take the un-acked one either; it takes the acked one
    res = run_retention(rec, RetentionPolicy(max_total_bytes=MB // 2, require_ack=True),
                        data_dir=data_dir_of(rec))
    assert [v.rec_id for v in res.deleted] == ["c_new_acked"]
    assert exists(b) and res.notes                               # says quota is still over


def test_age_first_then_quota_oldest_first(rec):
    names = {}
    for name, age in (("u_oldest", 60), ("x1", 50), ("x2", 40), ("x3", 30), ("x4", 20),
                      ("very_old", 400)):
        names[name] = mk(rec, name, age_days=age, size=MB)
    ack(rec, "x1", "x2", "x3", "x4", "very_old")               # u_oldest never acked
    policy = RetentionPolicy(max_age_days=90, max_total_bytes=3 * MB, require_ack=True)
    res = run_retention(rec, policy, data_dir=data_dir_of(rec))
    # 6 MB on disk: `very_old` goes by age (5 MB left), then quota (limit 3 MB)
    # removes the oldest ACKED ones: x1, x2 -> 3 MB. u_oldest is skipped.
    assert [(v.rec_id, v.reason) for v in res.deleted] == [
        ("very_old", "age"), ("x1", "quota"), ("x2", "quota")]
    assert exists(names["u_oldest"]) and exists(names["x3"]) and exists(names["x4"])
    assert res.unacked_deleted == 0
    assert [x["action"] for x in audit(rec) if x["action"] != "ack"] == ["retention_run"]


def test_without_the_export_api_finalized_recordings_need_no_ack(rec):
    a = mk(rec, "a", age_days=200)
    res = run_retention(rec, RetentionPolicy(max_age_days=90, require_ack=False),
                        data_dir=data_dir_of(rec))
    assert [v.rec_id for v in res.deleted] == ["a"] and gone(a)


def test_cli_requires_acks_when_asked_and_deletes_after_the_ack(rec, capsys):
    p = mk(rec, "one", age_days=200)
    kw = dict(recordings_dir=rec, max_age_days=90, max_total_gb=0, min_free_gb=0,
              min_keep=0, require_ack=True, state_dir=data_dir_of(rec))
    assert run(rec, **kw) == 0
    assert exists(p) and "nothing to prune" in capsys.readouterr().out
    ack(rec, "one")
    assert run(rec, **kw) == 0
    assert gone(p)
    out = capsys.readouterr().out
    assert "removing one.sqrx" in out and "yaş sınırı" in out
    # the run is recorded for /export/v1/health
    st = read_retention_state(data_dir_of(rec))
    assert st["lastDeleted"] == 1 and st["lastRunAt"].endswith("Z")


def test_require_ack_defaults_to_the_export_api_being_on():
    from sqreader.export_state import policy_from_config
    base = {"retention_max_age_days": 30, "retention_max_total_gb": 2,
            "retention_min_free_gb": 1, "retention_in_progress_min": 45,
            "retention_min_keep": 2, "retention_include_unverified": False,
            "retention_unverified_max_age_days": 90}
    off = policy_from_config({**base, "export_token_file": None}.get)
    on = policy_from_config({**base, "export_token_file": "/x/token"}.get)
    forced = policy_from_config({**base, "export_token_file": "/x/token",
                                 "retention_require_ack": False}.get)
    assert (off.require_ack, on.require_ack, forced.require_ack) == (False, True, False)
    assert (on.max_age_days, on.max_total_bytes, on.min_free_bytes) == (30, 2 * GB, GB)
    assert (on.in_progress_min, on.min_keep, on.unverified_max_age_days) == (45, 2, 90)
    junk = policy_from_config({"retention_max_age_days": "x",
                               "retention_max_total_gb": -5}.get)
    assert junk.max_age_days == 0 and junk.max_total_bytes == 0


# -- 9. emergency ---------------------------------------------------------------

def test_emergency_deletes_unacked_oldest_first_logs_and_audits(rec, monkeypatch, caplog):
    def free_now():
        # 2 MB free to start with; every 1 MB recording removed gives 1 MB back
        return 2 * MB + sum(MB for n in ("b", "c", "d") if not (rec / f"{n}.sqrx").exists())

    fake_disk(monkeypatch, free_now)
    a = mk(rec, "a", age_days=50, size=10)     # acked, tiny
    b = mk(rec, "b", age_days=40, size=MB)     # un-acked, oldest un-acked
    c = mk(rec, "c", age_days=30, size=MB)     # un-acked
    d = mk(rec, "d", age_days=20, size=MB)     # acked
    ack(rec, "a", "d")
    policy = RetentionPolicy(min_free_bytes=4 * MB, require_ack=True, min_keep=0)
    with caplog.at_level(logging.WARNING, logger="sqreader.retention"):
        res = run_retention(rec, policy, data_dir=data_dir_of(rec), free=2 * MB)
    # free 2 MB, need 4 MB: acked first (a: tiny, d: 1 MB), then the oldest un-acked
    assert res.emergency is True
    assert [v.rec_id for v in res.deleted] == ["a", "d", "b"]
    assert [v.acked for v in res.deleted] == [True, True, False]
    assert res.unacked_deleted == 1
    assert exists(c) and gone(a) and gone(b) and gone(d)
    warns = [r_ for r_ in caplog.records if r_.levelno == logging.WARNING]
    assert len(warns) == 1
    assert ": b " in warns[0].getMessage() and "onaysız" in warns[0].getMessage()
    lines = audit(rec)
    unacked = [x for x in lines if x["action"] == "retention_delete_unacked"]
    assert len(unacked) == 1 and unacked[0]["recording"] == "b"
    assert unacked[0]["reason"] == "free_space" and unacked[0]["ts"].endswith("Z")
    assert any(x["action"] == "retention_run" and x["unackedDeleted"] == 1 for x in lines)
    st = read_retention_state(data_dir_of(rec))
    assert st["lastUnackedDeleted"] == 1 and st["lastEmergency"] is True


def test_emergency_never_happens_while_free_space_is_fine(rec, monkeypatch):
    fake_disk(monkeypatch, 500 * MB)
    a = mk(rec, "unacked_old", age_days=900, size=MB)
    res = run_retention(rec, RetentionPolicy(min_free_bytes=10 * MB, require_ack=True,
                                             max_age_days=30),
                        data_dir=data_dir_of(rec))
    assert res.deleted == [] and res.emergency is False and exists(a)
    assert audit(rec) == []


def test_emergency_target_unreachable_deletes_nothing(rec, monkeypatch):
    fake_disk(monkeypatch, 1 * MB)
    files = [mk(rec, f"r{i}", age_days=30 + i, size=MB) for i in range(4)]
    res = run_retention(rec, RetentionPolicy(min_free_bytes=100 * MB, require_ack=True),
                        data_dir=data_dir_of(rec))
    assert res.deleted == [] and any("cannot reach" in n for n in res.notes)
    assert all(exists(p) for p in files)


def test_unacked_delete_needs_the_audit_line(rec, monkeypatch):
    u = mk(rec, "u", age_days=30, size=MB)
    fake_disk(monkeypatch, lambda: (9 if not u.exists() else 1) * MB)
    (data_dir_of(rec) / AUDIT_FILE).mkdir()                   # cannot be appended to
    res = run_retention(rec, RetentionPolicy(min_free_bytes=2 * MB, require_ack=True),
                        data_dir=data_dir_of(rec))
    assert res.deleted == [] and exists(u)
    assert any("denetim" in e for e in res.errors)


def test_health_reports_unacked_deletion_as_degraded(rec, monkeypatch):
    from sqreader.export_api import ExportApi
    u = mk(rec, "u", age_days=30, size=MB)
    fake_disk(monkeypatch, lambda: (9 if not u.exists() else 1) * MB)
    policy = RetentionPolicy(min_free_bytes=2 * MB, require_ack=True)
    run_retention(rec, policy, data_dir=data_dir_of(rec))
    assert gone(u)
    api = ExportApi(token="t" * 40, server_id="s", stats_db=rec.parent / "stats.db",
                    recordings_dir=rec, retention=policy)
    body = json.loads(api.handle("GET", "/export/v1/health", "Bearer " + "t" * 40).body)
    assert "unacked_deleted" in body["degradedReasons"]
    assert body["retention"]["lastDeleted"] == 1
    assert body["retention"]["lastRunAt"].endswith("Z")
    assert "t" * 40 not in json.dumps(body)


# -- 10. disk_low: no new recording, stats continue ------------------------------

def test_disk_low_blocks_new_recordings_but_stats_flow(tmp_path, monkeypatch):
    import sqlite3

    from test_export_control import MID_1, Rig
    rig = Rig(tmp_path / "live")
    try:
        state = ExportState(rig.data_dir)
        fake_disk(monkeypatch, 1 * MB)                       # below the 5 MB floor
        gate = RecordingGate(state, rig.rec_dir, RetentionPolicy(min_free_bytes=5 * MB))
        assert gate() == "disk_low"                          # nothing to clean
        rig.play("E", MID_1, 0, gate=gate)
        assert rig.sqrx_files() == []
    finally:
        rig.close()
    conn = sqlite3.connect(rig.db)
    try:
        assert conn.execute("SELECT match_id, end_state FROM matches").fetchall() == [
            (MID_1, "finalized")]
        assert conn.execute("SELECT COUNT(*) FROM player_matches").fetchone()[0] > 0
    finally:
        conn.close()
    # the operator switch is untouched by a low disk
    assert state.recording_control()["enabled"] is True


def test_low_disk_gate_cleans_first_and_records_when_that_helps(rec, monkeypatch):
    victim = mk(rec, "acked_old", age_days=100, size=MB)
    state = ExportState(data_dir_of(rec))
    state.ack(["acked_old"], None)
    fake_disk(monkeypatch, lambda: (1 if victim.exists() else 20) * MB)
    policy = RetentionPolicy(min_free_bytes=2 * MB, require_ack=True, min_keep=0)
    gate = RecordingGate(state, rec, policy, data_dir=data_dir_of(rec))
    assert gate() is None                                    # freed space -> record
    assert gone(victim)


def test_low_disk_gate_says_disk_low_when_nothing_may_be_cleaned(rec, monkeypatch):
    keep = mk(rec, "recent_unacked", age_days=1, size=MB)   # inside the in-progress guard
    fake_disk(monkeypatch, 1 * MB)
    state = ExportState(data_dir_of(rec))
    policy = RetentionPolicy(min_free_bytes=5 * MB, require_ack=True,
                             in_progress_min=60 * 24 * 7)
    gate = RecordingGate(state, rec, policy, data_dir=data_dir_of(rec))
    assert gate() == "disk_low"
    assert exists(keep)


def test_gate_failure_never_stops_recording(tmp_path):
    from test_export_control import MID_1, Rig
    rig = Rig(tmp_path / "live")
    try:
        def broken():
            raise RuntimeError("gate bug")
        rig.play("E", MID_1, 0, gate=broken)
        assert len(rig.sqrx_files()) == 1                    # fail open, like before
    finally:
        rig.close()


# -- 11. dry run ----------------------------------------------------------------

def test_dry_run_lists_what_and_why_and_changes_nothing(rec, monkeypatch, capsys):
    fake_disk(monkeypatch, 1 * MB)
    aged = mk(rec, "aged", age_days=200, size=10)
    unacked = mk(rec, "unacked", age_days=150, size=MB)
    ack(rec, "aged")
    before = sorted((p.name, p.stat().st_size, p.stat().st_mtime) for p in rec.iterdir())
    audit_before = audit(rec)
    args = dict(recordings_dir=rec, max_age_days=90, max_total_gb=0, min_free_gb=2 * MB / GB,
                min_keep=0, require_ack=True, dry_run=True, state_dir=data_dir_of(rec))
    assert run(rec, **args) == 0
    out = capsys.readouterr()
    assert "DRY RUN" in out.out
    assert "would remove aged.sqrx" in out.out and "yaş sınırı" in out.out
    assert "would remove unacked.sqrx" in out.out and "ACİL" in out.out
    assert "removing " not in out.out
    assert sorted((p.name, p.stat().st_size, p.stat().st_mtime)
                  for p in rec.iterdir()) == before
    assert exists(aged) and exists(unacked)
    assert audit(rec) == audit_before                         # dry run audits nothing
    assert not (data_dir_of(rec) / RETENTION_STATE_FILE).exists()


# -- 12. nothing outside the recordings directory, no symlinks -----------------

def test_never_follows_symlinks_or_leaves_the_directory(rec, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = mk(outside, "secret", age_days=500)              # an old, "finalized" pair
    inner = rec / "sub"
    inner.mkdir()
    nested = mk(inner, "nested", age_days=500)                # a subdirectory is not scanned
    weird = []
    for name in ("bad name", "..dotdot", "sp ace", "semi;colon"):
        try:
            weird.append(mk(rec, name, age_days=500))
        except OSError:
            pass
    linked = 0
    for link, target in (("linked.sqrx", secret),
                         ("linked.meta.json", secret.with_suffix(".meta.json"))):
        try:
            os.symlink(target, rec / link)
            linked += 1
        except (OSError, NotImplementedError):
            pass
    policy = RetentionPolicy(max_age_days=1, max_total_bytes=1, min_free_bytes=0,
                             include_unverified=True, unverified_max_age_days=1)
    res = run_retention(rec, policy, data_dir=data_dir_of(rec))
    assert res.deleted == []
    assert exists(secret) and exists(nested)
    assert all(p.exists() for p in weird)
    if linked == 2:
        assert (rec / "linked.sqrx").is_symlink() and (rec / "linked.meta.json").is_symlink()
    elif linked == 0:
        pytest.skip("symlinks cannot be created here (only the rest was verified)")


def test_symlinked_sidecar_alone_protects_the_recording(rec, tmp_path):
    """A sidecar that is a symlink (to a "finalized" one elsewhere) is not trusted."""
    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "x.meta.json"
    real.write_text(json.dumps({"recordingState": "finalized", "inProgress": False}))
    p = rec / "x.sqrx"
    p.write_bytes(b"x")
    os.utime(p, (time.time() - 500 * 86400,) * 2)
    try:
        os.symlink(real, rec / "x.meta.json")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks cannot be created here")
    res = run_retention(rec, RetentionPolicy(max_age_days=1), data_dir=data_dir_of(rec))
    assert res.deleted == [] and p.exists() and real.exists()


def test_sidecar_content_never_chooses_a_path(rec, tmp_path):
    """Fields like `filename`/`id`/`matchId` pointing elsewhere are ignored."""
    outside = tmp_path / "victim.txt"
    outside.write_text("keep me")
    p = mk(rec, "evil", age_days=300)
    p.with_suffix(".meta.json").write_text(json.dumps({
        "recordingState": "finalized", "inProgress": False,
        "id": "../../victim", "filename": str(outside), "matchId": "../victim"}))
    res = run_retention(rec, RetentionPolicy(max_age_days=1), data_dir=data_dir_of(rec))
    assert [v.rec_id for v in res.deleted] == ["evil"]
    assert gone(p) and outside.read_text() == "keep me"


# -- 6. .sqrx and sidecar go together; a half-done deletion is repaired --------

def test_pair_removal_is_marked_first_and_repaired_after_a_crash(rec, monkeypatch):
    from sqreader import export_state as es
    a = mk(rec, "half", age_days=300)
    real_unlink = es._unlink_quiet

    def crash_on_sqrx(path):
        if str(path).endswith(".sqrx"):
            raise OSError("power cut")
        real_unlink(path)

    monkeypatch.setattr(es, "_unlink_quiet", crash_on_sqrx)
    res = run_retention(rec, RetentionPolicy(max_age_days=1), data_dir=data_dir_of(rec))
    assert res.deleted == [] and res.errors
    # the pair is left in a state that says so: never "finalized" again
    meta = json.loads(a.with_suffix(".meta.json").read_text())
    assert meta["recordingState"] == "deleting"
    assert a.exists()
    monkeypatch.undo()
    # the next run completes it (even with no age limit set)
    res = run_retention(rec, RetentionPolicy(), data_dir=data_dir_of(rec))
    assert res.repaired == ["half"] and gone(a)
    # sidecar-only leftover (crash between the two unlinks)
    b = mk(rec, "left", age_days=1)
    b.unlink()
    b.with_suffix(".meta.json").write_text(json.dumps({"recordingState": "deleting"}))
    assert run_retention(rec, RetentionPolicy(),
                         data_dir=data_dir_of(rec)).repaired == ["left"]
    assert not b.with_suffix(".meta.json").exists()


def test_a_deleting_recording_is_never_exported(rec):
    """A pair in the middle of removal must not look like a finalized recording."""
    from sqreader.export_api import _SidecarIndex
    p = mk(rec, "gone_soon", age_days=1)
    assert _SidecarIndex(rec, "s").finalized("gone_soon") is not None
    meta = json.loads(p.with_suffix(".meta.json").read_text())
    meta["recordingState"] = "deleting"
    p.with_suffix(".meta.json").write_text(json.dumps(meta))
    assert _SidecarIndex(rec, "s").finalized("gone_soon") is None


def test_both_files_go_together(rec):
    p = mk(rec, "pair", age_days=200)
    assert run_retention(rec, RetentionPolicy(max_age_days=90),
                         data_dir=data_dir_of(rec)).deleted
    assert not p.exists() and not p.with_suffix(".meta.json").exists()
    assert not list(rec.glob("*.tmp"))
