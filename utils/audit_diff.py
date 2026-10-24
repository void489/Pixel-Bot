"""Pure helpers that turn "before" and "after" copies of dashboard settings into short,
readable change lines for the dashboard audit log. No Flask, Discord or DB imports.

    changed   welcome.channel_id: #general -> #welcome
    set       strikes.types[t1].expire_days: set to 30
    cleared   welcome.message: cleared (was "Hello {user}")

Lists of dicts are matched by their "id" / "name" / "index" so a reordered or edited
ladder or panel list reads as the single field that really changed.
"""
import re

IGNORE_KEYS = {"_id", "updated_at", "created_at", "at"}
SECRET_RE = re.compile(r"token|secret|password|webhook|api_?key", re.I)
SNOWFLAKE_RE = re.compile(r"^\d{17,20}$")
MAX_VALUE = 48
MAX_LINES = 14
KEY_FIELDS = ("id", "name", "index", "key", "command")


def _item_key(item, position):
    for f in KEY_FIELDS:
        if isinstance(item, dict) and item.get(f) not in (None, ""):
            return f"{item[f]}"
    return str(position)


def flatten(value, path="", out=None):
    """{path: leaf}. Dicts nest with '.', lists of dicts nest as [key]."""
    out = {} if out is None else out
    if isinstance(value, dict):
        for k, v in value.items():
            if k in IGNORE_KEYS:
                continue
            flatten(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(value, list) and value and all(isinstance(i, dict) for i in value):
        for pos, item in enumerate(value):
            flatten(item, f"{path}[{_item_key(item, pos)}]", out)
    else:
        out[path] = value
    return out


def _show(path, value, names):
    if SECRET_RE.search(path):
        return "(hidden)"
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None or value == "" or value == []:
        return "empty"
    if isinstance(value, (list, tuple)):
        text = ", ".join(_show(path, v, names) for v in value[:6]) + ("…" if len(value) > 6 else "")
    else:
        text = str(value)
        if SNOWFLAKE_RE.match(text) and names.get(text):
            text = names[text]
    return text if len(text) <= MAX_VALUE else text[:MAX_VALUE - 1] + "…"


def diff_lines(before, after, names=None) -> list:
    """Readable lines for every leaf that differs between two dict snapshots."""
    names = names or {}
    a, b = flatten(before or {}), flatten(after or {})
    lines = []
    for path in sorted(set(a) | set(b)):
        old, new = a.get(path), b.get(path)
        if path in a and path in b and old == new:
            continue
        if SECRET_RE.search(path):
            lines.append(f"{path}: changed (hidden)")
        elif path not in a or old in (None, "", []):
            if new in (None, "", [], False, 0):
                continue            # a default appearing for the first time isn't a change
            lines.append(f"{path}: set to {_show(path, new, names)}")
        elif path not in b or new in (None, "", []):
            lines.append(f"{path}: cleared (was {_show(path, old, names)})")
        else:
            lines.append(f"{path}: {_show(path, old, names)} -> {_show(path, new, names)}")
    return lines


def label_of(doc: dict) -> str:
    for f in ("name", "title", "command", "message", "label"):
        if doc.get(f):
            return str(doc[f])[:40]
    return str(doc.get("_id"))[:12]


def diff_collection(coll: str, before: dict, after: dict, names=None) -> list:
    """before/after = {str(_id): doc}. Added / removed / changed documents."""
    lines = []
    for _id in after.keys() - before.keys():
        lines.append(f"{coll}: added '{label_of(after[_id])}'")
    for _id in before.keys() - after.keys():
        lines.append(f"{coll}: removed '{label_of(before[_id])}'")
    for _id in before.keys() & after.keys():
        sub = diff_lines(before[_id], after[_id], names)
        if sub:
            tag = label_of(after[_id])
            lines.extend(f"{coll}[{tag}].{s}" for s in sub)
    return sorted(lines)


def cap(lines: list, limit: int = MAX_LINES) -> list:
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"…and {len(lines) - limit} more change(s)"]


def page_label(endpoint: str) -> str:
    name = (endpoint or "").split(".")[-1]
    for suffix in ("_page", "_view"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.replace("_", " ").strip().title() or "Dashboard"
