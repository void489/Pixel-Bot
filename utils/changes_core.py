"""Pure helpers for "Changes" (promotion / demotion ladders, one per type).

No Discord or DB imports on purpose, so the logic is easy to test and the changes cog,
the applications cog, the LOA cog and the dashboard can all share it.

A guild has several TYPES (e.g. "staff", "builder"). Each type is
    {"id": "a1b2c3d4", "name": "staff", "channel_id": "123", "ladder": [...]}
and a ladder is a list of entries, lowest rank first:
    {"index": 0, "role_id": "123", "nick": "Member"}
    {"index": 1, "role_id": "456", "nick": "Trial Mod"}
Index 0 is always the base rank (the "Member" level). Indexes always equal the list
position. Rank inside a type = highest ladder index among the roles a member holds.

Rule: a role may be on more than one ladder ONLY as index 0 (so Staff and Builder can
both start at "Member"). Any role at index 1 or higher belongs to exactly one type.

EXTRA ROLES: each type may also carry "extra_role_ids" (up to MAX_EXTRAS), e.g. a
"Staff Team" or separator role. They are given when a member crosses 0 -> 1 in that type
(or enters straight at rung 1+) and taken away when they drop from rung 1+ back to the
base rung. They are never ladder roles (see ladders_valid) and moves like 1->2 or 3->2
never touch them. Several types may share an extra role: leaving one type keeps it while
the member is still at rung 1+ in another type that lists it.

STAFF AUTOMATION TYPE: at most ONE type may carry "staff_automation" (like nick priority). When
one does, the Staff Automation page and the bot's suggestion / probation sweeps only use that
type; with none ticked every type is used. See automation_types().

NICK FONT: a type with auto nick may also set "nick_custom_font" (off by default) and
"nick_font" (a key of NICK_FONTS, default "small_caps"). The font only restyles the RANK part
AND the member's name ("ᴛʀɪᴀʟ ᴍᴏᴅ | ꜱᴛᴇᴠᴇ"); strip_rank_prefix() recognises every font so old
prefixes are always replaced, never stacked, and unfont() turns a fonted name back to plain
letters when the font is switched or turned off.

EXCEPTION ROLES: each type may carry "exception_role_ids" (up to MAX_EXCEPTIONS), e.g. an
"Owner" or "Head Admin" role. A member holding one of them is skipped by everything tied to
that type: no strikes, no strike-threshold demotion, no /demote, no promotion suggestions,
probation reviews, weekly-report rows or activity-check duty. Use is_excepted() everywhere.
"""
import re
import unicodedata
import uuid

MAX_TYPES = 10
MAX_LADDER = 25
MAX_NICK = 32
MAX_NAME = 20
MAX_EXTRAS = 10
MAX_EXCEPTIONS = 10

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,%d}$" % MAX_NAME)


# ── Nickname fonts ─────────────────────────────────────────────────────────
_SMALL_CAPS = dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ"))
_SMALL_CAPS.update({k.upper(): v for k, v in list(_SMALL_CAPS.items())})  # capitals look the same


def _math_font(style: str):
    """Map for a Unicode 'Mathematical <style>' alphabet. Letters that live elsewhere in
    Unicode (script B, double-struck C...) are found by name, so there are no holes."""
    table = {}
    for i in range(26):
        for kind, ch in (("CAPITAL", chr(65 + i)), ("SMALL", chr(97 + i))):
            for name in (f"MATHEMATICAL {style} {kind} {ch.upper()}", f"{style} {kind} {ch.upper()}"):
                try:
                    table[ch] = unicodedata.lookup(name)
                    break
                except KeyError:
                    continue
    digits = ["ZERO", "ONE", "TWO", "THREE", "FOUR", "FIVE", "SIX", "SEVEN", "EIGHT", "NINE"]
    for d, word in enumerate(digits):
        try:
            table[str(d)] = unicodedata.lookup(f"MATHEMATICAL {style} DIGIT {word}")
        except KeyError:
            pass
    return table


def _circled_font():
    table = {}
    for i in range(26):
        table[chr(65 + i)] = chr(0x24B6 + i)
        table[chr(97 + i)] = chr(0x24D0 + i)
    table["0"] = "\u24EA"
    for d in range(1, 10):
        table[str(d)] = chr(0x2460 + d - 1)
    return table


def _wide_font():
    return {chr(c): chr(c + 0xFEE0) for c in range(0x21, 0x7F)}


# key -> (dropdown label, character map or None for plain upper-casing)
NICK_FONTS = {
    "small_caps": ("Fun small caps (Recommended)", _SMALL_CAPS),
    "caps": ("Normal caps", None),
    "bold": ("Bold", _math_font("BOLD")),
    "script": ("Script", _math_font("SCRIPT")),
    "double": ("Double-struck", _math_font("DOUBLE-STRUCK")),
    "mono": ("Monospace", _math_font("MONOSPACE")),
    "wide": ("Wide", _wide_font()),
    "circled": ("Circled", _circled_font()),
}
DEFAULT_NICK_FONT = "small_caps"


def clean_nick_font(raw) -> str:
    raw = str(raw or "").strip()
    return raw if raw in NICK_FONTS else DEFAULT_NICK_FONT


def apply_font(text: str, font) -> str:
    """Restyle `text` in a NICK_FONTS font. font None / unknown = unchanged.
    Characters the font has no version of (spaces, punctuation, accents...) stay as they are."""
    if not font or font not in NICK_FONTS:
        return text
    table = NICK_FONTS[font][1]
    if table is None:
        return text.upper()
    return "".join(table.get(ch, ch) for ch in text)


def _build_reverse():
    rev = {}
    for key, (_label, table) in NICK_FONTS.items():
        if table is None:
            continue
        back = {}
        for plain, fancy in table.items():
            if fancy == plain:
                continue
            # small caps maps both cases to one glyph: the lower-case letter wins
            if fancy not in back or plain.islower():
                back[fancy] = plain
        rev[key] = back
    return rev


_REVERSE = _build_reverse()


def unfont(text: str, keep=None) -> str:
    """Turn letters written in any nick font back into plain letters. Letters in the `keep`
    font are left alone (so re-applying that font changes nothing). Small caps cannot tell
    capitals from lower case, so those come back in lower case."""
    out = []
    for ch in text:
        for key, back in _REVERSE.items():
            if key != keep and ch in back:
                ch = back[ch]
                break
        out.append(ch)
    return "".join(out)


def nick_font_for(ctype):
    """The font key a type's nicknames use right now, or None for the plain look."""
    if ctype and ctype.get("auto_nick") and ctype.get("nick_custom_font"):
        return clean_nick_font(ctype.get("nick_font"))
    return None


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


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


def normalize_extras(raw) -> list:
    """Clean a raw list of extra role ids: strings, no blanks, no duplicates, max MAX_EXTRAS."""
    out = []
    for r in raw or []:
        r = str(r or "").strip()
        if r.isdigit() and r not in out:
            out.append(r)
    return out[:MAX_EXTRAS]


def normalize_exceptions(raw) -> list:
    """Clean a raw list of exception role ids (same rules as extras, own cap)."""
    out = []
    for r in raw or []:
        r = str(r or "").strip()
        if r.isdigit() and r not in out:
            out.append(r)
    return out[:MAX_EXCEPTIONS]


def is_excepted(member_role_ids, ctype) -> bool:
    """True if the member holds any of the type's exception roles."""
    if not ctype:
        return False
    wanted = set(ctype.get("exception_role_ids") or [])
    return bool(wanted) and any(str(r) in wanted for r in member_role_ids)


def is_excepted_anywhere(member_role_ids, types) -> bool:
    """True if the member is excepted in at least one type."""
    return any(is_excepted(member_role_ids, t) for t in types or [])


def normalize_types(raw) -> list:
    """Clean the raw list of types from Mongo."""
    out = []
    for t in raw or []:
        if not isinstance(t, dict) or not t.get("id") or not t.get("name"):
            continue
        out.append({
            "id": str(t["id"]),
            "name": str(t["name"]),
            "channel_id": str(t["channel_id"]) if t.get("channel_id") else None,
            "ladder": normalize_ladder(t.get("ladder")),
            "extra_role_ids": normalize_extras(t.get("extra_role_ids")),
            "exception_role_ids": normalize_exceptions(t.get("exception_role_ids")),
            "auto_nick": bool(t.get("auto_nick", False)),
            # Nick priority only means anything on a type that has auto nick on.
            "nick_priority": bool(t.get("nick_priority", False)) and bool(t.get("auto_nick", False)),
            "nick_custom_font": bool(t.get("nick_custom_font", False)) and bool(t.get("auto_nick", False)),
            "nick_font": clean_nick_font(t.get("nick_font")),
            "staff_automation": bool(t.get("staff_automation", False)),
        })
    # Only ONE type may hold nick priority (the first one wins if the data ever says otherwise).
    seen = False
    for t in out:
        if t["nick_priority"]:
            if seen:
                t["nick_priority"] = False
            seen = True
    # Same rule for the Staff Automation type.
    seen = False
    for t in out:
        if t["staff_automation"]:
            if seen:
                t["staff_automation"] = False
            seen = True
    return out


def automation_types(types) -> list:
    """The types Staff Automation works with: just the one ticked on the Changes page,
    or every type while none is ticked."""
    flagged = [t for t in types or [] if t.get("staff_automation")]
    return flagged[:1] if flagged else list(types or [])


def new_type_id() -> str:
    return uuid.uuid4().hex[:8]


def clean_type_name(raw):
    """One word: letters, digits, _ or -, up to MAX_NAME. Returns None if invalid."""
    raw = (raw or "").strip()
    return raw if _NAME_RE.match(raw) else None


def find_type(types, ref):
    """Look a type up by id or (case-insensitive) name."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    for t in types:
        if t["id"] == ref:
            return t
    low = ref.lower()
    for t in types:
        if t["name"].lower() == low:
            return t
    return None


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


def all_ladder_role_ids(types) -> set:
    """Every role id that sits on any ladder (any type, any rung)."""
    return {e["role_id"] for t in types for e in t["ladder"]}


def all_extra_role_ids(types) -> set:
    """Every role id used as an extra role by any type."""
    return {r for t in types for r in t.get("extra_role_ids") or []}


def ladders_valid(types) -> bool:
    """True if no role is duplicated inside a ladder, every role that appears in
    more than one place is at index 0 everywhere it appears, and no extra role is also
    a ladder role (of any type)."""
    seen = {}  # role_id -> list of indexes it appears at (across all ladders)
    for t in types:
        in_this = set()
        for e in t["ladder"]:
            if e["role_id"] in in_this:
                return False
            in_this.add(e["role_id"])
            seen.setdefault(e["role_id"], []).append(e["index"])
    if not all(len(idxs) == 1 or all(i == 0 for i in idxs) for idxs in seen.values()):
        return False
    return not (all_extra_role_ids(types) & set(seen))


def pausable_role_ids(member_role_ids, types) -> list:
    """Every ladder role at index 1 or higher (in ANY type) that the member holds -
    these are what LOA takes away. Index 0 (Member) is never touched."""
    held = {str(r) for r in member_role_ids}
    out = []
    for t in types:
        for e in t["ladder"]:
            if e["index"] >= 1 and e["role_id"] in held and int(e["role_id"]) not in out:
                out.append(int(e["role_id"]))
    return out


def crossing(old_idx: int, new_idx: int):
    """Did a rank change cross the base rung of its type?

    Returns "enter" for 0 -> 1+ (including no rank -> 1+), "leave" for 1+ -> 0 (or below),
    else None. No rank -> 0 is NOT an entry, and 1->2 / 3->2 never count."""
    if old_idx < 1 <= new_idx:
        return "enter"
    if old_idx >= 1 > new_idx:
        return "leave"
    return None


def extras_to_remove(ctype, types, held_role_ids_after) -> list:
    """Extra role ids of `ctype` to take away after the member dropped to the base rung.

    Overlap-safe: an extra that another type also lists is kept while the member is still
    at rung 1+ in that other type. `held_role_ids_after` = roles the member holds once the
    ladder role has been removed."""
    keep = set()
    for other in types:
        if other["id"] == ctype["id"]:
            continue
        if highest_index(held_role_ids_after, other["ladder"]) >= 1:
            keep.update(other.get("extra_role_ids") or [])
    return [r for r in ctype.get("extra_role_ids") or [] if r not in keep]


def loa_pause_role_ids(member_role_ids, types) -> list:
    """Everything LOA takes away: every ladder role at rung 1+ (any type) plus the extra
    roles of every type where the member is at rung 1+ (LOA drops them to rung 0 everywhere,
    so shared extras go too). Only roles the member actually holds. Rung 0 is never touched."""
    held = {str(r) for r in member_role_ids}
    out = pausable_role_ids(member_role_ids, types)
    for t in types:
        if highest_index(held, t["ladder"]) < 1:
            continue
        for r in t.get("extra_role_ids") or []:
            if r in held and int(r) not in out:
                out.append(int(r))
    return out


def strip_rank_prefix_ex(name: str, labels):
    """Like strip_rank_prefix, but returns (name_without_prefix, was_a_prefix_removed)."""
    name = name or ""
    low = name.lower()
    prefixes = set()
    for lab in {l for l in labels if l}:
        prefixes.add(lab)
        for key in NICK_FONTS:
            prefixes.add(apply_font(lab, key))
    for lab in sorted(prefixes, key=len, reverse=True):
        pre = f"{lab} | ".lower()
        if low.startswith(pre) and len(name) > len(pre):
            return name[len(pre):], True
    return name, False


def strip_rank_prefix(name: str, labels) -> str:
    """Remove a leading "<rank label> | " (one we added earlier) from a display name.
    `labels` = every rung label (nick or role name) across all ladders. Case-insensitive.
    Also recognises the label in every nick font, so switching fonts never stacks prefixes."""
    return strip_rank_prefix_ex(name, labels)[0]


def _fit(text: str, limit: int) -> str:
    """Cut `text` so it takes at most `limit` UTF-16 units (never splits a letter)."""
    while text and _utf16_len(text) > limit:
        text = text[:-1]
    return text


def build_nick(rank_label: str, base_name: str, font=None) -> str:
    """"<rank> | <name>", cut to MAX_NICK (the name part is shortened, the rank is kept).
    `font` restyles BOTH the rank and the name (see NICK_FONTS). Fancy fonts can use two
    UTF-16 units per letter, so everything is measured that way to stay safely inside
    Discord's limit."""
    prefix = f"{apply_font(rank_label, font)} | "
    if _utf16_len(prefix) >= MAX_NICK:
        cut = _fit(prefix, MAX_NICK - 1)
        return cut.rstrip() + "\u2026" if len(prefix) > len(cut) else prefix
    return prefix + _fit(apply_font(base_name, font), MAX_NICK - _utf16_len(prefix))
