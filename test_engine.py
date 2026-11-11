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
mv = g.move_token(C3, "Wolf", 4, 7)
assert mv["distance_feet"] == 10 and mv["square"] == "E8"
bm = g.battle_map(C3)["map"]
assert bm["svg"].startswith("<svg") and bm["svg"].endswith("</svg>") and ">E<" in bm["svg"]
assert bm["text_grid"].splitlines()[0].split()[:3] == ["A", "B", "C"]
assert [t["square"] for t in bm["tokens"] if t["name"] == "Vilik"] == ["F8"]
assert bm["features"][0]["squares"] == "H5-I5"
import xml.dom.minidom
xml.dom.minidom.parseString(bm["svg"])
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

# lore briefs are listed at session start so the Narrator knows what is prepared
g.seal(C6, "lore-the-gates", "The gates are stone arches opened by command words.")
ls = g.session_start(C6)
assert ls["lore_briefs"] == ["lore-the-gates"] and "## Lore" in ls["playbook"]
assert "lore briefs" in g.author_brief(C6)["procedure"] and "Lore balance" in g.reviewer_brief(C6)["procedure"]

# full sheet: extra fields are kept, resources are spent and come back on the right rest
C7 = "sheet"
g.upsert_entity(C7, "Vilik", "pc", {
    "max_hp": 73, "ac": 16, "skills": {"persuasion": 11}, "max_slots": {"1": 4},
    "resources": {"Bardic Inspiration": {"max": 5, "recharge": "short"},
                  "Luck Points": {"max": 3, "recharge": "long"}}})
assert g.get_entity(C7, "Vilik")["skills"]["persuasion"] == 11
assert g.use_resource(C7, "Vilik", "bardic inspiration", 2)["left"] == 3
g.use_resource(C7, "Vilik", "Luck Points", 3)
try:
    g.use_resource(C7, "Vilik", "Luck Points")
    raise AssertionError("spent a luck point that was not there")
except g.GameError:
    pass
g.rest(C7, "short")
v = g.get_entity(C7, "Vilik")["resources"]
assert v["Bardic Inspiration"]["current"] == 5 and v["Luck Points"]["current"] == 0
g.rest(C7, "long")
assert g.get_entity(C7, "Vilik")["resources"]["Luck Points"]["current"] == 3
g.upsert_entity(C7, "Vilik", "pc", {"level": 8})
assert g.get_entity(C7, "Vilik")["resources"]["Bardic Inspiration"]["current"] == 5
assert C7 not in [c["campaign"] for c in g.list_campaigns()["campaigns"]]  # no setting yet: hidden
assert "resources" in g.session_start(C7)["party"][0]

# starting a new campaign: saved settings, characters and table rules are reused
g.set_setting_notes(C7, "Aldenshire", "Pulpy and fast.", ["I roll my own dice."], "Shards linked by gates.")
g.apply_damage(C7, "Vilik", 30)
g.use_resource(C7, "Vilik", "Luck Points", 2)
names = [c["campaign"] for c in g.list_campaigns()["campaigns"]]
assert C7 in names and "review" not in names
guide = g.campaign_setup_guide()
assert "review" in guide["existing_campaigns"]
assert "Starting a new campaign" in guide["procedure"]
assert any(x["name"] == "Aldenshire" for x in guide["saved_settings"])
assert any(x["name"] == "Vilik" for x in guide["saved_characters"])
assert guide["saved_table_rules"] == ["I roll my own dice."]
nc = g.create_campaign("Aldenshire 2", "aldenshire", character="vilik")
assert nc["campaign"] == "aldenshire-2" and nc["setting_notes"]["description"] == "Shards linked by gates."
assert nc["setting_notes"]["table_rules"] == ["I roll my own dice."]
copy = g.get_entity("aldenshire-2", "Vilik")
assert copy["hp"] == 73 and copy["resources"]["Luck Points"]["current"] == 3 and copy["skills"]["persuasion"] == 11
assert g.get_entity(C7, "Vilik")["hp"] == 43
try:
    g.create_campaign("aldenshire-2", "aldenshire")
    raise AssertionError("created a campaign over an existing one")
except g.GameError:
    pass
try:
    g.create_campaign("mystery", "Nowhere")
    raise AssertionError("created a campaign with no setting described")
except g.GameError:
    pass
inv = g.create_campaign("dunes", "The Glass Dunes", description="Invent one: desert survival horror.",
                        tone="Harsh and desperate.")
assert inv["setting_notes"]["table_rules"] == ["I roll my own dice."] and "None yet" in inv["character"]
assert "invent the world" in g.author_brief("dunes")["procedure"]

# a session opened during prep and never played is not counted, and the briefing is still owed
C8 = "stray"
g.upsert_entity(C8, "Vilik", "pc", {"max_hp": 40, "ac": 15})
assert g.session_start(C8)["session"] == 1
g.seal(C8, "common-knowledge", "You are Vilik.")
again = g.session_start(C8)
assert again["session"] == 1 and "where you stand" in again["grounding"]
g.apply_damage(C8, "Vilik", 5)
g.session_end(C8, "Played.", [])
later = g.session_start(C8)
assert later["session"] == 2 and "has been given the basics" in later["grounding"]
g.apply_damage(C8, "Vilik", 5)
assert g.session_start(C8)["session"] == 3

# an attack that Shield would turn is paused for the player; other hits land as before
C9 = "react"
g.upsert_entity(C9, "Vilik", "pc", {"max_hp": 73, "ac": 16, "reactions": [{"name": "Shield", "ac_bonus": 5}]})
g.upsert_entity(C9, "Raider", "creature", {"max_hp": 20, "ac": 12,
                "attacks": [{"name": "Bolt", "to_hit": 5, "damage": "1d6+2"}]})
seen = {"pending": 0, "miss": 0, "hit": 0}
for _ in range(300):
    r = g.creature_attack(C9, "Raider", "Vilik", "Bolt")
    assert "rolls" in r["tell_player"]
    if r.get("pending"):
        seen["pending"] += 1
        hp = g.get_entity(C9, "Vilik")["hp"]
        total = int(r["roll"].split("=")[1])
        assert 16 <= total < 21 and hp == g.get_entity(C9, "Vilik")["hp"]
        if seen["pending"] % 2:
            done = g.resolve_attack(C9, "Shield")
            assert not done["hit"] and g.get_entity(C9, "Vilik")["hp"] == hp
        else:
            done = g.creature_attack(C9, "Raider", "Vilik", "RESOLVE")
            assert done["hit"] and g.get_entity(C9, "Vilik")["hp"] < hp
    elif r["hit"]:
        seen["hit"] += 1
    else:
        seen["miss"] += 1
    g.heal(C9, "Vilik", 100)
assert seen["pending"] > 30 and seen["miss"] > 80 and seen["hit"] > 20, seen
first = g.creature_attack(C9, "Raider", "Vilik", "Bolt")
while not first.get("pending"):
    g.heal(C9, "Vilik", 100)
    first = g.creature_attack(C9, "Raider", "Vilik", "Bolt")
nxt = g.creature_attack(C9, "Raider", "Vilik", "Bolt")
assert nxt["earlier_attack_applied"]["hit"]
if nxt.get("pending"):
    g.resolve_attack(C9)
try:
    g.resolve_attack(C9)
    raise AssertionError("resolved an attack that was not pending")
except g.GameError:
    pass

# canon: a new campaign in a saved setting sees the lore written for earlier ones
g.create_campaign("canon-a", "Glasswater", description="Canals. Gates open by spoken word.", tone="Wet.")
g.seal("canon-a", "lore-the-metal", "The metal is called sunbrass.")
g.seal("canon-a", "villain", "Secret.")
g.create_campaign("canon-b", "glasswater")
est = g.author_brief("canon-b")["established_in_this_setting"]
assert [e["subject"] for e in est] == ["the-metal"] and "sunbrass" in est[0]["text"]
assert g.author_brief("canon-b")["setting_notes"]["description"].startswith("Canals")
assert "binding" in g.author_brief("canon-b")["procedure"]
assert g.author_brief(C7)["established_in_this_setting"] == []

# encounter check: weak enemies rate low, heavy ones high, and a crowd is flagged
CE = "enc"
g.upsert_entity(CE, "Vilik", "pc", {"max_hp": 73, "ac": 16, "attacks": [
    {"name": "Eldritch Blast", "to_hit": 8, "damage": "1d10+5", "count": 2}]})
g.upsert_entity(CE, "Raider", "creature", {"max_hp": 11, "ac": 13,
                "attacks": [{"name": "Bolt", "to_hit": 4, "damage": "1d6+2"}]})
g.upsert_entity(CE, "Brute", "creature", {"max_hp": 85, "ac": 15, "attacks_per_round": 2,
                "attacks": [{"name": "Maul", "to_hit": 8, "damage": "2d10+5"}]})
g.upsert_entity(CE, "Hamund", "ally", {"max_hp": 40, "ac": 16,
                "attacks": [{"name": "Crossbow", "to_hit": 6, "damage": "1d10+3"}]})
weak = g.encounter_check(CE, ["Raider", "Raider"])
assert weak["rating"] in ("trivial", "light"), weak
hard = g.encounter_check(CE, ["Brute", "Raider", "Raider"])
assert hard["rating"] in ("dangerous", "deadly") and hard["expected_hp_lost"] > weak["expected_hp_lost"], hard
eased = g.encounter_check(CE, ["Brute", "Raider", "Raider"], ["Hamund"])
assert eased["expected_hp_lost"] < hard["expected_hp_lost"]
assert "warning" in g.encounter_check(CE, ["Raider"] * 6)
assert abs(g._avg("2d6+3") - 10) < 1e-9 and abs(g._avg("2d6+3", True) - 7) < 1e-9

# concentration: damage calls for the player's save; creatures roll their own
g.concentrate(CE, "Vilik", "Hypnotic Pattern")
ev = g.apply_damage(CE, "Vilik", 30)["events"]
assert any("Constitution save, DC 15" in x for x in ev), ev
assert g.concentrate(CE, "Vilik", "")["ended"] == "Hypnotic Pattern"
assert not g.apply_damage(CE, "Vilik", 5)["events"]
g.concentrate(CE, "Brute", "Hold Person")
outcomes = set()
for _ in range(60):
    g.concentrate(CE, "Brute", "Hold Person")
    ev = g.apply_damage(CE, "Brute", 1)["events"]
    outcomes.add("loses" if any("loses" in x for x in ev) else "holds")
    g.heal(CE, "Brute", 100)
assert outcomes == {"loses", "holds"}

# backups: a snapshot restores one campaign and leaves the others alone
g.set_setting_notes(CE, "Enc", "test")
b = g.snapshot("test", 0)
assert b and g.list_backups()["backups"][0]["backup"] == b
g.apply_damage(CE, "Vilik", 20)
hp_now = g.get_entity(CE, "Vilik")["hp"]
g.apply_damage(C9, "Vilik", 7)
other = g.get_entity(C9, "Vilik")["hp"]
time_gap = __import__("time"); time_gap.sleep(1.1)
r = g.restore_campaign(CE, b)
assert g.get_entity(CE, "Vilik")["hp"] > hp_now and g.get_entity(C9, "Vilik")["hp"] == other
assert r["undo"]
for bad in ("../engine.db", "nope.db"):
    try:
        g.restore_campaign(CE, bad)
        raise AssertionError("restored from a bad backup name")
    except g.GameError:
        pass
assert g.snapshot("again") is None  # too soon after the last one

# archive hides a campaign without deleting it
assert CE in [c["campaign"] for c in g.list_campaigns()["campaigns"]]
g.archive_campaign(CE)
assert CE not in [c["campaign"] for c in g.list_campaigns()["campaigns"]]
assert g.get_entity(CE, "Vilik")["max_hp"] == 73
g.archive_campaign(CE, False)
assert CE in [c["campaign"] for c in g.list_campaigns()["campaigns"]]

print("all engine checks passed")
