from sqreader import cli
from sqreader.squad.killmerge import DamageEventMerger


class FakeTailer:
    path = "/x/SquadGame.log"

    def __init__(self, events, ok=True):
        self.events, self.ok = events, ok

    def drain(self):
        ev, self.events = self.events, []
        return ev

    def healthy(self):
        return self.ok


PLAYERS = [{"name": "Vic", "eosId": "E1"}]


def _log():
    return {"victim": "Vic", "attacker": "Bob", "killed": True,
            "causerWeapon": "AK"}


def _mem():
    return {"victim": "Vic", "victimEosId": "E1", "killed": True,
            "damage": 100.0}


def test_same_victim_single_event():
    snap = {"damageEvents": [_mem()]}
    cli._merge_damage_events(snap, PLAYERS, FakeTailer([_log()]),
                             DamageEventMerger(), 1, {})
    assert len(snap["damageEvents"]) == 1
    assert snap["damageEvents"][0]["attacker"] == "Bob"


def test_unhealthy_passes_memory():
    snap = {"damageEvents": [_mem()]}
    cli._merge_damage_events(snap, PLAYERS, FakeTailer([], ok=False),
                             DamageEventMerger(), 1, {})
    assert [e["victim"] for e in snap["damageEvents"]] == ["Vic"]
    assert snap["damageEvents"][0]["source"] == "memory"


def test_warning_once(capsys):
    t, m, st = FakeTailer([], ok=False), DamageEventMerger(), {}
    for i in range(3):
        cli._merge_damage_events({}, PLAYERS, t, m, i, st)
    err = capsys.readouterr().err
    assert err.count("okunamıyor") == 1
    t.ok = True
    for i in range(3, 6):
        cli._merge_damage_events({}, PLAYERS, t, m, i, st)
    err = capsys.readouterr().err
    assert err.count("tekrar okunuyor") == 1
