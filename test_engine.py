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

# the Author gets the procedure and the campaign state; a session guide is flagged at session start
b = g.author_brief(C4)
assert "Author's procedure" in b["procedure"] and b["cast"][0]["secret"] == "She sold the map"
assert "No session guide" in g.session_start(C4)["prepared"]
g.seal(C4, "next-session", "Open on the bridge.", ["session-1"])
assert "next-session" in g.session_start(C4)["prepared"]

# the Reviewer: session records, player feedback, the log since the session began, and review status
C5 = "review"
g.upsert_entity(C5, "Vilik", "pc", {"max_hp": 40, "ac": 15})
g.session_start(C5)
g.apply_damage(C5, "Vilik", 7, "slashing", "a knife")
g.add_feedback(C5, "The harbour scene dragged.")
e = g.session_end(C5, "Vilik reached the harbour.", ["Who sent the knife?"], "Scene 1: ... Scene 2: ...")
assert "Reviewer" in e["next"]
rb = g.reviewer_brief(C5)
assert "Reviewer's procedure" in rb["procedure"] and not rb["already_reviewed"]
assert rb["session_records"][0]["record"].startswith("Scene 1") and rb["pc_damage_taken_this_session"] == 7
assert any("knife" in line for line in rb["engine_log_this_session"])
assert rb["player_feedback"][0]["text"] == "The harbour scene dragged."
assert "Not yet written" in g.finish_review(C5)["warning"]
g.seal(C5, "next-session", "Open at the customs house.")
g.seal(C5, "narrator-notes", "Make the harbourmaster bargain.")
assert g.finish_review(C5)["warning"] == "Next session is prepared."
s2 = g.session_start(C5)
assert s2["review_status"] == "Up to date." and "narrator-notes" in s2["narrator_notes"]
assert s2["player_feedback"] == ["The harbour scene dragged."]
g.session_end(C5, "More happened.", [])
assert "never reviewed" in g.session_start(C5)["review_status"]

# grounding: the primer is delivered once, then recalled; a campaign without one still orients the player
assert "No common knowledge" in g.session_start("bare")["grounding"]
C6 = "ground"
g.seal(C6, "common-knowledge", "You are Vilik, a gate trader. Hesk is your quartermaster.")
first = g.session_start(C6)
assert "where you stand" in first["grounding"] and "Hesk" in first["common_knowledge"]
g.session_end(C6, "x", [])
assert "has been given the basics" in g.session_start(C6)["grounding"]
assert "Grounding the player" in first["playbook"] and "common-knowledge" in g.author_brief(C6)["procedure"]

print("all engine checks passed")
