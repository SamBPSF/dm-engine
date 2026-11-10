"""DM engine server. Exposes engine.py to Claude as tools over HTTP.

Environment variables:
  DM_KEY    required secret. Requests must send it in an x-api-key header (or "Authorization: Bearer <key>").
  DATA_DIR  where the game database lives (mount a Railway volume here, e.g. /data).
  PORT      set by the host.
"""
from __future__ import annotations

import hmac
import os

import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse, PlainTextResponse

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
- Never track hit points, slots or initiative yourself. The engine's numbers are the truth.
- Time passing lets villains act. Call advance_time or rest whenever hours pass, and show the consequences.
- In every fight, call set_battle_map when combat starts and move_token whenever anyone moves.
- At the start of each of the player's turns, draw a simple battle grid from the engine's map: 5 ft squares,
  labelled shapes for cover, hazards and exits, a lettered token for each combatant, the player's token
  distinct. Use an inline visual if the app can show one, otherwise a plain text grid. Positions come from
  the engine, never from memory.
- Clocks and sealed entries are secret. Never reveal, quote or hint that they exist.
- If a tool returns an error, fix the call. Do not improvise the outcome instead."""

mcp = MCPServer("dm-engine", instructions=INSTRUCTIONS)


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
def session_end(campaign: str, summary: str, open_threads: list[str]) -> dict:
    """End a session. Save a short factual summary and the unresolved threads for next time."""
    return _run(engine.session_end, campaign, summary, open_threads)


@mcp.tool()
def roll(campaign: str, expression: str, reason: str) -> dict:
    """Roll dice for the DM side (NPC checks, random tables, hidden checks). Never use for the player's own rolls."""
    return _run(engine.roll, campaign, expression, reason)


@mcp.tool()
def save_character(campaign: str, name: str, kind: str, sheet: dict) -> dict:
    """Create or update a character or creature. kind: pc, ally, npc or creature.
    sheet fields: max_hp, ac, init (bonus), saves {str,dex,con,int,wis,cha: bonus},
    attacks [{name, to_hit, damage like '1d8+3', type, rider}], resist/immune/vulnerable [types],
    max_slots {'1': 4, ...}, notes. Fields merge over what is already stored."""
    return _run(engine.upsert_entity, campaign, name, kind, sheet)


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
    """Lay out a fight on a grid of 5 ft squares (x east from 0, y south from 0).
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
                 advantage: str = "none") -> dict:
    """A saving throw. For the player character pass player_total; for anyone else the engine rolls."""
    return _run(engine.saving_throw, campaign, who, ability, dc, player_total, advantage)


@mcp.tool()
def death_save(campaign: str, pc: str, natural_roll: int) -> dict:
    """Record the player's death saving throw (the number on the d20)."""
    return _run(engine.death_save, campaign, pc, natural_roll)


@mcp.tool()
def spend_slot(campaign: str, caster: str, level: int) -> dict:
    """Spend a spell slot. Fails if none are left, in which case the spell is not cast."""
    return _run(engine.spend_slot, campaign, caster, level)


@mcp.tool()
def set_condition(campaign: str, target: str, condition: str, active: bool) -> dict:
    """Add or remove a condition such as poisoned, prone, frightened."""
    return _run(engine.set_condition, campaign, target, condition, active)


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
    return _run(engine.save_card, campaign, name, card)


@mcp.tool()
def get_card(campaign: str, name: str) -> dict:
    """Load a named character's card before voicing them. Never state their secret outright."""
    return _run(engine.get_card, campaign, name)


@mcp.tool()
def adjust_attitude(campaign: str, name: str, steps: int, reason: str) -> dict:
    """Shift how a character feels about the player character, by steps up (+) or down (-), with the reason."""
    return _run(engine.adjust_attitude, campaign, name, steps, reason)


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
def author_seal(campaign: str, key: str, content: str, tags: list[str] | None = None) -> dict:
    """AUTHOR ONLY (prep sessions, never during play). Store hidden campaign material."""
    return _run(engine.seal, campaign, key, content, tags)


@mcp.tool()
def author_set_clock(campaign: str, name: str, segments: int, steps: list[str],
                     hours_per_segment: float = 0, filled: int = 0) -> dict:
    """AUTHOR ONLY. Create a villain plan or threat. steps: what changes in the world as each segment fills.
    hours_per_segment > 0 makes it advance on its own as time passes."""
    return _run(engine.set_clock, campaign, name, segments, steps, hours_per_segment, filled)


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


class KeyGate:
    """Reject every request that does not carry the secret key. '/' stays open as a health check."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        if scope["path"] in ("/", "/health"):
            return await PlainTextResponse("dm-engine ok")(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        sent = (
            headers.get("x-api-key")
            or headers.get("api-key")
            or headers.get("x-auth-token")
            or headers.get("authorization", "").removeprefix("Bearer ").strip()
        ).strip()
        if not DM_KEY or not hmac.compare_digest(sent, DM_KEY):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        return await self.inner(scope, receive, send)


application = KeyGate(app)

if __name__ == "__main__":
    if not DM_KEY:
        raise SystemExit("Set the DM_KEY environment variable before starting the server.")
    uvicorn.run(application, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
