"""Game state and rules for the DM engine.

Everything that has to be honest lives here: dice the DM rolls, hit points,
spell slots, combat order, villain clocks and the sealed campaign notes.
The chat model (the Narrator) calls these functions through server.py and
reports what they return. It never holds the numbers itself.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from typing import Any

DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "engine.db")

_rng = secrets.SystemRandom()


# ---------------------------------------------------------------- storage

def _db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS doc (
            campaign TEXT, kind TEXT, id TEXT, data TEXT,
            PRIMARY KEY (campaign, kind, id));
        CREATE TABLE IF NOT EXISTS log (
            campaign TEXT, ts REAL, kind TEXT, text TEXT);
        """
    )
    return con


def _get(campaign: str, kind: str, id: str, default: Any = None) -> Any:
    with _db() as con:
        row = con.execute(
            "SELECT data FROM doc WHERE campaign=? AND kind=? AND id=?",
            (campaign, kind, id),
        ).fetchone()
    return json.loads(row[0]) if row else default


def _put(campaign: str, kind: str, id: str, data: Any) -> None:
    with _db() as con:
        con.execute(
            "INSERT OR REPLACE INTO doc VALUES (?,?,?,?)",
            (campaign, kind, id, json.dumps(data)),
        )


def _all(campaign: str, kind: str) -> dict[str, Any]:
    with _db() as con:
        rows = con.execute(
            "SELECT id, data FROM doc WHERE campaign=? AND kind=?", (campaign, kind)
        ).fetchall()
    return {r[0]: json.loads(r[1]) for r in rows}


def _log(campaign: str, kind: str, text: str) -> None:
    with _db() as con:
        con.execute("INSERT INTO log VALUES (?,?,?,?)", (campaign, time.time(), kind, text))


def _meta(campaign: str) -> dict:
    return _get(campaign, "meta", "meta", {
        "session": 0, "world_hours": 0.0, "last_danger_session": 0,
        "session_damage": 0, "last_summary": "", "open_threads": [],
    })


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


class GameError(Exception):
    """A mistake in how a tool was called. The message tells the Narrator how to fix it."""


def _entity(campaign: str, id: str) -> dict:
    e = _get(campaign, "entity", _slug(id))
    if e is None:
        known = ", ".join(sorted(_all(campaign, "entity"))) or "none yet"
        raise GameError(f"No entity '{id}' in campaign '{campaign}'. Known: {known}.")
    return e


# ------------------------------------------------------------------- dice

_TERM = re.compile(r"([+-]?)\s*(?:(\d*)d(\d+)|(\d+))")


def roll_expr(expr: str, crit: bool = False) -> dict:
    """Roll something like '2d6+3' or '1d20-1'. A crit doubles the dice, not the bonus."""
    cleaned = expr.replace(" ", "").lower()
    if not cleaned or not re.fullmatch(r"([+-]?(\d*d\d+|\d+))+", cleaned):
        raise GameError(f"Can't read dice expression '{expr}'. Use forms like 1d20+5 or 2d6+3.")
    total, parts = 0, []
    for sign, n, sides, flat in _TERM.findall(cleaned):
        mult = -1 if sign == "-" else 1
        if sides:
            count = int(n or 1) * (2 if crit else 1)
            if count > 100 or int(sides) > 1000:
                raise GameError("Dice expression too large.")
            rolls = [_rng.randint(1, int(sides)) for _ in range(count)]
            total += mult * sum(rolls)
            parts.append(f"{'-' if mult < 0 else ''}{count}d{sides}{rolls}")
        else:
            total += mult * int(flat)
            parts.append(f"{'-' if mult < 0 else '+'}{flat}")
    return {"total": total, "detail": " ".join(parts)}


def _d20(advantage: str = "none") -> tuple[int, str]:
    a, b = _rng.randint(1, 20), _rng.randint(1, 20)
    if advantage == "advantage":
        return max(a, b), f"d20 {a},{b} (advantage)"
    if advantage == "disadvantage":
        return min(a, b), f"d20 {a},{b} (disadvantage)"
    return a, f"d20 {a}"


def roll(campaign: str, expr: str, reason: str) -> dict:
    r = roll_expr(expr)
    _log(campaign, "roll", f"{reason}: {expr} = {r['total']} ({r['detail']})")
    return {"reason": reason, **r}


# --------------------------------------------------------------- entities

def upsert_entity(campaign: str, name: str, kind: str, sheet: dict) -> dict:
    """Create or update a character or creature. `sheet` fields are merged over what exists."""
    if kind not in ("pc", "ally", "npc", "creature"):
        raise GameError("kind must be pc, ally, npc or creature.")
    id = _slug(name)
    e = _get(campaign, "entity", id, {})
    e.update(sheet)
    e.update({"id": id, "name": e.get("name", name), "kind": kind})
    if "max_hp" not in e:
        raise GameError("sheet needs max_hp (and ac) the first time an entity is saved.")
    e.setdefault("hp", e["max_hp"])
    e.setdefault("ac", 10)
    e.setdefault("temp_hp", 0)
    e.setdefault("conditions", [])
    e.setdefault("attacks", [])
    e.setdefault("saves", {})
    e.setdefault("init", 0)
    if "max_slots" in e:
        e.setdefault("slots", dict(e["max_slots"]))
    if kind == "pc":
        e.setdefault("death", {"successes": 0, "failures": 0})
    _put(campaign, "entity", id, e)
    return e


def get_entity(campaign: str, name: str) -> dict:
    return _entity(campaign, name)


def _status(e: dict) -> str:
    if e.get("dead"):
        return "dead"
    if e["hp"] <= 0:
        return "down and dying" if e["kind"] == "pc" else "dead"
    frac = e["hp"] / e["max_hp"]
    if frac <= 0.25:
        return "badly hurt"
    if frac <= 0.5:
        return "bloodied"
    return "hurt" if frac < 1 else "unhurt"


def _public(e: dict) -> dict:
    out = {"name": e["name"], "kind": e["kind"], "status": _status(e), "conditions": e["conditions"]}
    if e["kind"] in ("pc", "ally"):
        out.update({"hp": e["hp"], "max_hp": e["max_hp"], "temp_hp": e["temp_hp"], "ac": e["ac"]})
        if "slots" in e:
            out["slots"] = e["slots"]
    return out


def apply_damage(campaign: str, target: str, amount: int, damage_type: str = "",
                 source: str = "", crit: bool = False) -> dict:
    e = _entity(campaign, target)
    if amount < 0:
        raise GameError("Damage must be zero or more. Use heal for healing.")
    note = ""
    dt = damage_type.lower()
    if dt and dt in [x.lower() for x in e.get("immune", [])]:
        amount, note = 0, "immune"
    elif dt and dt in [x.lower() for x in e.get("resist", [])]:
        amount, note = amount // 2, "resisted (halved)"
    elif dt and dt in [x.lower() for x in e.get("vulnerable", [])]:
        amount, note = amount * 2, "vulnerable (doubled)"
    absorbed = min(e["temp_hp"], amount)
    e["temp_hp"] -= absorbed
    dealt = amount - absorbed
    was_down = e["hp"] <= 0
    before = e["hp"]
    e["hp"] = max(0, e["hp"] - dealt)
    events = []
    if e["kind"] == "pc":
        m = _meta(campaign)
        m["session_damage"] += dealt
        if e["hp"] <= e["max_hp"] / 2 and dealt > 0:
            m["last_danger_session"] = m["session"]
        _put(campaign, "meta", "meta", m)
        if was_down and dealt > 0 and not e.get("dead"):
            e["death"]["failures"] += 2 if crit else 1
            events.append(f"Damage while down: death save failure ({e['death']['failures']}/3).")
        elif e["hp"] == 0 and dealt - before >= e["max_hp"]:
            e["dead"] = True
            events.append("Massive damage: the character is killed outright.")
        elif e["hp"] == 0 and not was_down:
            e["death"] = {"successes": 0, "failures": 0}
            if "unconscious" not in e["conditions"]:
                e["conditions"].append("unconscious")
            events.append("The character drops to 0 HP: unconscious and making death saves.")
        if e["death"]["failures"] >= 3:
            e["dead"] = True
            events.append("Three failures: the character is dead.")
    elif e["hp"] == 0:
        e["dead"] = True
        events.append(f"{e['name']} is killed.")
    _put(campaign, "entity", e["id"], e)
    _log(campaign, "damage", f"{e['name']} takes {dealt} {damage_type} from {source or 'unknown'} -> {e['hp']}/{e['max_hp']}")
    return {"target": _public(e), "damage_dealt": dealt, "note": note, "events": events}


def heal(campaign: str, target: str, amount: int) -> dict:
    e = _entity(campaign, target)
    if e.get("dead"):
        raise GameError(f"{e['name']} is dead. Healing does nothing.")
    e["hp"] = min(e["max_hp"], e["hp"] + max(0, amount))
    if e["hp"] > 0 and "unconscious" in e["conditions"]:
        e["conditions"].remove("unconscious")
        if e["kind"] == "pc":
            e["death"] = {"successes": 0, "failures": 0}
    _put(campaign, "entity", e["id"], e)
    return {"target": _public(e)}


def set_condition(campaign: str, target: str, condition: str, active: bool) -> dict:
    e = _entity(campaign, target)
    c = condition.lower()
    if active and c not in e["conditions"]:
        e["conditions"].append(c)
    if not active and c in e["conditions"]:
        e["conditions"].remove(c)
    _put(campaign, "entity", e["id"], e)
    return {"target": _public(e)}


def spend_slot(campaign: str, caster: str, level: int) -> dict:
    e = _entity(campaign, caster)
    slots = e.get("slots", {})
    left = slots.get(str(level), 0)
    if left <= 0:
        raise GameError(f"{e['name']} has no level {level} slots left. The spell cannot be cast at that level.")
    slots[str(level)] = left - 1
    e["slots"] = slots
    _put(campaign, "entity", e["id"], e)
    return {"caster": e["name"], "level": level, "slots_left": slots}


# ----------------------------------------------------------------- combat

def creature_attack(campaign: str, attacker: str, target: str, attack_name: str = "",
                    advantage: str = "none") -> dict:
    """The engine rolls a non-player attack and applies the damage. The Narrator only reports it."""
    a, t = _entity(campaign, attacker), _entity(campaign, target)
    if a["kind"] == "pc":
        raise GameError("The player rolls their own attacks. Use player_attack with the numbers they report.")
    if a.get("dead") or a["hp"] <= 0:
        raise GameError(f"{a['name']} is dead or down and cannot attack.")
    if not a["attacks"]:
        raise GameError(f"{a['name']} has no attacks on its sheet. Add them with save_character first.")
    atk = next((x for x in a["attacks"] if x["name"].lower() == attack_name.lower()), a["attacks"][0])
    nat, detail = _d20(advantage)
    total = nat + int(atk.get("to_hit", 0))
    crit = nat == 20
    hit = crit or (nat != 1 and total >= t["ac"])
    out: dict[str, Any] = {
        "attacker": a["name"], "attack": atk["name"], "roll": f"{detail} + {atk.get('to_hit', 0)} = {total}",
        "hit": hit, "critical": crit,
    }
    if hit:
        dmg = roll_expr(atk["damage"], crit=crit)
        out["damage_roll"] = dmg["detail"]
        out.update(apply_damage(campaign, t["id"], dmg["total"], atk.get("type", ""), a["name"], crit=crit))
        if atk.get("rider"):
            out["rider"] = atk["rider"]
    else:
        out["target"] = _public(t)
    _log(campaign, "attack", f"{a['name']} {atk['name']} vs {t['name']}: {out['roll']} {'HIT' if hit else 'miss'}")
    out["instruction"] = "Report this result as rolled. Do not soften, reroll or reinterpret it."
    return out


def player_attack(campaign: str, attacker: str, target: str, attack_total: int,
                  natural_roll: int, damage: int, damage_type: str = "") -> dict:
    """The player rolled. The engine checks it against the target's AC and applies the damage."""
    a, t = _entity(campaign, attacker), _entity(campaign, target)
    crit = natural_roll == 20
    hit = crit or (natural_roll != 1 and attack_total >= t["ac"])
    out: dict[str, Any] = {"attacker": a["name"], "hit": hit, "critical": crit}
    if hit:
        out.update(apply_damage(campaign, t["id"], damage, damage_type, a["name"], crit=crit))
        if crit:
            out["note_crit"] = "Natural 20. Make sure the damage the player reported used doubled dice."
    else:
        out["target"] = _public(t)
    return out


def saving_throw(campaign: str, who: str, ability: str, dc: int,
                 player_total: int | None = None, advantage: str = "none") -> dict:
    e = _entity(campaign, who)
    if e["kind"] == "pc":
        if player_total is None:
            raise GameError("Ask the player to roll this save, then call again with player_total.")
        return {"who": e["name"], "total": player_total, "dc": dc, "success": player_total >= dc}
    nat, detail = _d20(advantage)
    bonus = int(e.get("saves", {}).get(ability.lower()[:3], 0))
    total = nat + bonus
    _log(campaign, "save", f"{e['name']} {ability} save DC {dc}: {total}")
    return {"who": e["name"], "roll": f"{detail} + {bonus} = {total}", "dc": dc, "success": total >= dc}


def death_save(campaign: str, pc: str, natural_roll: int) -> dict:
    e = _entity(campaign, pc)
    if e["kind"] != "pc" or e["hp"] > 0 or e.get("dead"):
        raise GameError(f"{e['name']} is not making death saves right now.")
    d = e["death"]
    events = []
    if natural_roll == 20:
        e["hp"], e["death"] = 1, {"successes": 0, "failures": 0}
        if "unconscious" in e["conditions"]:
            e["conditions"].remove("unconscious")
        events.append("Natural 20: back on their feet with 1 HP.")
    elif natural_roll == 1:
        d["failures"] += 2
    elif natural_roll >= 10:
        d["successes"] += 1
    else:
        d["failures"] += 1
    if e["hp"] <= 0:
        if d["failures"] >= 3:
            e["dead"] = True
            events.append("Third failure: the character dies. This is final unless the fiction provides a real way back.")
        elif d["successes"] >= 3:
            e["stable"] = True
            events.append("Third success: stable at 0 HP, still unconscious.")
    _put(campaign, "entity", e["id"], e)
    return {"pc": _public(e), "death_saves": e["death"], "events": events}


def start_combat(campaign: str, combatants: list[str], player_initiatives: dict[str, int]) -> dict:
    """Engine rolls initiative for everyone the player does not control."""
    pinit = {_slug(k): v for k, v in player_initiatives.items()}
    order = []
    for name in combatants:
        e = _entity(campaign, name)
        if e["id"] in pinit:
            init = pinit[e["id"]]
        elif e["kind"] == "pc":
            raise GameError(f"Ask the player for {e['name']}'s initiative roll and pass it in player_initiatives.")
        else:
            init = _rng.randint(1, 20) + int(e.get("init", 0))
        order.append({"id": e["id"], "name": e["name"], "init": init, "tiebreak": int(e.get("init", 0))})
    order.sort(key=lambda x: (-x["init"], -x["tiebreak"]))
    state = {"active": True, "round": 1, "turn": 0, "order": order}
    _put(campaign, "combat", "combat", state)
    _log(campaign, "combat", "Combat begins: " + ", ".join(f"{o['name']} {o['init']}" for o in order))
    return {"round": 1, "order": [{"name": o["name"], "init": o["init"]} for o in order], "now_acting": order[0]["name"]}


def next_turn(campaign: str) -> dict:
    s = _get(campaign, "combat", "combat")
    if not s or not s["active"]:
        raise GameError("No combat is running. Call start_combat first.")
    for _ in range(len(s["order"]) + 1):
        s["turn"] += 1
        if s["turn"] >= len(s["order"]):
            s["turn"], s["round"] = 0, s["round"] + 1
        e = _entity(campaign, s["order"][s["turn"]]["id"])
        if not e.get("dead") and (e["hp"] > 0 or e["kind"] == "pc"):
            break
    _put(campaign, "combat", "combat", s)
    out = {"round": s["round"], "now_acting": _public(e)}
    if e["kind"] == "pc" and e["hp"] <= 0 and not e.get("stable"):
        out["instruction"] = "The player rolls a death save now. Call death_save with the natural roll."
    elif e["kind"] != "pc":
        out["attacks_available"] = [a["name"] for a in e["attacks"]]
        out["instruction"] = ("Decide what this creature does from its own goals, then resolve attacks with "
                              "creature_attack. Enemies fight to win and target whoever it makes sense to target.")
    return out


def end_combat(campaign: str) -> dict:
    s = _get(campaign, "combat", "combat") or {"order": []}
    s["active"] = False
    _put(campaign, "combat", "combat", s)
    survivors = [_public(_entity(campaign, o["id"])) for o in s["order"]]
    advance = advance_time(campaign, 0.1, "combat")
    return {"combat_over": True, "combatants": survivors, **advance}


# ------------------------------------------------------- time and clocks

def set_clock(campaign: str, name: str, segments: int, steps: list[str],
              hours_per_segment: float = 0, filled: int = 0) -> dict:
    """A villain plan or looming threat. Each step is what happens in the world when that segment fills."""
    if len(steps) != segments:
        raise GameError("Give exactly one step per segment: what visibly changes in the world when it fills.")
    c = {"name": name, "segments": segments, "filled": filled, "steps": steps,
         "hours_per_segment": hours_per_segment, "progress_hours": 0.0}
    _put(campaign, "clock", _slug(name), c)
    return c


def _tick(campaign: str, id: str, c: dict, n: int, reason: str) -> list[dict]:
    fired = []
    for _ in range(n):
        if c["filled"] >= c["segments"]:
            break
        fired.append({"clock": c["name"], "segment": c["filled"] + 1, "of": c["segments"],
                      "what_happens": c["steps"][c["filled"]]})
        c["filled"] += 1
    _put(campaign, "clock", id, c)
    for f in fired:
        _log(campaign, "clock", f"{f['clock']} {f['segment']}/{f['of']} ({reason}): {f['what_happens']}")
    return fired


def tick_clock(campaign: str, name: str, segments: int = 1, reason: str = "") -> dict:
    id = _slug(name)
    c = _get(campaign, "clock", id)
    if c is None:
        raise GameError(f"No clock '{name}'. Known: {', '.join(_all(campaign, 'clock')) or 'none'}.")
    fired = _tick(campaign, id, c, segments, reason)
    return {"consequences": fired, "instruction": _consequence_rule(fired)}


def _consequence_rule(fired: list[dict]) -> str:
    if not fired:
        return "No clock advanced."
    return ("These things have now happened in the world. They are not optional and cannot be undone by "
            "narration. Show their effects the next time the player could plausibly notice them.")


def advance_time(campaign: str, hours: float, reason: str) -> dict:
    """Time passing is what lets villains act. Call it for travel, rests, research and waiting."""
    m = _meta(campaign)
    m["world_hours"] += hours
    _put(campaign, "meta", "meta", m)
    fired = []
    for id, c in _all(campaign, "clock").items():
        per = c.get("hours_per_segment") or 0
        if per <= 0 or c["filled"] >= c["segments"]:
            continue
        c["progress_hours"] += hours
        n = int(c["progress_hours"] // per)
        c["progress_hours"] -= n * per
        if n:
            fired += _tick(campaign, id, c, n, reason)
        else:
            _put(campaign, "clock", id, c)
    return {"world_hours": round(m["world_hours"], 1), "consequences": fired,
            "instruction": _consequence_rule(fired)}


def rest(campaign: str, kind: str) -> dict:
    if kind not in ("short", "long"):
        raise GameError("kind must be short or long.")
    restored = []
    if kind == "long":
        for id, e in _all(campaign, "entity").items():
            if e["kind"] in ("pc", "ally") and not e.get("dead"):
                e["hp"] = e["max_hp"]
                if "max_slots" in e:
                    e["slots"] = dict(e["max_slots"])
                e["conditions"] = [c for c in e["conditions"] if c != "unconscious"]
                _put(campaign, "entity", id, e)
                restored.append(e["name"])
    out = advance_time(campaign, 8 if kind == "long" else 1, f"{kind} rest")
    out["restored"] = restored
    if kind == "short":
        out["note"] = "Short rest: the player spends hit dice and rolls them; record the result with heal."
    return out


# ------------------------------------------------------ sealed campaign

def seal(campaign: str, key: str, content: str, tags: list[str] | None = None) -> dict:
    """Author-only. Store hidden campaign material: villains, secrets, scenes, twists."""
    _put(campaign, "sealed", _slug(key), {"key": key, "content": content, "tags": tags or []})
    return {"sealed": key, "characters": len(content)}


def sealed_index(campaign: str) -> dict:
    items = _all(campaign, "sealed")
    return {"entries": [{"key": v["key"], "tags": v["tags"]} for v in items.values()]}


def open_sealed(campaign: str, key: str) -> dict:
    s = _get(campaign, "sealed", _slug(key))
    if s is None:
        raise GameError(f"No sealed entry '{key}'. Call sealed_index to see the keys.")
    return {"key": s["key"], "content": s["content"],
            "instruction": "For your eyes only. Use it to run the scene. Never quote or summarise it to the player."}


# --------------------------------------------------------------- sessions

def session_start(campaign: str) -> dict:
    m = _meta(campaign)
    m["session"] += 1
    m["session_damage"] = 0
    _put(campaign, "meta", "meta", m)
    ents = _all(campaign, "entity")
    quiet = m["session"] - 1 - m["last_danger_session"]
    if m["session"] == 1:
        danger = "First session. Establish a real threat early so the stakes are felt."
    elif quiet >= 2:
        danger = (f"The player character has not been below half HP for {quiet} sessions. This session must "
                  "contain a threat that can realistically put them there or kill them. Run it through the "
                  "engine, at full strength.")
    else:
        danger = "Danger has been real recently. Keep consequences honest; vary the kind of pressure."
    return {
        "session": m["session"],
        "world_hours": round(m["world_hours"], 1),
        "last_session": m["last_summary"],
        "open_threads": m["open_threads"],
        "party": [_public(e) for e in ents.values() if e["kind"] in ("pc", "ally")],
        "clocks": [{"name": c["name"], "filled": c["filled"], "segments": c["segments"],
                    "next": c["steps"][c["filled"]] if c["filled"] < c["segments"] else "complete"}
                   for c in _all(campaign, "clock").values()],
        "sealed_keys": sealed_index(campaign)["entries"],
        "danger_directive": danger,
        "reminder": "Clocks and sealed entries are hidden from the player. Never show them.",
    }


def session_end(campaign: str, summary: str, open_threads: list[str]) -> dict:
    m = _meta(campaign)
    m["last_summary"], m["open_threads"] = summary, open_threads
    _put(campaign, "meta", "meta", m)
    _log(campaign, "session", f"Session {m['session']} ended. PC damage taken: {m['session_damage']}. {summary}")
    return {"session": m["session"], "pc_damage_taken": m["session_damage"], "saved": True}


def recent_log(campaign: str, limit: int = 40) -> dict:
    with _db() as con:
        rows = con.execute(
            "SELECT kind, text FROM log WHERE campaign=? ORDER BY ts DESC LIMIT ?", (campaign, limit)
        ).fetchall()
    return {"log": [f"[{k}] {t}" for k, t in reversed(rows)]}
