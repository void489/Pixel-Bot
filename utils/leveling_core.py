"""Leveling - pure helpers shared by the bot cog and the Flask dashboard.

No discord / database imports on purpose (the bot uses Motor, the dashboard
PyMongo), but both need identical config parsing and identical level maths.

Level curve (MEE6 / Arcane style): XP needed to go from level L to L+1 is
    5*L^2 + 50*L + 100
Only TOTAL xp is stored; the level is always derived from it, so editing xp
with /xp set can never leave a stale level behind.
"""
import re
from datetime import datetime, timezone

MAX_XP = 10 ** 10
MAX_LEVEL = 1000


def xp_for_level(level: int) -> int:
    """XP needed to go from `level` to `level + 1`."""
    return 5 * level * level + 50 * level + 100


def total_xp_for_level(level: int) -> int:
    """Total XP at which `level` begins."""
    n = max(0, int(level))
    return (5 * (n - 1) * n * (2 * n - 1)) // 6 + 25 * (n - 1) * n + 100 * n if n else 0


def progress(xp: int):
    """(level, xp_into_level, xp_needed_for_next) for a total xp value."""
    xp = max(0, min(int(xp), MAX_XP))
    level, remaining = 0, xp
    while level < MAX_LEVEL:
        need = xp_for_level(level)
        if remaining < need:
            break
        remaining -= need
        level += 1
    return level, remaining, xp_for_level(level)


def level_from_xp(xp: int) -> int:
    return progress(xp)[0]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANNOUNCE_MODES = ("current", "channel", "dm", "off")
ROLE_MODES = ("stack", "replace")

ANNOUNCE_STYLES = ("card", "text", "embed")
DEFAULT_ACCENT = "#5865f2"

DEFAULT_ANNOUNCE_MESSAGE = "🎉 {user} leveled up from **level {old_level}** to **level {level}**!"

# (placeholder, meaning) - shown on the dashboard page.
ANNOUNCE_PLACEHOLDERS = [
    ("{user}", "mentions the member"),
    ("{name}", "their display name (no ping)"),
    ("{old_level}", "the level they were"),
    ("{level}", "the level they are now"),
    ("{xp_to_next}", "XP left until the next level"),
    ("{server}", "the server name"),
]
_ANNOUNCE_RE = re.compile(r"\{(user|name|old_level|level|xp_to_next|server)\}")

DEFAULT_CONFIG = {
    "enabled": False,
    "xp_min": 15,
    "xp_max": 25,
    "cooldown_seconds": 60,
    "min_length": 3,
    "voice_enabled": True,
    "voice_xp_per_minute": 10,
    "voice_require_company": True,
    "voice_ignore_muted": True,
    "announce_mode": "current",
    "announce_channel_id": None,
    "announce_message": DEFAULT_ANNOUNCE_MESSAGE,
    "announce_style": "card",       # card = text line + image, text = text only, embed = embed
    "card_accent": DEFAULT_ACCENT,  # colour of the rank / level-up card
    "card_background_url": "",      # optional https image behind the cards
    "role_mode": "stack",
    "rewards": [],            # [{"level": 5, "role_id": "123"}]
    "role_multipliers": [],   # [{"role_id": "123", "mult": 2.0}]
    "channel_multipliers": [],  # [{"channel_id": "123", "mult": 1.5}]
    "ignored_channel_ids": [],
    "ignored_role_ids": [],
    "event_multiplier": 1.0,
    "event_ends_at": None,
}


def _int(raw, default, lo, hi):
    try:
        v = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _mult(raw, default=1.0):
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return max(0.0, min(100.0, round(v, 2)))


def _ids(raw):
    return [str(x) for x in (raw or []) if str(x).isdigit()]


def _aware(dt):
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def clean_hex(v, default: str = DEFAULT_ACCENT) -> str:
    s = str(v or "").strip()
    return "#" + s.lstrip("#").lower() if _HEX_RE.match(s) else default


def clean_https_url(v) -> str:
    """Only plain https links, no spaces, sane length (used for the card background)."""
    s = str(v or "").strip()
    return s if len(s) <= 512 and re.match(r"^https://[^\s]+$", s, re.IGNORECASE) else ""


def normalize_config(raw: dict) -> dict:
    raw = raw or {}
    d = DEFAULT_CONFIG
    xp_min = _int(raw.get("xp_min"), d["xp_min"], 0, 10000)
    xp_max = _int(raw.get("xp_max"), d["xp_max"], 0, 10000)
    if xp_max < xp_min:
        xp_min, xp_max = xp_max, xp_min

    mode = raw.get("announce_mode")
    role_mode = raw.get("role_mode")
    message = str(raw.get("announce_message") or "").strip() or DEFAULT_ANNOUNCE_MESSAGE

    rewards = {}
    for r in raw.get("rewards") or []:
        try:
            lvl = int(r.get("level"))
        except (TypeError, ValueError, AttributeError):
            continue
        rid = str(r.get("role_id") or "")
        if rid.isdigit() and 1 <= lvl <= MAX_LEVEL:
            rewards[rid] = {"level": lvl, "role_id": rid}
    rewards = sorted(rewards.values(), key=lambda r: (r["level"], r["role_id"]))

    def mults(key, id_key):
        out = {}
        for m in raw.get(key) or []:
            try:
                _id = str(m.get(id_key) or "")
            except AttributeError:
                continue
            if _id.isdigit():
                out[_id] = {id_key: _id, "mult": _mult(m.get("mult"), 1.0)}
        return list(out.values())

    return {
        "enabled": bool(raw.get("enabled", d["enabled"])),
        "xp_min": xp_min,
        "xp_max": xp_max,
        "cooldown_seconds": _int(raw.get("cooldown_seconds"), d["cooldown_seconds"], 0, 3600),
        "min_length": _int(raw.get("min_length"), d["min_length"], 0, 500),
        "voice_enabled": bool(raw.get("voice_enabled", d["voice_enabled"])),
        "voice_xp_per_minute": _int(raw.get("voice_xp_per_minute"), d["voice_xp_per_minute"], 0, 1000),
        "voice_require_company": bool(raw.get("voice_require_company", d["voice_require_company"])),
        "voice_ignore_muted": bool(raw.get("voice_ignore_muted", d["voice_ignore_muted"])),
        "announce_mode": mode if mode in ANNOUNCE_MODES else d["announce_mode"],
        "announce_channel_id": str(raw["announce_channel_id"]) if str(raw.get("announce_channel_id") or "").isdigit() else None,
        "announce_message": message[:500],
        "announce_style": raw.get("announce_style") if raw.get("announce_style") in ANNOUNCE_STYLES else d["announce_style"],
        "card_accent": clean_hex(raw.get("card_accent")),
        "card_background_url": clean_https_url(raw.get("card_background_url")),
        "role_mode": role_mode if role_mode in ROLE_MODES else d["role_mode"],
        "rewards": rewards,
        "role_multipliers": mults("role_multipliers", "role_id"),
        "channel_multipliers": mults("channel_multipliers", "channel_id"),
        "ignored_channel_ids": _ids(raw.get("ignored_channel_ids")),
        "ignored_role_ids": _ids(raw.get("ignored_role_ids")),
        "event_multiplier": _mult(raw.get("event_multiplier"), 1.0),
        "event_ends_at": _aware(raw.get("event_ends_at")),
    }


def event_active(cfg: dict, now: datetime = None) -> bool:
    ends = cfg.get("event_ends_at")
    now = now or datetime.now(timezone.utc)
    return bool(ends) and ends > now and cfg.get("event_multiplier", 1.0) != 1.0


def multiplier_for(cfg: dict, role_ids, channel_ids, now: datetime = None) -> float:
    """Highest matching role boost x highest matching channel boost x event boost."""
    roles = set(str(r) for r in role_ids)
    chans = set(str(c) for c in channel_ids)
    role_m = max([m["mult"] for m in cfg["role_multipliers"] if m["role_id"] in roles], default=1.0)
    chan_m = max([m["mult"] for m in cfg["channel_multipliers"] if m["channel_id"] in chans], default=1.0)
    event_m = cfg["event_multiplier"] if event_active(cfg, now) else 1.0
    return role_m * chan_m * event_m


def reward_roles_for(cfg: dict, level: int):
    """(roles_member_should_hold, all_reward_roles) as role-id lists, per role_mode."""
    all_ids = [r["role_id"] for r in cfg["rewards"]]
    eligible = [r for r in cfg["rewards"] if r["level"] <= level]
    if cfg["role_mode"] == "replace":
        eligible = eligible[-1:]  # rewards are sorted by level -> the highest one
    return [r["role_id"] for r in eligible], all_ids


def short_number(n) -> str:
    n = int(n)
    for div, suffix in ((10**9, "B"), (10**6, "M"), (10**3, "K")):
        if abs(n) >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


# ---------------------------------------------------------------------------
# Level-up helpers
# ---------------------------------------------------------------------------

def render_announce(template: str, *, user: str, name: str, old_level, level,
                    xp_to_next, server: str) -> str:
    """Fill the level-up placeholders in ONE pass, so a display name that happens to
    contain "{level}" can never be expanded into something else."""
    values = {
        "user": user, "name": name, "old_level": str(old_level), "level": str(level),
        "xp_to_next": f"{int(xp_to_next):,}", "server": server,
    }
    return _ANNOUNCE_RE.sub(lambda m: values[m.group(1)], str(template or ""))


def rewards_unlocked(cfg: dict, old_level: int, new_level: int) -> list:
    """Reward entries ({level, role_id}) reached by going from old_level to new_level."""
    return [r for r in cfg["rewards"] if old_level < r["level"] <= new_level]


def next_reward(cfg: dict, level: int):
    """The closest reward above `level` as {level, role_id}, or None."""
    above = [r for r in cfg["rewards"] if r["level"] > level]
    return min(above, key=lambda r: r["level"]) if above else None
