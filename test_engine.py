"""Quick checks that the rules behave. Run with: python test_engine.py"""
import os
import time
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
def new_turn(c, who):
    e = g.get_entity(c, who)
    e["reaction_used"] = False
    e.pop("ac_until_turn", None)
    g._put(c, "entity", e["id"], e)


seen = {"pending": 0, "miss": 0, "hit": 0}
for _ in range(300):
    new_turn(C9, "Vilik")
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
new_turn(C9, "Vilik")
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

# reactions are recognised on any sheet, by name, and each kind does its own thing
CR = "react2"
bard = g.upsert_entity(CR, "Sella", "pc", {
    "max_hp": 200, "ac": 15, "proficiency_bonus": 3, "level": 6, "abilities": {"dex": 16},
    "features": ["Cutting Words: reaction, subtract a Bardic Inspiration die", "Lucky: 3 Luck Points",
                 {"name": "Uncanny Dodge", "text": "halve an attack's damage"}, "Shield Master: not a reaction here"],
    "spells": {"1": ["Shield (once per long rest free)", "Shield of Faith", "Healing Word"]},
    "resources": {"Bardic Inspiration (d8)": {"max": 2, "recharge": "short"}, "Luck Points": {"max": 1}}})
kinds = {r["name"]: r for r in bard["reactions"]}
assert sorted(kinds) == ["Cutting Words", "Lucky", "Shield", "Uncanny Dodge"]
assert kinds["Cutting Words"]["die"] == "1d8" and kinds["Cutting Words"]["resource"] == "Bardic Inspiration (d8)"
assert kinds["Lucky"]["uses_reaction"] is False and kinds["Shield"]["until_next_turn"]
assert "reactions" not in g.upsert_entity(CR, "Plain", "pc", {"max_hp": 10, "features": ["Second Wind"]})
g.upsert_entity(CR, "Archer", "creature", {"max_hp": 20, "ac": 12, "attacks": [
    {"name": "Bow", "to_hit": 6, "damage": "2d6+4", "reach": "range 80/320 ft"}]})


def until_pending(want=None):
    for _ in range(400):
        new_turn(CR, "Sella")
        r = g.creature_attack(CR, "Archer", "Sella", "Bow")
        if r.get("pending") and (want is None or want in [o["name"] for o in r["reactions_available"]]):
            return r
        if r.get("pending"):
            g.resolve_attack(CR)
        g.heal(CR, "Sella", 500)
    raise AssertionError(f"never offered {want}")


r = until_pending("Cutting Words")
names = [o["name"] for o in r["reactions_available"]]
assert "Uncanny Dodge" in names and "Lucky" in names and "damage_roll" not in r
total = int(r["roll"].split("=")[-1])
try:
    g.resolve_attack(CR, "Cutting Words")                                  # needs the player's die
    raise AssertionError("Cutting Words accepted without a roll")
except g.GameError:
    pass
hp = g.get_entity(CR, "Sella")["hp"]
done = g.resolve_attack(CR, "Cutting Words", roll=total - 15 + 1)          # exactly enough
assert not done["hit"] and "1 left" in done["spent"] and g.get_entity(CR, "Sella")["hp"] == hp
assert g.get_entity(CR, "Sella")["reaction_used"]
r = g.creature_attack(CR, "Archer", "Sella", "Bow")                        # same round: the reaction is gone
while not r["hit"]:
    r = g.creature_attack(CR, "Archer", "Sella", "Bow")
assert r.get("pending") and [o["name"] for o in r["reactions_available"]] == ["Lucky"]    # Lucky costs no reaction
done = g.resolve_attack(CR, "Lucky")
assert "lower die" in done["roll"] and "0 left" in done["spent"] and not done.get("pending")
g.heal(CR, "Sella", 500)
r = until_pending("Uncanny Dodge")                                         # Lucky is spent: not offered again
assert "Lucky" not in [o["name"] for o in r["reactions_available"]]
hp = g.get_entity(CR, "Sella")["hp"]
done = g.creature_attack(CR, "Archer", "Sella", "RESOLVE Uncanny Dodge")
assert done["hit"] and "halved" in done["damage_roll"]
lost = hp - g.get_entity(CR, "Sella")["hp"]
assert 3 <= lost <= 8, lost                                                # 2d6+4 is 6 to 16, halved
r = until_pending("Shield")
g.resolve_attack(CR, "Shield")
assert g.get_entity(CR, "Sella")["ac_until_turn"] == 5
for _ in range(60):                                                        # the ward holds for the rest of the round
    r = g.creature_attack(CR, "Archer", "Sella", "Bow")
    assert "warded" in r
    if r["hit"]:
        assert int(r["roll"].split("=")[-1]) >= 20 or "natural 20" in r["roll"].lower() or r["critical"] or r.get("pending") is None
    g.heal(CR, "Sella", 500)
e = g.get_entity(CR, "Sella")
for x in e["reactions"]:
    if x["name"] == "Uncanny Dodge":
        x["auto"] = True                                                   # a standing order from the player
g._put(CR, "entity", e["id"], e)
for _ in range(200):
    new_turn(CR, "Sella")
    r = g.creature_attack(CR, "Archer", "Sella", "Bow")
    if r["hit"] and not r.get("pending"):
        assert "halved" in r["damage_roll"] and "standing_order" in r
        break
    if r.get("pending"):
        g.resolve_attack(CR)
    g.heal(CR, "Sella", 500)
else:
    raise AssertionError("the standing order never fired")
g.set_condition(CR, "Sella", "stunned", True)                              # no reactions while unable to act
new_turn(CR, "Sella")
for _ in range(40):
    assert not g.creature_attack(CR, "Archer", "Sella", "Bow").get("pending")
    g.heal(CR, "Sella", 500)

# sheet check: any character saved is told what is missing
gaps = " ".join(g.sheet_check(g.get_entity(CR, "Plain")))
assert "attacks" in gaps and "saves" in gaps and "notes" in gaps and "max_slots" not in gaps
full = {"max_hp": 30, "ac": 14, "class": "Rogue", "level": 4, "proficiency_bonus": 2,
        "abilities": {a: 10 for a in ("str", "dex", "con", "int", "wis", "cha")},
        "saves": {a: 0 for a in ("str", "dex", "con", "int", "wis", "cha")}, "skills": {"stealth": 6},
        "attacks": [{"name": "Shortsword", "to_hit": 5, "damage": "1d6+3", "type": "piercing"}],
        "features": ["Sneak Attack 2d6", "Uncanny Dodge"],
        "notes": "A former customs clerk who wants her name cleared, looks after a younger brother, and owes the Guild."}
saved = g.library_save("Tess", full)
assert "sheet_is_missing" not in saved and saved["reactions_the_engine_will_offer"] == ["Uncanny Dodge"]
assert "spellcasting" in " ".join(g.library_save("Wiz", {"max_hp": 9, "spells": {"1": ["Shield"]}})["sheet_is_missing"])
for gone in ("tess", "wiz"):                                               # leave the library as later checks expect
    g._put(g.LIBRARY, "character", gone, None)
    g._put(g.LIBRARY, "previous", gone, None)

# things the rehearsal found
RH = "rehearse"
rogue = {"max_hp": 31, "ac": 15, "level": 4, "features": ["Sneak Attack: 2d6 extra damage once per turn", "Uncanny Dodge"],
         "attacks": [{"name": "Shortsword", "to_hit": 6, "damage": "1d6+4", "type": "piercing"}]}
g.library_save("Rhea", rogue)
plain = g.upsert_entity(RH, "Plainblade", "pc", {**rogue, "features": []})
copied = g.upsert_entity(RH, "Rhea", "pc", {"from_library": True})          # no second copy of the sheet needed
assert copied["attacks"][0]["name"] == "Shortsword" and copied["hp"] == 31 and "library" not in copied
assert g._dpr(copied, 12) > g._dpr(plain, 12) + 4                            # Sneak Attack counts in fight ratings
assert not any("resources" in x for x in g.sheet_check(copied))             # 'once per turn' is not a limited use
try:
    g.upsert_entity(RH, "Nobody", "pc", {"from_library": True})
    raise AssertionError("copied a character who is not in the library")
except g.GameError:
    pass
g.monster_import(RH, "Ghoul")
sc = g.start_combat(RH, ["Rhea", "Ghoul"], {"Rhea": 1})                      # the ghoul goes first and is briefed
assert sc["first_turn"]["routine"].startswith("The ghoul makes two Bite") and "set_battle_map" in sc["next"]
sc = g.start_combat(RH, ["Rhea", "Ghoul"], {"Rhea": 40})
assert sc["first_turn"]["now_acting"]["kind"] == "pc" and "map" not in sc["first_turn"]
g.set_battle_map(RH, 8, 6, [{"name": "gap in the jetty", "kind": "hazard", "x": 3, "y": 1, "w": 1, "h": 1},
                             {"name": "grain sacks", "kind": "cover", "x": 5, "y": 2, "w": 1, "h": 2}],
                 {"Rhea": [1, 1], "Ghoul": [2, 1]})
svg = g.battle_map(RH)["map"]["svg"]
assert ">grain<" in svg and ">sacks<" in svg and ">jetty<" in svg            # labels wrap on whole words
assert svg.rstrip("</svg>").rsplit("<text", 1)[1].count("1 square = 5 ft") == 1   # and the legend is still the legend
assert g._wrap_label("gap in the jetty", 7, 3) == ["gap in", "the", "jetty"] and g._wrap_label("portcullis", 6, 1) == ["portc…"]
for _ in range(60):
    r = g.creature_attack(RH, "Ghoul", "Rhea", "Bite")
    if r.get("pending"):
        done = g.resolve_attack(RH, "Uncanny Dodge")
        assert "spend" not in done and "halved" in done["damage_roll"]
        break
    g.heal(RH, "Rhea", 100)
else:
    raise AssertionError("no hit to dodge")
g.end_combat(RH)
g._put(g.LIBRARY, "character", "rhea", None)

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

# conditions: effects on the engine's own rolls, durations, repeated saves
CC = "conds"
g.upsert_entity(CC, "Vilik", "pc", {"max_hp": 60, "ac": 15})
g.upsert_entity(CC, "Tamsin", "ally", {"max_hp": 12, "ac": 13, "saves": {"wis": 2},
                                       "attacks": [{"name": "Bow", "to_hit": 5, "damage": "1d8+3", "type": "piercing"}]})
g.upsert_entity(CC, "Brute", "creature", {"max_hp": 400, "ac": 12, "saves": {"wis": -5, "con": 30},
                                           "attacks": [{"name": "Maul", "to_hit": 5, "damage": "1d4", "type": "bludgeoning"}]})
c = g.set_condition(CC, "Brute", "Poisoned", True, rounds=2)
assert c["rounds_left"] == 2 and "Disadvantage" in c["rule"]
out = g.creature_attack(CC, "Brute", "Vilik")
assert "disadvantage" in out["rolled_with"] and "Brute is poisoned" in out["rolled_with"]
g.set_condition(CC, "Vilik", "restrained", True)
out = g.creature_attack(CC, "Brute", "Vilik")
assert out["rolled_with"].startswith("a straight roll")       # advantage and disadvantage cancel
g.set_condition(CC, "Vilik", "restrained", False)
assert g.get_entity(CC, "Vilik")["conditions"] == []
c = g.set_condition(CC, "Vilik", "frightened, save wis 14", True, source="Brute")   # duration in the name
assert c["condition"] == "frightened" and "DC 14" in c["ends_on"]
assert g.set_condition(CC, "Tamsin", "blinded for 1 round", True)["rounds_left"] == 1
g.set_condition(CC, "Brute", "stunned", True, save_ability="con", save_dc=5)
try:
    g.creature_attack(CC, "Brute", "Vilik")
    raise AssertionError("a stunned creature attacked")
except g.GameError:
    pass
assert not g.saving_throw(CC, "Brute", "dex", 1)["success"]       # stunned: fails without a roll
g.start_combat(CC, ["Vilik", "Brute", "Tamsin"], {"Vilik": 30})
g.set_battle_map(CC, 8, 6, [], {"Vilik": [1, 1], "Brute": [2, 1], "Tamsin": [6, 4]})
t = g.next_turn(CC)                                                # Vilik's turn ends: asked for the save
assert any("ends_condition 'frightened'" in x for x in t["end_of_last_turn"])
assert not g.saving_throw(CC, "Vilik", "wis", 14, player_total=9, ends_condition="frightened")["success"]
r = g.saving_throw(CC, "Vilik", "wis", 14, player_total=15, ends_condition="frightened")
assert r["ended"] == "frightened" and r["target"]["conditions"] == []
order = [o["name"] for o in g._get(CC, "combat", "combat")["order"]]
seen = {}
for _ in range(2):
    seen[t["now_acting"]["name"]] = t
    t = g.next_turn(CC)
assert "loses this turn" in seen["Brute"]["instruction"] and "companion" in seen["Tamsin"]["instruction"]
assert t["now_acting"]["name"] == "Vilik" and t["round"] == 2
b = g.get_entity(CC, "Brute")
assert "stunned" not in b["conditions"]                            # +30 to the save: shaken off
assert b["condition_info"]["poisoned"]["rounds"] == 1              # one of two rounds used
assert "blinded" not in g.get_entity(CC, "Tamsin")["conditions"]   # one round, gone
g.set_condition(CC, "Brute", "prone", True)
g.set_condition(CC, "Vilik", "poisoned", True, rounds=5)
for _ in range(3):
    t = g.next_turn(CC)
notes = " ".join(t["tell_player_before_they_roll"])
assert "Vilik is poisoned" in notes and "Brute" in notes and "cancel" in notes
g.move_token(CC, "Brute", 7, 5)
out = g.player_attack(CC, "Vilik", "Brute", 20, 12, 5)
assert "disadvantage" in out["should_have_rolled_with"]            # prone and far, and poisoned

# companions go down, roll death saves on their own turns, and can be lost or saved
d = g.apply_damage(CC, "Tamsin", 12, "slashing")
assert d["target"]["status"] == "down and dying" and not g.get_entity(CC, "Tamsin").get("dead")
g.set_condition(CC, "Brute", "prone", False)
g.move_token(CC, "Brute", 6, 5)
hit = None
for _ in range(80):                                                # helpless and adjacent: any hit is a critical
    g.heal(CC, "Tamsin", 1); g.apply_damage(CC, "Tamsin", 1)
    out = g.creature_attack(CC, "Brute", "Tamsin")
    if out["hit"]:
        hit = out
        break
assert hit and hit["critical"] and "advantage" in hit["rolled_with"]
assert g.get_entity(CC, "Tamsin")["death"]["failures"] == 2        # a critical on the downed: two failures
g.heal(CC, "Tamsin", 5)
assert g.get_entity(CC, "Tamsin")["conditions"] == [] and g.get_entity(CC, "Tamsin")["death"]["failures"] == 0
g.apply_damage(CC, "Tamsin", 5)
fate = None
for _ in range(40):
    t = g.next_turn(CC)
    if t["now_acting"]["name"] == "Tamsin" and "death_save" in t:
        fate = t
    e = g.get_entity(CC, "Tamsin")
    if e.get("dead") or e.get("stable") or e["hp"] > 0:
        break
e = g.get_entity(CC, "Tamsin")
assert fate and (e.get("dead") or e.get("stable") or e["hp"] == 1)
end = g.end_combat(CC)
assert any("poisoned" in x for x in end.get("conditions_ended", []))   # round-counted conditions end with the fight
g.set_condition(CC, "Vilik", "exhaustion 3", True)
g.rest(CC, "long")
assert g.get_entity(CC, "Vilik")["conditions"] == ["exhaustion 2"]

# lore shelf: books per setting, shared by every campaign in it
g.set_setting_notes(CC, "Ravenloft: the Mists", "gothic horror")
assert g._shelf_for(CC) == "ravenloft" and g._shelf_for("Ravenloft - Barovia") == "ravenloft"
assert "No books" in g.session_start(CC)["lore_shelf"]
notes = """# Barovia
The village of Barovia lies under the castle. Its burgomaster died three days ago and lies unburied.

# The Vistani
The Vistani travel in painted wagons and can leave through the mists at will. Madam Eva reads fortunes
from the Tarokka deck at the Tser Pool encampment.
""" + "\n".join(f"Filler sentence number {i} about nothing in particular." for i in range(200))
r = g.lore_add("Ravenloft", "House Notes", notes)
assert r["shelf"] == "ravenloft" and r["passages"] >= 3
hits = g.lore_search(CC, "Who is Madam Eva and what is the Tarokka deck?")
assert hits["results"][0]["where"] == "The Vistani" and "fortunes" in hits["results"][0]["text"]
assert g.open_sealed(CC, "search: burgomaster")["results"][0]["where"] == "Barovia"
assert g.lore_search(CC, "zeppelin")["results"] == [] 
assert g.session_start(CC)["lore_shelf"]["books"] == ["House Notes"]
assert g.author_brief(CC)["lore_shelf"]["books"] == ["House Notes"]
import pathlib, zipfile
tmp = pathlib.Path(os.environ["DATA_DIR"])
(tmp / "Van_Richtens_Guide.md").write_text("# Darklords\nEach domain is the prison of its darklord, who cannot leave.")
rec = g.lore_add_file("ravenloft", "Van_Richtens_Guide.md", str(tmp / "Van_Richtens_Guide.md"))
assert rec["title"] == "Van Richtens Guide" and rec["contents"] == ["Darklords"]
with zipfile.ZipFile(tmp / "gaz.docx", "w") as z:
    z.writestr("word/document.xml", "<w:document><w:p><w:r><w:t>Krezk has an abbey of Saint Markovia.</w:t></w:r></w:p></w:document>")
g.lore_add_file("ravenloft", "gaz.docx", str(tmp / "gaz.docx"))
assert g.lore_search("ravenloft", "abbey", book="gaz")["results"][0]["book"] == "gaz"
try:
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(width=200, height=200)
    with open(tmp / "scan.pdf", "wb") as f:
        w.write(f)
    try:
        g.lore_add_file("ravenloft", "scan.pdf", str(tmp / "scan.pdf"))
        raise AssertionError("a PDF with no text was accepted")
    except g.GameError:
        pass
except ImportError:
    pass
# books are read in a background queue; a scanned page is read by text recognition where the server can
(tmp / "queued.md").write_text("# Harbour\nThe harbour chain is raised at dusk.")
job = g.submit_book("ravenloft", "Queued Notes.md", str(tmp / "queued.md"), token="tkn")
for _ in range(100):
    state = g.book_jobs(token="tkn")[0]
    if state["status"] in ("done", "failed"):
        break
    time.sleep(0.05)
assert state["status"] == "done" and state["result"]["title"] == "Queued Notes" and not (tmp / "queued.md").exists()
assert g.lore_search("ravenloft", "harbour chain")["results"][0]["book"] == "Queued Notes"
bad = g.submit_book("ravenloft", "nope.pdf", str(tmp / "missing.pdf"), token="tkn2")
for _ in range(100):
    state = g.book_jobs(token="tkn2")[0]
    if state["status"] in ("done", "failed"):
        break
    time.sleep(0.05)
assert state["status"] == "failed" and any(j["id"] == bad["id"] for j in g.lore_shelf("ravenloft")["failed_uploads"])
g.lore_remove("Ravenloft", "Queued Notes")
g._jobs.clear()
try:
    from reportlab.pdfgen import canvas as _canvas
    from PIL import Image, ImageDraw, ImageFont
    can_test_scan = g.ocr_available()
except ImportError:
    can_test_scan = False
if can_test_scan:
    img = Image.new("L", (1700, 500), 255)
    font = ImageFont.load_default(size=44)
    ImageDraw.Draw(img).text((60, 60), "The lighthouse keeper of Graben Island\nhas not been seen since the thaw.", font=font, fill=0)
    img.save(tmp / "scan.png")
    c = _canvas.Canvas(str(tmp / "Island Notes.pdf"), pagesize=(612, 180))
    c.drawImage(str(tmp / "scan.png"), 0, 0, width=612, height=180)
    c.save()
    rec = g.lore_add_file("ravenloft", "Island Notes.pdf", str(tmp / "Island Notes.pdf"))
    assert rec["pages_read_by_text_recognition"] == 1 and "scan" in rec["note"]
    assert "lighthouse" in g.lore_search("ravenloft", "Graben Island keeper")["results"][0]["text"].lower()
    g.lore_remove("Ravenloft", "Island Notes")
assert len(g.lore_shelf(CC)["books"]) == 3
g.lore_remove("Ravenloft", "gaz")
assert len(g.lore_shelf(CC)["books"]) == 2 and g.lore_search(CC, "abbey")["results"] == []
link = g.lore_upload_link(CC)
token = link["link"].rsplit("/", 1)[1]
assert link["shelf"] == "ravenloft" and g.upload_ticket(token) == "ravenloft" and g.upload_ticket("nope") is None
assert "~lore" not in g._campaign_names(include_hidden=True)


# character library: one master sheet per character, apart from every campaign
lib = g.character_library()["characters"]
assert [c["name"] for c in lib] == ["Vilik"] and lib[0]["from_campaign"] == C7    # backfilled at campaign creation
master = g.character_library("Vilik")["character"]
assert master["hp"] == 73 and master["level"] == 8 and master["resources"]["Luck Points"]["current"] == 3
g.library_save("Mira", {"max_hp": 31, "ac": 14, "class": "Rogue", "level": 4, "max_slots": {"1": 2},
                        "resources": {"Second Wind": {"max": 1}}})
assert len(g.character_library()["characters"]) == 2
assert any(x["name"] == "Mira" for x in g.campaign_setup_guide()["saved_characters"])
g.create_campaign("lib-test", "aldenshire", character="Mira")
mira = g.get_entity("lib-test", "Mira")
assert mira["kind"] == "pc" and mira["hp"] == 31 and mira["slots"] == {"1": 2} and "library" not in mira
g.apply_damage("lib-test", "Mira", 20)
g.set_condition("lib-test", "Mira", "poisoned", True)
g.upsert_entity("lib-test", "Mira", "pc", {"level": 5, "max_hp": 38})
assert g.character_library("Mira")["character"]["max_hp"] == 31                    # play does not touch the master
g.session_start("lib-test")
end = g.session_end("lib-test", "s", [])
assert "library_save" in end["character_library"] and "level 5" in end["character_library"]
g.session_start("lib-test")
assert "character_library" not in g.session_end("lib-test", "s", [])              # asked once per level
up = g.library_save("Mira", from_campaign="lib-test")
assert "now level 5 with 38" in up["changed"]
m2 = g.character_library("Mira")
assert m2["character"]["hp"] == 38 and m2["character"]["conditions"] == [] and "lib-test" in m2["in_campaigns"]
assert g.library_restore_previous("Mira")["restored"]["level"] == 4
g.library_save("Mira", {"level": 6})                                              # a partial update merges
assert g.character_library("Mira")["character"]["class"] == "Rogue"
g.apply_damage("lib-test", "Mira", 500)
try:
    g.library_save("Mira", from_campaign="lib-test")
    raise AssertionError("a dead character was copied to the library")
except g.GameError:
    pass
g.library_retire("Mira")
assert not any(x["name"] == "Mira" for x in g.campaign_setup_guide()["saved_characters"])
assert "retired" in [c for c in g.character_library()["characters"] if c["name"] == "Mira"][0]
assert "~library" not in g._campaign_names(include_hidden=True)
assert g.open_sealed(CC, 'call: character_library {}')["characters"]
assert "changed" not in g.open_sealed(C7, 'call: library_save {"name": "Vilik", "from_campaign": "this"}')

# journal: what play establishes, who it concerns, what is still owed
CJ = "journal"
g.set_setting_notes(CJ, "Testland", "test")
g.upsert_entity(CJ, "Vilik", "pc", {"max_hp": 60, "ac": 15})
g.session_start(CJ)
r = g.journal_note(CJ, [
    {"text": "The ferryman Odd Harl saw Vilik take the ledger from the customs house.", "about": ["Odd Harl", "customs house"]},
    {"text": "Vilik promised Sister Maren he would bring her brother's ring back from the marsh.", "about": ["Sister Maren"], "kind": "promise"},
    {"text": "Odd Harl is paid by the Salt Guild to report strangers.", "about": ["Odd Harl", "Salt Guild"], "secret": True},
])
assert r["saved"] == [1, 2, 3] and r["still_open"] == 1
assert [e["n"] for e in g.journal_search(CJ, about="Odd Harl")["entries"]] == [3, 1]
pv = g.journal_search(CJ, about="odd harl", player_view=True)
assert [e["n"] for e in pv["entries"]] == [1] and "does not know" in pv["instruction"]
assert g.journal_search(CJ, query="ring marsh")["entries"][0]["n"] == 2
assert g.session_start(CJ)["open_promises_debts_threats"][0]["n"] == 2
g.save_card(CJ, "Odd Harl", {"want": "to be left alone"})
assert len(g.get_card(CJ, "Odd Harl")["remembers_from_play"]) == 2          # people remember
g.journal_note(CJ, {"text": "Vilik returned the ring to Sister Maren.", "about": ["Sister Maren"], "closes": 2})
assert g.session_start(CJ)["open_promises_debts_threats"] == []
assert g.journal_search(CJ, about="Sister Maren")["entries"][1]["outcome"].startswith("Vilik returned")
for bad in ([{"text": ""}], [{"text": "x", "kind": "gossip"}], [{"text": "x", "closes": 99}]):
    try:
        g.journal_note(CJ, bad)
        raise AssertionError("bad journal note accepted")
    except g.GameError:
        pass
assert "journal" not in g.session_end(CJ, "s", [])                          # this session has entries
g.session_start(CJ)
assert "journal_note" in g.session_end(CJ, "s", [])["journal"]              # an empty session is nudged
assert len(g.reviewer_brief(CJ)["journal_this_session"]) == 0 and "journal_open_items" in g.author_brief(CJ)

# factions: a group's standing is where its members start, and moves those with no history of their own
g.save_card(CJ, "Salt Guild", {"kind": "faction", "want": "the river trade, all of it", "attitude": "wary"})
g.save_card(CJ, "Weigher Pell", {"faction": "Salt Guild", "want": "a quiet shift"})
g.save_card(CJ, "Factor Brisa", {"faction": "Salt Guild", "want": "Pell's job", "attitude": "warm"})
assert g.get_card(CJ, "Weigher Pell")["attitude"] == "wary"
assert g.get_card(CJ, "Weigher Pell")["faction_standing"]["faction"] == "Salt Guild"
g.adjust_attitude(CJ, "Factor Brisa", 1, "Vilik tipped her off about the audit.")
moved = g.adjust_attitude(CJ, "Salt Guild", -2, "Vilik burned the tally house.")
assert moved["attitude"] == "hostile" and moved["members_who_followed"] == ["Weigher Pell"]
assert g.get_card(CJ, "Weigher Pell")["attitude"] == "hostile" and g.get_card(CJ, "Factor Brisa")["attitude"] == "friendly"
start = g.session_start(CJ)
assert [f["name"] for f in start["factions"]] == ["Salt Guild"] and "Salt Guild" not in [c["name"] for c in start["cast"]]
assert len(g.get_card(CJ, "Salt Guild")["members"]) == 2

# surprise tables: no repeats until a table is used up; names come from the campaign's own list
seen = set()
for _ in range(10):
    got = g.surprise(CJ, "complication", 3)["results"]
    assert len(got) == 3 and not (set(got) & seen)
    seen |= set(got)
assert len(seen) == 30 and len(g.surprise(CJ, "complication", 2)["results"]) == 2    # table resets when empty
assert " | " in g.surprise(CJ, "person")["results"][0]
g.seal(CJ, "names", "Marsh folk:\n- Marsh: Ysolt Renn\n- Marsh: Dab Corrow\nGuild:\n- Guild: Tessaly Orn")
assert g.surprise(CJ, "name", about="guild")["names"] == ["Tessaly Orn"]
two = g.surprise(CJ, "name", 2)
assert sorted(two["names"]) == ["Dab Corrow", "Ysolt Renn"] and "note" not in two
assert "note" in g.surprise(CJ, "name")                                     # list used up: generated, and says so
for _ in range(40):
    r = g.surprise(CJ, "reaction", about="Weigher Pell")                     # hostile: -3 on 2d6
    assert not r["result"].startswith("generous")
try:
    g.surprise(CJ, "banana")
    raise AssertionError("unknown surprise kind accepted")
except g.GameError:
    pass

# areas and cover on the grid
g.upsert_entity(CJ, "Raider", "creature", {"max_hp": 500, "ac": 13, "attacks": [{"name": "Bow", "to_hit": 5, "damage": "1", "type": "piercing"}]})
g.upsert_entity(CJ, "Hound", "creature", {"max_hp": 20, "ac": 12, "attacks": [{"name": "Bite", "to_hit": 4, "damage": "1", "type": "piercing"}]})
g.start_combat(CJ, ["Vilik", "Raider", "Hound"], {"Vilik": 30})
g.set_battle_map(CJ, 12, 8, [{"kind": "cover", "label": "crates", "x": 5, "y": 2, "w": 1, "h": 2},
                              {"kind": "wall", "label": "wall", "x": 5, "y": 6, "w": 1, "h": 2},
                              {"kind": "hazard", "label": "fire", "x": 9, "y": 1}],
                 {"Vilik": [1, 2], "Raider": [9, 2], "Hound": [9, 4]})
toks = {t["name"]: t for t in g.battle_map(CJ)["map"]["tokens"]}
assert toks["Raider"]["feet_from_you"] == 40 and "half cover" in toks["Raider"]["cover_from_you"]
assert "half cover" in toks["Hound"]["cover_from_you"]                    # the crates are two squares tall
assert not g.player_attack(CJ, "Vilik", "Raider", 14, 9, 5)["hit"]          # AC 13, +2 for the crates
assert g.player_attack(CJ, "Vilik", "Raider", 15, 9, 5)["hit"]
assert "half cover" in g.creature_attack(CJ, "Raider", "Vilik")["cover"]
g.move_token(CJ, "Vilik", 4, 2)                                             # hugging the crates: they shelter Vilik, not the raider
assert "cover" not in g.player_attack(CJ, "Vilik", "Raider", 13, 9, 1)
assert "half cover" in g.creature_attack(CJ, "Raider", "Vilik")["cover"]
g.move_token(CJ, "Vilik", 1, 7); g.move_token(CJ, "Raider", 9, 7)
assert "+5 AC" in g.player_attack(CJ, "Vilik", "Raider", 14, 9, 1)["cover"]
g.move_token(CJ, "Vilik", 1, 2); g.move_token(CJ, "Raider", 9, 2)
blast = g.area(CJ, "sphere", 10, "J3")                                      # 10 ft radius on the raider
assert {x["name"] for x in blast["inside"]} == {"Raider", "Hound"} and blast["hazards_inside"] == ["J2"]
assert [x["name"] for x in g.area(CJ, "sphere", 5, "Raider")["inside"]] == ["Raider"]
cone = g.area(CJ, "cone", 60, "Vilik", "Raider")
assert {x["name"] for x in cone["inside"]} == {"Raider", "Hound"} and "Vilik" not in str(cone["inside"])
assert [x["name"] for x in g.area(CJ, "line", 60, "Vilik", "J3")["inside"]] == ["Raider"]
assert [x["name"] for x in g.area(CJ, "line", 30, "Vilik", "Raider")["inside"]] == []      # falls short
assert sorted(x["name"] for x in g.area(CJ, "cube", 15, "J4")["inside"]) == ["Hound", "Raider"]
for bad in (("sphere", 10, "Z99", ""), ("cone", 30, "Vilik", ""), ("blob", 10, "A1", "")):
    try:
        g.area(CJ, *bad)
        raise AssertionError("bad area accepted")
    except g.GameError:
        pass
g.end_combat(CJ)

# travel map: routes cost time, time moves clocks, the road can interrupt
g.set_travel_map(CJ, places=[{"name": "Shirerun", "note": "home"}, {"name": "Gatehouse"}, {"name": "Ixmar"},
                             {"name": "The Sump", "known": False}],
                 routes=[{"from": "Shirerun", "to": "Gatehouse", "hours": 2},
                         {"from": "Gatehouse", "to": "Ixmar", "hours": 0.5, "kind": "gate"},
                         {"from": "Shirerun", "to": "Ixmar", "hours": 40, "kind": "sea"},
                         {"from": "Ixmar", "to": "The Sump", "hours": 3, "danger": 3, "known": False}],
                 here="Shirerun")
g.set_clock(CJ, "Tide", 4, ["a", "b", "c", "d"], hours_per_segment=1)
w = g.session_start(CJ)["where"]
assert w["here"] == "Shirerun" and [r["to"] for r in w["known_routes"]] == ["Gatehouse", "Ixmar"]
before = g._meta(CJ)["world_hours"]
trip = g.travel(CJ, "Ixmar")
assert [x["to"] for x in trip["legs"]] == ["Gatehouse", "Ixmar"] and trip["hours"] == 2.5 and trip["arrived"]
assert abs(g._meta(CJ)["world_hours"] - before - 2.5) < 0.001 and len(trip["consequences"]) == 2   # the clock moved twice
assert "The Sump" not in [r["to"] for r in trip["onward"]]
try:
    g.travel(CJ, "The Sump")
    raise AssertionError("travelled by a route the character does not know")
except g.GameError:
    pass
g.set_travel_map(CJ, places=[{"name": "The Sump", "known": True}],
                 routes=[{"from": "Ixmar", "to": "The Sump", "hours": 3, "danger": 3, "known": True}])
events = 0
for _ in range(30):                                                         # danger 3: the road interrupts about half the time
    trip = g.travel(CJ, "The Sump")
    assert trip["arrived"]
    events += "event" in trip
    assert g.travel(CJ, "Ixmar")["now_at"] == "Ixmar"
assert 4 < events < 27, events

# rules shelf and monster import
assert g.rules_ready()
hit = g.rules_lookup("fireball")["results"][0]
assert hit["where"] == "Fireball" and "8d6" in hit["text"] and hit["book"] == "SRD Spells"
assert any("Grappl" in r["where"] for r in g.rules_lookup("how does grappling work", book="glossary")["results"])
assert g.rules_lookup("zzyzx")["results"] == [] and g.rules_ready()         # second call is a no-op
low = g.monsters(type="undead", cr_max=1)
assert low["matching"] >= 3 and all(m["cr"] <= 1 for m in low["monsters"]) and "Ghoul" in [m["name"] for m in low["monsters"]]
imp = g.monster_import(CJ, "ghoul", names=["Ghoul A", "Ghoul B"])
assert imp["as"] == ["Ghoul A", "Ghoul B"] and imp["hp"] == 22 and imp["attacks_per_round"] == 2
gh = g.get_entity(CJ, "Ghoul A")
assert gh["kind"] == "creature" and gh["ac"] == 12 and gh["immune"] == ["poison"] and gh["saves"]["dex"] == 2
bite = next(a for a in gh["attacks"] if a["name"] == "Bite")
assert bite["to_hit"] == 4 and bite["damage"] == "1d6+2+1d6" and bite["type"] == "piercing"
claw = next(a for a in gh["attacks"] if a["name"] == "Claw")
assert "Paralyzed" in claw["rider"] and "DC 10" in claw["rider"]
drag = g.monster_import(CJ, "Adult Red Dragon")
assert "Fire Breath" in drag["other_actions"] and g.get_entity(CJ, "Adult Red Dragon")["saves"]["dex"] == 6
assert g.encounter_check(CJ, ["Ghoul A", "Ghoul B"])["rating"] in ("light", "fair", "dangerous", "deadly")
try:
    g.monster_import(CJ, "dragon")
    raise AssertionError("an ambiguous monster name was accepted")
except g.GameError as ex:
    assert "Close:" in str(ex)
assert "rules" not in [c for c in g._campaign_names(include_hidden=True)]
for mm in g._all_monsters():                                                # every monster converts cleanly
    sh = g._monster_sheet(mm)
    assert sh["max_hp"] > 0 and sh["ac"] > 0
    for a in sh["attacks"]:
        g.roll_expr(a["damage"])

# newer tools are reachable through open_sealed for chats with an out-of-date tool list
assert g.open_sealed(CC, 'call: lore_search {"query": "darklord prison"}')["results"]
assert g.open_sealed(CC, "upload link")["shelf"] == "ravenloft"
assert g.open_sealed(CC, 'call: set_condition {"target": "Vilik", "condition": "prone", "active": true, "rounds": 1}')["rounds_left"] == 1
assert "rating" in g.open_sealed(CC, 'call: encounter_check {"enemies": ["Brute"]}')
for bad in ("call: restore_campaign {}", "call: encounter_check {oops}", 'call: encounter_check {"nope": 1}'):
    try:
        g.open_sealed(CC, bad)
        raise AssertionError("bad call accepted: " + bad)
    except g.GameError:
        pass
assert "open_sealed" in g.session_start(CC)["if_a_tool_is_missing"]

print("all engine checks passed")
