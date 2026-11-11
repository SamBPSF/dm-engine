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

def _doc(name: str) -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name)) as f:
            return f.read()
    except OSError:
        return ""


PLAYBOOK = _doc("playbook.md")
AUTHOR = _doc("author.md")
REVIEWER = _doc("reviewer.md")
SETUP = _doc("setup.md")


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


BACKUP_DIR = os.path.join(DATA_DIR, "backups")
_BACKUP_NAME = re.compile(r"^\d{8}-\d{6}-[a-z0-9-]*\.db$")


def snapshot(reason: str = "auto", min_gap_minutes: float = 10) -> str | None:
    """Copy the whole saved game to the backups folder. Keeps the newest 30 copies."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    have = sorted(f for f in os.listdir(BACKUP_DIR) if _BACKUP_NAME.match(f))
    if have and min_gap_minutes and time.time() - os.path.getmtime(os.path.join(BACKUP_DIR, have[-1])) < min_gap_minutes * 60:
        return None
    name = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + _slug(reason)[:24] + ".db"
    src, dst = _db(), sqlite3.connect(os.path.join(BACKUP_DIR, name))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(f for f in os.listdir(BACKUP_DIR) if _BACKUP_NAME.match(f))[:-30]:
        os.remove(os.path.join(BACKUP_DIR, old))
    return name


def _safe_snapshot(reason: str) -> None:
    try:
        snapshot(reason)
    except Exception:  # a failed backup must never stop play
        pass


def list_backups() -> dict:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    out = []
    for f in sorted((f for f in os.listdir(BACKUP_DIR) if _BACKUP_NAME.match(f)), reverse=True):
        out.append({"backup": f, "taken_utc": f"{f[0:4]}-{f[4:6]}-{f[6:8]} {f[9:11]}:{f[11:13]}",
                    "reason": f[16:-3]})
    return {"backups": out, "note": "Backups are taken automatically before sessions, prep and reviews."}


def restore_campaign(campaign: str, backup: str) -> dict:
    """Put one campaign back to how it was in a backup. Other campaigns are untouched."""
    if not _BACKUP_NAME.match(backup) or not os.path.exists(os.path.join(BACKUP_DIR, backup)):
        raise GameError(f"No backup called '{backup}'. Call list_backups for the names.")
    old = sqlite3.connect(os.path.join(BACKUP_DIR, backup))
    try:
        docs = old.execute("SELECT campaign, kind, id, data FROM doc WHERE campaign=?", (campaign,)).fetchall()
        logs = old.execute("SELECT campaign, ts, kind, text FROM log WHERE campaign=?", (campaign,)).fetchall()
    finally:
        old.close()
    if not docs:
        raise GameError(f"That backup holds nothing for campaign '{campaign}'.")
    kept = snapshot("before-restore", 0)
    with _db() as con:
        con.execute("DELETE FROM doc WHERE campaign=?", (campaign,))
        con.execute("DELETE FROM log WHERE campaign=?", (campaign,))
        con.executemany("INSERT INTO doc VALUES (?,?,?,?)", docs)
        con.executemany("INSERT INTO log VALUES (?,?,?,?)", logs)
    return {"restored": campaign, "from": backup, "entries": len(docs),
            "undo": f"The state just replaced was saved as '{kept}'."}


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
    for r in e.get("resources", {}).values():
        r.setdefault("recharge", "long")
        r.setdefault("current", r.get("max", 0))
    _put(campaign, "entity", id, e)
    return e


def use_resource(campaign: str, who: str, name: str, amount: int = 1) -> dict:
    """Spend a limited-use feature: Bardic Inspiration, Luck Points, pact slots and the like."""
    e = _entity(campaign, who)
    res = e.get("resources", {})
    key = next((k for k in res if k.lower() == name.lower()), None)
    if key is None:
        raise GameError(f"{e['name']} has no resource '{name}'. On the sheet: {', '.join(res) or 'none'}.")
    r = res[key]
    r.setdefault("current", r.get("max", 0))
    if amount < 1 or r["current"] < amount:
        raise GameError(f"{e['name']} has {r['current']} of {key} left and cannot spend {amount}.")
    r["current"] -= amount
    _put(campaign, "entity", e["id"], e)
    _log(campaign, "resource", f"{e['name']} spends {amount} {key}: {r['current']}/{r.get('max', 0)} left")
    return {"who": e["name"], "resource": key, "left": r["current"], "max": r.get("max", 0)}


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
    if e.get("concentrating"):
        out["concentrating_on"] = e["concentrating"]
    if e["kind"] in ("pc", "ally"):
        out.update({"hp": e["hp"], "max_hp": e["max_hp"], "temp_hp": e["temp_hp"], "ac": e["ac"]})
        if "slots" in e:
            out["slots"] = e["slots"]
        if e.get("resources"):
            out["resources"] = {k: f"{r.get('current', r.get('max', 0))}/{r.get('max', 0)}"
                                for k, r in e["resources"].items()}
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
    spell = e.get("concentrating")
    if spell and dealt > 0:
        dc = max(10, dealt // 2)
        if e["hp"] == 0:
            e["concentrating"] = ""
            events.append(f"Concentration on {spell} ends.")
        elif e["kind"] == "pc":
            events.append(f"Concentration on {spell}: the player rolls a Constitution save, DC {dc}. "
                          + (e.get("save_notes", "") + " " if e.get("save_notes") else "")
                          + "If it fails, call concentrate with an empty spell.")
        else:
            nat, _ = _d20()
            total = nat + int(e.get("saves", {}).get("con", 0))
            if total < dc:
                e["concentrating"] = ""
                events.append(f"{e['name']} rolls {total} against DC {dc} and loses concentration on {spell}.")
            else:
                events.append(f"{e['name']} rolls {total} against DC {dc} and holds concentration on {spell}.")
    _put(campaign, "entity", e["id"], e)
    _log(campaign, "damage", f"{e['name']} takes {dealt} {damage_type} from {source or 'unknown'} -> {e['hp']}/{e['max_hp']}")
    return {"target": _public(e), "damage_dealt": dealt, "note": note, "events": events}


def concentrate(campaign: str, who: str, spell: str = "") -> dict:
    """Record that someone is concentrating on a spell, or (empty spell) that they have stopped."""
    e = _entity(campaign, who)
    dropped = e.get("concentrating") or ""
    e["concentrating"] = spell
    _put(campaign, "entity", e["id"], e)
    out = {"who": e["name"], "concentrating_on": spell or None}
    if dropped and dropped != spell:
        out["ended"] = dropped
    return out


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

def _land(campaign: str, a: dict, t: dict, atk: dict, crit: bool, out: dict) -> dict:
    dmg = roll_expr(atk["damage"], crit=crit)
    out["damage_roll"] = dmg["detail"]
    out.update(apply_damage(campaign, t["id"], dmg["total"], atk.get("type", ""), a["name"], crit=crit))
    if atk.get("rider"):
        out["rider"] = atk["rider"]
    return out


def resolve_attack(campaign: str, use_reaction: str = "") -> dict:
    """Finish an attack that was paused so the player could decide on a reaction."""
    p = _get(campaign, "attack", "pending")
    if not p:
        raise GameError("No attack is waiting on the player.")
    _put(campaign, "attack", "pending", None)
    a, t = _entity(campaign, p["attacker"]), _entity(campaign, p["target"])
    out: dict[str, Any] = {"attacker": a["name"], "attack": p["atk"]["name"], "roll": p["roll"]}
    if use_reaction:
        out.update({"hit": False, "turned_by": use_reaction, "target": _public(t),
                    "instruction": f"The attack misses. Spend the cost of {use_reaction} now with "
                                   "use_resource or spend_slot."})
        _log(campaign, "attack", f"{a['name']} {p['atk']['name']} vs {t['name']}: {p['roll']} turned by {use_reaction}")
        return out
    out.update({"hit": True, "critical": False})
    _land(campaign, a, t, p["atk"], False, out)
    _log(campaign, "attack", f"{a['name']} {p['atk']['name']} vs {t['name']}: {p['roll']} HIT")
    out["instruction"] = "Report this result as rolled. Do not soften, reroll or reinterpret it."
    return out


def creature_attack(campaign: str, attacker: str, target: str, attack_name: str = "",
                    advantage: str = "none") -> dict:
    """The engine rolls a non-player attack and applies the damage. The Narrator only reports it."""
    if attack_name.upper().startswith("RESOLVE"):
        return resolve_attack(campaign, attack_name[7:].strip(" :"))
    earlier = None
    if _get(campaign, "attack", "pending"):
        earlier = resolve_attack(campaign)
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
    if earlier:
        out["earlier_attack_applied"] = earlier
    if t["kind"] == "pc":
        out["tell_player"] = f"{a['name']} rolls {total} to hit."
    turners = [r["name"] for r in t.get("reactions", [])
               if hit and not crit and total < t["ac"] + int(r.get("ac_bonus", 0))]
    if turners and t["kind"] == "pc" and t["hp"] > 0:
        _put(campaign, "attack", "pending", {"attacker": a["id"], "target": t["id"], "atk": atk,
                                              "roll": out["roll"]})
        out.update({"pending": True, "would_be_turned_by": turners,
                    "instruction": (f"STOP before any damage. Tell the player: {a['name']} hits with a {total}; "
                                    f"{' or '.join(turners)} would turn it. Ask if they use it. Then call "
                                    "resolve_attack with use_reaction set to its name, or empty to take the hit. "
                                    "If you do not have resolve_attack, call creature_attack again with "
                                    "attack_name 'RESOLVE' (take the hit) or 'RESOLVE Shield' (use it).")})
        return out
    if hit:
        _land(campaign, a, t, atk, crit, out)
    else:
        out["target"] = _public(t)
    _log(campaign, "attack", f"{a['name']} {atk['name']} vs {t['name']}: {out['roll']} {'HIT' if hit else 'miss'}")
    out["instruction"] = ("Report this result as rolled, including the total to hit when the target is the "
                          "player. Do not soften, reroll or reinterpret it.")
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
    if e["kind"] == "pc":
        out.update(battle_map(campaign))
        out["draw_grid"] = ("It is the player's turn. First report every turn taken since their last one, a "
                            "line each. Then show the battle grid as map.how_to_show says. Then ask what they do.")
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


# ------------------------------------------------------------ battle map

def set_battle_map(campaign: str, width: int, height: int, features: list[dict],
                   positions: dict[str, list[int]]) -> dict:
    """Lay out the fight on a grid of 5 ft squares. x counts east from 0, y counts south from 0."""
    if not (3 <= width <= 30 and 3 <= height <= 30):
        raise GameError("Keep the grid between 3 and 30 squares each way.")
    m = {"width": width, "height": height, "features": features, "positions": {}}
    for name, xy in positions.items():
        e = _entity(campaign, name)
        m["positions"][e["id"]] = _square(m, xy)
    _put(campaign, "map", "map", m)
    return battle_map(campaign)


def _square(m: dict, xy: list[int]) -> list[int]:
    if len(xy) != 2 or not (0 <= xy[0] < m["width"] and 0 <= xy[1] < m["height"]):
        raise GameError(f"Square {xy} is off the {m['width']}x{m['height']} grid.")
    return [int(xy[0]), int(xy[1])]


def _col(x: int) -> str:
    return chr(65 + x) if x < 26 else "A" + chr(65 + x - 26)


def _label(x: int, y: int) -> str:
    """Chess-style square name: column letter, then row number from 1 at the top."""
    return f"{_col(x)}{y + 1}"


def move_token(campaign: str, name: str, x: int, y: int) -> dict:
    m = _get(campaign, "map", "map")
    if m is None:
        raise GameError("No battle map yet. Call set_battle_map first.")
    e = _entity(campaign, name)
    old = m["positions"].get(e["id"])
    m["positions"][e["id"]] = _square(m, [x, y])
    _put(campaign, "map", "map", m)
    feet = None if old is None else 5 * max(abs(old[0] - x), abs(old[1] - y))
    return {"moved": e["name"], "to": [x, y], "square": _label(x, y), "distance_feet": feet}


_FEATURE_STYLE = {  # kind: (fill, stroke, text, text-grid mark)
    "cover": ("#D3D1C7", "#5F5E5A", "#2C2C2A", "#"),
    "wall": ("#B4B2A9", "#444441", "#2C2C2A", "X"),
    "hazard": ("#FAC775", "#854F0B", "#412402", "!"),
    "exit": ("#C0DD97", "#3B6D11", "#173404", ">"),
    "other": ("#CECBF6", "#534AB7", "#26215C", "+"),
}
_TOKEN_STYLE = {"pc": ("#9FE1CB", "#085041", "#04342C"), "ally": ("#B5D4F4", "#0C447C", "#042C53"),
                "npc": ("#F1EFE8", "#444441", "#2C2C2A")}  # bystanders and unknowns; enemies are "creature"
_ENEMY_STYLE = ("#F5C4B3", "#712B13", "#4A1B0C")


def _xml(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _codes(tokens: list[dict]) -> None:
    """Give each token a short code: its initial, numbered where several share one."""
    by: dict[str, list[dict]] = {}
    for t in tokens:
        by.setdefault(t["name"][:1].upper() or "?", []).append(t)
    for letter, group in by.items():
        group.sort(key=lambda t: (t["kind"] != "pc", t["name"]))
        for n, t in enumerate(group, 1):
            t["code"] = letter if len(group) == 1 or (n == 1 and t["kind"] == "pc") else f"{letter}{n}"


def _text_grid(w: int, h: int, feats: list[dict], tokens: list[dict]) -> str:
    cells = [[" . " for _ in range(w)] for _ in range(h)]
    for f in feats:
        mark = _FEATURE_STYLE.get(f.get("kind", "other"), _FEATURE_STYLE["other"])[3]
        for yy in range(f["y"], min(h, f["y"] + f.get("h", 1))):
            for xx in range(f["x"], min(w, f["x"] + f.get("w", 1))):
                cells[yy][xx] = f" {mark} "
    for t in tokens:
        cells[t["y"]][t["x"]] = f"{t['code']:^3}"
    head = "    " + "".join(f"{_col(x):^3}" for x in range(w))
    return "\n".join([head] + [f"{y + 1:>3} " + "".join(row) for y, row in enumerate(cells)])


def _svg_grid(w: int, h: int, feats: list[dict], tokens: list[dict], legend: list[str]) -> str:
    c = max(18, min(44, 600 // w))
    left, top = (680 - w * c) // 2 + 8, 30
    lines, line = [], ""
    for item in legend:
        if len(line) + len(item) > 88:
            lines.append(line)
            line = ""
        line = f"{line}  ·  {item}" if line else item
    lines.append(line)
    height = top + h * c + 24 + 18 * len(lines)
    o = [f'<svg width="100%" viewBox="0 0 680 {height}" role="img" xmlns="http://www.w3.org/2000/svg" '
         f'font-family="sans-serif"><title>Battle grid</title><desc>{_xml("; ".join(legend))}</desc>']
    for f in feats:
        fill, stroke, ink, _ = _FEATURE_STYLE.get(f.get("kind", "other"), _FEATURE_STYLE["other"])
        fw, fh = f.get("w", 1) * c, f.get("h", 1) * c
        fx, fy = left + f["x"] * c, top + f["y"] * c
        dash = ' stroke-dasharray="5 3"' if f.get("kind") == "hazard" else ""
        o.append(f'<rect x="{fx}" y="{fy}" width="{fw}" height="{fh}" rx="3" fill="{fill}" stroke="{stroke}"{dash}/>')
        name = str(f.get("name", ""))[: max(0, fw // 7)]
        if name and fh >= 16:
            o.append(f'<text x="{fx + fw / 2}" y="{fy + fh / 2}" font-size="12" fill="{ink}" text-anchor="middle" '
                     f'dominant-baseline="central">{_xml(name)}</text>')
    for x in range(w + 1):
        o.append(f'<line x1="{left + x * c}" y1="{top}" x2="{left + x * c}" y2="{top + h * c}" stroke="#888780" stroke-width="0.5"/>')
    for y in range(h + 1):
        o.append(f'<line x1="{left}" y1="{top + y * c}" x2="{left + w * c}" y2="{top + y * c}" stroke="#888780" stroke-width="0.5"/>')
    for x in range(w):
        o.append(f'<text x="{left + x * c + c / 2}" y="{top - 10}" font-size="12" fill="#888780" text-anchor="middle">{_col(x)}</text>')
    for y in range(h):
        o.append(f'<text x="{left - 8}" y="{top + y * c + c / 2}" font-size="12" fill="#888780" text-anchor="end" '
                 f'dominant-baseline="central">{y + 1}</text>')
    for t in tokens:
        fill, stroke, ink = _TOKEN_STYLE.get(t["kind"], _ENEMY_STYLE)
        span = 2 if t.get("size") in ("large", "huge") else 1
        cx, cy = left + t["x"] * c + span * c / 2, top + t["y"] * c + span * c / 2
        extra = ' stroke-dasharray="3 2"' if t["status"] in ("down and dying",) else ""
        o.append(f'<circle cx="{cx}" cy="{cy}" r="{span * c / 2 - 3}" fill="{fill}" stroke="{stroke}" '
                 f'stroke-width="{2.5 if t["kind"] == "pc" else 1}"{extra}/>')
        o.append(f'<text x="{cx}" y="{cy}" font-size="{13 if c >= 30 else 11}" font-weight="500" fill="{ink}" '
                 f'text-anchor="middle" dominant-baseline="central">{_xml(t["code"])}</text>')
    for n, text in enumerate(lines):
        o.append(f'<text x="{left}" y="{top + h * c + 24 + 18 * n}" font-size="12" fill="#888780">{_xml(text)}</text>')
    o.append("</svg>")
    return "".join(o)


def battle_map(campaign: str) -> dict:
    m = _get(campaign, "map", "map")
    if m is None:
        return {"map": None}
    w, h = m["width"], m["height"]
    tokens = []
    for id, xy in m["positions"].items():
        e = _get(campaign, "entity", id)
        if e is None or _status(e) == "dead":
            continue
        tokens.append({"name": e["name"], "kind": e["kind"], "x": xy[0], "y": xy[1], "square": _label(*xy),
                       "status": _status(e), "size": e.get("size", "medium")})
    _codes(tokens)
    feats = []
    for f in m["features"]:
        f = {"kind": "other", "w": 1, "h": 1, **f}
        f["x"], f["y"] = max(0, min(w - 1, int(f.get("x", 0)))), max(0, min(h - 1, int(f.get("y", 0))))
        f["w"], f["h"] = max(1, min(w - f["x"], int(f["w"]))), max(1, min(h - f["y"], int(f["h"])))
        f["squares"] = _label(f["x"], f["y"]) + ("" if f["w"] == f["h"] == 1 else
                                                 "-" + _label(f["x"] + f["w"] - 1, f["y"] + f["h"] - 1))
        feats.append(f)
    legend = [f"{t['code']} {t['name']}" + (" (you)" if t["kind"] == "pc" else "")
              + ("" if t["status"] in ("unhurt", "hurt") else f", {t['status']}") for t in tokens]
    legend.append("1 square = 5 ft")
    return {"map": {"width": w, "height": h, "square_feet": 5, "features": feats, "tokens": tokens,
                    "svg": _svg_grid(w, h, feats, tokens, legend),
                    "text_grid": _text_grid(w, h, feats, tokens),
                    "how_to_show": ("Show `svg` exactly as given, with whatever you have for visuals (an inline "
                                    "visual or widget tool, or an artifact). Do not redraw it or move anything. "
                                    "Only if you cannot show an image, print `text_grid` in a code block. Name "
                                    "squares to the player only by these labels, such as E6.")}}


# ------------------------------------------------------ encounter check

def _avg(expr: str, dice_only: bool = False) -> float:
    total = 0.0
    for sign, n, sides, flat in _TERM.findall(str(expr).replace(" ", "").lower()):
        mult = -1 if sign == "-" else 1
        if sides:
            total += mult * int(n or 1) * (int(sides) + 1) / 2
        elif not dice_only:
            total += mult * int(flat)
    return total


def _dpr(e: dict, ac: float) -> float:
    """Expected damage a round from the best attack on the sheet against a given armour class."""
    best = 0.0
    for a in e.get("attacks", []):
        try:
            p = min(0.95, max(0.05, (21 - (ac - int(a.get("to_hit", 0)))) / 20))
            best = max(best, (p * _avg(a["damage"]) + 0.05 * _avg(a["damage"], True)) * int(a.get("count", 1)))
        except (KeyError, ValueError, TypeError):
            continue
    return best * int(e.get("attacks_per_round", 1))


def encounter_check(campaign: str, enemies: list[str], allies: list[str] | None = None, pc: str = "") -> dict:
    """Estimate how dangerous a fight is for the player character, from the sheets on file."""
    ents = _all(campaign, "entity")
    hero = _entity(campaign, pc) if pc else next((e for e in ents.values() if e["kind"] == "pc"), None)
    if hero is None:
        raise GameError("No player character is saved in this campaign yet.")
    if not enemies:
        raise GameError("List the enemies by name, repeating a name for each copy.")
    foes = [_entity(campaign, n) for n in enemies]
    friends = [_entity(campaign, n) for n in (allies or [])]
    foe_ac = sum(f["ac"] for f in foes) / len(foes)
    foe_hp = sum(f["max_hp"] for f in foes)
    incoming = sum(_dpr(f, hero["ac"]) for f in foes)
    outgoing = _dpr(hero, foe_ac) + sum(_dpr(a, foe_ac) for a in friends)
    rounds_to_win = foe_hp / outgoing if outgoing > 0 else 99.0
    share = 1.0 if not friends else 0.6
    # Enemies fall over the course of the fight, so on average about 60% of them are still swinging.
    loss = incoming * share * min(rounds_to_win, 20) * 0.6
    pct = loss / hero["max_hp"]
    rating = ("trivial" if pct < 0.12 else "light" if pct < 0.3 else "fair" if pct < 0.55
              else "dangerous" if pct < 0.85 else "deadly")
    advice = {
        "trivial": "The player will feel no danger. Add enemies, raise their damage, or give them terrain.",
        "light": "A skirmish. Fine as a side beat, too soft for a set piece.",
        "fair": "A fight that costs something. Right for most set pieces.",
        "dangerous": "The character could go down if it is played badly. Right for a main set piece.",
        "deadly": "The character probably loses a straight fight. Use only with a real way out or a goal "
                  "other than winning.",
    }[rating]
    swings = sum(int(f.get("attacks_per_round", 1)) for f in foes)
    out = {
        "rating": rating, "advice": advice,
        "expected_hp_lost": round(loss), "of_max_hp": hero["max_hp"], "share_of_hp": f"{round(pct * 100)}%",
        "enemy_damage_per_round_if_all_target_the_character": round(incoming, 1),
        "rounds_until_character_drops_if_focused": round(hero["max_hp"] / incoming, 1) if incoming else None,
        "party_damage_per_round": round(outgoing, 1), "rounds_to_defeat_enemies": round(rounds_to_win, 1),
        "caveat": ("Counts only the attacks written on the sheets. Spells, control effects, terrain, surprise "
                   "and enemy riders are not counted: a caster with area or control spells is stronger than "
                   "this, and enemies who grab, net or use hazards are more dangerous than this."),
    }
    if swings > 3 + 2 * len(friends):
        out["warning"] = (f"{swings} enemy attacks a round against {1 + len(friends)} on the player's side. One "
                          "character cannot answer that many; use fewer, heavier enemies.")
    return out


# ----------------------------------------------------- checks and cards

def set_check(campaign: str, who: str, skill: str, dc: int, on_failure: str, on_success: str = "") -> dict:
    """Commit the difficulty and the stakes of a player's check before they roll."""
    e = _entity(campaign, who)
    if not 1 <= dc <= 40:
        raise GameError("dc should be between 1 and 40.")
    if not on_failure.strip():
        raise GameError("Say what failure costs. If failure would change nothing, do not ask for a roll.")
    _put(campaign, "check", "pending", {"who": e["name"], "skill": skill, "dc": dc,
                                        "on_failure": on_failure, "on_success": on_success})
    return {"committed": True, "ask_player": f"Roll {skill} for {e['name']} and report the total.",
            "instruction": "Do not tell the player the difficulty. Call resolve_check with their total."}


def resolve_check(campaign: str, total: int) -> dict:
    c = _get(campaign, "check", "pending")
    if not c:
        raise GameError("No check is pending. Call set_check before the player rolls.")
    _put(campaign, "check", "pending", None)
    success = total >= c["dc"]
    margin = total - c["dc"]
    _log(campaign, "check", f"{c['who']} {c['skill']} {total} vs DC {c['dc']}: {'success' if success else 'failure'}")
    return {"who": c["who"], "skill": c["skill"], "success": success, "margin": margin,
            "result": c["on_success"] if success else c["on_failure"],
            "instruction": "This outcome was committed before the roll. Narrate it; do not soften it."}


ATTITUDES = ["hostile", "unfriendly", "wary", "neutral", "warm", "friendly", "devoted"]


def save_card(campaign: str, name: str, card: dict) -> dict:
    """A character card: want, voice, secret, signature_line, attitude, notes. Fields merge."""
    id = _slug(name)
    c = _get(campaign, "card", id, {"name": name, "attitude": "neutral", "history": []})
    c.update({k: v for k, v in card.items() if k != "history"})
    if c["attitude"] not in ATTITUDES:
        raise GameError(f"attitude must be one of: {', '.join(ATTITUDES)}.")
    _put(campaign, "card", id, c)
    return c


def get_card(campaign: str, name: str) -> dict:
    c = _get(campaign, "card", _slug(name))
    if c is None:
        known = ", ".join(v["name"] for v in _all(campaign, "card").values()) or "none yet"
        raise GameError(f"No card for '{name}'. Known: {known}. Create one with save_card before voicing them.")
    return {**c, "instruction": "Play the want, the voice and the secret. Never state the secret outright."}


def adjust_attitude(campaign: str, name: str, steps: int, reason: str) -> dict:
    id = _slug(name)
    c = _get(campaign, "card", id)
    if c is None:
        raise GameError(f"No card for '{name}'.")
    i = max(0, min(len(ATTITUDES) - 1, ATTITUDES.index(c["attitude"]) + steps))
    c["attitude"] = ATTITUDES[i]
    c["history"] = (c.get("history", []) + [reason])[-12:]
    _put(campaign, "card", id, c)
    _log(campaign, "attitude", f"{c['name']} is now {c['attitude']}: {reason}")
    return {"name": c["name"], "attitude": c["attitude"], "remembers": c["history"]}


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
    for id, e in _all(campaign, "entity").items():
        if e["kind"] not in ("pc", "ally") or e.get("dead"):
            continue
        for r in e.get("resources", {}).values():
            if kind == "long" or r.get("recharge", "long") == "short":
                r["current"] = r.get("max", 0)
        if kind == "long":
            e["hp"] = e["max_hp"]
            if "max_slots" in e:
                e["slots"] = dict(e["max_slots"])
            e["conditions"] = [c for c in e["conditions"] if c != "unconscious"]
            restored.append(e["name"])
        _put(campaign, "entity", id, e)
    out = advance_time(campaign, 8 if kind == "long" else 1, f"{kind} rest")
    out["restored"] = restored
    if kind == "short":
        out["note"] = ("Short rest: features that recharge on a short rest are restored. The player spends "
                       "hit dice and rolls them; record the result with heal.")
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


def set_setting_notes(campaign: str, setting: str, tone: str, table_rules: list[str] | None = None,
                      description: str = "") -> dict:
    """Per-campaign notes that sit on top of the playbook: the setting's name, how it should feel, house rules."""
    prev = _get(campaign, "setting", "notes") or {}
    n = {**prev, "setting": setting, "tone": tone, "table_rules": table_rules or prev.get("table_rules", []),
         "description": description or prev.get("description", ""), "saved_at": time.time(),
         "canon_version": prev.get("canon_version") if prev else 2}
    _put(campaign, "setting", "notes", n)
    return n


def author_brief(campaign: str) -> dict:
    """Everything the Author needs: the procedure and the full current state of the campaign."""
    _safe_snapshot("before-author")
    m = _meta(campaign)
    ents = _all(campaign, "entity")
    return {
        "procedure": AUTHOR,
        "setting_notes": _get(campaign, "setting", "notes") or "None saved yet.",
        "established_in_this_setting": _established(campaign),
        "party": [e for e in ents.values() if e["kind"] in ("pc", "ally")],
        "creatures_on_file": [e["name"] for e in ents.values() if e["kind"] in ("npc", "creature")],
        "cast": list(_all(campaign, "card").values()),
        "clocks": list(_all(campaign, "clock").values()),
        "sealed_keys": sealed_index(campaign)["entries"],
        "sessions_played": m["session"],
        "last_session": m["last_summary"],
        "open_threads": m["open_threads"],
        "recent_events": recent_log(campaign, 60)["log"],
        "secrecy": "Nothing you read or write here may appear in your visible replies.",
    }


def _campaign_names(include_hidden: bool = False) -> list[str]:
    """Campaign names. Archived campaigns and scratch ones with no setting are hidden unless asked for."""
    with _db() as con:
        names = [r[0] for r in con.execute("SELECT DISTINCT campaign FROM doc ORDER BY campaign")]
    if include_hidden:
        return names
    return [c for c in names
            if not _meta(c).get("archived") and (_get(c, "setting", "notes") or {}).get("setting")]


def archive_campaign(campaign: str, archived: bool = True) -> dict:
    """Hide a campaign from lists and from setup without deleting anything. Reversible."""
    if campaign not in _campaign_names(include_hidden=True):
        raise GameError(f"No campaign called '{campaign}'.")
    m = _meta(campaign)
    m["archived"] = archived
    _put(campaign, "meta", "meta", m)
    return {"campaign": campaign, "archived": archived}


def _saved_settings() -> dict[str, dict]:
    """Settings already described in any campaign, newest campaign winning, keyed by slug."""
    found = []
    for c in _campaign_names():
        n = _get(c, "setting", "notes")
        if n and n.get("setting"):
            found.append({**n, "from_campaign": c})
    # A setting saved with a description outranks a bare name; within each, the newest wins.
    found.sort(key=lambda n: (bool(n.get("description")), n.get("saved_at", 0)))
    return {_slug(n["setting"])[:40]: n for n in found}


def _established(campaign: str) -> list[dict]:
    """Common-knowledge lore written for earlier campaigns in the same setting, to keep names consistent."""
    mine = _get(campaign, "setting", "notes") or {}
    if not mine.get("setting"):
        return []
    out, size = [], 0
    for c in _campaign_names():
        n = _get(c, "setting", "notes") or {}
        if c == campaign or n.get("canon_version", 0) < 2 or _slug(n.get("setting", "")) != _slug(mine["setting"]):
            continue
        for v in _all(c, "sealed").values():
            if v["key"].startswith("lore-") and size < 12000:
                out.append({"from_campaign": c, "subject": v["key"][5:], "text": v["content"]})
                size += len(v["content"])
    return out


def _latest_table_rules(saved: dict[str, dict]) -> list[str]:
    with_rules = [n for n in saved.values() if n.get("table_rules")]
    return max(with_rules, key=lambda n: n.get("saved_at", 0))["table_rules"] if with_rules else []


def _saved_characters() -> dict[str, dict]:
    """Player characters in any campaign. Where one appears twice, the fullest sheet wins."""
    out: dict[str, dict] = {}
    for c in _campaign_names():
        for e in _all(c, "entity").values():
            if e["kind"] == "pc" and len(e) > len(out.get(e["id"], {})):
                out[e["id"]] = {**e, "_from": c}
    return out


def campaign_setup_guide() -> dict:
    settings = _saved_settings()
    rules = _latest_table_rules(settings)
    return {
        "procedure": SETUP,
        "saved_settings": [{"name": n["setting"], "tone": n.get("tone", "")} for n in settings.values()
                           if n.get("description")],
        "saved_characters": [{"name": e["name"], "class": e.get("class", ""), "level": e.get("level", "")}
                             for e in _saved_characters().values()],
        "saved_table_rules": rules,
        "existing_campaigns": _campaign_names(include_hidden=True),
    }


def create_campaign(campaign: str, setting: str, description: str = "", tone: str = "",
                    table_rules: list[str] | None = None, character: str = "", length: str = "") -> dict:
    """Set up a new campaign: its setting notes and, if a saved character is named, a rested copy of them."""
    name = _slug(campaign)
    if not name:
        raise GameError("Give the campaign a short name.")
    if name in _campaign_names(include_hidden=True):
        raise GameError(f"A campaign called '{name}' already exists. Pick another name, such as '{name}-2'.")
    saved = _saved_settings()
    base = saved.get(_slug(setting)[:40], {})
    if not description and not base:
        raise GameError(f"No saved setting called '{setting}'. Pass a description of the world, or the feel "
                        "the player wants if the Author is to invent it. Saved: "
                        + (", ".join(n["setting"] for n in saved.values()) or "none") + ".")
    if table_rules is None:
        table_rules = base.get("table_rules") or _latest_table_rules(saved)
    notes = {"setting": setting or base.get("setting", name),
             "description": description or base.get("description", ""),
             "tone": tone or base.get("tone", ""),
             "table_rules": table_rules,
             "length": length or "A short adventure of two or three sessions.",
             "saved_at": time.time(), "canon_version": 2}
    _put(name, "setting", "notes", notes)
    out: dict[str, Any] = {"campaign": name, "setting_notes": notes}
    if character:
        src = _saved_characters().get(_slug(character))
        if src is None:
            out["character"] = (f"No saved character called '{character}'. Ask the player for the sheet and "
                                "save it with save_character before running the Author.")
        else:
            e = {k: v for k, v in src.items() if k not in ("_from", "dead", "stable")}
            e.update({"hp": e["max_hp"], "temp_hp": 0, "conditions": [],
                      "death": {"successes": 0, "failures": 0}})
            if "max_slots" in e:
                e["slots"] = dict(e["max_slots"])
            for r in e.get("resources", {}).values():
                r["current"] = r.get("max", 0)
            _put(name, "entity", e["id"], e)
            out["character"] = f"{e['name']} copied from '{src['_from']}', fully rested."
    else:
        out["character"] = "None yet. Save the player character with save_character before running the Author."
    out["next"] = f"Call author_brief('{name}') and follow its procedure. Reveal nothing."
    return out


def list_campaigns() -> dict:
    """Every campaign saved on this engine, with enough to tell them apart."""
    out = []
    for c in _campaign_names():
        m = _meta(c)
        notes = _get(c, "setting", "notes") or {}
        pcs = [e["name"] for e in _all(c, "entity").values() if e["kind"] == "pc"]
        out.append({"campaign": c, "setting": notes.get("setting", ""), "characters": pcs,
                    "sessions_played": m["session"], "last_session": m["last_summary"][:160]})
    return {"campaigns": out}


# --------------------------------------------------------------- sessions

def session_start(campaign: str) -> dict:
    _safe_snapshot("before-session")
    m = _meta(campaign)
    records = _all(campaign, "record")
    with _db() as con:
        played = con.execute("SELECT COUNT(*) FROM log WHERE campaign=? AND ts>=?",
                             (campaign, m.get("session_started", 0))).fetchone()[0]
    # A session that was opened but never played or ended (for example during prep) is reused, not counted.
    stray = m["session"] > 0 and str(m["session"]) not in records and played == 0
    if not stray:
        m["session"] += 1
    m["session_damage"] = 0
    m["session_started"] = time.time()
    _put(campaign, "meta", "meta", m)
    ents = _all(campaign, "entity")
    ck = _get(campaign, "sealed", "common-knowledge")
    if ck and not records:
        grounding = ("Before the first scene, give the player 'where you stand' from common_knowledge: who "
                     "they are, where and why, who is with them, what everyone knows. They have not had it yet.")
    elif ck:
        grounding = "The player has been given the basics. Re-attach names to faces in the recap."
    else:
        grounding = ("No common knowledge is written for this campaign. Before the first scene, tell the "
                     "player plainly what their character would know: who they are, where and why, who is "
                     "with them. Introduce every name with a face and a role.")
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
        "cast": [{"name": c["name"], "attitude": c["attitude"]} for c in _all(campaign, "card").values()],
        "playbook": PLAYBOOK,
        "grounding": grounding,
        "sheets": ("Full character sheets are on file. Call get_character for skills, saves, spells, features "
                   "and inventory instead of asking the player for them. Spend limited-use features with "
                   "use_resource and spell slots with spend_slot."),
        "common_knowledge": ck["content"] if ck else None,
        "lore_briefs": [v["key"] for v in _all(campaign, "sealed").values() if v["key"].startswith("lore-")],
        "setting_notes": _get(campaign, "setting", "notes")
        or "None saved. Ask the player what this game should feel like, then call set_setting_notes.",
        "danger_directive": danger,
        "prepared": ("A session guide is sealed under 'next-session'. Open it before the first scene."
                     if _get(campaign, "sealed", "next-session") else
                     "No session guide is prepared. Run from the sealed entries, or improvise within the playbook."),
        "narrator_notes": ("Corrections from the last review are sealed under 'narrator-notes'. Open and "
                           "follow them before the first scene."
                           if _get(campaign, "sealed", "narrator-notes") else "None yet."),
        "player_feedback": [f["text"] for f in _get(campaign, "feedback", "all", [])][-10:],
        "review_status": ("The previous session was never reviewed, so consequences may be thin. Suggest "
                          "running the Reviewer after this session."
                          if m["session"] > 1 and m.get("last_reviewed", 0) < m["session"] - 1 else "Up to date."),
        "reminder": "Clocks and sealed entries are hidden from the player. Never show them.",
    }


def session_end(campaign: str, summary: str, open_threads: list[str], record: str = "") -> dict:
    m = _meta(campaign)
    m["last_summary"], m["open_threads"] = summary, open_threads
    _put(campaign, "meta", "meta", m)
    _put(campaign, "record", str(m["session"]), {
        "session": m["session"], "summary": summary, "open_threads": open_threads,
        "record": record, "pc_damage_taken": m["session_damage"]})
    _log(campaign, "session", f"Session {m['session']} ended. PC damage taken: {m['session_damage']}. {summary}")
    return {"session": m["session"], "pc_damage_taken": m["session_damage"], "saved": True,
            "next": ("Ask the player whether to run the Reviewer now. If yes, call reviewer_brief in this "
                     "chat and follow its procedure without revealing anything.")}


def add_feedback(campaign: str, text: str) -> dict:
    """The player's own words about how the game is going. Kept, and shown to the Narrator and Reviewer."""
    m = _meta(campaign)
    f = _get(campaign, "feedback", "all", [])
    f.append({"session": m["session"], "text": text.strip()})
    _put(campaign, "feedback", "all", f[-30:])
    return {"saved": True, "feedback_on_file": len(f[-30:])}


def reviewer_brief(campaign: str) -> dict:
    """Everything the Reviewer needs to judge the last session and prepare the next."""
    _safe_snapshot("before-review")
    m = _meta(campaign)
    since = m.get("session_started", 0)
    with _db() as con:
        rows = con.execute("SELECT kind, text FROM log WHERE campaign=? AND ts>=? ORDER BY ts",
                           (campaign, since)).fetchall()
    records = sorted(_all(campaign, "record").values(), key=lambda r: -r["session"])[:3]
    ents = _all(campaign, "entity")
    return {
        "procedure": REVIEWER,
        "session": m["session"],
        "already_reviewed": m.get("last_reviewed", 0) >= m["session"],
        "session_records": records,
        "engine_log_this_session": [f"[{k}] {t}" for k, t in rows][-200:],
        "pc_damage_taken_this_session": m["session_damage"],
        "sessions_since_pc_below_half": m["session"] - m["last_danger_session"],
        "party": [e for e in ents.values() if e["kind"] in ("pc", "ally")],
        "setting_notes": _get(campaign, "setting", "notes") or "None saved.",
        "clocks": list(_all(campaign, "clock").values()),
        "cast": list(_all(campaign, "card").values()),
        "sealed_keys": sealed_index(campaign)["entries"],
        "player_feedback": _get(campaign, "feedback", "all", []),
        "world_hours": round(m["world_hours"], 1),
        "secrecy": "Nothing you read or write here may appear in your visible replies.",
    }


def finish_review(campaign: str) -> dict:
    m = _meta(campaign)
    m["last_reviewed"] = m["session"]
    _put(campaign, "meta", "meta", m)
    _log(campaign, "review", f"Session {m['session']} reviewed.")
    missing = [k for k in ("next-session", "narrator-notes") if not _get(campaign, "sealed", k)]
    return {"reviewed_session": m["session"],
            "warning": f"Not yet written: {', '.join(missing)}." if missing else "Next session is prepared."}


def recent_log(campaign: str, limit: int = 40) -> dict:
    with _db() as con:
        rows = con.execute(
            "SELECT kind, text FROM log WHERE campaign=? ORDER BY ts DESC LIMIT ?", (campaign, limit)
        ).fetchall()
    return {"log": [f"[{k}] {t}" for k, t in reversed(rows)]}
