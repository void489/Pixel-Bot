"""Shared bits for the Join Ad feature (used by the bot cog AND the dashboard).

Config lives in guild_settings.join_ads:

    {
      "enabled": False,
      "delay_seconds": 0,          # wait before DMing a new member
      "pace_seconds": 5,           # gap between two DMs (queue speed)
      "daily_cap": 200,            # max DMs per UTC day, 0 = no cap
      "min_account_age_days": 3,   # skip accounts younger than this
      "cooldown_days": 7,          # min days between two DMs to the same member, 0 = off
      "skip_role_ids": [],         # members with any of these roles get no DM
      "window_enabled": False, "window_start": 9, "window_end": 22, "utc_offset": 0,
      "notify_channel_id": None,   # told when a campaign ends
      "ads": [ {...}, ... ],
    }

Counters (sent / failed / recent failures) are NOT stored here - they live in the
join_ad_stats collection so saving the dashboard page can never overwrite them.
"""
import random
import re
import uuid
from datetime import datetime, timedelta, timezone

from utils.welcome import render_welcome

MAX_ADS = 20
MAX_BUTTONS = 5
MAX_CONTENT = 2000
MAX_TITLE = 256
MAX_DESC = 4000
MAX_FOOTER = 2048
MAX_URL = 512

FORMATS = ("plain", "embed", "both")

DELAY_CHOICES = [
    (0, "Instantly"),
    (30, "After 30 seconds"),
    (60, "After 1 minute"),
    (300, "After 5 minutes"),
    (900, "After 15 minutes"),
    (3600, "After 1 hour"),
    (21600, "After 6 hours"),
]
PACE_CHOICES = [
    (3, "1 DM every 3 seconds"),
    (5, "1 DM every 5 seconds"),
    (10, "1 DM every 10 seconds"),
    (30, "1 DM every 30 seconds"),
    (60, "1 DM every minute"),
]

# Why a join did not get a DM (shown on the dashboard).
SKIP_REASONS = {
    "young_account": "Account too new",
    "left": "Left before the DM was due",
    "window": "Outside the schedule window",
    "role": "Has a skip role",
    "cooldown": "Messaged too recently",
    "cap": "Daily cap reached",
    "no_ad": "No active ad",
}

DEFAULT_AD_CONTENT = (
    "# Your headline here\n"
    "## What we offer!\n"
    "> • **Something great**\n"
    "> • **Something even better**\n\n"
    "**JOIN RIGHT NOW TO NOT MISS OUT!**"
)

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_ID_RE = re.compile(r"^[a-f0-9]{8,32}$")
_URL_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _text(v, limit: int, *, multiline: bool = False) -> str:
    s = str(v or "")
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if not multiline:
        s = s.replace("\n", " ")
    return s.strip()[:limit]


def _url(v) -> str:
    s = str(v or "").strip()
    return s if len(s) <= MAX_URL and _URL_RE.match(s) else ""


def _int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        n = default
    return max(lo, min(hi, n))


def _hex(v, default: str = "#5865f2") -> str:
    s = str(v or "").strip()
    if _HEX_RE.match(s):
        return "#" + s.lstrip("#").lower()
    return default


def _id_list(v, allowed=None) -> list:
    out = []
    for x in (v or []):
        s = str(x).strip()
        if not s.isdigit() or s in out:
            continue
        if allowed is not None and s not in allowed:
            continue
        out.append(s)
    return out


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------

def default_config() -> dict:
    return {
        "enabled": False,
        "delay_seconds": 0,
        "pace_seconds": 5,
        "daily_cap": 200,
        "min_account_age_days": 3,
        "cooldown_days": 7,
        "skip_role_ids": [],
        "window_enabled": False,
        "window_start": 9,
        "window_end": 22,
        "utc_offset": 0,
        "notify_channel_id": None,
        "ads": [],
    }


def new_ad() -> dict:
    return sanitize_ad({"name": "New ad", "content": DEFAULT_AD_CONTENT, "enabled": False})


def sanitize_ad(raw: dict, prev: dict | None = None, *, now: datetime | None = None) -> dict:
    """Clean one ad. `prev` is the stored version (keeps start_at / finished_reason)."""
    raw = raw if isinstance(raw, dict) else {}
    prev = prev or {}
    now = now or utcnow()

    ad_id = str(raw.get("id") or "")
    if not _ID_RE.match(ad_id):
        ad_id = uuid.uuid4().hex[:12]

    fmt = raw.get("format") if raw.get("format") in FORMATS else "plain"
    emb = raw.get("embed") if isinstance(raw.get("embed"), dict) else {}

    buttons = []
    for b in (raw.get("buttons") or [])[:50]:
        if len(buttons) >= MAX_BUTTONS:
            break
        if not isinstance(b, dict):
            continue
        label, url = _text(b.get("label"), 80), _url(b.get("url"))
        if label and url:
            buttons.append({"label": label, "url": url})

    start_at = prev.get("start_at") if parse_iso(prev.get("start_at")) else None
    finished = prev.get("finished_reason") or None
    enabled = bool(raw.get("enabled"))
    restart = bool(raw.get("restart"))

    if restart:
        start_at, finished, enabled = _iso(now), None, True
    elif finished:
        enabled = False  # a finished campaign stays off until it is restarted
    if enabled and not start_at:
        start_at = _iso(now)

    return {
        "id": ad_id,
        "name": _text(raw.get("name"), 40) or "Untitled ad",
        "enabled": enabled,
        "weight": _int(raw.get("weight"), 1, 1, 10),
        "format": fmt,
        "content": _text(raw.get("content"), MAX_CONTENT, multiline=True),
        "hide_previews": bool(raw.get("hide_previews", True)),
        "embed": {
            "title": _text(emb.get("title"), MAX_TITLE),
            "title_url": _url(emb.get("title_url")),
            "description": _text(emb.get("description"), MAX_DESC, multiline=True),
            "color": _hex(emb.get("color")),
            "image_url": _url(emb.get("image_url")),
            "thumbnail_url": _url(emb.get("thumbnail_url")),
            "footer": _text(emb.get("footer"), MAX_FOOTER),
        },
        "buttons": buttons,
        "limit_days": _int(raw.get("limit_days"), 0, 0, 3650),
        "limit_members": _int(raw.get("limit_members"), 0, 0, 1_000_000),
        "start_at": start_at,
        "finished_reason": finished,
        "_restart": restart,  # stripped by sanitize_config
    }


def sanitize_config(raw, existing=None, *, now: datetime | None = None,
                    channel_ids=None, role_ids=None):
    """Clean a whole config. Returns (config, restarted_ad_ids).

    channel_ids / role_ids: sets of valid id strings from Discord, or None when the
    lists couldn't be fetched (then the stored values are kept instead of wiped).
    """
    raw = raw if isinstance(raw, dict) else {}
    existing = existing if isinstance(existing, dict) else {}
    now = now or utcnow()
    base = default_config()

    cfg = {
        "enabled": bool(raw.get("enabled")),
        "delay_seconds": _int(raw.get("delay_seconds"), 0, 0, 86400),
        "pace_seconds": _int(raw.get("pace_seconds"), 5, 2, 600),
        "daily_cap": _int(raw.get("daily_cap"), base["daily_cap"], 0, 100_000),
        "min_account_age_days": _int(raw.get("min_account_age_days"), 3, 0, 3650),
        "cooldown_days": _int(raw.get("cooldown_days"), 0, 0, 3650),
        "window_enabled": bool(raw.get("window_enabled")),
        "window_start": _int(raw.get("window_start"), 9, 0, 23),
        "window_end": _int(raw.get("window_end"), 22, 0, 23),
        "utc_offset": _int(raw.get("utc_offset"), 0, -12, 14),
    }

    if role_ids is None:
        cfg["skip_role_ids"] = _id_list(existing.get("skip_role_ids"))
    else:
        cfg["skip_role_ids"] = _id_list(raw.get("skip_role_ids"), role_ids)

    wanted = str(raw.get("notify_channel_id") or "")
    if channel_ids is None:
        cfg["notify_channel_id"] = existing.get("notify_channel_id")
    else:
        cfg["notify_channel_id"] = wanted if wanted in channel_ids else None

    old = {a.get("id"): a for a in (existing.get("ads") or []) if isinstance(a, dict)}
    ads, restarted, seen = [], [], set()
    for a in (raw.get("ads") or [])[:MAX_ADS]:
        if not isinstance(a, dict):
            continue
        a = dict(a)
        if str(a.get("id") or "") in seen:  # duplicated id in the payload
            a["id"] = ""
        clean = sanitize_ad(a, old.get(str(a.get("id") or "")), now=now)
        seen.add(clean["id"])
        if clean.pop("_restart"):
            restarted.append(clean["id"])
        ads.append(clean)
    cfg["ads"] = ads
    return cfg, restarted


def load_config(stored) -> dict:
    """Read a stored config (bot side). Missing/odd values fall back to defaults."""
    cfg, _ = sanitize_config(stored or {}, stored or {})
    return cfg


# ---------------------------------------------------------------------------
# campaign state
# ---------------------------------------------------------------------------

def ad_is_sendable(ad: dict) -> bool:
    fmt = ad.get("format")
    emb = ad.get("embed") or {}
    has_text = bool((ad.get("content") or "").strip())
    has_embed = bool(emb.get("title") or emb.get("description") or emb.get("image_url"))
    if fmt == "plain":
        return has_text
    if fmt == "embed":
        return has_embed
    return has_text or has_embed


def campaign_state(ad: dict, sent: int, now: datetime | None = None):
    """-> (state, reason)  state is 'active', 'off' or 'finished'."""
    now = now or utcnow()
    if ad.get("finished_reason"):
        return "finished", ad["finished_reason"]
    if not ad.get("enabled"):
        return "off", None
    start = parse_iso(ad.get("start_at"))
    days = int(ad.get("limit_days") or 0)
    if days and start and now >= start + timedelta(days=days):
        return "finished", f"reached its {days}-day limit"
    cap = int(ad.get("limit_members") or 0)
    if cap and sent >= cap:
        return "finished", f"reached {cap} members"
    return "active", None


def days_left(ad: dict, now: datetime | None = None):
    now = now or utcnow()
    start, days = parse_iso(ad.get("start_at")), int(ad.get("limit_days") or 0)
    if not start or not days:
        return None
    left = (start + timedelta(days=days) - now).total_seconds() / 86400
    return max(0.0, left)


def pick_ad(ads: list, sent_by_ad: dict, now: datetime | None = None, rng=random):
    """One random active ad (weighted), or None."""
    pool = [a for a in ads
            if ad_is_sendable(a) and campaign_state(a, sent_by_ad.get(a["id"], 0), now)[0] == "active"]
    if not pool:
        return None
    return rng.choices(pool, weights=[max(1, int(a.get("weight") or 1)) for a in pool], k=1)[0]


def in_window(cfg: dict, now: datetime | None = None) -> bool:
    if not cfg.get("window_enabled"):
        return True
    now = now or utcnow()
    hour = (now + timedelta(hours=int(cfg.get("utc_offset") or 0))).hour
    start, end = int(cfg["window_start"]), int(cfg["window_end"])
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps past midnight


# ---------------------------------------------------------------------------
# building the message
# ---------------------------------------------------------------------------

def build_message(ad: dict, *, user_mention: str, username: str, server: str, member_count) -> dict:
    """-> {content, embed (dict|None), buttons [(label, url)], suppress_embeds}"""
    def fill(s):
        return render_welcome(s, user_mention=user_mention, username=username,
                              server=server, member_count=member_count)

    fmt = ad.get("format", "plain")
    emb = ad.get("embed") or {}

    content = None
    if fmt in ("plain", "both"):
        content = fill(ad.get("content") or "")[:MAX_CONTENT].strip() or None

    embed = None
    if fmt in ("embed", "both"):
        e = {}
        title = fill(emb.get("title"))[:MAX_TITLE]
        desc = fill(emb.get("description"))[:MAX_DESC]
        footer = fill(emb.get("footer"))[:MAX_FOOTER]
        if title:
            e["title"] = title
            if emb.get("title_url"):
                e["url"] = emb["title_url"]
        if desc:
            e["description"] = desc
        if footer:
            e["footer"] = {"text": footer}
        if emb.get("image_url"):
            e["image"] = {"url": emb["image_url"]}
        if emb.get("thumbnail_url"):
            e["thumbnail"] = {"url": emb["thumbnail_url"]}
        if e:
            e["color"] = int(_hex(emb.get("color")).lstrip("#"), 16)
            embed = e

    return {
        "content": content,
        "embed": embed,
        "buttons": [(b["label"], b["url"]) for b in (ad.get("buttons") or [])][:MAX_BUTTONS],
        "suppress_embeds": fmt == "plain" and bool(ad.get("hide_previews", True)),
    }


def to_rest_payload(msg: dict) -> dict:
    """The same message as a Discord REST body (used by the dashboard's test DM)."""
    body = {}
    if msg.get("content"):
        body["content"] = msg["content"]
    if msg.get("embed"):
        body["embeds"] = [msg["embed"]]
    if msg.get("buttons"):
        body["components"] = [{
            "type": 1,
            "components": [{"type": 2, "style": 5, "label": label, "url": url}
                           for label, url in msg["buttons"]],
        }]
    if msg.get("suppress_embeds"):
        body["flags"] = 4
    return body
