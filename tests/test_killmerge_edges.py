"""Edge cases for DamageEventMerger and the log->merge pipeline."""
import copy

from sqreader import cli
from sqreader.squad.killmerge import DamageEventMerger
from sqreader.squad.logtail import DamageLogParser, resolve_event_names
from tests.test_killmerge import PLAYERS, log_ev, mem_ev, run
from tests.test_logtail import actual, die, wound


def test_exactly_three_ticks_matches_four_does_not():
    m = DamageEventMerger()
    run(m, [], [mem_ev()], 10)
    assert run(m, [log_ev()], [], 13)[0]["source"] == "log+memory"
    m = DamageEventMerger()
    run(m, [], [mem_ev()], 10)
    assert run(m, [log_ev()], [], 14)[0]["source"] == "log"


def test_window_sec_boundary_and_exceeded():
    m = DamageEventMerger()
    run(m, [], [mem_ev()], 10, now=0.0)
    assert run(m, [log_ev()], [], 11, now=6.0)[0]["source"] == "log+memory"
    m = DamageEventMerger()
    run(m, [], [mem_ev()], 10, now=0.0)
    assert run(m, [log_ev()], [], 11, now=6.5)[0]["source"] == "log"


def test_custom_window_ticks():
    m = DamageEventMerger(window_ticks=1)
    run(m, [], [mem_ev()], 10)
    assert run(m, [log_ev()], [], 12)[0]["source"] == "log"


def test_same_tick_two_victims_each_gets_own_fields():
    m = DamageEventMerger()
    mems = [mem_ev("Bob", "E-BOB", bone="head"), mem_ev("Al", "E-AL", bone="leg")]
    logs = [log_ev("Al", attacker="Bob"), log_ev("Bob", attacker="Al")]
    out = run(m, logs, mems, 5)
    assert [o["bone"] for o in out] == ["leg", "head"]
    assert [o["victimEosId"] for o in out] == ["E-AL", "E-BOB"]


def test_memory_event_not_copied_to_two_log_events():
    m = DamageEventMerger()
    out = run(m, [log_ev(), log_ev()], [mem_ev()], 5)
    assert sorted(o["source"] for o in out) == ["log", "log+memory"]
    # the plain-log copy must not carry rich fields
    plain = [o for o in out if o["source"] == "log"][0]
    assert plain.get("bone") is None and plain.get("damage") is None


def test_killed_and_wounded_both_true_is_treated_as_killed():
    m = DamageEventMerger()
    mem = mem_ev(killed=True, wounded=True)
    out = run(m, [log_ev(killed=False)], [mem], 1)   # wounded log
    assert out[0]["source"] == "log"
    m = DamageEventMerger()
    out = run(m, [log_ev(killed=True)], [mem], 1)
    assert out[0]["source"] == "log+memory"


def test_event_with_neither_flag_never_merges_and_follows_health():
    mem = mem_ev(killed=False, wounded=False)
    assert run(DamageEventMerger(), [log_ev()], [mem], 1)[0]["source"] == "log"
    out = run(DamageEventMerger(), [], [mem], 1, healthy=False)
    assert [o["source"] for o in out] == ["memory"]


def test_victim_not_in_roster_matches_by_name():
    m = DamageEventMerger()
    mem = mem_ev(victim="Ghost", eos="E-GHOST")
    out = run(m, [log_ev(victim="Ghost")], [mem], 2)
    assert out[0]["source"] == "log+memory"
    assert out[0]["victimEosId"] == "E-GHOST"


def test_victim_not_in_roster_different_name_no_match():
    m = DamageEventMerger()
    out = run(m, [log_ev(victim="Other")], [mem_ev(victim="Ghost", eos="E-G")], 2)
    assert out[0]["source"] == "log"


def test_log_without_victim_never_matches():
    m = DamageEventMerger()
    out = run(m, [log_ev(victim=None)], [mem_ev(victim=None, eos=None)], 2)
    assert out[0]["source"] == "log"


def test_attacker_eos_filled_from_memory_but_log_value_kept():
    out = run(DamageEventMerger(), [log_ev(attackerEosId=None)], [mem_ev()], 1)
    assert out[0]["attackerEosId"] == "E-AL"
    out = run(DamageEventMerger(), [log_ev(attackerEosId="E-LOG")], [mem_ev()], 1)
    assert out[0]["attackerEosId"] == "E-LOG"


def test_log_event_with_no_candidates_passes_through_with_source():
    out = run(DamageEventMerger(), [log_ev()], [], 1)
    assert len(out) == 1 and out[0]["source"] == "log"
    assert out[0]["ts"] == 1700000000.0


def test_merged_ts_is_log_ts_not_memory_ts():
    out = run(DamageEventMerger(), [log_ev(ts=1700000123.5)], [mem_ev(ts=7.0)], 1)
    assert out[0]["ts"] == 1700000123.5


def test_inputs_not_mutated_multi_tick_and_unhealthy():
    m = DamageEventMerger()
    mems = [mem_ev(), mem_ev("Al", "E-AL")]
    logs = [log_ev()]
    snap = copy.deepcopy((mems, logs))
    out = run(m, logs, mems, 1, healthy=False)
    assert (mems, logs) == snap
    out[0]["bone"] = "MUTATED"
    out[-1]["victimPos"].append(9)
    assert (mems, logs) == snap


def test_health_flip_healthy_to_unhealthy_unmatched_memory_appears():
    m = DamageEventMerger()
    assert run(m, [], [mem_ev()], 1, healthy=True) == []
    out = run(m, [], [mem_ev("Al", "E-AL")], 2, healthy=False)
    # only the memory event of the unhealthy tick is emitted, no replay of
    # the earlier dropped one
    assert [o["victim"] for o in out] == ["Al"]


def test_health_flip_unhealthy_then_healthy_no_double_count():
    """A death already published as 'memory' while unhealthy is not published
    again when its log event shows up after recovery."""
    m = DamageEventMerger()
    first = run(m, [], [mem_ev()], 1, healthy=False)
    second = run(m, [log_ev()], [], 2, healthy=True)
    assert [e["source"] for e in first] == ["memory"]
    assert second == []


def test_cli_midtransition_events_not_lost(capsys):
    class T:
        path = "/p"
        ok = True
        evs: list = []

        def drain(self):
            e, self.evs = self.evs, []
            return e

        def healthy(self):
            return self.ok

    t, m, st = T(), DamageEventMerger(), {}
    cli._merge_damage_events({}, PLAYERS, t, m, 0, st)
    snap = {"damageEvents": [mem_ev()]}
    t.ok = False
    cli._merge_damage_events(snap, PLAYERS, t, m, 1, st)
    assert [e["source"] for e in snap["damageEvents"]] == ["memory"]
    t.ok = True
    t.evs = [log_ev("Al", attacker="Bob")]
    snap = {"damageEvents": []}
    cli._merge_damage_events(snap, PLAYERS, t, m, 2, st)
    assert [e["source"] for e in snap["damageEvents"]] == ["log"]
    err = capsys.readouterr().err
    assert err.count("okunamıyor") == 1 and err.count("tekrar okunuyor") == 1


def test_cli_missing_damage_events_key_and_none():
    class T:
        path = "/p"

        def drain(self):
            return []

        def healthy(self):
            return True

    for snap in ({}, {"damageEvents": None}):
        cli._merge_damage_events(snap, PLAYERS, T(), DamageEventMerger(), 1, {})
        assert snap["damageEvents"] == []


def test_end_to_end_real_parser_lines_with_clan_tag():
    p = DamageLogParser()
    victim = "『GM』 Bob"
    p.feed(actual(victim, "Al", "E-AL-LOG", "BP_AK74_C_2000000001"))
    p.feed(wound(victim))
    log_events = resolve_event_names(p.drain(), PLAYERS)
    assert len(log_events) == 1
    assert log_events[0]["victim"] == "Bob" and log_events[0]["wounded"]
    out = DamageEventMerger().merge(
        log_events, [mem_ev(killed=False, bone="chest")], PLAYERS,
        tick=7, now=7.0, log_healthy=True)
    assert len(out) == 1
    e = out[0]
    assert e["source"] == "log+memory" and e["wounded"] and not e["killed"]
    assert e["bone"] == "chest" and e["victimEosId"] == "E-BOB"
    assert e["ts"] == log_events[0]["ts"] and e["ts"] > 1e9


def test_end_to_end_kill_then_unhealthy_fallback_and_stats_source():
    p = DamageLogParser()
    p.feed(actual("Bob", "Al", "E-AL", "BP_AK74_C_2000000001"))
    p.feed(die("Bob", ctrl="BP_PlayerController_C_1", eos="E-AL",
               caused="BP_AK74_C_2000000001"))
    logs = resolve_event_names(p.drain(), PLAYERS)
    killed = [e for e in logs if e["killed"]]
    assert len(killed) == 1
    m = DamageEventMerger()
    out = m.merge(killed, [mem_ev()], PLAYERS, tick=1, now=1.0, log_healthy=True)
    assert len(out) == 1 and out[0]["source"] == "log+memory"
    # same death replayed: log gone, memory re-reported while unhealthy
    out = DamageEventMerger().merge([], [mem_ev()], PLAYERS, tick=1, now=1.0,
                                    log_healthy=False)
    assert out[0]["source"] == "memory"


def test_emitted_entry_of_other_attacker_does_not_swallow_log_event():
    # scenario A: Bob's wound was published from memory while unhealthy;
    # a later hit by Carl must not be dropped against that emitted entry
    m = DamageEventMerger()
    out = run(m, [], [mem_ev(killed=False, attacker="Bob")], 1, healthy=False)
    assert [o["source"] for o in out] == ["memory"]
    out = run(m, [log_ev(killed=False, attacker="Carl")], [], 2)
    assert [(o["source"], o["attacker"]) for o in out] == [("log", "Carl")]
    out = run(m, [], [mem_ev(killed=False, attacker="Carl")], 3)
    assert out == []


def test_emitted_entry_same_attacker_is_skipped():
    m = DamageEventMerger()
    run(m, [], [mem_ev(killed=False, attacker="Bob")], 1, healthy=False)
    assert run(m, [log_ev(killed=False, attacker="bob")], [], 2) == []


def test_same_drain_two_attackers_emitted_and_fresh_memory():
    # scenario B: log(Bob) -> emitted Bob, log(Carl) -> fresh mem(Carl)
    m = DamageEventMerger()
    run(m, [], [mem_ev(killed=False, attacker="Bob")], 1, healthy=False)
    out = run(m, [log_ev(killed=False, attacker="Bob"),
                  log_ev(killed=False, attacker="Carl")],
              [mem_ev(killed=False, attacker="Carl")], 2)
    assert [(o["source"], o["attacker"]) for o in out] == [
        ("log+memory", "Carl")]


def test_oldest_candidate_preferred_among_non_emitted():
    m = DamageEventMerger()
    run(m, [], [mem_ev(bone="old")], 1)
    run(m, [], [mem_ev(bone="new")], 2)
    assert run(m, [log_ev()], [], 3)[0]["bone"] == "old"


def test_attacker_ctrl_addr_not_copied_to_suicide():
    m = DamageEventMerger()
    out = run(m, [log_ev(attacker=None, selfInflicted=True)], [mem_ev()], 1)
    assert out[0]["source"] == "log+memory"
    assert out[0].get("attackerCtrlAddr") is None
    out = run(m, [log_ev()], [mem_ev()], 2)
    assert out[0]["attackerCtrlAddr"] == 99
