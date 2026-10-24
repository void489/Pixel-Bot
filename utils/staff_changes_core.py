"""Pure helpers for the staff-changes (promotion / demotion) ladder.

No Discord or DB imports on purpose, so the logic is easy to test and the cog,
the applications cog and the dashboard can all share it.

The ladder is a list of entries, lowest rank first:
    {"index": 0, "role_id": "123", "nick": "Member"}
    {"index": 1, "role_id": "456", "nick": "Trial Mod"}
Index 0 is the lowest rank. Indexes always equal the list position.
"""


def normalize_ladder(raw) -> list:
    """Clean a raw ladder from Mongo: drop junk, sort by index, renumber 0..n-1."""
    out = []
    for e in raw or []:
        if not isinstance(e, dict) or not e.get("role_id"):
            continue
        try:
            idx = int(e.get("index", len(out)))
        except (TypeError, ValueError):
            idx = len(out)
        out.append({
            "index": idx,
            "role_id": str(e["role_id"]),
            "nick": str(e.get("nick") or "").strip(),
        })
    out.sort(key=lambda e: e["index"])
    for i, e in enumerate(out):
        e["index"] = i
    return out


def highest_index(member_role_ids, ladder) -> int:
    """Highest ladder index among the roles a member holds, or -1 if none."""
    held = {str(r) for r in member_role_ids}
    best = -1
    for e in ladder:
        if e["role_id"] in held and e["index"] > best:
            best = e["index"]
    return best


def entry_at(ladder, index: int):
    if 0 <= index < len(ladder):
        return ladder[index]
    return None


def label(entry, role_name: str = None) -> str:
    """Nickname if one was set, else the role's name, else a placeholder."""
    if entry is None:
        return "No rank"
    return entry.get("nick") or role_name or "Unknown role"
