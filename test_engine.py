"""Quick checks that the rules behave. Run with: python test_engine.py"""
import os
import tempfile

os.environ["DATA_DIR"] = tempfile.mkdtemp()
import engine as g  # noqa: E402

C = "test"

g.upsert_entity(C, "Vilik", "pc", {"max_hp": 40, "ac": 15, "max_slots": {"1": 2}})
g.upsert_entity(C, "Ghoul", "creature", {
    "max_hp": 22, "ac": 12, "init": 2, "saves": {"con": 0}, "immune": ["poison"],
    "attacks": [{"name": "Claws", "to_hit": 4, "damage": "2d4+2", "type": "slashing"}]})

# dice
for _ in range(200):
    r = g.roll_expr("2d6+3")
    assert 5 <= r["total"] <= 15
    assert 4 <= g.roll_expr("1d4+1d6+2")["total"] <= 12
assert 4 <= g.roll_expr("2d6", crit=True)["total"] <= 24
try:
    g.roll_expr("banana")
    raise AssertionError("bad dice accepted")
except g.GameError:
    pass

# session and danger directive
s = g.session_start(C)
assert s["session"] == 1 and len(s["party"]) == 1

# combat: engine rolls for the ghoul, player supplies their own
order = g.start_combat(C, ["Vilik", "Ghoul"], {"Vilik": 14})
assert len(order["order"]) == 2
hits = 0
for _ in range(60):
    g.heal(C, "Vilik", 100)
    out = g.creature_attack(C, "Ghoul", "Vilik", "Claws")
    if out["hit"]:
        hits += 1
        assert out["damage_dealt"] >= 4 and out["target"]["hp"] < 40
assert 10 < hits < 55, hits  # needs 11+ on the die: about half

# player attack vs hidden AC, immunity, kill
assert not g.player_attack(C, "Vilik", "Ghoul", 11, 7, 9)["hit"]
assert g.player_attack(C, "Vilik", "Ghoul", 12, 8, 9)["hit"]
assert g.apply_damage(C, "Ghoul", 30, "poison")["damage_dealt"] == 0
k = g.apply_damage(C, "Ghoul", 50, "slashing")
assert k["target"]["status"] == "dead"
try:
    g.creature_attack(C, "Ghoul", "Vilik")
    raise AssertionError("dead creature attacked")
except g.GameError:
    pass

# dropping, death saves, death
g.heal(C, "Vilik", 100)
d = g.apply_damage(C, "Vilik", 45, "necrotic")
assert d["target"]["hp"] == 0 and "unconscious" in d["target"]["conditions"]
assert g.death_save(C, "Vilik", 5)["death_saves"]["failures"] == 1
assert g.apply_damage(C, "Vilik", 3, "slashing")["events"]
final = g.death_save(C, "Vilik", 1)
assert final["pc"]["status"] == "dead"

# massive damage kills outright
g.upsert_entity(C, "Mara", "pc", {"max_hp": 10, "ac": 12})
assert g.apply_damage(C, "Mara", 25)["target"]["status"] == "dead"

# slots
g.upsert_entity(C, "Tam", "pc", {"max_hp": 20, "ac": 12, "max_slots": {"1": 1}})
g.spend_slot(C, "Tam", 1)
try:
    g.spend_slot(C, "Tam", 1)
    raise AssertionError("spent a slot that was not there")
except g.GameError:
    pass

# clocks advance with time, and a long rest costs something
g.set_clock(C, "The Ritual", 3, ["Livestock found drained", "A child goes missing", "The ritual completes"],
            hours_per_segment=6)
assert g.advance_time(C, 5, "travel")["consequences"] == []
r = g.rest(C, "long")
assert len(r["consequences"]) == 2 and "Tam" in r["restored"]
assert g.tick_clock(C, "The Ritual", 1, "failed to stop the cult")["consequences"][0]["segment"] == 3

# sealed notes
g.seal(C, "villain", "Father Aldous wants the bell silenced.", ["act1"])
assert g.sealed_index(C)["entries"][0]["key"] == "villain"
assert "Aldous" in g.open_sealed(C, "villain")["content"]

# danger directive kicks in after quiet sessions
g.session_end(C, "Test", [])
C2 = "quiet"
g.upsert_entity(C2, "Vilik", "pc", {"max_hp": 40, "ac": 15})
for _ in range(3):
    g.session_start(C2)
    g.session_end(C2, "Nothing happened.", [])
assert "must" in g.session_start(C2)["danger_directive"]

# battle map: positions are stored, moves measured, and the grid comes back on the player's turn
C3 = "map"
g.upsert_entity(C3, "Vilik", "pc", {"max_hp": 40, "ac": 15})
g.upsert_entity(C3, "Wolf", "creature", {"max_hp": 11, "ac": 13,
                "attacks": [{"name": "Bite", "to_hit": 4, "damage": "2d4+2"}]})
m = g.set_battle_map(C3, 12, 10, [{"name": "Boulder", "kind": "cover", "x": 7, "y": 4, "w": 2, "h": 1}],
                     {"Vilik": [5, 7], "Wolf": [2, 6]})
assert len(m["map"]["tokens"]) == 2
assert g.move_token(C3, "Wolf", 4, 7)["distance_feet"] == 10
try:
    g.move_token(C3, "Wolf", 40, 7)
    raise AssertionError("moved off the grid")
except g.GameError:
    pass
g.start_combat(C3, ["Vilik", "Wolf"], {"Vilik": 25})
t = g.next_turn(C3)
assert t["now_acting"]["name"] == "Wolf" and "map" not in t
t = g.next_turn(C3)
assert t["now_acting"]["name"] == "Vilik" and t["map"]["tokens"] and "draw_grid" in t

# skill checks: stakes are committed before the roll and come back unchanged
C4 = "story"
g.upsert_entity(C4, "Vilik", "pc", {"max_hp": 40, "ac": 15})
try:
    g.resolve_check(C4, 15)
    raise AssertionError("resolved a check that was never set")
except g.GameError:
    pass
try:
    g.set_check(C4, "Vilik", "Persuasion", 15, "")
    raise AssertionError("accepted a check with no cost of failure")
except g.GameError:
    pass
g.set_check(C4, "Vilik", "Persuasion", 15, "The guard calls the captain.", "The guard looks away.")
r = g.resolve_check(C4, 14)
assert not r["success"] and r["margin"] == -1 and "captain" in r["result"]
g.set_check(C4, "Vilik", "Stealth", 12, "Seen.", "Unseen.")
assert g.resolve_check(C4, 12)["success"]

# character cards: wants, secrets and attitudes that remember why they changed
g.save_card(C4, "Mother Hesk", {"want": "Her son back from the mine", "voice": "Dry, exact, never raises it",
                                 "secret": "She sold the map", "attitude": "wary"})
assert g.get_card(C4, "mother hesk")["secret"] == "She sold the map"
a = g.adjust_attitude(C4, "Mother Hesk", 2, "Vilik brought word of her son")
assert a["attitude"] == "warm" and len(a["remembers"]) == 1
assert g.adjust_attitude(C4, "Mother Hesk", -9, "Vilik lied to her")["attitude"] == "hostile"
start = g.session_start(C4)
assert start["cast"][0]["name"] == "Mother Hesk" and "Narrator's playbook" in start["playbook"]

# setting notes are per campaign and come back at session start
assert "None saved" in g.session_start("fresh")["setting_notes"]
g.set_setting_notes("fresh", "Ravenloft", "Gothic dread that builds slowly.", ["2e fear and horror checks"])
assert g.session_start("fresh")["setting_notes"]["setting"] == "Ravenloft"

print("all engine checks passed")
