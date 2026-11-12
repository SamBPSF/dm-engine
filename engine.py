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
    if kind == "pc":
        _recognise_reactions(e)
    _put(campaign, "entity", id, e)
    return e


def sheet_check(e: dict) -> list[str]:
    """What a player character's sheet still lacks for the engine and the Author to do their jobs."""
    gaps = []
    casts = bool(e.get("spells") or e.get("cantrips"))
    if not e.get("attacks"):
        gaps.append("attacks: each weapon or attack cantrip as {name, to_hit, damage, type}. Without them fights cannot be rated.")
    if len(e.get("saves", {})) < 6:
        gaps.append("saves: the bonus for all six saving throws.")
    if len(e.get("abilities", {})) < 6:
        gaps.append("abilities: the six ability scores.")
    if not e.get("skills"):
        gaps.append("skills: the bonus for each skill.")
    if not e.get("class") or not e.get("level"):
        gaps.append("class and level.")
    if not e.get("proficiency_bonus"):
        gaps.append("proficiency_bonus.")
    if not e.get("features"):
        gaps.append("features: class features and feats, one line each saying what it does.")
    if casts and not e.get("max_slots") and not any("slot" in k.lower() for k in e.get("resources", {})):
        gaps.append("max_slots: spell slots by level, for example {\"1\": 4, \"2\": 2}.")
    if casts and not (e.get("spellcasting") or {}).get("save_dc"):
        gaps.append("spellcasting: {ability, save_dc, attack_bonus}.")
    if e.get("features") and not e.get("resources") and re.search(
            r"per (short|long)|/ ?(short|long) rest|once per|uses", " ".join(_strings(e.get("features"))).lower()):
        gaps.append("resources: limited-use features as {name: {max, recharge: short or long}} so the engine can count them.")
    if len(str(e.get("notes", "")) + str(e.get("backstory", ""))) < 80:
        gaps.append("notes: a few lines on who they are. What they want, someone they care about, someone "
                    "they owe or fear. The Author builds the adventure's hook from this; ask the player.")
    return gaps


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
        if e["kind"] in ("pc", "ally"):
            return "down but stable" if e.get("stable") else "down and dying"
        return "dead"
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
    if e["kind"] in ("pc", "ally"):
        who = "The character" if e["kind"] == "pc" else e["name"]
        if e["kind"] == "pc":
            m = _meta(campaign)
            m["session_damage"] += dealt
            if e["hp"] <= e["max_hp"] / 2 and dealt > 0:
                m["last_danger_session"] = m["session"]
            _put(campaign, "meta", "meta", m)
        e.setdefault("death", {"successes": 0, "failures": 0})
        if was_down and dealt > 0 and not e.get("dead"):
            e["death"]["failures"] += 2 if crit else 1
            e["stable"] = False
            events.append(f"Damage while down: death save failure ({e['death']['failures']}/3).")
        elif e["hp"] == 0 and dealt - before >= e["max_hp"]:
            e["dead"] = True
            events.append(f"Massive damage: {who} is killed outright.")
        elif e["hp"] == 0 and not was_down:
            e["death"] = {"successes": 0, "failures": 0}
            e["stable"] = False
            if "unconscious" not in e["conditions"]:
                e["conditions"].append("unconscious")
            events.append(f"{who} drops to 0 HP: unconscious and making death saves."
                          + ("" if e["kind"] == "pc" else " The engine rolls them on this companion's turns. "
                             "They can be stabilised or healed before the third failure."))
        if e["death"]["failures"] >= 3 and not e.get("dead"):
            e["dead"] = True
            events.append(f"Three failures: {who} is dead.")
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
    if e["hp"] > 0:
        _drop_condition(e, "unconscious")
        e["stable"] = False
        if "death" in e:
            e["death"] = {"successes": 0, "failures": 0}
    _put(campaign, "entity", e["id"], e)
    return {"target": _public(e)}


# ------------------------------------------------------------- conditions

CONDITION_RULES = {
    "blinded": "Cannot see and fails any check that needs sight. Its attacks have disadvantage; attacks "
               "against it have advantage.",
    "charmed": "Cannot attack the charmer or target them with anything harmful. The charmer has advantage "
               "on social checks with it.",
    "deafened": "Cannot hear and fails any check that needs hearing.",
    "frightened": "Disadvantage on attacks and ability checks while the source of the fear is in sight. "
                  "Cannot willingly move closer to it.",
    "grappled": "Speed 0. Disadvantage on attacks against anyone but the grappler. Ends if the grappler is "
                "incapacitated or the two are forced apart.",
    "incapacitated": "No actions, bonus actions or reactions. Concentration ends. Cannot speak.",
    "invisible": "Cannot be seen without magic or a special sense. Its attacks have advantage; attacks "
                 "against it have disadvantage.",
    "paralyzed": "No actions, speed 0. Fails Strength and Dexterity saves. Attacks against it have "
                 "advantage, and a hit from within 5 ft is a critical hit.",
    "petrified": "Turned to stone: no actions, speed 0, resists all damage. Fails Strength and Dexterity "
                 "saves. Attacks against it have advantage.",
    "poisoned": "Disadvantage on attack rolls and ability checks.",
    "prone": "Can only crawl; standing up costs half its speed. Its attacks have disadvantage. Attacks "
             "against it have advantage from within 5 ft and disadvantage from further away.",
    "restrained": "Speed 0. Its attacks have disadvantage; attacks against it have advantage. Disadvantage "
                  "on Dexterity saves.",
    "stunned": "No actions. Fails Strength and Dexterity saves. Attacks against it have advantage.",
    "unconscious": "No actions, prone, unaware, drops what it holds. Fails Strength and Dexterity saves. "
                   "Attacks against it have advantage, and a hit from within 5 ft is a critical hit.",
    "exhaustion": "Each level: -2 on every d20 roll and -5 ft of speed. Death at level 6. A long rest "
                  "removes one level. Record it as 'exhaustion 1', 'exhaustion 2' and so on.",
}
_NO_ACTIONS = {"incapacitated", "paralyzed", "petrified", "stunned", "unconscious"}
_OWN_ATTACKS_DISADV = {"blinded", "frightened", "poisoned", "prone", "restrained"}
_ATTACKED_WITH_ADV = {"blinded", "paralyzed", "petrified", "restrained", "stunned", "unconscious"}
_FAILS_STR_DEX = {"paralyzed", "petrified", "stunned", "unconscious"}
_CRIT_WHEN_ADJACENT = {"paralyzed", "unconscious"}


def _conds(e: dict) -> set[str]:
    """Condition names without any level, so 'exhaustion 2' reads as 'exhaustion'."""
    return {c.split()[0] for c in e.get("conditions", []) if c}


def _exhaustion(e: dict) -> int:
    for c in e.get("conditions", []):
        parts = c.split()
        if parts and parts[0] == "exhaustion":
            return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    return 0


def _drop_condition(e: dict, name: str) -> bool:
    name = name.lower().strip()
    had = [c for c in e.get("conditions", []) if c == name or c.split()[0] == name]
    e["conditions"] = [c for c in e.get("conditions", []) if c not in had]
    for c in had:
        e.get("condition_info", {}).pop(c, None)
    return bool(had)


def _condition_line(e: dict, c: str) -> dict:
    info = e.get("condition_info", {}).get(c, {})
    line = {"condition": c, "rule": CONDITION_RULES.get(c.split()[0], "No standard rule: apply it as described when it was set.")}
    if info.get("rounds"):
        line["rounds_left"] = info["rounds"]
    if info.get("save_dc"):
        line["ends_on"] = f"{info['save_ability']} save, DC {info['save_dc']}, at the end of each of its turns"
    if info.get("source"):
        line["source"] = info["source"]
    return line


def set_condition(campaign: str, target: str, condition: str, active: bool, rounds: int = 0,
                  save_ability: str = "", save_dc: int = 0, source: str = "") -> dict:
    """Put a condition on someone or take it off. rounds: it ends by itself after that many of the target's
    turns. save_ability and save_dc: the target repeats that save at the end of each of its turns and the
    condition ends on a success. source: who or what caused it."""
    e = _entity(campaign, target)
    c = " ".join(condition.lower().split())
    # Durations can also ride in the name, for chats without the newer parameters:
    # 'poisoned for 3 rounds' or 'frightened, save wis 14'.
    m = re.search(r"[,;]?\s*\(?for (\d+) rounds?\)?", c)
    if m:
        rounds, c = rounds or int(m.group(1)), (c[:m.start()] + c[m.end():]).strip(" ,;")
    m = re.search(r"[,;]?\s*\(?save (str|dex|con|int|wis|cha)\w*,? (?:dc ?)?(\d+)\)?", c)
    if m:
        save_ability, save_dc = save_ability or m.group(1), save_dc or int(m.group(2))
        c = (c[:m.start()] + c[m.end():]).strip(" ,;")
    if not c:
        raise GameError("Name the condition.")
    if not active:
        _drop_condition(e, c)
        _put(campaign, "entity", e["id"], e)
        return {"target": _public(e), "removed": c}
    if c.split()[0] == "exhaustion":
        _drop_condition(e, "exhaustion")  # one level at a time on file
        if _exhaustion({"conditions": [c]}) >= 6:
            e["dead"] = True
    if c not in e["conditions"]:
        e["conditions"].append(c)
    info = {k: v for k, v in {"rounds": int(rounds), "save_ability": save_ability.lower()[:3],
                              "save_dc": int(save_dc), "source": source}.items() if v}
    e.setdefault("condition_info", {})[c] = info
    if _conds(e) & _NO_ACTIONS and e.get("concentrating"):
        info["note"] = f"Concentration on {e['concentrating']} ends."
        e["concentrating"] = ""
    _put(campaign, "entity", e["id"], e)
    _log(campaign, "condition", f"{e['name']} is {c}" + (f" for {rounds} rounds" if rounds else "")
         + (f" ({source})" if source else ""))
    out = {"target": _public(e), **_condition_line(e, c)}
    if not rounds and not save_dc and c.split()[0] not in ("exhaustion", "unconscious"):
        out["lasts"] = ("Until you remove it. To have the engine end it, set rounds or a repeated save "
                        "(or name it like 'poisoned for 3 rounds' or 'frightened, save wis 14').")
    if info.get("note"):
        out["note"] = info["note"]
    if e.get("dead"):
        out["note"] = "Exhaustion level 6: dead."
    return out


def _end_of_turn(campaign: str, e: dict) -> list[str]:
    """Count down this creature's conditions as its turn ends. Returns what the Narrator must report or do."""
    events = []
    for c in list(e.get("conditions", [])):
        info = e.get("condition_info", {}).get(c)
        if not info:
            continue
        if info.get("save_dc"):
            ab, dc = info["save_ability"], info["save_dc"]
            if e["kind"] == "pc":
                events.append(f"{e['name']} may shake off '{c}': the player rolls a {ab.upper()} save, DC {dc}. "
                              f"Call saving_throw with player_total and ends_condition '{c}'.")
            elif ab in ("str", "dex") and _conds(e) & _FAILS_STR_DEX:
                events.append(f"{e['name']} cannot shake off '{c}' (fails {ab.upper()} saves in this state).")
            else:
                nat, _ = _d20()
                total = nat + int(e.get("saves", {}).get(ab, 0)) - 2 * _exhaustion(e)
                if total >= dc:
                    _drop_condition(e, c)
                    events.append(f"{e['name']} rolls {total} against DC {dc} and shakes off '{c}'.")
                    continue
                events.append(f"{e['name']} rolls {total} against DC {dc} and is still {c}.")
        if info.get("rounds"):
            info["rounds"] -= 1
            if info["rounds"] <= 0:
                _drop_condition(e, c)
                events.append(f"'{c}' ends on {e['name']}.")
    if events:
        _put(campaign, "entity", e["id"], e)
    return events


def _expire_timed_conditions(campaign: str) -> list[str]:
    """Outside a fight, anything counted in rounds is over within a minute."""
    ended = []
    for id, e in _all(campaign, "entity").items():
        timed = [c for c, i in e.get("condition_info", {}).items() if i.get("rounds")]
        for c in timed:
            _drop_condition(e, c)
            ended.append(f"'{c}' ends on {e['name']}.")
        if timed:
            _put(campaign, "entity", id, e)
    return ended


def _adjacent(campaign: str, a: dict, t: dict) -> bool | None:
    """True if the two stand within 5 ft on the battle map, None if the map does not say."""
    s = _get(campaign, "combat", "combat")
    m = _get(campaign, "map", "map")
    if not s or not s.get("active") or not m:
        return None
    pa, pt = m["positions"].get(a["id"]), m["positions"].get(t["id"])
    if pa is None or pt is None:
        return None
    return max(abs(pa[0] - pt[0]), abs(pa[1] - pt[1])) <= 1


def _attack_mode(campaign: str, a: dict, t: dict, asked: str = "none") -> tuple[str, list[str], bool, list[str]]:
    """Work out advantage or disadvantage from conditions. Returns (mode, reasons, auto_crit, notes)."""
    ca, ct = _conds(a), _conds(t)
    adv, dis, notes = [], [], []
    if asked == "advantage":
        adv.append("as called")
    if asked == "disadvantage":
        dis.append("as called")
    dis += [f"{a['name']} is {c}" for c in sorted(ca & _OWN_ATTACKS_DISADV)]
    if "invisible" in ca:
        adv.append(f"{a['name']} is invisible")
    if "grappled" in ca:
        src = a.get("condition_info", {}).get("grappled", {}).get("source", "")
        if src and _slug(src) != t["id"]:
            dis.append(f"{a['name']} is grappled by {src}")
        elif not src:
            notes.append(f"{a['name']} is grappled: disadvantage unless the target is the grappler.")
    adv += [f"{t['name']} is {c}" for c in sorted(ct & _ATTACKED_WITH_ADV)]
    if "invisible" in ct:
        dis.append(f"{t['name']} is invisible")
    near = _adjacent(campaign, a, t)
    if "prone" in ct or "unconscious" in ct:
        if "unconscious" in ct:
            if near is False:
                dis.append(f"{t['name']} is down and more than 5 ft away")
        elif near is True:
            adv.append(f"{t['name']} is prone and within 5 ft")
        elif near is False:
            dis.append(f"{t['name']} is prone and more than 5 ft away")
        else:
            notes.append(f"{t['name']} is prone: advantage from within 5 ft, disadvantage from further away.")
    auto_crit = bool(ct & _CRIT_WHEN_ADJACENT) and near is True
    if ct & _CRIT_WHEN_ADJACENT and near is None:
        notes.append(f"{t['name']} is helpless: a hit from within 5 ft is a critical hit.")
    mode = "advantage" if adv and not dis else "disadvantage" if dis and not adv else "none"
    return mode, adv + dis, auto_crit, notes


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

def _land(campaign: str, a: dict, t: dict, atk: dict, crit: bool, out: dict,
          halve: bool = False, reduce: int = 0) -> dict:
    dmg = roll_expr(atk["damage"], crit=crit)
    out["damage_roll"] = dmg["detail"]
    amount = dmg["total"]
    if halve:
        amount //= 2
        out["damage_roll"] += f", halved to {amount}"
    if reduce:
        amount = max(0, amount - int(reduce))
        out["damage_roll"] += f", reduced by {int(reduce)} to {amount}"
    out.update(apply_damage(campaign, t["id"], amount, atk.get("type", ""), a["name"], crit=crit))
    if atk.get("rider"):
        out["rider"] = atk["rider"]
    return out


# Reactions a character can use against an attack that has hit them. Recognised by name on any sheet.
# effect: ac_bonus (turns the hit if the bonus is enough), roll_penalty (the player rolls a die that is
# subtracted from the attack roll), disadvantage (the engine rolls again and the lower die stands),
# halve_damage, reduce_damage (the player rolls the reduction).
REACTION_CATALOGUE: list[tuple[str, dict]] = [
    (r"^shield$", {"name": "Shield", "effect": "ac_bonus", "ac_bonus": 5, "until_next_turn": True,
                   "cost": "a 1st-level spell slot (or a free casting, if the sheet has one)"}),
    (r"^cutting words$", {"name": "Cutting Words", "effect": "roll_penalty", "die": "1d6",
                          "resource_like": "bardic inspiration", "cost": "one Bardic Inspiration die"}),
    (r"^lucky$", {"name": "Lucky", "effect": "disadvantage", "uses_reaction": False,
                  "resource_like": "luck", "cost": "one Luck Point"}),
    (r"^uncanny dodge$", {"name": "Uncanny Dodge", "effect": "halve_damage", "cost": "nothing but the reaction"}),
    (r"^defensive duelist$", {"name": "Defensive Duelist", "effect": "ac_bonus", "ac_bonus": "proficiency",
                              "until_next_turn": True, "melee_only": True, "cost": "nothing but the reaction"}),
    (r"^parry$", {"name": "Parry", "effect": "ac_bonus", "ac_bonus": "proficiency", "melee_only": True,
                  "cost": "nothing but the reaction"}),
    (r"^deflect (attacks?|missiles?)$", {"name": "Deflect Attacks", "effect": "reduce_damage", "die": "1d10",
                                           "cost": "nothing but the reaction"}),
    (r"^warding flare$", {"name": "Warding Flare", "effect": "disadvantage", "resource_like": "warding flare",
                          "cost": "one use of Warding Flare"}),
    (r"^silvery barbs$", {"name": "Silvery Barbs", "effect": "disadvantage", "cost": "a 1st-level spell slot"}),
]


def _strings(x: Any) -> list[str]:
    if isinstance(x, str):
        return [x]
    if isinstance(x, dict):
        return [s for k, v in x.items() for s in ([k] if isinstance(v, (dict, str)) and not str(k).isdigit() else []) + _strings(v)]
    if isinstance(x, (list, tuple)):
        return [s for v in x for s in _strings(v)]
    return []


def _recognise_reactions(e: dict) -> list[str]:
    """Find known reactions among a character's features, feats and spells, and put them on the sheet
    in a form the engine can run. Anything already listed by that name is kept as the player set it."""
    have = e.setdefault("reactions", [])
    for r in have:                                    # the earlier, simpler form
        if "effect" not in r and "ac_bonus" in r:
            r["effect"] = "ac_bonus"
    names = {r["name"].lower() for r in have}
    labels = set()
    for text in _strings([e.get(k) for k in ("features", "feats", "spells", "cantrips", "traits", "monster_reactions")]):
        labels.add(re.split(r"[:(\[,–—]| - ", text, maxsplit=1)[0].strip().lower())
    found = []
    die = re.search(r"\bd(6|8|10|12)\b", " ".join(k for k in e.get("resources", {}) if "inspiration" in k.lower())
                    + " " + " ".join(t for t in _strings(e.get("features")) if "inspiration" in t.lower()))
    for pattern, template in REACTION_CATALOGUE:
        if template["name"].lower() in names or not any(re.match(pattern, lab) for lab in labels):
            continue
        r = dict(template)
        like = r.pop("resource_like", None)
        if like:
            key = next((k for k in e.get("resources", {}) if like in k.lower()), None)
            if key:
                r["resource"] = key
        if r["name"] == "Cutting Words" and die:
            r["die"] = f"1d{die.group(1)}"
        if r.get("ac_bonus") == "proficiency":
            r["ac_bonus"] = int(e.get("proficiency_bonus", 2))
        if r["name"] == "Deflect Attacks":
            r["die"] = f"1d10+{_mod(e.get('abilities', {}).get('dex', 10)) + int(e.get('level', 1) or 1)}"
        have.append(r)
        found.append(r["name"])
    if not have:
        e.pop("reactions")
    return found


def _reaction_options(t: dict, atk: dict, st: dict, skip: list[str]) -> list[dict]:
    """Which of the target's reactions could change this hit. st: nat, total, ac, mode."""
    if t["kind"] != "pc" or t["hp"] <= 0 or _conds(t) & _NO_ACTIONS:
        return []
    ranged = str(atk.get("reach", "")).startswith("range")
    out = []
    for r in t.get("reactions", []):
        name, effect = r["name"], r.get("effect", "ac_bonus")
        if name in skip or (r.get("uses_reaction", True) and t.get("reaction_used")):
            continue
        if r.get("melee_only") and ranged:
            continue
        res = t.get("resources", {}).get(r.get("resource", ""))
        if r.get("resource") and (not res or res.get("current", 0) < 1):
            continue
        line = {"name": name, "cost": r.get("cost", "the reaction")}
        if effect == "ac_bonus":
            bonus = int(r.get("ac_bonus", 0))
            if st["nat"] == 20 or st["total"] >= st["ac"] + bonus:
                continue
            line["does"] = f"+{bonus} AC: turns this hit into a miss" + (
                ", and lasts until the start of their next turn" if r.get("until_next_turn") else "")
        elif effect == "roll_penalty":
            need = st["total"] - st["ac"] + 1
            top = _die_max(r.get("die", "1d6"))
            if st["nat"] == 20 or need > top:
                continue
            line["does"] = f"the player rolls {r.get('die', '1d6')} and subtracts it from the attack: {need} or more turns the hit"
            line["needs_roll"] = r.get("die", "1d6")
        elif effect == "disadvantage":
            if st["mode"] == "disadvantage":
                continue
            line["does"] = "the attacker rolls again and the lower die stands"
        elif effect == "halve_damage":
            line["does"] = "the damage from this hit is halved"
        elif effect == "reduce_damage":
            line["does"] = f"the player rolls {r.get('die', '1d10')} and the damage is reduced by that much"
            line["needs_roll"] = r.get("die", "1d10")
        else:
            continue
        if r.get("auto"):
            line["auto"] = True
        out.append(line)
    return out


def _die_max(expr: str) -> int:
    total = 0
    for sign, n, sides, flat in re.findall(r"([+-]?)\s*(?:(\d*)d(\d+)|(\d+))", expr.replace(" ", "")):
        v = int(n or 1) * int(sides) if sides else int(flat)
        total += -v if sign == "-" else v
    return total


def _pause(campaign: str, a: dict, t: dict, atk: dict, st: dict, options: list[dict], out: dict) -> dict:
    _put(campaign, "attack", "pending", {"attacker": a["id"], "target": t["id"], "atk": atk, "state": st,
                                          "roll": out["roll"], "used": st.get("used", [])})
    names = [o["name"] for o in options]
    out.update({"pending": True, "hit": True, "reactions_available": options,
                "instruction": (f"STOP before any damage. Tell the player: {a['name']} hits with a {st['total']}. "
                                f"They may respond with {' or '.join(names)}: say what each would do and cost. "
                                "Ask what they do. Then call resolve_attack with use_reaction set to the name "
                                "they chose (and roll set to their die, if it needs one), or empty to take "
                                "the hit. If you do not have resolve_attack, call creature_attack again with "
                                "attack_name 'RESOLVE' (take the hit), 'RESOLVE Shield', or 'RESOLVE Cutting "
                                "Words 5' (name, then their die).")})
    return out


def _finish(campaign: str, a: dict, t: dict, atk: dict, st: dict, out: dict,
            halve: bool = False, reduce: int = 0) -> dict:
    hit = st["nat"] == 20 or (st["nat"] != 1 and st["total"] >= st["ac"])
    out.update({"hit": hit, "critical": bool(hit and st.get("crit"))})
    if hit:
        _land(campaign, a, t, atk, out["critical"], out, halve=halve, reduce=reduce)
    else:
        out["target"] = _public(_entity(campaign, t["id"]))
    _log(campaign, "attack", f"{a['name']} {atk['name']} vs {t['name']}: {out['roll']} {'HIT' if hit else 'miss'}"
         + (f" (after {', '.join(st.get('used', []))})" if st.get("used") else ""))
    out["instruction"] = ("Report this result as rolled, including the total to hit when the target is the "
                          "player. Do not soften, reroll or reinterpret it.")
    return out


def resolve_attack(campaign: str, use_reaction: str = "", roll: int = 0) -> dict:
    """Finish an attack that was paused so the player could decide on a reaction. roll: the player's
    die, for reactions that need one."""
    p = _get(campaign, "attack", "pending")
    if not p:
        raise GameError("No attack is waiting on the player.")
    a, t = _entity(campaign, p["attacker"]), _entity(campaign, p["target"])
    atk, st = p["atk"], p["state"]
    out: dict[str, Any] = {"attacker": a["name"], "attack": atk["name"], "roll": p["roll"]}
    if not use_reaction:
        _put(campaign, "attack", "pending", None)
        return _finish(campaign, a, t, atk, st, out)
    m = re.match(r"^(.*?)[\s:]+(\d+)$", use_reaction.strip())       # 'Cutting Words 5'
    if m and not roll:
        use_reaction, roll = m.group(1), int(m.group(2))
    r = next((x for x in t.get("reactions", []) if x["name"].lower() == use_reaction.strip().lower()), None)
    offered = {o["name"]: o for o in _reaction_options(t, atk, st, st.get("used", []))}
    if r is None or r["name"] not in offered:
        raise GameError(f"'{use_reaction}' cannot be used on this attack. Available: "
                        f"{', '.join(offered) or 'nothing'}. Call resolve_attack with one of those, or empty.")
    if offered[r["name"]].get("needs_roll") and int(roll) < 1:
        raise GameError(f"{r['name']} needs the player's roll of {offered[r['name']]['needs_roll']}. Ask for "
                        "it, then call again with roll set.")
    _put(campaign, "attack", "pending", None)
    effect = r.get("effect", "ac_bonus")
    st.setdefault("used", []).append(r["name"])
    out["reaction"] = r["name"]
    if r.get("uses_reaction", True):
        t["reaction_used"] = True
    if r.get("resource"):
        res = t["resources"][r["resource"]]
        res["current"] -= 1
        out["spent"] = f"1 {r['resource']} ({res['current']} left)"
    else:
        out["spend"] = f"Now spend its cost: {r.get('cost', 'the reaction')}. Use spend_slot or use_resource."
    halve, reduce = False, 0
    if effect == "ac_bonus":
        st["ac"] += int(r.get("ac_bonus", 0))
        if r.get("until_next_turn"):
            t["ac_until_turn"] = t.get("ac_until_turn", 0) + int(r.get("ac_bonus", 0))
            out["lasts"] = f"{t['name']} keeps +{r['ac_bonus']} AC until the start of their next turn."
    elif effect == "roll_penalty":
        st["total"] -= int(roll)
        out["roll"] += f", minus {int(roll)} ({r['name']}) = {st['total']}"
    elif effect == "disadvantage":
        second = _rng.randint(1, 20)
        low = min(st["nat"], second)
        st["total"] += low - st["nat"]
        st["crit"] = st.get("crit") and low == 20
        out["roll"] += f"; rolled again for {r['name']}: {second}. The lower die, {low}, stands: {st['total']}"
        st["nat"], st["mode"] = low, "disadvantage"
    elif effect == "halve_damage":
        halve = True
    elif effect == "reduce_damage":
        reduce = int(roll)
    _put(campaign, "entity", t["id"], t)
    still_hit = st["nat"] == 20 or (st["nat"] != 1 and st["total"] >= st["ac"])
    if still_hit and not halve and not reduce:
        more = [o for o in _reaction_options(t, atk, st, st["used"]) if not o.get("auto")]
        if more:
            out["note"] = f"It still hits after {r['name']}."
            return _pause(campaign, a, t, atk, st, more, out)
    return _finish(campaign, a, t, atk, st, out, halve=halve, reduce=reduce)


def creature_attack(campaign: str, attacker: str, target: str, attack_name: str = "",
                    advantage: str = "none") -> dict:
    """The engine rolls a non-player attack and applies the damage. The Narrator only reports it."""
    if attack_name.upper().startswith("RESOLVE"):
        return resolve_attack(campaign, attack_name[7:].strip(" :"))
    attack_name = attack_name.strip()
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
    helpless = sorted(_conds(a) & _NO_ACTIONS)
    if helpless:
        raise GameError(f"{a['name']} is {helpless[0]} and cannot act this turn.")
    atk = next((x for x in a["attacks"] if x["name"].lower() == attack_name.lower()), a["attacks"][0])
    mode, why, auto_crit, notes = _attack_mode(campaign, a, t, advantage)
    nat, detail = _d20(mode)
    tired = 2 * _exhaustion(a)
    total = nat + int(atk.get("to_hit", 0)) - tired
    cover, cover_why = _cover(campaign, a, t)
    ac = t["ac"] + cover + int(t.get("ac_until_turn", 0))
    hit = nat == 20 or (nat != 1 and total >= ac)
    crit = nat == 20 or (hit and auto_crit)
    out: dict[str, Any] = {
        "attacker": a["name"], "attack": atk["name"],
        "roll": f"{detail} + {atk.get('to_hit', 0)}" + (f" - {tired} exhaustion" if tired else "") + f" = {total}",
        "hit": hit, "critical": crit,
    }
    if cover:
        out["cover"] = f"{t['name']} has {cover_why}."
    if t.get("ac_until_turn"):
        out["warded"] = f"{t['name']} has +{t['ac_until_turn']} AC until the start of their next turn."
    if why:
        out["rolled_with"] = f"{mode if mode != 'none' else 'a straight roll (advantage and disadvantage cancel)'}: " + "; ".join(why)
    if notes:
        out["check"] = notes
    if crit and nat != 20:
        out["critical_because"] = f"{t['name']} is helpless and the attacker is within 5 ft."
    if earlier:
        out["earlier_attack_applied"] = earlier
    if t["kind"] == "pc":
        out["tell_player"] = f"{a['name']} rolls {total} to hit."
    st = {"nat": nat, "total": total, "ac": ac, "mode": mode, "crit": crit, "used": []}
    if hit:
        options = _reaction_options(t, atk, st, [])
        auto = next((o for o in options if o.get("auto") and not o.get("needs_roll")), None)
        if auto:                                         # a standing order from the player
            _put(campaign, "attack", "pending", {"attacker": a["id"], "target": t["id"], "atk": atk,
                                                  "state": st, "roll": out["roll"], "used": []})
            done = resolve_attack(campaign, auto["name"])
            done["standing_order"] = f"{auto['name']} was used automatically, as the player asked."
            return {**out, **done}
        if options:
            return _pause(campaign, a, t, atk, st, options, out)
    return _finish(campaign, a, t, atk, st, out)


def player_attack(campaign: str, attacker: str, target: str, attack_total: int,
                  natural_roll: int, damage: int, damage_type: str = "") -> dict:
    """The player rolled. The engine checks it against the target's AC and applies the damage."""
    a, t = _entity(campaign, attacker), _entity(campaign, target)
    crit = natural_roll == 20
    cover, cover_why = _cover(campaign, a, t)
    hit = crit or (natural_roll != 1 and attack_total >= t["ac"] + cover)
    out: dict[str, Any] = {"attacker": a["name"], "hit": hit, "critical": crit}
    if cover:
        out["cover"] = f"{t['name']} has {cover_why}."
    mode, why, auto_crit, notes = _attack_mode(campaign, a, t)
    if hit:
        out.update(apply_damage(campaign, t["id"], damage, damage_type, a["name"], crit=crit))
        if crit:
            out["note_crit"] = "Natural 20. Make sure the damage the player reported used doubled dice."
        elif auto_crit:
            out["note_crit"] = (f"{t['name']} is helpless and within 5 ft, so this hit is a critical. Ask the "
                                "player to roll the damage dice again and add that with apply_damage.")
    else:
        out["target"] = _public(t)
    if why and mode != "none":
        out["should_have_rolled_with"] = f"{mode}: " + "; ".join(why)
    if notes:
        out["check"] = notes
    return out


def saving_throw(campaign: str, who: str, ability: str, dc: int,
                 player_total: int | None = None, advantage: str = "none",
                 ends_condition: str = "") -> dict:
    """A saving throw. ends_condition: a condition on this creature that a success removes."""
    e = _entity(campaign, who)
    ab = ability.lower()[:3]

    def done(out: dict) -> dict:
        if ends_condition:
            if out["success"] and _drop_condition(e, ends_condition):
                _put(campaign, "entity", e["id"], e)
                out["ended"] = ends_condition
            elif not out["success"]:
                out["still"] = ends_condition
            out["target"] = _public(e)
        _log(campaign, "save", f"{e['name']} {ability} save DC {dc}: {out.get('total', 'fails')} "
                               f"{'success' if out['success'] else 'failure'}")
        return out

    if ab in ("str", "dex") and _conds(e) & _FAILS_STR_DEX:
        state = sorted(_conds(e) & _FAILS_STR_DEX)[0]
        return done({"who": e["name"], "dc": dc, "success": False,
                     "note": f"{e['name']} is {state} and fails this save without a roll."})
    notes = []
    if ab == "dex" and "restrained" in _conds(e):
        if advantage == "none":
            advantage = "disadvantage"
        notes.append(f"{e['name']} is restrained: disadvantage on Dexterity saves.")
    if e["kind"] == "pc":
        if player_total is None:
            raise GameError("Ask the player to roll this save, then call again with player_total."
                            + (" " + notes[0] if notes else "")
                            + (f" Exhaustion: -{2 * _exhaustion(e)} on the roll." if _exhaustion(e) else ""))
        out = {"who": e["name"], "total": player_total, "dc": dc, "success": player_total >= dc}
        if notes:
            out["check"] = notes
        return done(out)
    nat, detail = _d20(advantage)
    bonus = int(e.get("saves", {}).get(ab, 0)) - 2 * _exhaustion(e)
    total = nat + bonus
    out = {"who": e["name"], "roll": f"{detail} + {bonus} = {total}", "total": total, "dc": dc,
           "success": total >= dc}
    if notes:
        out["check"] = notes
    return done(out)


def death_save(campaign: str, pc: str, natural_roll: int) -> dict:
    e = _entity(campaign, pc)
    if e["kind"] not in ("pc", "ally") or e["hp"] > 0 or e.get("dead") or e.get("stable"):
        raise GameError(f"{e['name']} is not making death saves right now.")
    d = e.setdefault("death", {"successes": 0, "failures": 0})
    events = []
    if natural_roll == 20:
        e["hp"], e["death"] = 1, {"successes": 0, "failures": 0}
        _drop_condition(e, "unconscious")
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
            events.append(f"Third failure: {e['name']} dies. This is final unless the fiction provides a real way back.")
        elif d["successes"] >= 3:
            e["stable"] = True
            events.append("Third success: stable at 0 HP, still unconscious.")
    _put(campaign, "entity", e["id"], e)
    _log(campaign, "death-save", f"{e['name']} rolls {natural_roll}: {e['death']} {' '.join(events)}")
    return {"pc": _public(e), "roll": natural_roll, "death_saves": e["death"], "events": events}


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
    leaving = _entity(campaign, s["order"][s["turn"]]["id"])
    ended = [] if leaving.get("dead") else _end_of_turn(campaign, leaving)
    for _ in range(len(s["order"]) + 1):
        s["turn"] += 1
        if s["turn"] >= len(s["order"]):
            s["turn"], s["round"] = 0, s["round"] + 1
        e = _entity(campaign, s["order"][s["turn"]]["id"])
        if e.get("dead"):
            continue
        if e["hp"] > 0 or e["kind"] == "pc" or (e["kind"] == "ally" and not e.get("stable")):
            break
    _put(campaign, "combat", "combat", s)
    if e.get("reaction_used") or e.get("ac_until_turn"):
        e["reaction_used"] = False
        e.pop("ac_until_turn", None)
        _put(campaign, "entity", e["id"], e)
    out: dict[str, Any] = {"round": s["round"], "now_acting": _public(e)}
    if ended:
        out["end_of_last_turn"] = ended
    if e["conditions"]:
        out["conditions_now"] = [_condition_line(e, c) for c in e["conditions"]]
    cannot = sorted(_conds(e) & _NO_ACTIONS)
    if e["kind"] == "pc":
        out.update(battle_map(campaign))
        out["draw_grid"] = ("It is the player's turn. First report every turn taken since their last one, a "
                            "line each. Then show the battle grid as map.how_to_show says. Then ask what they do.")
        notes = _turn_notes(campaign, e, s)
        if notes:
            out["tell_player_before_they_roll"] = notes
    if e["kind"] == "pc" and e["hp"] <= 0 and not e.get("stable"):
        out["instruction"] = "The player rolls a death save now. Call death_save with the natural roll."
    elif e["kind"] == "pc" and cannot and e["hp"] > 0:
        out["instruction"] = (f"The character is {cannot[0]} and cannot act. Say so, describe what they "
                              "perceive, then call next_turn. Any save to end it comes at the end of this turn.")
    elif e["kind"] == "ally" and e["hp"] <= 0:
        out["death_save"] = death_save(campaign, e["id"], _rng.randint(1, 20))
        out["now_acting"] = out["death_save"]["pc"]
        out["instruction"] = ("This companion is dying. The engine has rolled their death save: report it in "
                              "one line, exactly, then call next_turn.")
    elif e["kind"] != "pc" and cannot:
        out["instruction"] = (f"{e['name']} is {cannot[0]} and loses this turn. One line, then call next_turn.")
    elif e["kind"] == "ally":
        out["attacks_available"] = [a["name"] for a in e["attacks"]]
        out["instruction"] = ("This is a companion, and you play them. Load their card if you have not. They act "
                              "on what they want and fear, not on what is best for the player, and they do not "
                              "wait to be told. Resolve attacks with creature_attack. One line of narration.")
    else:
        out["attacks_available"] = [a["name"] for a in e["attacks"]]
        if e.get("multiattack"):
            out["routine"] = e["multiattack"]
        extras = [x["name"] for key in ("other_actions", "bonus_actions", "legendary_actions") for x in e.get(key, [])]
        if extras:
            out["also_on_its_sheet"] = extras
        out["instruction"] = ("Decide what this creature does from its own goals, then resolve attacks with "
                              "creature_attack. Enemies fight to win and target whoever it makes sense to target.")
    return out


def _turn_notes(campaign: str, pc: dict, s: dict) -> list[str]:
    """What the player needs to know before rolling this turn: how conditions change their dice."""
    notes = []
    mine = _conds(pc)
    for c in sorted(mine & _OWN_ATTACKS_DISADV):
        notes.append(f"{pc['name']} is {c}: disadvantage on attack rolls"
                     + (" and ability checks." if c in ("poisoned", "frightened") else "."))
    if "invisible" in mine:
        notes.append(f"{pc['name']} is invisible: advantage on attack rolls.")
    if _exhaustion(pc):
        notes.append(f"Exhaustion {_exhaustion(pc)}: -{2 * _exhaustion(pc)} on every d20 roll.")
    for o in s["order"]:
        t = _entity(campaign, o["id"])
        if t["id"] == pc["id"] or t.get("dead") or t["hp"] <= 0:
            continue
        mode, why, crit, extra = _attack_mode(campaign, pc, t)
        theirs = [w for w in why if w.startswith(t["name"])]
        if theirs and mode != "none":
            notes.append(f"Attacks on {t['name']} are rolled with {mode} ({'; '.join(theirs)}).")
        elif theirs:
            notes.append(f"Attacks on {t['name']}: advantage and disadvantage cancel ({'; '.join(why)}).")
        notes += [x for x in extra if x.startswith(t["name"])]
    return notes


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
    pc_id = next((id for id in m["positions"] if (_get(campaign, "entity", id) or {}).get("kind") == "pc"), None)
    if pc_id:
        pc = _get(campaign, "entity", pc_id)
        for t in tokens:
            if t["kind"] != "pc":
                t["feet_from_you"] = _feet(m["positions"][pc_id], [t["x"], t["y"]])
                bonus, why = _cover(campaign, pc, _get(campaign, "entity", _slug(t["name"])))
                if bonus:
                    t["cover_from_you"] = why
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
    if outgoing <= 0:
        out["warning"] = ("No attacks are written on the player's side's sheets, so this rating assumes they deal "
                          "no damage and is not meaningful. Add the character's attacks with save_character first.")
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
    is_new = _get(campaign, "card", id) is None
    c = _get(campaign, "card", id, {"name": name, "attitude": "neutral", "history": []})
    c.update({k: v for k, v in card.items() if k != "history"})
    if is_new and "attitude" not in card:             # a new member starts where their faction stands
        faction = _faction_of(campaign, c)
        if faction:
            c["attitude"] = faction["attitude"]
    if c["attitude"] not in ATTITUDES:
        raise GameError(f"attitude must be one of: {', '.join(ATTITUDES)}.")
    _put(campaign, "card", id, c)
    return c


def get_card(campaign: str, name: str) -> dict:
    c = _get(campaign, "card", _slug(name))
    if c is None:
        known = ", ".join(v["name"] for v in _all(campaign, "card").values()) or "none yet"
        raise GameError(f"No card for '{name}'. Known: {known}. Create one with save_card before voicing them.")
    out = {**c, "instruction": "Play the want, the voice and the secret. Never state the secret outright."}
    faction = _faction_of(campaign, c)
    if faction:
        out["faction_standing"] = {"faction": faction["name"], "attitude": faction["attitude"],
                                   "wants": faction.get("want", "")}
    if c.get("kind") == "faction":
        out["members"] = [f"{x['name']} ({x['attitude']})" for x in _members(campaign, c)]
        out["instruction"] = ("A group, not a person. Its attitude is where its members start, and how its "
                              "strangers treat the character.")
    mem = [_jline(e) for e in _journal(campaign) if _about(e, c["name"])][-8:]
    if mem:
        out["remembers_from_play"] = mem
    return out


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
    out = {"name": c["name"], "attitude": c["attitude"], "remembers": c["history"]}
    if c.get("kind") == "faction":                    # members with no history of their own move with the group
        moved = []
        for mem in _members(campaign, c):
            if not mem.get("history"):
                j = max(0, min(len(ATTITUDES) - 1, ATTITUDES.index(mem["attitude"]) + steps))
                mem["attitude"] = ATTITUDES[j]
                _put(campaign, "card", _slug(mem["name"]), mem)
                moved.append(mem["name"])
        out["members_who_followed"] = moved
        out["note"] = "Word travels. Members the character has dealt with personally keep their own view."
    return out


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
    out = {"world_hours": round(m["world_hours"], 1), "consequences": fired,
           "instruction": _consequence_rule(fired)}
    ended = _expire_timed_conditions(campaign) if hours >= 0.02 else []
    if ended:
        out["conditions_ended"] = ended
    return out


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
            tired = _exhaustion(e)
            e["conditions"], e["condition_info"] = [], {}
            if tired > 1:
                e["conditions"].append(f"exhaustion {tired - 1}")
            e["stable"] = False
            e["death"] = {"successes": 0, "failures": 0}
            restored.append(e["name"])
        _put(campaign, "entity", id, e)
    out = advance_time(campaign, 8 if kind == "long" else 1, f"{kind} rest")
    out["restored"] = restored
    if kind == "short":
        out["note"] = ("Short rest: features that recharge on a short rest are restored. The player spends "
                       "hit dice and rolls them; record the result with heal.")
    return out


# ------------------------------------------------------ character library
#
# One master sheet per player character, kept apart from every campaign. A new campaign takes a rested
# copy. The master changes only when the player says so, so a death or a bad turn in one game never
# reaches the library by accident.

LIBRARY = "~library"
_PLAY_STATE = ("_from", "dead", "stable", "condition_info", "concentrating", "library")


def _fresh(sheet: dict) -> dict:
    """A copy of a sheet with everything from play wiped: full hit points, slots and resources."""
    e = json.loads(json.dumps({k: v for k, v in sheet.items() if k not in _PLAY_STATE}))
    e.update({"kind": "pc", "hp": e["max_hp"], "temp_hp": 0, "conditions": [],
              "death": {"successes": 0, "failures": 0}})
    if "max_slots" in e:
        e["slots"] = dict(e["max_slots"])
    for r in e.get("resources", {}).values():
        r["current"] = r.get("max", 0)
    return e


def _library_line(e: dict) -> dict:
    info = e.get("library", {})
    line = {"name": e["name"], "class": e.get("class", ""), "level": e.get("level", ""),
            "species": e.get("species", ""), "max_hp": e["max_hp"], "ac": e.get("ac", 10),
            "updated": info.get("updated", ""), "from_campaign": info.get("from_campaign", "")}
    if info.get("retired"):
        line["retired"] = True
    return {k: v for k, v in line.items() if v != ""}


def library_save(name: str, sheet: dict | None = None, from_campaign: str = "") -> dict:
    """Put a character in the library, or update the one there. Either pass the sheet (fields merge over
    what is stored) or name a campaign to take that campaign's current version of the character."""
    id = _slug(name)
    if not id:
        raise GameError("Name the character.")
    old = _get(LIBRARY, "character", id)
    if from_campaign:
        src = _get(from_campaign, "entity", id)
        if src is None or src.get("kind") != "pc":
            raise GameError(f"No player character called '{name}' in campaign '{from_campaign}'.")
        if src.get("dead"):
            raise GameError(f"{src['name']} died in '{from_campaign}'. The library copy is unchanged. To stop "
                            "offering this character for new games, use library_retire.")
        new = _fresh(src)
    elif sheet:
        merged = {**(old or {}), **sheet, "id": id, "name": (old or {}).get("name", name)}
        if "max_hp" not in merged:
            raise GameError("The sheet needs max_hp (and ac) at least. Pass the whole sheet the first time.")
        merged.setdefault("ac", 10)
        for key, default in (("attacks", []), ("saves", {}), ("init", 0)):
            merged.setdefault(key, default)
        for r in merged.get("resources", {}).values():
            r.setdefault("recharge", "long")
        new = _fresh(merged)
    else:
        raise GameError("Pass the character's sheet, or from_campaign to copy the version in that campaign.")
    new["library"] = {"updated": time.strftime("%Y-%m-%d", time.gmtime()),
                      "from_campaign": from_campaign or (old or {}).get("library", {}).get("from_campaign", "")}
    if old:
        _put(LIBRARY, "previous", id, old)      # one step of undo
    _put(LIBRARY, "character", id, new)
    _recognise_reactions(new)
    _put(LIBRARY, "character", id, new)
    out = {"saved_to_library": _library_line(new), "fields_on_file": sorted(k for k in new if k != "library")}
    if new.get("reactions"):
        out["reactions_the_engine_will_offer"] = [r["name"] for r in new["reactions"]]
    gaps = sheet_check(new)
    if gaps:
        out["sheet_is_missing"] = gaps
    if old and (str(old.get("level", "")), old["max_hp"]) != (str(new.get("level", "")), new["max_hp"]):
        out["changed"] = (f"Was level {old.get('level', '?')} with {old['max_hp']} hit points; now level "
                          f"{new.get('level', '?')} with {new['max_hp']}.")
    if old:
        out["undo"] = "The earlier version is kept. Call library_restore_previous to put it back."
    return out


def library_restore_previous(name: str) -> dict:
    id = _slug(name)
    prev, cur = _get(LIBRARY, "previous", id), _get(LIBRARY, "character", id)
    if not prev:
        raise GameError(f"No earlier version of '{name}' is kept.")
    _put(LIBRARY, "character", id, prev)
    _put(LIBRARY, "previous", id, cur)
    return {"restored": _library_line(prev)}


def character_library(name: str = "") -> dict:
    """List the characters in the library, or give one character's full sheet."""
    if name:
        e = _get(LIBRARY, "character", _slug(name))
        if e is None:
            known = ", ".join(c["name"] for c in _all(LIBRARY, "character").values() if c) or "none yet"
            raise GameError(f"No character called '{name}' in the library. In it: {known}.")
        playing = [c for c in _campaign_names() if (_get(c, "entity", e["id"]) or {}).get("kind") == "pc"]
        return {"character": e, "in_campaigns": playing}
    chars = [_library_line(e) for e in _all(LIBRARY, "character").values() if e]
    return {"characters": sorted(chars, key=lambda c: c["name"]),
            "note": "Each new campaign takes a fully rested copy. The library copy changes only when the "
                    "player asks, with library_save."}


def library_retire(name: str, retired: bool = True) -> dict:
    """Stop offering a character for new campaigns without deleting them. Reversible."""
    e = _get(LIBRARY, "character", _slug(name))
    if e is None:
        raise GameError(f"No character called '{name}' in the library.")
    e.setdefault("library", {})["retired"] = retired
    _put(LIBRARY, "character", e["id"], e)
    return {"character": e["name"], "retired": retired}


def _library_backfill(campaign: str) -> list[str]:
    """Any player character in this campaign who is not in the library yet is added, so nobody is lost."""
    added = []
    if campaign not in _campaign_names():       # scratch and archived campaigns do not feed the library
        return added
    for e in _all(campaign, "entity").values():
        if e.get("kind") == "pc" and not e.get("dead") and _get(LIBRARY, "character", e["id"]) is None:
            new = _fresh(e)
            new["library"] = {"updated": time.strftime("%Y-%m-%d", time.gmtime()), "from_campaign": campaign}
            _put(LIBRARY, "character", e["id"], new)
            added.append(e["name"])
    return added


def _library_drift(campaign: str) -> str | None:
    """Say so, once per level, when a character has grown past their library copy."""
    m = _meta(campaign)
    for e in _all(campaign, "entity").values():
        lib = _get(LIBRARY, "character", e["id"]) if e.get("kind") == "pc" and not e.get("dead") else None
        if not lib:
            continue
        now, was = (str(e.get("level", "")), e["max_hp"]), (str(lib.get("level", "")), lib["max_hp"])
        if now != was and m.get("library_asked") != list(now):
            m["library_asked"] = list(now)
            _put(campaign, "meta", "meta", m)
            return (f"{e['name']} is level {now[0] or '?'} with {now[1]} hit points in this campaign; the library "
                    f"copy is level {was[0] or '?'} with {was[1]}. Ask the player once whether new campaigns "
                    f"should start from this version. If yes, call library_save with name '{e['name']}' and "
                    f"from_campaign '{campaign}'.")
    return None


# ------------------------------------------------------------ lore shelf
#
# Books and notes for a published or home-made setting, kept per setting and shared by every campaign
# played in it. They live in their own file so game backups stay small.

LORE_PATH = os.path.join(DATA_DIR, "lore.db")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
_STOP = set("a about an and are as at be but by can do does for from has have how i in is it its of on or "
            "rule rules tell that the their there this to was what when where which who whom why will with "
            "work works".split())


def _lore_db() -> tuple[sqlite3.Connection, bool]:
    """Open the shelf. The second value says whether full-text search is available."""
    con = sqlite3.connect(LORE_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS book (shelf TEXT, slug TEXT, data TEXT, PRIMARY KEY (shelf, slug));
        CREATE TABLE IF NOT EXISTS ticket (token TEXT PRIMARY KEY, shelf TEXT, expires REAL);
        """
    )
    try:
        # Book titles and headings are searched too, and count for more than body text.
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5("
                    "shelf UNINDEXED, book, place, text, tokenize='porter unicode61')")
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='lore'").fetchone():   # the first layout
            with con:
                con.execute("INSERT INTO passages (shelf, book, place, text) SELECT shelf, book, place, text FROM lore")
                con.execute("DROP TABLE lore")
        return con, True
    except sqlite3.OperationalError:
        con.execute("CREATE TABLE IF NOT EXISTS lore_plain (shelf TEXT, book TEXT, place TEXT, text TEXT)")
        return con, False


def _shelf_name(setting: str) -> str:
    """'Ravenloft: Barovia' and 'Ravenloft - the Mists' share the shelf 'ravenloft'."""
    head = re.split(r"\s+[—–-]\s+|[:(,.;]", setting.strip(), maxsplit=1)[0]
    return _slug(head)[:40]


def _shelf_for(name: str) -> str:
    """Accepts a campaign name or a setting name."""
    n = _get(name, "setting", "notes") if name else None
    if n and n.get("setting"):
        return n.get("shelf") or _shelf_name(n["setting"])
    shelf = _shelf_name(name or "")
    if not shelf:
        raise GameError("Name the campaign or the setting.")
    return shelf


def _tidy(text: str) -> str:
    text = text.replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)          # words split across lines
    text = re.sub(r"[ \t ]+", " ", text)
    return re.sub(r"\s*\n\s*", "\n", text).strip()


def _chunk(parts: list[tuple[str, str]], size: int = 1600, by_section: bool = True) -> list[tuple[str, str]]:
    """Cut (place, text) parts into passages of about `size` characters, breaking at sentence ends.
    by_section starts a new passage at each new place (headings); pages of a PDF run together instead."""
    out, buf, start, last = [], "", "", ""

    def flush() -> None:
        nonlocal buf
        if buf.strip():
            out.append((start if start == last or not last else f"{start} to {last}", buf.strip()))
        buf = ""

    for place, text in parts:
        if by_section and buf and place != last:
            flush()
        for piece in re.split(r"(?<=[.!?])\s+|\n", _tidy(text)):
            piece = piece.strip()
            if not piece:
                continue
            while len(piece) > size:                      # a run with no sentence ends: cut it hard
                if buf:
                    flush()
                start = last = place
                buf = piece[:size]
                flush()
                piece = piece[size:]
            if not buf:
                start = place
            if len(buf) + len(piece) > size:
                flush()
                start = place
            buf += (" " if buf else "") + piece
            last = place
    flush()
    return out


def _store_book(shelf: str, title: str, parts: list[tuple[str, str]], extra: dict | None = None) -> dict:
    title = " ".join(title.split())[:120]
    if not title:
        raise GameError("The book needs a title.")
    by_section = not (extra or {}).get("pages")
    chunks = _chunk(parts, by_section=by_section)
    if not chunks:
        raise GameError(f"No readable text found in '{title}'.")
    slug = _slug(title)
    con, fts = _lore_db()
    table = "passages" if fts else "lore_plain"
    with con:
        con.execute(f"DELETE FROM {table} WHERE shelf=? AND book=?", (shelf, title))
        con.executemany(f"INSERT INTO {table} (shelf, book, place, text) VALUES (?,?,?,?)",
                        [(shelf, title, place, text) for place, text in chunks])
        rec = {"shelf": shelf, "title": title, "passages": len(chunks),
               "characters": sum(len(t) for _, t in chunks), "added": time.strftime("%Y-%m-%d", time.gmtime()),
               **(extra or {})}
        con.execute("INSERT OR REPLACE INTO book VALUES (?,?,?)", (shelf, slug, json.dumps(rec)))
    con.close()
    return rec


_SUBHEADS = {"actions", "traits", "bonus actions", "reactions", "legendary actions", "overview", "description",
             "notes", "tactics", "lair", "history", "appearance", "personality"}


def _sections(text: str) -> list[tuple[str, str]]:
    """Split notes on markdown-style headings so each passage can say where it came from."""
    parts, place, buf = [], "", []
    above: dict[int, str] = {}                           # the latest heading at each level
    for line in text.splitlines():
        m = re.match(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$", line)
        if m:
            if buf:
                parts.append((place, "\n".join(buf)))
            level, head = len(m.group(1)), m.group(2)[:80]
            above = {k: v for k, v in above.items() if k < level}
            if head.lower() in _SUBHEADS and above:      # 'Actions' means little without whose
                place = f"{above[max(above)]}: {head}"[:80]
            else:
                place = head
                above[level] = head
            buf = []
        else:
            buf.append(line)
    if buf:
        parts.append((place, "\n".join(buf)))
    return parts


def lore_add(setting: str, title: str, text: str) -> dict:
    """Put notes or a pasted chapter on a setting's shelf. A book with the same title is replaced."""
    shelf = _shelf_for(setting)
    parts = _sections(text)
    rec = _store_book(shelf, title, parts, {"contents": [p for p, _ in parts if p][:60]})
    return {"saved": rec["title"], "shelf": shelf, "passages": rec["passages"]}


def _strip_tags(html: str) -> str:
    import html as _html
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    html = re.sub(r"(?i)<(br|/p|/div|/h[1-6]|/li|/tr)\b[^>]*>", "\n", html)
    return _html.unescape(re.sub(r"<[^>]+>", " ", html))


def _outline(reader: Any) -> list[str]:
    titles: list[str] = []

    def walk(items: Any, depth: int) -> None:
        for it in items:
            if isinstance(it, list):
                if depth < 2:
                    walk(it, depth + 1)
            elif getattr(it, "title", None) and len(titles) < 80:
                titles.append(("  " * depth) + str(it.title).strip()[:80])

    try:
        walk(reader.outline, 0)
    except Exception:
        pass
    return titles


def lore_add_file(shelf: str, filename: str, path: str) -> dict:
    """Read an uploaded book (PDF, EPUB, Word, text, markdown or HTML) and put it on the shelf."""
    import zipfile
    stem, ext = os.path.splitext(os.path.basename(filename))
    title = re.sub(r"[_\s]+", " ", stem).strip() or "Untitled"
    ext = ext.lower()
    extra: dict[str, Any] = {"file": os.path.basename(filename)[:120]}
    if ext == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            raise GameError("This server cannot read PDFs yet (pypdf is not installed).")
        try:
            reader = PdfReader(path)
            if reader.is_encrypted:
                reader.decrypt("")
            pages = len(reader.pages)
        except Exception as ex:
            raise GameError(f"Could not open '{filename}' as a PDF: {ex}")
        parts, empty = [], 0
        for i, page in enumerate(reader.pages, 1):
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
            if len(text.strip()) < 40:
                empty += 1
            else:
                parts.append((f"p. {i}", text))
        extra.update({"pages": pages, "pages_without_text": empty, "contents": _outline(reader)})
        if empty > pages / 2:
            extra["warning"] = ("Most pages have no text layer, so this looks like a scan. It needs text "
                                "recognition (OCR) before it can be searched properly.")
    elif ext in (".txt", ".md", ".markdown"):
        with open(path, encoding="utf-8", errors="replace") as f:
            parts = _sections(f.read())
        extra["contents"] = [p for p, _ in parts if p][:60]
    elif ext in (".html", ".htm"):
        with open(path, encoding="utf-8", errors="replace") as f:
            parts = [("", _strip_tags(f.read()))]
    elif ext == ".docx":
        try:
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except Exception as ex:
            raise GameError(f"Could not open '{filename}' as a Word file: {ex}")
        xml = re.sub(r"</w:p>", "\n", xml)
        parts = [("", _strip_tags(xml))]
    elif ext == ".epub":
        parts = []
        try:
            with zipfile.ZipFile(path) as z:
                for name in sorted(n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))):
                    raw = z.read(name).decode("utf-8", "replace")
                    head = re.search(r"(?is)<h[1-3][^>]*>(.*?)</h[1-3]>", raw)
                    place = " ".join(_strip_tags(head.group(1)).split())[:80] if head else ""
                    parts.append((place, _strip_tags(raw)))
        except Exception as ex:
            raise GameError(f"Could not open '{filename}' as an EPUB: {ex}")
        extra["contents"] = [p for p, _ in parts if p][:80]
    else:
        raise GameError(f"'{ext or filename}' is not a kind of file the shelf can read. Use PDF, EPUB, Word "
                        "(.docx), text, markdown or HTML.")
    return _store_book(shelf, title, parts, extra)


def _books(shelf: str) -> list[dict]:
    con, _ = _lore_db()
    rows = con.execute("SELECT data FROM book WHERE shelf=? AND slug NOT LIKE '~%' ORDER BY slug",
                       (shelf,)).fetchall()
    con.close()
    return [json.loads(r[0]) for r in rows]


def lore_shelf(name: str) -> dict:
    """What is on the shelf for a campaign's setting (or a named setting)."""
    shelf = _shelf_for(name)
    books = _books(shelf)
    con, _ = _lore_db()
    others = sorted({r[0] for r in con.execute("SELECT DISTINCT shelf FROM book")} - {shelf})
    con.close()
    out: dict[str, Any] = {"shelf": shelf, "books": [
        {k: b[k] for k in ("title", "pages", "passages", "added", "contents", "warning") if b.get(k)}
        for b in books]}
    if not books:
        out["note"] = ("Nothing on this shelf. If the player has books or notes for the setting, call "
                       "lore_upload_link and give them the link, or paste short notes with lore_add.")
    if others:
        out["other_shelves"] = others
    return out


def _shelf_summary(campaign: str) -> dict | None:
    """Short form for briefs: titles only, and how to use them."""
    try:
        books = _books(_shelf_for(campaign))
    except Exception:
        return None
    if not books:
        return None
    return {"books": [b["title"] for b in books],
            "use": ("These books are this setting's canon. Look things up with lore_search before you "
                    "describe or invent anything they might cover. Where the campaign's setting notes "
                    "differ from a book, the setting notes win. If you have no lore_search tool, call "
                    "open_sealed with the key 'search: ' followed by your words.")}


def lore_search(name: str, query: str, book: str = "", limit: int = 5) -> dict:
    """Search the setting's books. name: the campaign (or a setting). Returns the best-matching passages."""
    shelf = _shelf_for(name)
    if not _books(shelf):
        raise GameError(f"There are no books on the '{shelf}' shelf. See lore_shelf.")
    out = _search_shelf(shelf, query, book, limit)
    if out["results"]:
        out["how_to_use"] = ("Reference for you, not text for the player. Say what the character would know or "
                             "can find out, in your own words; never paste or closely paraphrase a passage. "
                             "Books hold secrets the character has not earned: keep those back. Setting notes "
                             "win over the books where they differ.")
    else:
        out["note"] = ("Nothing on the shelf matches. Try a name or a different word. If the books do not "
                       "cover it, decide it yourself in keeping with what they do say.")
    return out


def _search_shelf(shelf: str, query: str, book: str = "", limit: int = 5) -> dict:
    books = _books(shelf)
    title = ""
    if book:
        title = next((b["title"] for b in books if _slug(book) in _slug(b["title"])), "")
        if not title:
            raise GameError(f"No book like '{book}' here. On the shelf: {', '.join(b['title'] for b in books)}.")
    words = [w for w in re.findall(r"[^\W_]{2,}", query.replace("'", "").lower()) if w not in _STOP]
    if not words:
        raise GameError("Give a few distinctive words to search for: a name, a place, a thing.")
    limit = max(1, min(int(limit), 8))
    con, fts = _lore_db()
    if fts:
        terms = [f'"{w}"' for w in words]
        if len(words) > 1:
            terms.insert(0, '"' + " ".join(words) + '"')        # the exact phrase scores on top
        sql = "SELECT book, place, text FROM passages WHERE passages MATCH ? AND shelf=?"
        args: list[Any] = [" OR ".join(terms), shelf]
        if title:
            sql += " AND book=?"
            args.append(title)
        rows = con.execute(sql + " ORDER BY bm25(passages, 0.0, 3.0, 8.0, 1.0) LIMIT ?",
                           args + [limit * 6]).fetchall()
    else:
        sql = "SELECT book, place, text FROM lore_plain WHERE shelf=?" + (" AND book=?" if title else "")
        found = con.execute(sql, [shelf] + ([title] if title else [])).fetchall()
        scored = [(sum(r[2].lower().count(w) + 8 * r[1].lower().count(w) for w in words), r) for r in found]
        rows = [r for n, r in sorted(scored, key=lambda x: -x[0]) if n][:limit * 6]
    con.close()
    # A heading that is exactly what was asked for comes first: 'Fireball' before 'Wand of Fireballs'.
    asked = set(words)

    def exact(row: tuple) -> int:
        plain = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", row[1].lower())      # drop tags like [Condition] and prices
        head = {w for w in re.findall(r"[^\W_]{2,}", plain) if w not in _STOP}
        if not head:
            return 2
        return 0 if head == asked else 1 if head <= asked else 2

    rows = sorted(rows, key=exact)[:limit]
    return {"shelf": shelf, "query": query, "results": [
        {"book": b, "where": place, "text": text[:1700]} for b, place, text in rows]}


def lore_remove(setting: str, title: str) -> dict:
    shelf = _shelf_for(setting)
    hit = next((b for b in _books(shelf) if _slug(title) == _slug(b["title"])), None)
    if not hit:
        raise GameError(f"No book called '{title}' on the '{shelf}' shelf.")
    con, fts = _lore_db()
    with con:
        con.execute(f"DELETE FROM {'passages' if fts else 'lore_plain'} WHERE shelf=? AND book=?", (shelf, hit["title"]))
        con.execute("DELETE FROM book WHERE shelf=? AND slug=?", (shelf, _slug(hit["title"])))
    con.close()
    return {"removed": hit["title"], "shelf": shelf}


def lore_upload_link(setting: str) -> dict:
    """A private, one-hour link to a page where the player can add their own books to a setting's shelf."""
    shelf = _shelf_for(setting)
    token = secrets.token_urlsafe(24)
    con, _ = _lore_db()
    with con:
        con.execute("DELETE FROM ticket WHERE expires < ?", (time.time(),))
        con.execute("INSERT INTO ticket VALUES (?,?,?)", (token, shelf, time.time() + 3600))
    con.close()
    base = os.environ.get("PUBLIC_URL", "").rstrip("/")
    if not base and os.environ.get("RAILWAY_PUBLIC_DOMAIN"):
        base = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    return {"shelf": shelf, "link": f"{base}/upload/{token}", "valid_for": "one hour",
            "tell_player": ("Give the player this link exactly. They open it in a browser and drop in their "
                            "books (PDF, EPUB, Word, text). Anyone with the link can add books for the next "
                            "hour, so it is for them only. When they say it is done, call lore_shelf to check.")}


def upload_ticket(token: str) -> str | None:
    """The shelf an upload link is good for, or None if the link is unknown or has expired."""
    con, _ = _lore_db()
    row = con.execute("SELECT shelf FROM ticket WHERE token=? AND expires>?", (token, time.time())).fetchone()
    con.close()
    return row[0] if row else None


# ------------------------------------------------------ sealed campaign

def seal(campaign: str, key: str, content: str, tags: list[str] | None = None) -> dict:
    """Author-only. Store hidden campaign material: villains, secrets, scenes, twists."""
    _put(campaign, "sealed", _slug(key), {"key": key, "content": content, "tags": tags or []})
    return {"sealed": key, "characters": len(content)}


MISSING_TOOL = ("Claude can take a day to notice tools added to this engine. If a tool named in these "
                "instructions is not in your tool list, reach it through open_sealed with the key "
                "'call: <tool name> <arguments as JSON>', for example: call: encounter_check "
                '{"enemies": ["Raider", "Raider"]}. The campaign is filled in for you.')


def _call_by_name(campaign: str, text: str) -> dict:
    import inspect
    reachable = {f.__name__: f for f in (
        concentrate, encounter_check, resolve_attack, set_condition, saving_throw, lore_search, lore_shelf,
        lore_upload_link, lore_add, lore_remove, list_backups, archive_campaign, campaign_setup_guide,
        create_campaign, use_resource, character_library, library_save, library_retire,
        library_restore_previous, journal_note, journal_search, surprise, area, set_travel_map, travel,
        rules_lookup, monsters, monster_import)}
    m = re.match(r"\s*(\w+)\s*(\{.*\})?\s*$", text, re.S)
    if not m or m.group(1) not in reachable:
        raise GameError("Use 'call: <tool> <JSON arguments>'. Reachable this way: " + ", ".join(sorted(reachable)) + ".")
    fn = reachable[m.group(1)]
    try:
        args = json.loads(m.group(2) or "{}")
    except ValueError as ex:
        raise GameError(f"The arguments are not valid JSON: {ex}")
    params = list(inspect.signature(fn).parameters)
    if "from_campaign" in params and args.get("from_campaign") == "this":
        args["from_campaign"] = campaign
    if params and params[0] in ("campaign", "setting") and params[0] not in args:
        args[params[0]] = campaign
    if fn in (lore_shelf, lore_search) and "name" not in args:
        args["name"] = campaign
    try:
        return fn(**args)
    except TypeError as ex:
        raise GameError(f"Wrong arguments for {m.group(1)}: {ex}. It takes: {', '.join(params)}.")


def sealed_index(campaign: str) -> dict:
    items = _all(campaign, "sealed")
    return {"entries": [{"key": v["key"], "tags": v["tags"]} for v in items.values()]}


def open_sealed(campaign: str, key: str) -> dict:
    low = key.lower().strip()
    if low.startswith("call:"):                   # any newer tool, for chats whose tool list is out of date
        return _call_by_name(campaign, key.strip()[5:])
    if low.startswith("search:"):
        return lore_search(campaign, key.strip()[7:])
    if low == "shelf":
        return lore_shelf(campaign)
    if low == "upload link":
        return lore_upload_link(campaign)
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
    _library_backfill(campaign)
    m = _meta(campaign)
    ents = _all(campaign, "entity")
    return {
        "procedure": AUTHOR,
        "setting_notes": _get(campaign, "setting", "notes") or "None saved yet.",
        "established_in_this_setting": _established(campaign),
        "lore_shelf": _shelf_summary(campaign) or "No books on this setting's shelf.",
        "sheet_check": {e["name"]: sheet_check(e) for e in ents.values() if e["kind"] == "pc" and sheet_check(e)}
                       or "The player character's sheet is complete enough.",
        "journal_open_items": _open_items(campaign, 20),
        "travel_map": _travel(campaign) if _travel(campaign)["places"] else "None yet.",
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
        "if_a_tool_is_missing": MISSING_TOOL,
    }


def _campaign_names(include_hidden: bool = False) -> list[str]:
    """Campaign names. Archived campaigns and scratch ones with no setting are hidden unless asked for."""
    with _db() as con:
        names = [r[0] for r in con.execute("SELECT DISTINCT campaign FROM doc ORDER BY campaign")
                 if not r[0].startswith("~")]
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
    """Characters a new campaign can start with: the library, then any player character found only in
    a campaign (the fullest sheet where there are several)."""
    out: dict[str, dict] = {}
    for c in _campaign_names():
        for e in _all(c, "entity").values():
            if e["kind"] == "pc" and not e.get("dead") and len(e) > len(out.get(e["id"], {})):
                out[e["id"]] = {**e, "_from": c}
    for id, e in _all(LIBRARY, "character").items():
        if not e:
            continue
        if e.get("library", {}).get("retired"):
            out.pop(id, None)
        else:
            out[id] = {**e, "_from": "the library"}
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
        "if_a_tool_is_missing": MISSING_TOOL,
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
            out["character"] = (f"No character called '{character}' in the library. Ask the player for the "
                                "sheet, save it with library_save, then copy it into this campaign by "
                                f"calling save_character('{name}', ...) with the same sheet, before running "
                                "the Author.")
        else:
            e = _fresh(src)
            _put(name, "entity", e["id"], e)
            if _get(LIBRARY, "character", e["id"]) is None:      # found only in an older campaign: keep it
                _put(LIBRARY, "character", e["id"], {**_fresh(src), "library": {
                    "updated": time.strftime("%Y-%m-%d", time.gmtime()), "from_campaign": src["_from"]}})
            out["character"] = f"{e['name']} copied from {src['_from']}, fully rested."
    else:
        out["character"] = ("None yet. Save the player character with library_save, and into this campaign "
                            "with save_character, before running the Author.")
    shelf = _shelf_summary(name)
    out["lore_shelf"] = shelf["books"] if shelf else (
        "No books on this setting's shelf. If the player has said they own books or notes for it, call "
        "lore_upload_link and give them the link before running the Author.")
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
        "cast": [{"name": c["name"], "attitude": c["attitude"]} for c in _all(campaign, "card").values()
                 if c.get("kind") != "faction"],
        "factions": [{"name": c["name"], "attitude": c["attitude"]} for c in _all(campaign, "card").values()
                     if c.get("kind") == "faction"],
        "open_promises_debts_threats": _open_items(campaign),
        "journal": (f"{len(_journal(campaign))} entries. Record what play establishes with journal_note as "
                    "scenes end. Look things up with journal_search rather than trusting your memory of "
                    "earlier sessions."),
        "where": _where_now(campaign) or "No travel map for this campaign.",
        "rules": "The rules as written are on file: rules_lookup for spells, conditions, actions and items.",
        "playbook": PLAYBOOK,
        "grounding": grounding,
        "sheets": ("Full character sheets are on file. Call get_character for skills, saves, spells, features "
                   "and inventory instead of asking the player for them. Spend limited-use features with "
                   "use_resource and spell slots with spend_slot."),
        "common_knowledge": ck["content"] if ck else None,
        "lore_briefs": [v["key"] for v in _all(campaign, "sealed").values() if v["key"].startswith("lore-")],
        "lore_shelf": _shelf_summary(campaign) or "No books on this setting's shelf.",
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
        "if_a_tool_is_missing": MISSING_TOOL,
    }


def session_end(campaign: str, summary: str, open_threads: list[str], record: str = "") -> dict:
    m = _meta(campaign)
    m["last_summary"], m["open_threads"] = summary, open_threads
    _put(campaign, "meta", "meta", m)
    _put(campaign, "record", str(m["session"]), {
        "session": m["session"], "summary": summary, "open_threads": open_threads,
        "record": record, "pc_damage_taken": m["session_damage"]})
    _log(campaign, "session", f"Session {m['session']} ended. PC damage taken: {m['session_damage']}. {summary}")
    out = {"session": m["session"], "pc_damage_taken": m["session_damage"], "saved": True,
           "next": ("Ask the player whether to run the Reviewer now. If yes, call reviewer_brief in this "
                    "chat and follow its procedure without revealing anything.")}
    if not any(e["session"] == m["session"] for e in _journal(campaign)):
        out["journal"] = ("Nothing was written in the journal this session. Before you finish, record with "
                          "journal_note what play established: names you invented, promises and debts, what "
                          "people saw the character do, things given or taken. One call, several notes.")
    _library_backfill(campaign)
    drift = _library_drift(campaign)
    if drift:
        out["character_library"] = drift
    return out


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
        "journal_this_session": [_jline(e) for e in _journal(campaign) if e["session"] == m["session"]],
        "journal_open_items": _open_items(campaign, 20),
        "travel_map": _travel(campaign) if _travel(campaign)["places"] else "None yet.",
        "world_hours": round(m["world_hours"], 1),
        "secrecy": "Nothing you read or write here may appear in your visible replies.",
        "if_a_tool_is_missing": MISSING_TOOL,
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


# ---------------------------------------------------------------- journal
#
# What has been established in play: names invented at the table, promises, debts, what someone saw.
# The Narrator adds to it as scenes end. Characters' cards pull their own entries, so people remember.

JOURNAL_KINDS = ("fact", "promise", "debt", "threat", "rumour")
_OPEN_KINDS = ("promise", "debt", "threat")


def _journal(campaign: str) -> list[dict]:
    return _get(campaign, "journal", "all", [])


def journal_note(campaign: str, notes: list[dict]) -> dict:
    """Record things established in play. Each note: text (one plain sentence), about (names of the
    people, places or groups it concerns), kind (fact, promise, debt, threat or rumour), secret (true if
    the player character does not know it), closes (the number of an open promise, debt or threat that
    this note settles)."""
    if isinstance(notes, dict):
        notes = [notes]
    if not notes:
        raise GameError("Pass at least one note.")
    j, m = _journal(campaign), _meta(campaign)
    saved, closed = [], []
    for raw in notes:
        text = " ".join(str(raw.get("text", "")).split())[:500]
        if not text:
            raise GameError("Every note needs text: one plain sentence saying what is now true.")
        kind = str(raw.get("kind", "fact")).lower()
        if kind not in JOURNAL_KINDS:
            raise GameError(f"kind must be one of: {', '.join(JOURNAL_KINDS)}.")
        about = raw.get("about") or []
        if isinstance(about, str):
            about = [about]
        n = (j[-1]["n"] + 1) if j else 1
        entry = {"n": n, "session": m["session"], "hours": round(m["world_hours"], 1), "text": text,
                 "about": [str(a).strip() for a in about if str(a).strip()][:8], "kind": kind,
                 "secret": bool(raw.get("secret", False))}
        if kind in _OPEN_KINDS:
            entry["open"] = True
        target = raw.get("closes")
        if target:
            hit = next((e for e in j if e["n"] == int(target)), None)
            if hit is None or not hit.get("open"):
                raise GameError(f"Entry {target} is not an open promise, debt or threat.")
            hit["open"], hit["outcome"] = False, text
            entry.pop("open", None)
            closed.append(hit["n"])
        j.append(entry)
        saved.append(n)
    _put(campaign, "journal", "all", j)
    out: dict[str, Any] = {"saved": saved, "entries_in_journal": len(j)}
    if closed:
        out["closed"] = closed
    still = [e for e in j if e.get("open")]
    if still:
        out["still_open"] = len(still)
    return out


def _about(entry: dict, who: str) -> bool:
    s = _slug(who)
    return any(s == _slug(a) or s in _slug(a) or _slug(a) in s for a in entry["about"] if _slug(a)) \
        or (len(s) > 2 and s.replace("-", " ") in entry["text"].lower())


def _jline(e: dict) -> dict:
    out = {"n": e["n"], "session": e["session"], "text": e["text"], "about": e["about"], "kind": e["kind"]}
    if e.get("secret"):
        out["secret"] = True
    if e.get("open"):
        out["open"] = True
    if e.get("outcome"):
        out["outcome"] = e["outcome"]
    return out


def journal_search(campaign: str, query: str = "", about: str = "", player_view: bool = False,
                   limit: int = 12) -> dict:
    """Look up what has been established in play. about: a person, place or group. query: any words.
    player_view: only what the player character knows, for answering 'what do I know about X?'."""
    j = _journal(campaign)
    found = [e for e in j if not (player_view and e.get("secret"))]
    if about:
        found = [e for e in found if _about(e, about)]
    words = [w for w in re.findall(r"[^\W_]{3,}", query.lower()) if w not in _STOP]
    if words:
        scored = [(sum(e["text"].lower().count(w) + 2 * any(w in a.lower() for a in e["about"]) for w in words), e)
                  for e in found]
        found = [e for n, e in sorted(scored, key=lambda x: (-x[0], -x[1]["n"])) if n]
    else:
        found = found[::-1]
    limit = max(1, min(int(limit), 40))
    out: dict[str, Any] = {"entries": [_jline(e) for e in found[:limit]], "matching": len(found)}
    if player_view:
        out["instruction"] = ("This is what the character has learned in play. Answer from it, plus common "
                              "knowledge. Where it runs out, say the character does not know. Do not fill the "
                              "gap from sealed material.")
    elif not found:
        out["note"] = "Nothing recorded. If it was established in play, record it now with journal_note."
    return out


def _open_items(campaign: str, limit: int = 10) -> list[dict]:
    return [_jline(e) for e in _journal(campaign) if e.get("open")][-limit:]


# ----------------------------------------------------------------- factions

def _faction_of(campaign: str, card: dict) -> dict | None:
    name = card.get("faction")
    return _get(campaign, "card", _slug(name)) if name else None


def _members(campaign: str, faction: dict) -> list[dict]:
    return [c for c in _all(campaign, "card").values()
            if c.get("faction") and _slug(c["faction"]) == _slug(faction["name"])]


# ---------------------------------------------------------- surprise tables
#
# Things the engine picks so that the Author and the Narrator do not always reach for their first idea.

SURPRISES: dict[str, list[str]] = {
    "complication": [
        "Someone the character needs is not where they should be, and left in a hurry.",
        "The obvious tool for the job is broken, missing or already in use by someone else.",
        "A third party arrives who wants the same thing for a different reason.",
        "It works, but loudly. Someone has heard.",
        "The way back is cut off: a door, a tide, a crowd, a collapse.",
        "An ally misunderstands and acts on the wrong plan.",
        "The price has gone up since it was agreed, and the seller knows why it matters.",
        "Weather, light or footing turns against whoever is winning.",
        "Something the character carries is noticed, recognised and wanted.",
        "A bystander is hurt or trapped, and only the character is close enough to help.",
        "The enemy was expecting someone. Not the character, but someone.",
        "A fire, a flood or a failing structure gives everything here a few minutes.",
        "Authority shows up early and assumes the worst about what it sees.",
        "The thing sought is here but is not what it was described as.",
        "An old debt of the character's is called in at the worst moment.",
        "Someone lies to protect a person the character has not met yet.",
        "The character's own gear or magic fails in a small, specific way that will matter again.",
        "A rival has been here first and left a message.",
        "A companion is tempted by what they find.",
        "What was meant to be guarded by one is guarded by several, or the reverse.",
        "The locals close ranks: nobody saw anything and everybody is watching.",
        "A wounded enemy begs for something reasonable.",
        "The map, key or instructions are right but out of date.",
        "Success here will be blamed on someone innocent unless the character prevents it.",
        "An animal or a child has taken the important thing somewhere awkward.",
        "Two people the character trusts give flatly opposite accounts.",
        "The safe place is not safe tonight, for a reason that has nothing to do with the character.",
        "Someone recognises the character from a story that is not quite true.",
        "The clock is shorter than anyone said.",
        "Help is offered by the last person the character wants to owe.",
    ],
    "twist": [
        "The person asking for help caused the problem and does not know it.",
        "The villain is right about one important thing.",
        "Two enemies who seem allied are each planning to betray the other, soon.",
        "The thing everyone is fighting over has already been replaced with a copy.",
        "An ally is reporting to someone else, out of love rather than greed.",
        "The victim does not want to be rescued, and has a reason.",
        "The danger is real but aimed at someone other than everyone assumes.",
        "What looks like the villain's plan is a cover for a smaller, more personal one.",
        "The legend is true except for who the monster was.",
        "A dead person is not dead, and has been busy.",
        "The character's own side did something shameful here once, and the locals remember.",
        "The lieutenant is the one with the conviction. The villain is tired.",
        "The treasure is a debt: whoever holds it owes someone dangerous.",
        "Someone thought to be a prisoner is in charge.",
        "The cure and the cause come from the same source.",
        "The enemy's weapon stops working if a particular person is harmed, and they know it.",
        "A place believed abandoned has been lived in the whole time.",
        "The message was meant for the character but written years ago.",
        "The traitor was told to betray by someone on the character's side.",
        "What the villain wants would help many people and ruin a few. The few are the ones in front of the character.",
        "The monster is guarding something, not hunting.",
        "An apparent accident was the first move of someone who has not appeared yet.",
        "The rival wants the same outcome and will not share credit.",
        "The safe route exists because someone keeps paying for it in a way nobody talks about.",
    ],
    "person": [
        "talks while working and never looks up | wants a witness to something they are about to do",
        "over-polite, counts everything twice | wants to leave this place for good and needs one more thing first",
        "laughs at the wrong moments | wants someone else blamed before the truth comes out",
        "very still, answers after a pause | wants news of a person who went the way the character came",
        "treats every stranger as a customer | wants a rival ruined, cheaply",
        "bitter about a promotion that went elsewhere | wants to be asked for their opinion",
        "keeps touching a ring, charm or scar | wants forgiveness from someone who is not here",
        "cheerful, hungry, owes money | wants a meal and one piece of luck",
        "speaks for a master who is in the next room | wants the visitor gone before the master wakes",
        "too young for the job and knows it | wants to do one thing right in front of somebody",
        "devout in a way that makes others uneasy | wants a sign, and may decide the character is it",
        "old soldier, bad knee, good memory | wants the names of the dead remembered correctly",
        "smiles with the mouth only | wants to know who sent the character",
        "collects small debts and never forgets one | wants a favour banked for later",
        "frightened and hiding it with rules | wants someone else to make the decision",
        "proud of a skill nobody here values | wants to be needed",
        "newly arrived, asks too many questions | wants to find a place to belong before winter",
        "grieving and practical about it | wants a body, a grave or a name",
        "gossip with a ledger for a mind | wants one secret in exchange for each one given",
        "drunk on a small authority | wants to be seen refusing someone important",
        "calm because the worst has already happened | wants to finish one task and stop",
        "quick hands, quicker apology | wants out from under whoever they work for",
        "honest to the point of rudeness | wants a liar exposed",
        "generous in public, careful in private | wants their good name kept clean at any cost",
    ],
    "activity": [
        "eating, and unwilling to leave the meal",
        "arguing among themselves about what to do with a captive or a find",
        "wounded from an earlier fight and trying to hide it",
        "waiting for someone, and it is not the character",
        "moving something heavy and badly placed to defend themselves",
        "searching for something they have lost",
        "asleep, with one poor sentry",
        "fleeing from something worse coming the same way",
        "building, repairing or digging",
        "performing a rite that must not be interrupted",
        "following the character already, and have been for a while",
        "trading with people the character would not expect them to know",
        "guarding a wounded or young one of their own",
        "lost, and too proud to say so",
        "setting an ambush for someone else",
        "celebrating, careless and loud",
        "carrying a message, and in a hurry",
        "burying their dead",
    ],
    "omen": [
        "birds or vermin leaving in one direction",
        "a bell, horn or drum where there should be none",
        "the same stranger seen twice in places too far apart",
        "water, milk or wine gone bad overnight",
        "a child's rhyme that names something it should not know",
        "tools laid out for a job nobody admits to planning",
        "a familiar road that is shorter or longer than it was",
        "animals refusing a threshold",
        "a fresh mark over an old warning",
        "lights where nobody lives",
        "a locked door found open, with nothing taken",
        "the smell of smoke with no fire",
    ],
}

_REACTIONS = [(4, "hostile: refuses, threatens or acts against the character"),
              (6, "unhelpful: wants them gone, gives nothing freely"),
              (8, "guarded: will deal, but wants something first"),
              (10, "helpful: gives what is asked, with a small price or condition"),
              (99, "generous: helps beyond what was asked, and expects to be remembered")]
_SYL_A = "al an ar bel bren cas dor el fen gar hal is jor kel lor mar ner or per quin ros sel tor ul ver wen yor zan".split()
_SYL_B = "a e i o u ae ia en in on ar or el".split()
_SYL_C = "bek den dra gan lis mir nor rak ren ric sa seth ta than vik wyn".split()


def _names_left(campaign: str) -> list[str]:
    entry = _get(campaign, "sealed", "names")
    if not entry:
        return []
    used = set(_get(campaign, "surprise", "names_used", []))
    names = [re.sub(r"^[-*\d.\s]+", "", ln).strip() for ln in entry["content"].splitlines()]
    return [n for n in names if n and not n.endswith(":") and n not in used]


def surprise(campaign: str, kind: str, count: int = 1, about: str = "") -> dict:
    """Let the engine choose. kind: complication, twist, person, activity, omen, reaction or name.
    about: for reaction, the character whose attitude applies; for name, a culture or group to draw from."""
    kind = kind.lower().strip()
    count = max(1, min(int(count), 6))
    if kind == "reaction":
        mod, who = 0, ""
        if about:
            card = _get(campaign, "card", _slug(about))
            if card:
                faction = _faction_of(campaign, card)
                mod, who = ATTITUDES.index(card["attitude"]) - 3, card["name"]
                if faction and not card.get("history"):
                    mod = ATTITUDES.index(faction["attitude"]) - 3
        dice = [_rng.randint(1, 6), _rng.randint(1, 6)]
        total = sum(dice) + mod
        result = next(text for top, text in _REACTIONS if total <= top)
        _log(campaign, "surprise", f"reaction {who}: {dice} {mod:+d} = {total}: {result}")
        return {"kind": "reaction", "roll": f"{dice[0]} + {dice[1]} {mod:+d} (attitude) = {total}", "result": result,
                "instruction": "This is how they respond at first. Play it; good play can still change it."}
    if kind == "name":
        pool = _names_left(campaign)
        if about:
            pick = [n for n in pool if about.lower() in n.lower()]
            pool = pick or pool
        chosen = _rng.sample(pool, min(count, len(pool)))
        made = []
        while len(chosen) + len(made) < count:
            made.append((_rng.choice(_SYL_A) + _rng.choice(_SYL_B) + _rng.choice(_SYL_C)).capitalize())
        used = _get(campaign, "surprise", "names_used", []) + chosen
        _put(campaign, "surprise", "names_used", used)
        out: dict[str, Any] = {"kind": "name", "names": [n.split(":", 1)[-1].strip() for n in chosen] + made}
        if made:
            out["note"] = ("The campaign's own name list is empty or used up, so some of these are generic. "
                           "Adjust them to the setting's sound.")
        return out
    if kind not in SURPRISES:
        raise GameError("kind must be one of: " + ", ".join(sorted(SURPRISES) + ["name", "reaction"]) + ".")
    table = SURPRISES[kind]
    used = _get(campaign, "surprise", kind, [])
    fresh = [i for i in range(len(table)) if i not in used]
    if len(fresh) < count:
        used, fresh = [], list(range(len(table)))
    picks = _rng.sample(fresh, count)
    _put(campaign, "surprise", kind, used + picks)
    results = [table[i] for i in picks]
    _log(campaign, "surprise", f"{kind}: " + " / ".join(results))
    how = {"complication": "Fit this to the scene and make it happen now. Do not swap it for something gentler.",
           "twist": "Build this into the adventure in the setting's own terms. It is a seed, not a script.",
           "person": "Manner | what they want right now. Give them a name from surprise('name') and a face.",
           "activity": "This is what they are doing when found. Let it shape how the meeting starts.",
           "omen": "Show it once, plainly, without explaining it."}[kind]
    return {"kind": kind, "results": results, "how_to_use": how}


# ------------------------------------------------------- areas and cover

def _feature_squares(m: dict, kinds: tuple[str, ...]) -> set[tuple[int, int]]:
    out = set()
    for f in m.get("features", []):
        if f.get("kind", "other") in kinds:
            x0, y0 = int(f.get("x", 0)), int(f.get("y", 0))
            for dx in range(max(1, int(f.get("w", 1)))):
                for dy in range(max(1, int(f.get("h", 1)))):
                    out.add((x0 + dx, y0 + dy))
    return out


def _between(a: list[int], b: list[int]) -> list[tuple[int, int]]:
    """Squares a straight line passes through, not counting the two ends."""
    steps = max(abs(b[0] - a[0]), abs(b[1] - a[1]))
    seen: list[tuple[int, int]] = []
    for i in range(1, steps):
        p = (round(a[0] + (b[0] - a[0]) * i / steps), round(a[1] + (b[1] - a[1]) * i / steps))
        if p not in seen:
            seen.append(p)
    return seen


def _feet(a: list[int], b: list[int]) -> int:
    return 5 * max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _cover(campaign: str, a: dict, t: dict) -> tuple[int, str]:
    """Armour class bonus the target gets from what stands between, and why. (0, '') if none or unknown."""
    s, m = _get(campaign, "combat", "combat"), _get(campaign, "map", "map")
    if not s or not s.get("active") or not m:
        return 0, ""
    pa, pt = m["positions"].get(a["id"]), m["positions"].get(t["id"])
    if pa is None or pt is None or _feet(pa, pt) <= 5:
        return 0, ""
    line = _between(pa, pt)
    path = set(line)
    if path & _feature_squares(m, ("wall",)):
        return 5, "a wall is in the line: three-quarters cover, +5 AC (if it blocks the line entirely, the attack cannot be made)"
    blockers = [id for id, xy in m["positions"].items() if tuple(xy) in path
                and not (_get(campaign, "entity", id) or {}).get("dead")]
    # Cover the attacker is leaning on protects the attacker, not the target.
    shelter = set(line[1:] if len(line) > 1 else line) & _feature_squares(m, ("cover",))
    if shelter or blockers:
        return 2, "half cover, +2 AC" + (" (someone is in the line)" if blockers else "")
    return 0, ""


def _where(m: dict, campaign: str, ref: str) -> list[int]:
    """A square from a label like 'E6' or from the name of a token on the map."""
    mm = re.fullmatch(r"\s*([A-Za-z]{1,2})\s*(\d{1,2})\s*", str(ref))
    if mm:
        col = mm.group(1).upper()
        x = ord(col[0]) - 65 if len(col) == 1 else 26 + ord(col[1]) - 65
        return _square(m, [x, int(mm.group(2)) - 1])
    e = _get(campaign, "entity", _slug(str(ref)))
    if e is None or e["id"] not in m["positions"]:
        raise GameError(f"'{ref}' is neither a square like E6 nor someone on the map.")
    return m["positions"][e["id"]]


def area(campaign: str, shape: str, size_feet: int, origin: str, toward: str = "") -> dict:
    """Who is inside an area of effect. shape: sphere (radius), cube (side), cone (length) or line
    (length, 5 ft wide). origin: a square like 'E6' or a token's name; for a cone or line it is where the
    effect starts. toward: for a cone or line, the square or token it is aimed at."""
    import math
    m = _get(campaign, "map", "map")
    if m is None:
        raise GameError("No battle map yet. Call set_battle_map first.")
    shape = shape.lower().strip()
    if shape in ("circle", "radius", "burst", "emanation"):
        shape = "sphere"
    if shape not in ("sphere", "cube", "cone", "line"):
        raise GameError("shape must be sphere, cube, cone or line.")
    o = _where(m, campaign, origin)
    size = max(5, int(size_feet))
    aim = None
    if shape in ("cone", "line"):
        if not toward:
            raise GameError(f"A {shape} needs 'toward': the square or token it is aimed at.")
        aim = _where(m, campaign, toward)
        if aim == o:
            raise GameError("origin and toward are the same square.")
        ang = math.atan2(aim[1] - o[1], aim[0] - o[0])
    inside = set()
    for x in range(m["width"]):
        for y in range(m["height"]):
            dx, dy = (x - o[0]) * 5.0, (y - o[1]) * 5.0
            dist = math.hypot(dx, dy)
            if shape == "sphere":
                ok = dist <= size + 0.01
            elif shape == "cube":
                half = size / 2
                ok = abs(dx) <= half and abs(dy) <= half if size > 5 else (x, y) == tuple(o)
            elif (x, y) == tuple(o):
                ok = False
            elif shape == "cone":
                off = abs((math.atan2(dy, dx) - ang + math.pi) % (2 * math.pi) - math.pi)
                ok = dist <= size + 2.5 and off <= math.atan(0.5) + 0.12
            else:
                along = dx * math.cos(ang) + dy * math.sin(ang)
                across = abs(-dx * math.sin(ang) + dy * math.cos(ang))
                ok = 0 < along <= size + 2.5 and across <= 3.0
            if ok:
                inside.add((x, y))
    caught = []
    for id, xy in m["positions"].items():
        e = _get(campaign, "entity", id)
        if e and not e.get("dead") and tuple(xy) in inside:
            caught.append({"name": e["name"], "kind": e["kind"], "square": _label(*xy), "status": _status(e)})
    hazards = sorted(_label(x, y) for x, y in inside & _feature_squares(m, ("hazard",)))
    out: dict[str, Any] = {"shape": shape, "size_feet": size, "origin": _label(*o),
                           "squares": len(inside), "inside": caught}
    if aim:
        out["toward"] = _label(*aim)
    if hazards:
        out["hazards_inside"] = hazards
    out["instruction"] = ("Everyone listed is in the area, friends included. Resolve each with saving_throw "
                          "and apply_damage. Walls and cover are not checked here: leave out anyone who is "
                          "plainly sheltered from the origin.")
    return out


# ------------------------------------------------------------ travel map

def _travel(campaign: str) -> dict:
    return _get(campaign, "travel", "map", {"places": {}, "routes": [], "here": ""})


def set_travel_map(campaign: str, places: list[dict] | None = None, routes: list[dict] | None = None,
                   here: str = "") -> dict:
    """Add to the map of places and the routes between them. places: [{name, note, known}]. routes:
    [{from, to, hours, kind (road, trail, river, sea, gate...), danger 0-3, note, known}]. known=false hides
    a place or route from the character until it is discovered. here: where the character is now.
    Entries merge by name, so call it again to add or reveal things."""
    t = _travel(campaign)
    for p in places or []:
        name = str(p.get("name", "")).strip()
        if not name:
            raise GameError("Every place needs a name.")
        old = t["places"].get(_slug(name), {"name": name, "known": True})
        t["places"][_slug(name)] = {**old, **{k: v for k, v in p.items() if k in ("note", "known")}, "name": old["name"]}
    for r in routes or []:
        a, b = _slug(str(r.get("from", ""))), _slug(str(r.get("to", "")))
        for end in (a, b):
            if end not in t["places"]:
                raise GameError(f"Route end '{end}' is not a place on the map. Add the place first, in this call "
                                "or an earlier one.")
        if a == b or float(r.get("hours", -1)) < 0:
            raise GameError("A route needs two different ends and hours of zero or more.")
        new = {"a": a, "b": b, "hours": float(r["hours"]), "kind": str(r.get("kind", "road")),
               "danger": max(0, min(3, int(r.get("danger", 0)))), "note": str(r.get("note", "")),
               "known": bool(r.get("known", True))}
        t["routes"] = [x for x in t["routes"] if {x["a"], x["b"]} != {a, b} or x["kind"] != new["kind"]] + [new]
    if here:
        if _slug(here) not in t["places"]:
            raise GameError(f"'{here}' is not a place on the map.")
        t["here"] = _slug(here)
    _put(campaign, "travel", "map", t)
    return {"places": len(t["places"]), "routes": len(t["routes"]),
            "here": t["places"][t["here"]]["name"] if t["here"] else "not set"}


def _routes_from(t: dict, place: str, known_only: bool = True) -> list[dict]:
    out = []
    for r in t["routes"]:
        if place in (r["a"], r["b"]) and (r["known"] or not known_only):
            other = r["b"] if r["a"] == place else r["a"]
            if known_only and not t["places"][other].get("known", True):
                continue
            line = {"to": t["places"][other]["name"], "hours": r["hours"], "kind": r["kind"]}
            if r["note"]:
                line["note"] = r["note"]
            out.append(line)
    return sorted(out, key=lambda x: x["hours"])


def _where_now(campaign: str) -> dict | None:
    t = _travel(campaign)
    if not t["here"]:
        return None
    p = t["places"][t["here"]]
    return {"here": p["name"], "note": p.get("note", ""), "known_routes": _routes_from(t, t["here"]),
            "use": "Move between these places with travel, which passes the time and lets the world act."}


def travel(campaign: str, to: str, use_hidden_routes: bool = False) -> dict:
    """Go from where the character is to another place on the travel map, by the quickest known way.
    Time passes for each leg, clocks may advance, and the road may interrupt."""
    t = _travel(campaign)
    if not t["here"]:
        raise GameError("The travel map does not say where the character is. Call set_travel_map with 'here'.")
    goal = _slug(to)
    if goal not in t["places"]:
        raise GameError(f"'{to}' is not on the travel map. Known: "
                        + ", ".join(p["name"] for p in t["places"].values() if p.get("known", True)) + ".")
    if goal == t["here"]:
        raise GameError("The character is already there.")
    usable = [r for r in t["routes"] if r["known"] or use_hidden_routes]
    best: dict[str, tuple[float, list[dict]]] = {t["here"]: (0.0, [])}
    frontier = [t["here"]]
    while frontier:
        frontier.sort(key=lambda p: best[p][0])
        cur = frontier.pop(0)
        for r in usable:
            if cur in (r["a"], r["b"]):
                nxt = r["b"] if r["a"] == cur else r["a"]
                cost = best[cur][0] + r["hours"]
                if nxt not in best or cost < best[nxt][0]:
                    best[nxt] = (cost, best[cur][1] + [{**r, "_to": nxt}])
                    frontier.append(nxt)
    if goal not in best:
        raise GameError(f"No known way from {t['places'][t['here']]['name']} to {t['places'][goal]['name']}. "
                        "Routes from here: " + (", ".join(f"{r['to']} ({r['hours']} h)" for r in
                                                          _routes_from(t, t["here"])) or "none known") + ".")
    legs, fired, ended, event = [], [], [], None
    for r in best[goal][1]:
        adv = advance_time(campaign, r["hours"], f"travel to {t['places'][r['_to']]['name']}")
        fired += adv["consequences"]
        ended += adv.get("conditions_ended", [])
        t["here"] = r["_to"]
        t["places"][r["_to"]]["known"] = True
        legs.append({"to": t["places"][r["_to"]]["name"], "hours": r["hours"], "kind": r["kind"]})
        if r["danger"] and _rng.randint(1, 6) <= r["danger"]:
            what = surprise(campaign, _rng.choice(["complication", "activity", "omen"]))["results"][0]
            event = {"on_the_way_to": t["places"][r["_to"]]["name"], "seed": what,
                     "instruction": ("Something happens on this leg. Use the seed, in this place's terms. If "
                                     "it is a fight, the character meets it tired from the road, not rested.")}
            break
    _put(campaign, "travel", "map", t)
    here = t["places"][t["here"]]
    out: dict[str, Any] = {"legs": legs, "hours": round(sum(x["hours"] for x in legs), 1), "now_at": here["name"],
                           "arrived": t["here"] == goal, "world_hours": round(_meta(campaign)["world_hours"], 1),
                           "consequences": fired, "instruction": _consequence_rule(fired)}
    if here.get("note"):
        out["place_note"] = here["note"]
    if event:
        out["event"] = event
        if t["here"] != goal:
            out["journey_interrupted"] = f"Call travel again to go on to {t['places'][goal]['name']}."
    if ended:
        out["conditions_ended"] = ended
    out["onward"] = _routes_from(t, t["here"])
    _log(campaign, "travel", " -> ".join(x["to"] for x in legs) + f" ({out['hours']} h)")
    return out


# ------------------------------------------------- rules shelf and monsters

SRD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "srd")
RULES_SHELF = "rules"
_SRD_VERSION = "srd-5.2.1-a"
_monsters: list[dict] | None = None


def rules_ready() -> bool:
    """Make sure the bundled rules reference is on the rules shelf. Cheap after the first time."""
    con, _ = _lore_db()
    row = con.execute("SELECT data FROM book WHERE shelf=? AND slug=?", (RULES_SHELF, "~version")).fetchone()
    con.close()
    if row and json.loads(row[0]).get("version") == _SRD_VERSION:
        return True
    try:
        with open(os.path.join(SRD_DIR, "books.json")) as f:
            books = json.load(f)
    except OSError:
        return False
    for filename, title in books.items():
        with open(os.path.join(SRD_DIR, filename), encoding="utf-8") as f:
            parts = _sections(f.read())
        _store_book(RULES_SHELF, f"SRD {title}", parts, {"contents": [], "source": "SRD 5.2.1, CC BY 4.0"})
    con, _ = _lore_db()
    with con:
        con.execute("INSERT OR REPLACE INTO book VALUES (?,?,?)",
                    (RULES_SHELF, "~version", json.dumps({"version": _SRD_VERSION})))
    con.close()
    return True


def rules_lookup(query: str, book: str = "", limit: int = 4) -> dict:
    """Look up the rules as written: a spell, a condition, an action, a magic item, a class feature,
    how something works. book narrows it: spells, glossary, playing, equipment, magic items, classes,
    feats, monsters, toolbox."""
    if not rules_ready():
        raise GameError("The rules reference is not installed on this server.")
    out = _search_shelf(RULES_SHELF, query, book, limit)
    if out["results"]:
        out["how_to_use"] = ("These are the rules as written (2024 rules reference). Apply them as they stand. "
                             "You may tell the player what a rule says in a sentence or two. The table rules "
                             "in the campaign's setting notes win where they differ.")
    else:
        out["note"] = ("No match in the rules reference, which covers the free core rules only. Try the "
                       "thing's exact name. If it is not there, rule on it yourself, briefly and consistently.")
    return out


def _all_monsters() -> list[dict]:
    global _monsters
    if _monsters is None:
        import gzip
        try:
            with gzip.open(os.path.join(SRD_DIR, "monsters.json.gz"), "rt", encoding="utf-8") as f:
                _monsters = json.load(f)
        except OSError:
            raise GameError("The monster reference is not installed on this server.")
    return _monsters


def _mod(score: int) -> int:
    return (int(score) - 10) // 2


def monsters(query: str = "", cr_min: float = 0, cr_max: float = 30, type: str = "", limit: int = 40) -> dict:
    """Browse the monster reference. query: part of a name. type: beast, undead, fiend and so on.
    Returns name, challenge rating, type, hit points and armour class, weakest first."""
    q, ty = query.lower().strip(), type.lower().strip()
    found = [m for m in _all_monsters()
             if (not q or q in m["name"].lower()) and (not ty or ty in m["type"].lower())
             and float(cr_min) <= float(m["challenge_rating"]) <= float(cr_max)]
    found.sort(key=lambda m: (float(m["challenge_rating"]), m["name"]))
    limit = max(1, min(int(limit), 80))
    return {"matching": len(found),
            "monsters": [{"name": m["name"], "cr": m["challenge_rating"], "type": m["type"], "size": m["size"],
                          "hp": m["hit_points"], "ac": m["armor_class"][0]["value"]} for m in found[:limit]],
            "next": "Bring one into a campaign with monster_import, then rate the fight with encounter_check."}


def _monster_sheet(m: dict) -> dict:
    abil = {k[:3]: m[k] for k in ("strength", "dexterity", "constitution", "intelligence", "wisdom", "charisma")}
    saves = {k: _mod(v) for k, v in abil.items()}
    skills = {}
    for p in m.get("proficiencies", []):
        label = str(p.get("proficiency", ""))
        if label.startswith("Saving Throw:"):
            saves[label.split(":")[1].strip().lower()[:3]] = int(p["value"])
        elif label.startswith("Skill:"):
            skills[label.split(":")[1].strip().lower()] = int(p["value"])
    attacks, other, per_round, routine = [], [], 1, ""
    for a in m.get("actions", []):
        if a["name"] == "Multiattack":
            routine = a.get("desc", "")
            counts = [x.get("count", 1) for x in a.get("actions", [])] \
                + [(a.get("action_options") or {}).get("choose", 0)]
            per_round = sum(int(c) if str(c).isdigit() else 1 for c in counts)   # 'Number of Heads' reads as 1
            continue
        dice = [d for d in a.get("damage", []) if d.get("damage_dice")]
        flat = re.search(r"Hit:\s*(\d+)\s+(\w+) damage", a.get("desc", ""))
        if "attack_bonus" in a and not dice and flat:    # tiny beasts: 'Hit: 1 Piercing damage'
            dice = [{"damage_dice": flat.group(1), "damage_type": flat.group(2)}]
        if "attack_bonus" in a and dice:
            rider = a.get("desc", "")
            cut = re.search(r"damage\.\s*(.+)$", rider, re.S)
            extra = (cut.group(1).strip() if cut else "")
            if len(dice) > 1:
                extra = ("Damage includes " + ", ".join(f"{d['damage_dice']} {str(d.get('damage_type', '')).lower()}"
                                                         for d in dice[1:]) + ". " + extra).strip()
            reach = re.search(r"(reach \d+ ft\.|range [\d/]+ ft\.)", rider)
            atk = {"name": a["name"], "to_hit": int(a["attack_bonus"]),
                   "damage": "+".join(d["damage_dice"].replace(" ", "") for d in dice).replace("+-", "-"),
                   "type": str(dice[0].get("damage_type", "")).lower()}
            if reach:
                atk["reach"] = reach.group(1).rstrip(".")
            if extra:
                atk["rider"] = extra[:400]
            attacks.append(atk)
        else:
            other.append({"name": a["name"], "text": a.get("desc", "")[:600]})
    sheet: dict[str, Any] = {
        "max_hp": m["hit_points"], "ac": m["armor_class"][0]["value"], "init": _mod(abil["dex"]),
        "abilities": abil, "saves": saves, "attacks": attacks, "attacks_per_round": max(1, per_round),
        "size": m["size"].lower(), "creature_type": m["type"], "cr": m["challenge_rating"],
        "speed": m.get("speed", {}), "senses": m.get("senses", {}), "languages": m.get("languages", ""),
        "resist": [str(x).lower() for x in m.get("damage_resistances", [])],
        "immune": [str(x).lower() for x in m.get("damage_immunities", [])],
        "vulnerable": [str(x).lower() for x in m.get("damage_vulnerabilities", [])],
        "condition_immunities": [str(x).lower() for x in m.get("condition_immunities", [])],
        "source": f"{m['name']}, SRD 5.2.1",
    }
    if skills:
        sheet["skills"] = skills
    if routine:
        sheet["multiattack"] = routine
    if other:
        sheet["other_actions"] = other
    for key, label in (("special_abilities", "traits"), ("bonus_actions", "bonus_actions"),
                       ("reactions", "monster_reactions"), ("legendary_actions", "legendary_actions")):
        items = [{"name": x["name"], "text": x.get("desc", "")[:600]} for x in m.get(key, [])]
        if items:
            sheet[label] = items
    return sheet


def monster_import(campaign: str, monster: str, names: list[str] | None = None, kind: str = "creature") -> dict:
    """Bring a monster from the rules reference into a campaign as a ready stat sheet. names: what to
    call each copy (default: one copy under the monster's own name). kind: creature (enemy), ally or npc."""
    pool = _all_monsters()
    q = monster.lower().strip()
    hit = next((m for m in pool if m["name"].lower() == q), None)
    if hit is None:
        close = [m["name"] for m in pool if q and q in m["name"].lower()]
        if len(close) == 1:
            hit = next(m for m in pool if m["name"] == close[0])
        else:
            raise GameError(f"No monster called '{monster}' in the rules reference."
                            + (f" Close: {', '.join(close[:12])}." if close else " Browse with monsters."))
    if kind not in ("creature", "ally", "npc"):
        raise GameError("kind must be creature, ally or npc.")
    sheet = _monster_sheet(hit)
    saved = []
    for name in (names or [hit["name"]]):
        upsert_entity(campaign, name, kind, json.loads(json.dumps(sheet)))
        saved.append(name)
    out: dict[str, Any] = {"imported": hit["name"], "as": saved, "cr": hit["challenge_rating"],
                           "hp": sheet["max_hp"], "ac": sheet["ac"],
                           "attacks": [f"{a['name']} +{a['to_hit']} {a['damage']} {a['type']}" for a in attacks_of(sheet)],
                           "attacks_per_round": sheet["attacks_per_round"]}
    if sheet.get("other_actions"):
        out["other_actions"] = [x["name"] for x in sheet["other_actions"]]
        out["note"] = ("Actions that are not attack rolls (breath, spells, auras) are on the sheet as text. "
                       "Resolve them with saving_throw, apply_damage and area.")
    return out


def attacks_of(sheet: dict) -> list[dict]:
    return sheet.get("attacks", [])
