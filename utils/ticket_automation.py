"""Ticket automations: rules, validation, keyword matching (pure: no Discord, Flask or DB).

An automation is   trigger  ->  up to 5 steps,  run once per ticket.

    trigger   "opened"    a ticket opens (optionally only in some panels)
              "keyword"   a message in a ticket contains one of the words / phrases
                          (from the opener or from anyone; optionally only in some panels)
              "no_reply"  N minutes after a ticket opened with no staff reply yet
    steps     set_priority {level}            rename {template}
              move {type_id}  (channels only) send_message {text, role_id?}
              add_member {user_id}

Stored in `ticket_automations`; the dashboard (Automations page) edits them and the bot
(cogs/ticket_automations.py) runs them. Dashboard and bot share this module, and
mark_dirty() tells the bot to reload a server's rules right away.
"""
import re
import time

from utils import ticket_settings as ts

TRIGGERS = ("opened", "keyword", "no_reply")
STEP_TYPES = ("set_priority", "rename", "move", "send_message", "add_member")
STEP_LABELS = {
    "set_priority": "Set priority", "rename": "Rename the ticket", "move": "Move to another panel",
    "send_message": "Send a message", "add_member": "Add a member",
}
TRIGGER_LABELS = {
    "opened": "A ticket is opened", "keyword": "A message contains a keyword",
    "no_reply": "No staff reply after a while",
}
MAX_AUTOMATIONS = 25            # per server
MAX_STEPS = 5
MAX_KEYWORDS = 10
MAX_KEYWORD_LEN = 50
MAX_NAME = 60
MAX_MESSAGE = 1500
MAX_TEMPLATE = 100
MIN_NO_REPLY = 5                # minutes
MAX_NO_REPLY = 7 * 24 * 60

ERRORS = {
    "name_required": "Give the automation a name.",
    "trigger_invalid": "Pick a trigger.",
    "keywords_required": "Add at least one keyword.",
    "minutes_invalid": f"Minutes must be between {MIN_NO_REPLY} and {MAX_NO_REPLY}.",
    "steps_required": "Add at least one step.",
    "too_many": f"A server can have up to {MAX_AUTOMATIONS} automations.",
}

# --- rules reload -----------------------------------------------------------------------
_dirty: dict = {}


def mark_dirty(guild_id) -> None:
    """Dashboard -> bot (same process): this server's automations changed."""
    _dirty[str(guild_id)] = time.monotonic()


def dirty_since(guild_id) -> float:
    return _dirty.get(str(guild_id), 0.0)


# --- keywords ---------------------------------------------------------------------------
def clean_keywords(raw) -> list:
    """"refund, charge back" or a list -> unique lowercase phrases (max 10, 50 chars each)."""
    items = re.split(r"[,\n]", raw) if isinstance(raw, str) else list(raw or [])
    out = []
    for item in items:
        k = " ".join(str(item).lower().split())[:MAX_KEYWORD_LEN]
        if k and k not in out:
            out.append(k)
    return out[:MAX_KEYWORDS]


def matches_keyword(text: str, keywords):
    """The first keyword found in `text` as a whole word / phrase, case-insensitive, else
    None ("pay" does not match "payment"; "charge back" matches "Charge  back!")."""
    hay = " ".join((text or "").lower().split())
    for k in keywords or []:
        if re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", hay):
            return k
    return None


def applies_to_panel(trigger: dict, type_id) -> bool:
    """No panels chosen = every panel."""
    ids = [str(p) for p in (trigger.get("panel_ids") or [])]
    return not ids or str(type_id) in ids


# --- validation -------------------------------------------------------------------------
def _clean_step(raw: dict, known_types, known_roles):
    """(step, error) for one form row; (None, None) for an empty row."""
    kind = str(raw.get("type") or "").strip()
    if not kind:
        return None, None
    if kind not in STEP_TYPES:
        return None, "step_type"
    if kind == "set_priority":
        level = ts.clean_priority(raw.get("level"))
        return ({"type": kind, "level": level}, None) if level else (None, "pick a priority")
    if kind == "rename":
        t = ts.clean_text(raw.get("template"), MAX_TEMPLATE)
        return ({"type": kind, "template": t}, None) if t else (None, "enter a name")
    if kind == "move":
        tid = str(raw.get("type_id") or "").strip()
        return ({"type": kind, "type_id": tid}, None) if tid in known_types else (None, "pick a panel")
    if kind == "send_message":
        text = ts.clean_text(raw.get("text"), MAX_MESSAGE)
        if not text:
            return None, "enter a message"
        role = str(raw.get("role_id") or "").strip()
        if role and role not in known_roles:
            return None, "that role doesn't exist"
        return {"type": kind, "text": text, "role_id": role or None}, None
    uid = str(raw.get("user_id") or "").strip()                      # add_member
    return ({"type": kind, "user_id": uid}, None) if uid.isdigit() and 15 <= len(uid) <= 25 \
        else (None, "enter a Discord user ID")


def normalize(raw: dict, known_types, known_roles):
    """Form data -> (document fields, None), or (None, error text).

    raw: name, enabled, trigger, panel_ids[], keywords, from, minutes, steps[{type, level,
    template, type_id, text, role_id, user_id}]. known_types / known_roles: IDs that exist,
    so a deleted panel or role can't be saved into a rule."""
    known_types, known_roles = {str(t) for t in known_types}, {str(r) for r in known_roles}
    name = ts.clean_text(raw.get("name"), MAX_NAME)
    if not name:
        return None, ERRORS["name_required"]
    kind = raw.get("trigger")
    if kind not in TRIGGERS:
        return None, ERRORS["trigger_invalid"]

    trigger = {"type": kind,
               "panel_ids": [p for p in ts.clean_id_list(raw.get("panel_ids")) if p in known_types]}
    if kind == "keyword":
        trigger["keywords"] = clean_keywords(raw.get("keywords"))
        if not trigger["keywords"]:
            return None, ERRORS["keywords_required"]
        trigger["from"] = "anyone" if raw.get("from") == "anyone" else "opener"
    if kind == "no_reply":
        try:
            minutes = int(raw.get("minutes"))
        except (TypeError, ValueError):
            minutes = 0
        if not MIN_NO_REPLY <= minutes <= MAX_NO_REPLY:
            return None, ERRORS["minutes_invalid"]
        trigger["minutes"] = minutes

    steps = []
    for i, row in enumerate((raw.get("steps") or [])[:MAX_STEPS], start=1):
        step, err = _clean_step(row or {}, known_types, known_roles)
        if err:
            return None, f"Step {i}: {err}." if err != "step_type" else f"Step {i}: pick a step type."
        if step:
            steps.append(step)
    if not steps:
        return None, ERRORS["steps_required"]
    return {"name": name, "enabled": bool(raw.get("enabled", True)), "trigger": trigger, "steps": steps}, None


def describe(automation: dict, type_names: dict = None) -> str:
    """One line for the dashboard list: "When a message contains 'refund' → Set priority, Send a message"."""
    t = automation.get("trigger") or {}
    when = TRIGGER_LABELS.get(t.get("type"), "?")
    if t.get("type") == "keyword":
        when += " (" + ", ".join(t.get("keywords") or []) + ")"
    elif t.get("type") == "no_reply":
        when = f"No staff reply after {t.get('minutes')} min"
    names = type_names or {}
    if t.get("panel_ids"):
        when += " in " + ", ".join(names.get(str(p), "?") for p in t["panel_ids"])
    return when + " → " + ", ".join(STEP_LABELS.get(s.get("type"), "?") for s in automation.get("steps") or [])
