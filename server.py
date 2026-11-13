"""DM engine server. Exposes engine.py to Claude as tools over HTTP.

Environment variables:
  DM_KEY    required secret. Requests must send it in an x-api-key header (or "Authorization: Bearer <key>").
  PUBLIC_URL  optional. The server's own address, used in book-upload links. On Railway it is found by itself.
  DATA_DIR  where the game database lives (mount a Railway volume here, e.g. /data).
  PORT      set by the host.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from urllib.parse import unquote

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server import CacheHint
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

import engine

DM_KEY = os.environ.get("DM_KEY", "")

INSTRUCTIONS = """You are the Narrator for a solo D&D 5e game. This engine is the referee.
- Call session_start first, every session. Follow the playbook and the danger_directive it returns.
- Before the player rolls a skill or ability check, commit the difficulty and stakes with set_check, then
  resolve it with resolve_check.
- Before voicing a named character, load their card with get_card. Create one with save_card if they
  matter and have none. Record changes of heart with adjust_attitude.
- The player rolls their own dice. Ask for the roll, then pass the numbers to the engine.
- Everything else is rolled here: creature_attack, saving_throw, roll. Report results exactly as returned.
- The character sheet is on the engine. Look up bonuses, spells and features with get_character; do not
  ask the player for them. Spend limited-use features with use_resource.
- When anyone casts a concentration spell, call concentrate; the engine then asks for the save on damage.
- Record every condition with set_condition, with how it ends (rounds, or a repeated save). The engine
  then applies advantage and disadvantage to the attacks it rolls, counts the rounds down and tells you,
  at the start of the player's turn, how conditions change their rolls. Say that before they roll.
- Companions are yours to play. On their turn they act from their own card without being asked. At 0 hit
  points they are dying, not dead: the engine rolls their death saves and they can be saved or lost.
- Memory: as each scene ends, record what it established with journal_note (one call, several notes):
  names you invented, promises, debts, what people saw. Look things up with journal_search instead of
  trusting your recollection of earlier sessions. A character's card shows what they remember.
- When a roll fails with no obvious cost, or someone new walks in, ask the engine: surprise gives
  complications, people, first reactions and names from this campaign's own list. Do not invent a name
  when the list can give one.
- Rules: when you are not sure what a spell, condition or feature does, call rules_lookup. For areas of
  effect call area. The engine applies cover to attacks from the map.
- Moving between mapped places uses travel, not advance_time.
- Groups have cards too (kind "faction"). Their attitude is where their members start.
- The player's characters live in a library, apart from any campaign. A new campaign takes a rested copy.
  To add a character, save the whole sheet with library_save. Changes made in play stay in the campaign;
  the library copy is updated only when the player says so (library_save with from_campaign).
- If session_start lists books on the lore shelf, they are the setting's canon. Use lore_search before
  describing a place, person, creature or piece of history they might cover. Tell it in your own words,
  as what the character knows; never paste passages, and keep back what the character has not learned.
- Never track hit points, slots or initiative yourself. The engine's numbers are the truth.
- Time passing lets villains act. Call advance_time or rest whenever hours pass, and show the consequences.
- In every fight, call set_battle_map when combat starts and move_token whenever anyone moves. Squares
  are named chess-style: column letter, then row number (x=0,y=0 is A1). Use only those names with the player.
- At the start of each of the player's turns: report every turn taken since their last one, a line each,
  then show the battle grid. The engine draws it: show the `svg` it returns exactly as given, using any
  visual tool you have (inline visual, widget or artifact). Print `text_grid` only if you cannot show an image.
- Anything that tests the player character is the player's roll: saves, checks, contests, escapes. Name
  the roll and wait for it. Tell the player the total of every attack roll made against them.
- If creature_attack returns pending, stop: tell the player the roll and what each available reaction
  would do and cost, and wait for their answer before going on. Then resolve_attack.
- If the player asks to start a new campaign or adventure, call campaign_setup_guide and follow it. If
  you do not have that tool yet, do it by hand: pick a short new name, save the setting with
  set_setting_notes and the character with save_character, then call author_brief. Never call
  session_start while setting up or preparing a campaign.
- If the player asks you to "run the Author" or prepare a campaign, call author_brief and follow its
  procedure. That work is sealed: your visible replies must reveal none of it.
- When the player says how the game feels or what they want changed, save it with add_feedback.
- End every session with session_end, including a detailed private record. Then offer to run the Reviewer.
  If the player asks to "run the Reviewer", call reviewer_brief and follow its procedure. That work is
  sealed too.
- Clocks and sealed entries are secret. Never reveal, quote or hint that they exist.
- If a tool returns an error, fix the call. Do not improvise the outcome instead."""

# Claude keeps a copy of a connector's tool list. Tell it, in every way the protocol offers, that the
# copy goes stale: no caching of the list, and a version that changes whenever the code does.
def _build_version() -> str:
    h = hashlib.sha256()
    for name in ("server.py", "engine.py"):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), "rb") as f:
            h.update(f.read())
    return "1." + h.hexdigest()[:10]


mcp = MCPServer("dm-engine", instructions=INSTRUCTIONS, version=_build_version(),
                cache_hints={"tools/list": CacheHint(ttl_ms=0, scope="private")})


def _run(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except engine.GameError as e:
        return {"error": str(e)}


@mcp.tool()
def session_start(campaign: str) -> dict:
    """Begin a play session. Returns the party, last session's summary, hidden clocks, sealed keys and the danger directive."""
    return _run(engine.session_start, campaign)


@mcp.tool()
def session_end(campaign: str, summary: str, open_threads: list[str], record: str = "") -> dict:
    """End a session. summary: two or three factual lines for next session's recap. open_threads: what is
    unresolved. record: a private, detailed account for the Reviewer, scene by scene: what the player
    chose, who they dealt with and how it went, what was revealed, where it dragged, what they enjoyed."""
    return _run(engine.session_end, campaign, summary, open_threads, record)


@mcp.tool()
def add_feedback(campaign: str, text: str) -> dict:
    """Save the player's own comment about how the game is going, in their words. Use whenever they say
    what they liked, disliked or want changed. It is shown to the Narrator and the Reviewer from then on."""
    return _run(engine.add_feedback, campaign, text)


@mcp.tool()
def reviewer_brief(campaign: str) -> dict:
    """REVIEWER ONLY. Start here when asked to run the Reviewer or review a session. Returns the
    procedure and everything about the last session. Never repeat any of it in a visible reply."""
    return _run(engine.reviewer_brief, campaign)


@mcp.tool()
def finish_review(campaign: str) -> dict:
    """REVIEWER ONLY. Mark the last session as reviewed once the next session is prepared."""
    return _run(engine.finish_review, campaign)


@mcp.tool()
def roll(campaign: str, expression: str, reason: str) -> dict:
    """Roll dice for the DM side (NPC checks, random tables, hidden checks). Never use for the player's own rolls."""
    return _run(engine.roll, campaign, expression, reason)


@mcp.tool()
def save_character(campaign: str, name: str, kind: str, sheet: dict) -> dict:
    """Create or update a character or creature in ONE campaign. A player character the player wants
    for other games goes in the library as well (library_save). kind: pc, ally (fights on the player's side), npc
    (bystander or undecided) or creature (enemy). The kind sets the token colour on the battle grid.
    sheet fields: max_hp, ac, init (bonus), saves {str,dex,con,int,wis,cha: bonus},
    attacks [{name, to_hit, damage like '1d8+3', type, rider, count}], attacks_per_round,
    resist/immune/vulnerable [types],
    reactions: for a player character the engine recognises the common ones by name from features,
    feats and spells (Shield, Cutting Words, Lucky, Uncanny Dodge, Defensive Duelist, Deflect Attacks,
    Parry, Warding Flare, Silvery Barbs) and pauses attacks so the player can use them. Add others as
    [{name, effect: ac_bonus|roll_penalty|disadvantage|halve_damage|reduce_damage, ac_bonus or die,
    resource (name of a resource it spends), cost, uses_reaction, auto (true = use without asking)}],
    max_slots {'1': 4, ...}, resources {name: {max, recharge: 'short'|'long'}}, notes. A player
    character can also carry abilities, skills, spells, features, inventory, money and level: any
    field is stored. Fields merge over what is already stored."""
    e = _run(engine.upsert_entity, campaign, name, kind, sheet)
    if "error" in e:
        return e
    # A short receipt, not the whole sheet: get_character returns the full thing when it is needed.
    out = {"saved": e["name"], "kind": e["kind"], "hp": e["hp"], "max_hp": e["max_hp"], "ac": e["ac"],
           "fields_on_file": sorted(k for k in e if k not in ("id", "name", "kind"))}
    if e["kind"] == "pc":
        if e.get("reactions"):
            out["reactions_the_engine_will_offer"] = [r["name"] for r in e["reactions"]]
        gaps = engine.sheet_check(e)
        if gaps:
            out["sheet_is_missing"] = gaps
    return out


@mcp.tool()
def get_character(campaign: str, name: str) -> dict:
    """Full stored sheet for one character or creature, including hidden numbers. Not for showing to the player."""
    return _run(engine.get_entity, campaign, name)


@mcp.tool()
def start_combat(campaign: str, combatants: list[str], player_initiatives: dict[str, int]) -> dict:
    """Start a fight. Pass the player's reported initiative as {name: total}; the engine rolls everyone else."""
    return _run(engine.start_combat, campaign, combatants, player_initiatives)


@mcp.tool()
def set_battle_map(campaign: str, width: int, height: int, features: list[dict],
                   positions: dict[str, list[int]]) -> dict:
    """Lay out a fight on a grid of 5 ft squares (x east from 0, y south from 0; x=0,y=0 is square A1).
    features: [{name, kind: cover|hazard|wall|exit|other, x, y, w, h}]. positions: {combatant name: [x, y]}.
    Large creatures take a 2x2 block; give their top-left square and set size 'large' on their sheet."""
    return _run(engine.set_battle_map, campaign, width, height, features, positions)


@mcp.tool()
def move_token(campaign: str, name: str, x: int, y: int) -> dict:
    """Move a combatant to a square. Call it for every move, the player's and the creatures'."""
    return _run(engine.move_token, campaign, name, x, y)


@mcp.tool()
def battle_map(campaign: str) -> dict:
    """The current grid, features and token positions, for drawing the map."""
    return _run(engine.battle_map, campaign)


@mcp.tool()
def next_turn(campaign: str) -> dict:
    """Advance to the next combatant who can act."""
    return _run(engine.next_turn, campaign)


@mcp.tool()
def end_combat(campaign: str) -> dict:
    """End the fight and report who is standing."""
    return _run(engine.end_combat, campaign)


@mcp.tool()
def creature_attack(campaign: str, attacker: str, target: str, attack_name: str = "", advantage: str = "none") -> dict:
    """A non-player attacks. The engine rolls to hit and damage and applies it. advantage: none, advantage or disadvantage."""
    return _run(engine.creature_attack, campaign, attacker, target, attack_name, advantage)


@mcp.tool()
def resolve_attack(campaign: str, use_reaction: str = "", roll: int = 0) -> dict:
    """Finish an attack that creature_attack paused for the player's decision. use_reaction: the name of
    the reaction they chose, or empty to take the hit. roll: the player's die, for reactions that need
    one (Cutting Words, Deflect Attacks). If the attack still hits and another reaction could apply,
    it pauses again."""
    return _run(engine.resolve_attack, campaign, use_reaction, roll)


@mcp.tool()
def player_attack(campaign: str, attacker: str, target: str, attack_total: int, natural_roll: int,
                  damage: int, damage_type: str = "") -> dict:
    """The player attacked and reported their rolls. The engine checks the hit against hidden AC and applies damage."""
    return _run(engine.player_attack, campaign, attacker, target, attack_total, natural_roll, damage, damage_type)


@mcp.tool()
def apply_damage(campaign: str, target: str, amount: int, damage_type: str = "", source: str = "") -> dict:
    """Damage from anything other than an attack roll: spells with saves, falls, traps, hazards."""
    return _run(engine.apply_damage, campaign, target, amount, damage_type, source)


@mcp.tool()
def heal(campaign: str, target: str, amount: int) -> dict:
    """Restore hit points."""
    return _run(engine.heal, campaign, target, amount)


@mcp.tool()
def saving_throw(campaign: str, who: str, ability: str, dc: int, player_total: int | None = None,
                 advantage: str = "none", ends_condition: str = "") -> dict:
    """A saving throw. For the player character pass player_total; for anyone else the engine rolls.
    ends_condition: a condition on this creature that a success removes."""
    return _run(engine.saving_throw, campaign, who, ability, dc, player_total, advantage, ends_condition)


@mcp.tool()
def death_save(campaign: str, pc: str, natural_roll: int) -> dict:
    """Record the player's death saving throw (the number on the d20). A dying companion's saves are
    rolled by the engine on their turn; out of combat, roll one for them here with a d20 from roll."""
    return _run(engine.death_save, campaign, pc, natural_roll)


@mcp.tool()
def spend_slot(campaign: str, caster: str, level: int) -> dict:
    """Spend a spell slot. Fails if none are left, in which case the spell is not cast."""
    return _run(engine.spend_slot, campaign, caster, level)


@mcp.tool()
def use_resource(campaign: str, who: str, name: str, amount: int = 1) -> dict:
    """Spend a limited-use feature from a character's sheet, such as Bardic Inspiration, Luck Points or a
    pact slot. Fails if none are left, in which case the feature is not used."""
    return _run(engine.use_resource, campaign, who, name, amount)


@mcp.tool()
def campaign_setup_guide() -> dict:
    """Start here when the player asks to start, create or set up a NEW campaign or adventure. Returns the
    setup procedure plus the settings, characters and table rules already saved."""
    return _run(engine.campaign_setup_guide)


@mcp.tool()
def create_campaign(campaign: str, setting: str, description: str = "", tone: str = "",
                    table_rules: list[str] | None = None, character: str = "", length: str = "") -> dict:
    """Create a new campaign. A saved setting is copied by name; pass description and tone only for a new
    or invented one. Naming a saved character copies their sheet, fully rested. Empty table_rules reuses
    the player's saved rules."""
    return _run(engine.create_campaign, campaign, setting, description, tone, table_rules, character, length)


@mcp.tool()
def list_campaigns() -> dict:
    """List every campaign saved on this engine: name, setting, characters and sessions played."""
    return _run(engine.list_campaigns)


@mcp.tool()
def concentrate(campaign: str, who: str, spell: str = "") -> dict:
    """Record that a character starts concentrating on a spell, or pass an empty spell when it ends.
    While it is set, damage to them triggers the concentration save automatically."""
    return _run(engine.concentrate, campaign, who, spell)


@mcp.tool()
def encounter_check(campaign: str, enemies: list[str], allies: list[str] | None = None, pc: str = "") -> dict:
    """AUTHOR AND REVIEWER. Estimate how dangerous a planned fight is for the player character. enemies:
    creature names already saved, repeating a name for each copy. allies: companions who will fight.
    Returns a rating (trivial, light, fair, dangerous, deadly) and the numbers behind it."""
    return _run(engine.encounter_check, campaign, enemies, allies, pc)


@mcp.tool()
def list_backups() -> dict:
    """List the automatic backups of the saved games, newest first."""
    return _run(engine.list_backups)


@mcp.tool()
def restore_campaign(campaign: str, backup: str) -> dict:
    """Put ONE campaign back to how it was in a named backup. Only when the player asks for it. The
    current state is backed up first, so it can be undone."""
    return _run(engine.restore_campaign, campaign, backup)


@mcp.tool()
def archive_campaign(campaign: str, archived: bool = True) -> dict:
    """Hide a campaign from lists and setup without deleting it (archived=false brings it back). Only
    when the player asks."""
    return _run(engine.archive_campaign, campaign, archived)


@mcp.tool()
def set_condition(campaign: str, target: str, condition: str, active: bool, rounds: int = 0,
                  save_ability: str = "", save_dc: int = 0, source: str = "") -> dict:
    """Add or remove a condition such as poisoned, prone, frightened, restrained, 'exhaustion 2'.
    rounds: it ends by itself after that many of the target's turns. save_ability and save_dc: the target
    repeats that save at the end of each of its turns and a success ends it. source: who caused it.
    The engine applies the condition's effect to attacks and saves it rolls, and counts it down."""
    return _run(engine.set_condition, campaign, target, condition, active, rounds, save_ability, save_dc, source)


@mcp.tool()
def set_check(campaign: str, who: str, skill: str, dc: int, on_failure: str, on_success: str = "") -> dict:
    """Commit the difficulty and what failure costs BEFORE the player rolls a skill or ability check.
    Do not tell the player the difficulty."""
    return _run(engine.set_check, campaign, who, skill, dc, on_failure, on_success)


@mcp.tool()
def resolve_check(campaign: str, total: int) -> dict:
    """Resolve the pending check with the total the player reported. Returns the committed outcome."""
    return _run(engine.resolve_check, campaign, total)


@mcp.tool()
def save_card(campaign: str, name: str, card: dict) -> dict:
    """Create or update a character card. card fields: want, voice, secret, signature_line,
    attitude (hostile, unfriendly, wary, neutral, warm, friendly, devoted), notes."""
    c = _run(engine.save_card, campaign, name, card)
    return c if "error" in c else {"saved": c["name"], "attitude": c["attitude"]}


@mcp.tool()
def get_card(campaign: str, name: str) -> dict:
    """Load a named character's card before voicing them. Never state their secret outright."""
    return _run(engine.get_card, campaign, name)


@mcp.tool()
def adjust_attitude(campaign: str, name: str, steps: int, reason: str) -> dict:
    """Shift how a character feels about the player character, by steps up (+) or down (-), with the reason."""
    return _run(engine.adjust_attitude, campaign, name, steps, reason)


@mcp.tool()
def set_setting_notes(campaign: str, setting: str, tone: str, table_rules: list[str] | None = None,
                      description: str = "") -> dict:
    """Save this campaign's setting name, tone, house rules and a description of the world. They apply
    on top of the playbook and win where the two disagree."""
    return _run(engine.set_setting_notes, campaign, setting, tone, table_rules, description)


@mcp.tool()
def journal_note(campaign: str, notes: list[dict]) -> dict:
    """Record what play has established, so it is not forgotten or contradicted later. Call it as a scene
    ends, with every note in one call. Each note: text (one plain sentence), about (names of the people,
    places or groups concerned), kind (fact, promise, debt, threat or rumour; default fact), secret (true
    if the player character does not know it), closes (number of an open promise, debt or threat this
    settles). Record: names you invented, promises and debts either way, what someone saw the character
    do, lies told, things given or taken, deaths."""
    return _run(engine.journal_note, campaign, notes)


@mcp.tool()
def journal_search(campaign: str, query: str = "", about: str = "", player_view: bool = False,
                   limit: int = 12) -> dict:
    """Look up what play has established. about: a person, place or group. query: any words. Leave both
    empty for the most recent entries. player_view=true returns only what the player character knows:
    use it to answer 'what do I know about X?' or to give a recap."""
    return _run(engine.journal_search, campaign, query, about, player_view, limit)


@mcp.tool()
def surprise(campaign: str, kind: str, count: int = 1, about: str = "") -> dict:
    """Let the engine choose instead of using your first idea. kind: complication (when a roll fails or a
    scene is too smooth), twist (for the Author and Reviewer), person (manner and want for someone new),
    activity (what creatures are doing when found), omen, reaction (how someone first responds; about =
    their name, so their attitude counts), name (from this campaign's own name list; about = a culture
    or group). Use what comes up."""
    return _run(engine.surprise, campaign, kind, count, about)


@mcp.tool()
def area(campaign: str, shape: str, size_feet: int, origin: str, toward: str = "") -> dict:
    """Who is caught in an area of effect on the battle map. shape: sphere (size = radius), cube (side),
    cone (length) or line (length). origin: a square such as 'E6', or a token's name. toward: for a cone
    or line, the square or token it is aimed at. Friends inside are listed too."""
    return _run(engine.area, campaign, shape, size_feet, origin, toward)


@mcp.tool()
def set_travel_map(campaign: str, places: list[dict] | None = None, routes: list[dict] | None = None,
                   here: str = "") -> dict:
    """Add places and the routes between them, or reveal ones the character has discovered. places:
    [{name, note, known}]. routes: [{from, to, hours, kind (road, trail, river, sea, gate...), danger 0-3,
    note, known}]. known=false keeps it hidden from the character. here: where the character is now.
    Entries merge by name."""
    return _run(engine.set_travel_map, campaign, places, routes, here)


@mcp.tool()
def travel(campaign: str, to: str, use_hidden_routes: bool = False) -> dict:
    """Move the character to another place on the travel map by the quickest known route. Passes the
    time for each leg (so clocks advance) and rolls for trouble on dangerous legs. Use this instead of
    advance_time whenever the journey is between mapped places."""
    return _run(engine.travel, campaign, to, use_hidden_routes)


@mcp.tool()
def rules_lookup(query: str, book: str = "", limit: int = 4) -> dict:
    """The 2024 rules as written: what a spell, condition, action, feature or magic item does, or how a
    rule works. Look it up when you are not certain; do not run spells or rules from recollection.
    book narrows the search: spells, glossary, playing, equipment, magic items, classes, feats, monsters."""
    return _run(engine.rules_lookup, query, book, limit)


@mcp.tool()
def monsters(query: str = "", cr_min: float = 0, cr_max: float = 30, type: str = "", limit: int = 40) -> dict:
    """Browse the monster reference by name, challenge rating and type (beast, undead, fiend...)."""
    return _run(engine.monsters, query, cr_min, cr_max, type, limit)


@mcp.tool()
def monster_import(campaign: str, monster: str, names: list[str] | None = None, kind: str = "creature") -> dict:
    """Bring a monster from the rules reference into the campaign as a complete stat sheet the engine can
    run: hit points, armour class, saves, attacks, multiattack, traits. names: what to call each copy,
    for example ['Ghoul A', 'Ghoul B']. Rename or adjust afterwards with save_character."""
    return _run(engine.monster_import, campaign, monster, names, kind)


@mcp.tool()
def character_library(name: str = "") -> dict:
    """The player's saved characters, kept apart from any campaign. No name: list them. With a name: that
    character's full master sheet and the campaigns they are in."""
    return _run(engine.character_library, name)


@mcp.tool()
def library_save(name: str, sheet: dict | None = None, from_campaign: str = "") -> dict:
    """Add a character to the library or update the one there. Pass the full sheet (same fields as
    save_character; later calls merge), OR from_campaign to take that campaign's current version, for
    example after levelling up. Only on the player's say-so: this is what every new campaign starts from."""
    return _run(engine.library_save, name, sheet, from_campaign)


@mcp.tool()
def library_retire(name: str, retired: bool = True) -> dict:
    """Stop offering a library character for new campaigns (or bring them back). Nothing is deleted."""
    return _run(engine.library_retire, name, retired)


@mcp.tool()
def library_restore_previous(name: str) -> dict:
    """Undo the last library_save for a character."""
    return _run(engine.library_restore_previous, name)


@mcp.tool()
def lore_search(campaign: str, query: str, book: str = "", limit: int = 5) -> dict:
    """Search the books on this campaign's setting shelf (campaign may also be a setting name). query: a
    few distinctive words, such as a name or place. book: limit the search to one title. Returns
    passages for your reference: tell the player what their character would know, in your own words."""
    return _run(engine.lore_search, campaign, query, book, limit)


@mcp.tool()
def lore_shelf(campaign: str) -> dict:
    """List the books and notes on a campaign's setting shelf, with their contents where known."""
    return _run(engine.lore_shelf, campaign)


@mcp.tool()
def lore_upload_link(setting: str) -> dict:
    """Get a private one-hour link the player opens in a browser to add their own books (PDF, EPUB, Word,
    text) to a setting's shelf. Use when the player wants to play in a setting they have books for."""
    return _run(engine.lore_upload_link, setting)


@mcp.tool()
def lore_add(setting: str, title: str, text: str) -> dict:
    """Put pasted notes or a short chapter on a setting's shelf. Replaces a book with the same title."""
    return _run(engine.lore_add, setting, title, text)


@mcp.tool()
def lore_remove(setting: str, title: str) -> dict:
    """Take a book off a setting's shelf. Only when the player asks."""
    return _run(engine.lore_remove, setting, title)


@mcp.tool()
def rest(campaign: str, kind: str) -> dict:
    """Take a short (1 hour) or long (8 hours) rest. Time passes and clocks may advance."""
    return _run(engine.rest, campaign, kind)


@mcp.tool()
def advance_time(campaign: str, hours: float, reason: str) -> dict:
    """Pass in-world time for travel, waiting, research or shopping. Returns anything the villains did meanwhile."""
    return _run(engine.advance_time, campaign, hours, reason)


@mcp.tool()
def tick_clock(campaign: str, name: str, segments: int = 1, reason: str = "") -> dict:
    """Advance a hidden clock because of a failure, a delay or an enemy success."""
    return _run(engine.tick_clock, campaign, name, segments, reason)


@mcp.tool()
def open_sealed(campaign: str, key: str) -> dict:
    """Read one sealed campaign entry when the current scene needs it. Never reveal its text to the player."""
    return _run(engine.open_sealed, campaign, key)


@mcp.tool()
def author_brief(campaign: str) -> dict:
    """AUTHOR ONLY. Start here when asked to run the Author or prepare a campaign. Returns the writing
    procedure and the campaign's full state. Never repeat any of it in a visible reply."""
    return _run(engine.author_brief, campaign)


@mcp.tool()
def author_seal(campaign: str, key: str, content: str, tags: list[str] | None = None) -> dict:
    """AUTHOR ONLY (prep sessions, never during play). Store hidden campaign material."""
    return _run(engine.seal, campaign, key, content, tags)


@mcp.tool()
def author_set_clock(campaign: str, name: str, segments: int, steps: list[str],
                     hours_per_segment: float = 0, filled: int = 0) -> dict:
    """AUTHOR ONLY. Create a villain plan or threat. steps: what changes in the world as each segment fills.
    hours_per_segment > 0 makes it advance on its own as time passes."""
    c = _run(engine.set_clock, campaign, name, segments, steps, hours_per_segment, filled)
    return c if "error" in c else {"saved": c["name"], "segments": c["segments"], "filled": c["filled"]}


@mcp.tool()
def recent_log(campaign: str, limit: int = 40) -> dict:
    """Recent engine events, for recaps and for the post-session review."""
    return _run(engine.recent_log, campaign, limit)


app = mcp.streamable_http_app(
    stateless_http=True,
    json_response=True,
    host="0.0.0.0",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


MAX_UPLOAD = 300 * 1024 * 1024

UPLOAD_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Add books</title>
<style>
:root{--bg:#f6f4ee;--fg:#22211e;--mute:#6b6962;--line:#cfccc2;--card:#fff;--ok:#2f6b1f;--bad:#a23a1b;--accent:#0c5b4b}
@media (prefers-color-scheme:dark){:root{--bg:#1b1a18;--fg:#eceae3;--mute:#a3a096;--line:#45433d;--card:#252421;--ok:#9fd08a;--bad:#f0a58c;--accent:#8fd9c6}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif}
main{max-width:640px;margin:0 auto;padding:32px 16px}h1{font-size:24px;margin:0 0 4px}p{color:var(--mute);margin:0 0 16px}
#drop{border:2px dashed var(--line);border-radius:12px;background:var(--card);padding:40px 16px;text-align:center;cursor:pointer}
#drop.over{border-color:var(--accent)}#drop b{color:var(--accent)}#drop[hidden]{display:none}
ul{list-style:none;padding:0;margin:20px 0 0}li{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:0 0 8px;overflow-wrap:anywhere}
li small{display:block;color:var(--mute)}li.ok small{color:var(--ok)}li.bad small{color:var(--bad)}li small.plain{color:var(--mute)}
h2{font-size:15px;margin:28px 0 8px;color:var(--mute);font-weight:600}
</style></head><body><main>
<h1>Add books to the __SHELF__ shelf</h1>
<p id="intro">PDF, EPUB, Word, text or markdown. Each book is read into a private search index on your game
server so the DM can look things up. Scanned books are read by text recognition, which takes a few minutes
each. A book with the same file name replaces the earlier copy. You can close this page once the files
have been sent; reading carries on without it.</p>
<div id="drop" tabindex="0"><b>Choose files</b> or drop them here</div>
<input id="pick" type="file" multiple hidden accept=".pdf,.epub,.docx,.txt,.md,.markdown,.html,.htm">
<ul id="list"></ul>
<h2>Already on the shelf</h2><ul id="have">__HAVE__</ul>
</main><script>
const OPEN=__OPEN__,drop=document.getElementById('drop'),pick=document.getElementById('pick'),list=document.getElementById('list');
const rows={};let queue=Promise.resolve(),timer=null;
if(!OPEN){drop.hidden=true;document.getElementById('intro').textContent='This link no longer takes new books. Books already sent are still being read; their progress is below. Ask the DM chat for a new link to add more.';}
function row(name){const li=document.createElement('li');const t=document.createElement('span');t.textContent=name;
const s=document.createElement('small');s.textContent='Waiting to send';const n=document.createElement('small');n.className='plain';
li.append(t,s,n);list.append(li);return {li,s,n,done:false};}
function show(r,j){r.n.textContent='';
if(j.status==='failed'){r.li.className='bad';r.s.textContent=j.detail||'Could not be read.';r.done=true;return;}
if(j.status==='done'){const x=j.result||{};r.li.className=x.warning?'bad':'ok';
r.s.textContent='Added: '+x.passages+' passages'+(x.pages?' from '+x.pages+' pages':'')+(x.pages_read_by_text_recognition?' ('+x.pages_read_by_text_recognition+' read by text recognition)':'')+(x.warning?'. '+x.warning:'');
if(x.note)r.n.textContent=x.note;r.done=true;return;}
r.li.className='';r.s.textContent=j.detail||'Waiting its turn.';}
async function poll(){try{const res=await fetch(location.pathname.replace(/[/]$/,'')+'/jobs',{cache:'no-store'});if(!res.ok)return;
const d=await res.json();for(const j of d.jobs){if(!rows[j.id])rows[j.id]=row(j.filename);show(rows[j.id],j);}
if(d.jobs.every(j=>j.status==='done'||j.status==='failed')&&timer){clearInterval(timer);timer=null;}}catch(e){}}
function watch(){if(!timer)timer=setInterval(poll,2500);}
function send(file){const r=row(file.name);queue=queue.then(async()=>{r.s.textContent='Sending';
try{const res=await fetch(location.href,{method:'POST',headers:{'x-filename':encodeURIComponent(file.name)},body:file});
const j=await res.json();if(!res.ok||j.error){r.li.className='bad';r.s.textContent=j.error||('Failed ('+res.status+')');return;}
rows[j.id]=r;show(r,j);watch();
}catch(e){r.li.className='bad';r.s.textContent='Sending failed: '+e.message;}});}
function take(files){for(const f of files)send(f);}
drop.onclick=()=>pick.click();drop.onkeydown=e=>{if(e.key==='Enter'||e.key===' ')pick.click();};
pick.onchange=()=>{take(pick.files);pick.value='';};
drop.ondragover=e=>{e.preventDefault();drop.classList.add('over');};drop.ondragleave=()=>drop.classList.remove('over');
drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');take(e.dataTransfer.files);};
poll().then(()=>{if(Object.values(rows).some(r=>!r.done))watch();});
</script></body></html>"""


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def _upload(scope, receive, send):
    """The book-upload page behind a one-hour link made by lore_upload_link. The link is the permission.
    Files are saved and read in the background; the page asks /jobs how each one is getting on."""
    parts = scope["path"].strip("/").split("/")
    token = parts[1] if len(parts) > 1 else ""
    shelf = engine.upload_ticket(token)
    lapsed = None if shelf else engine.upload_ticket(token, grace=86400)   # progress stays visible for a day
    quiet = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}
    if not shelf and not lapsed:
        return await PlainTextResponse("This upload link has expired. Ask the DM chat for a new one.",
                                       status_code=404)(scope, receive, send)
    if parts[2:] == ["jobs"]:
        return await JSONResponse({"jobs": engine.book_jobs(token=token)}, headers=quiet)(scope, receive, send)
    if scope["method"] == "GET":
        have = "".join(f"<li>{_esc(b['title'])}<small>{b['passages']} passages</small></li>"
                       for b in engine.lore_shelf(shelf or lapsed)["books"]) or "<li><small>Nothing yet</small></li>"
        page = (UPLOAD_PAGE.replace("__SHELF__", _esc(shelf or lapsed)).replace("__HAVE__", have)
                .replace("__OPEN__", "true" if shelf else "false"))
        return await HTMLResponse(page, headers=quiet)(scope, receive, send)
    if scope["method"] != "POST":
        return await JSONResponse({"error": "Use the page to upload."}, status_code=405)(scope, receive, send)
    if not shelf:
        return await JSONResponse({"error": "This link no longer takes new books. Ask the DM chat for a new one."},
                                  status_code=403)(scope, receive, send)
    request = Request(scope, receive)
    filename = os.path.basename(unquote(request.headers.get("x-filename", "")).replace("\\", "/"))[:200] or "book.txt"
    ext = os.path.splitext(filename)[1].lower()
    if ext not in (".pdf", ".epub", ".docx", ".txt", ".md", ".markdown", ".html", ".htm"):
        return await JSONResponse({"error": f"'{ext or filename}' is not a kind of file the shelf can read. Use "
                                            "PDF, EPUB, Word (.docx), text, markdown or HTML."},
                                  status_code=400)(scope, receive, send)
    os.makedirs(engine.UPLOAD_DIR, exist_ok=True)
    path = os.path.join(engine.UPLOAD_DIR, secrets.token_hex(8) + ext)
    size, kept = 0, False
    try:
        with open(path, "wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD:
                    return await JSONResponse({"error": "That file is over 300 MB. Split it or send a smaller copy."},
                                              status_code=413)(scope, receive, send)
                f.write(chunk)
        if size == 0:
            return await JSONResponse({"error": "The file arrived empty."}, status_code=400)(scope, receive, send)
        job = engine.submit_book(shelf, filename, path, token)
        kept = True
        return await JSONResponse(job, headers=quiet)(scope, receive, send)
    finally:
        if not kept and os.path.exists(path):
            os.remove(path)


class KeyGate:
    """Reject every request that does not carry the secret key. '/' stays open as a health check."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        if scope["path"] in ("/", "/health"):
            return await PlainTextResponse("dm-engine ok")(scope, receive, send)
        if scope["path"].startswith("/upload/"):
            return await _upload(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        sent = (
            headers.get("x-api-key")
            or headers.get("api-key")
            or headers.get("x-auth-token")
            or headers.get("authorization", "").removeprefix("Bearer ").strip()
        ).strip()
        if not DM_KEY or not hmac.compare_digest(sent, DM_KEY):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        if scope["path"] == "/backup":  # download a fresh copy of every saved game
            name = engine.snapshot("download", 0)
            return await FileResponse(os.path.join(engine.BACKUP_DIR, name), filename=f"dm-engine-{name}",
                                      media_type="application/octet-stream")(scope, receive, send)
        return await self.inner(scope, receive, send)


application = KeyGate(app)

if __name__ == "__main__":
    try:
        resumed = engine.resume_jobs()                   # uploads that a restart interrupted
        if resumed:
            print(f"Resumed reading {resumed} uploaded book(s).")
    except Exception as ex:
        print(f"Could not resume uploads: {ex}")
    if not DM_KEY:
        raise SystemExit("Set the DM_KEY environment variable before starting the server.")
    uvicorn.run(application, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
