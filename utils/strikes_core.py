"""Pure helpers for staff strikes (no Discord or DB imports, easy to test).

Strikes belong to a Change type (see utils/changes_core.py). Each type has its own
strike settings, stored in guild_settings under "strikes.types.<type_id>":

    {"rules": [{"at": 3, "demote": 1}, {"at": 5, "demote": 2}],
     "reset_on_promotion": False,
     "reset_on_demotion": False,
     "expire_days": 0,          # strikes older than this many days are removed (0 = never)
     "expire_dm": False}        # DM the member when one of their strikes expires

A rule reads "when a member reaches `at` active strikes in this type, demote them
`demote` rank(s)". Rules only run when a NEW strike is added, never when settings change.
"""

MAX_RULES = 10
MAX_STRIKES_AT = 50
MAX_DEMOTE = 25
MAX_REASON = 1000
MAX_EXPIRE_DAYS = 3650   # 10 years; 0 = strikes never expire


def _to_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clean_expire_days(raw) -> int:
    """Days after which a strike expires. 0 / empty / junk = never."""
    n = _to_int(raw, 0)
    return max(0, min(MAX_EXPIRE_DAYS, n))


def normalize_rules(raw) -> list:
    """Clean a raw rule list: ints, in range, one rule per `at` (first wins), sorted."""
    out, seen = [], set()
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        at, demote = _to_int(r.get("at")), _to_int(r.get("demote"))
        if at is None or demote is None:
            continue
        if not (1 <= at <= MAX_STRIKES_AT and 1 <= demote <= MAX_DEMOTE) or at in seen:
            continue
        seen.add(at)
        out.append({"at": at, "demote": demote})
    out.sort(key=lambda r: r["at"])
    return out[:MAX_RULES]


def normalize_type_config(raw) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "rules": normalize_rules(raw.get("rules")),
        "reset_on_promotion": bool(raw.get("reset_on_promotion", False)),
        "reset_on_demotion": bool(raw.get("reset_on_demotion", False)),
        "expire_days": clean_expire_days(raw.get("expire_days")),
        "expire_dm": bool(raw.get("expire_dm", False)),
    }


def rule_for_count(rules, count: int):
    """The rule to run after a strike brings a member to `count`: the one with the highest
    `at` that is <= count, or None. (Highest wins, so a 5-strike rule beats the 3-strike one.)"""
    best = None
    for r in rules or []:
        if r["at"] <= count and (best is None or r["at"] > best["at"]):
            best = r
    return best


def next_rule(rules, count: int):
    """The next rule still ahead of `count` (lowest `at` > count), or None."""
    best = None
    for r in rules or []:
        if r["at"] > count and (best is None or r["at"] < best["at"]):
            best = r
    return best


def rank_word(n: int) -> str:
    return f"{n} rank" if n == 1 else f"{n} ranks"


def describe_rule(rule: dict) -> str:
    return f"at {rule['at']} strike{'' if rule['at'] == 1 else 's'} → demote {rank_word(rule['demote'])}"


def clean_reason(raw) -> str:
    return (raw or "").strip()[:MAX_REASON]
