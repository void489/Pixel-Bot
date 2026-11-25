"""Live Tickets: read and answer an open ticket from the dashboard.

Everything goes through Discord's HTTP API with the bot's token (the dashboard runs inside
the bot's process but has no access to its gateway cache), so it works the same for ticket
channels and private threads.

  * messages are read by polling `GET /channels/{id}/messages` (the page asks every few
    seconds); the text is returned as plain data and the page shows it with textContent,
    so nothing from a ticket is ever interpreted as HTML
  * a reply is posted by the bot as an EMBED carrying the staff member's name and avatar and
    a small footer, with mentions restricted to (at most) the member who opened the ticket
  * an archived thread is reopened when Discord says it's archived
  * a reply sent from here is recorded as the ticket's first staff reply for analytics. It
    can't count for staff points or taskbooks: those are fed by the bot's message listener,
    which ignores the bot's own messages.
"""
import re
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests

from dashboard import botguilds

MAX_REPLY = 2000
REPLY_LIMIT = (20, 60)                # at most 20 replies per 60 s per staff member
FOOTER = "Sent from the dashboard"
REPLY_COLOR = 0x5865F2
ARCHIVED_THREAD = 50083               # Discord error code: the thread is archived
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
_NORMAL_TYPES = (0, 19)               # default message, reply
_TIMEOUT = 8
_NAME_TTL = 600


def _api(method: str, path: str, **kw):
    return requests.request(method, botguilds.DISCORD_API + path, headers=botguilds._headers(),
                            timeout=_TIMEOUT, **kw)


def _json(resp) -> dict:
    try:
        data = resp.json()
        return data if isinstance(data, (dict, list)) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def avatar_url(user: dict, size: int = 64) -> str:
    uid, h = str(user.get("id") or "0"), user.get("avatar")
    if h:
        return f"https://cdn.discordapp.com/avatars/{uid}/{h}.{'gif' if h.startswith('a_') else 'png'}?size={size}"
    return f"https://cdn.discordapp.com/embed/avatars/{(int(uid) >> 22) % 6 if uid.isdigit() else 0}.png"


_CDN = ("https://cdn.discordapp.com/", "https://media.discordapp.net/")


def _safe_cdn(url) -> str:
    """Only Discord's own CDN is ever handed to the page as an image / link source."""
    url = str(url or "")
    return url if url.startswith(_CDN) else ""


def _plain(text: str, mentions) -> str:
    """Mentions and emoji as readable text (we only know the names of mentioned users)."""
    names = {str(u.get("id")): (u.get("global_name") or u.get("username") or "user") for u in (mentions or [])}
    text = re.sub(r"<@!?(\d+)>", lambda m: "@" + names.get(m.group(1), "user"), text or "")
    text = re.sub(r"<@&\d+>", "@role", text)
    text = re.sub(r"<#\d+>", "#channel", text)
    return re.sub(r"<a?:(\w+):\d+>", r":\1:", text)


def normalize_message(m: dict) -> dict:
    """One Discord message -> the small plain-data shape the live page draws."""
    author = m.get("author") or {}
    embeds = []
    for e in (m.get("embeds") or [])[:5]:
        embeds.append({
            "title": _plain(e.get("title"), m.get("mentions"))[:256],
            "description": _plain(e.get("description"), m.get("mentions"))[:2000],
            "author_name": ((e.get("author") or {}).get("name") or "")[:256],
            "author_icon": _safe_cdn((e.get("author") or {}).get("icon_url")),
            "footer": ((e.get("footer") or {}).get("text") or "")[:200],
            "fields": [{"name": _plain(f.get("name"), None)[:256], "value": _plain(f.get("value"), m.get("mentions"))[:500]}
                       for f in (e.get("fields") or [])[:10]],
        })
    out = {
        "id": str(m.get("id")), "at": m.get("timestamp"),
        "author": {"id": str(author.get("id")), "bot": bool(author.get("bot")),
                   "name": author.get("global_name") or author.get("username") or "unknown",
                   "avatar": avatar_url(author)},
        "content": _plain(m.get("content"), m.get("mentions"))[:4000],
        "attachments": [{"url": _safe_cdn(a.get("url")), "name": (a.get("filename") or "file")[:120],
                         "image": (a.get("content_type") or "") in IMAGE_TYPES}
                        for a in (m.get("attachments") or [])[:10] if _safe_cdn(a.get("url"))],
        "embeds": embeds, "system": m.get("type", 0) not in _NORMAL_TYPES, "dashboard": False,
    }
    if out["system"] and not out["content"]:
        out["content"] = "(system message)"
    # a reply sent from this page: show it as that staff member, not as the bot
    if author.get("bot") and embeds and embeds[0]["footer"] == FOOTER:
        e = embeds[0]
        out["author"].update(name=e["author_name"] or out["author"]["name"], avatar=e["author_icon"] or out["author"]["avatar"])
        out["content"], out["embeds"], out["dashboard"] = e["description"], [], True
    return out


def fetch_messages(channel_id, after=None, limit: int = 50):
    """(messages oldest-first, error). error: None | "gone" | "forbidden" | "rate_limited" | "error"."""
    params = {"limit": max(1, min(int(limit), 100))}
    if after and str(after).isdigit():
        params["after"] = str(after)
    try:
        resp = _api("GET", f"/channels/{int(channel_id)}/messages", params=params)
    except Exception as e:
        print(f"[live] message fetch failed: {e}")
        return [], "error"
    if resp.status_code == 200:
        data = _json(resp)
        msgs = sorted((m for m in data if isinstance(m, dict) and m.get("id")), key=lambda m: int(m["id"]))
        return [normalize_message(m) for m in msgs], None
    return [], {404: "gone", 403: "forbidden", 429: "rate_limited"}.get(resp.status_code, "error")


# ---------------------------------------------------------------------------
# Replying
# ---------------------------------------------------------------------------
_hits: dict = {}
_hits_lock = threading.Lock()


def allow_reply(guild_id, user_id, now=None) -> bool:
    """Sliding window: REPLY_LIMIT[0] replies per REPLY_LIMIT[1] seconds per staff member."""
    now = time.monotonic() if now is None else now
    key = (str(guild_id), str(user_id))
    with _hits_lock:
        q = _hits.setdefault(key, deque())
        while q and now - q[0] > REPLY_LIMIT[1]:
            q.popleft()
        if len(q) >= REPLY_LIMIT[0]:
            return False
        q.append(now)
        return True


def clean_reply(text) -> str:
    return str(text or "").replace("\r\n", "\n").strip()[:MAX_REPLY]


def send_reply(channel_id, staff_name: str, staff_avatar: str, text: str, ping_user_id=None):
    """Post `text` as the bot, labelled with the staff member. (ok, error): error is None |
    "gone" | "forbidden" | "rate_limited" | "error"."""
    author = {"name": (staff_name or "Staff")[:256]}
    if staff_avatar and staff_avatar.startswith("https://"):
        author["icon_url"] = staff_avatar
    payload = {
        "embeds": [{"author": author, "description": text, "color": REPLY_COLOR, "footer": {"text": FOOTER}}],
        # nobody is pinged except (optionally) the member who opened the ticket
        "allowed_mentions": {"parse": [], "users": [str(ping_user_id)] if ping_user_id else []},
    }
    if ping_user_id:
        payload["content"] = f"<@{int(ping_user_id)}>"
    cid = int(channel_id)
    try:
        resp = _api("POST", f"/channels/{cid}/messages", json=payload)
        if resp.status_code in (400, 403) and _json(resp).get("code") == ARCHIVED_THREAD:
            _api("PATCH", f"/channels/{cid}", json={"archived": False})      # reopen the thread, try once more
            resp = _api("POST", f"/channels/{cid}/messages", json=payload)
    except Exception as e:
        print(f"[live] reply failed: {e}")
        return False, "error"
    if resp.status_code in (200, 201):
        return True, None
    return False, {404: "gone", 403: "forbidden", 429: "rate_limited"}.get(resp.status_code, "error")


def record_web_first_reply(db, channel_id, staff_id, staff_name, now=None) -> bool:
    """Count a web reply as the ticket's first staff reply (analytics), if it has none yet."""
    cid = str(channel_id)
    doc = db.ticket_stats.find_one({"_id": cid}, {"opened_at": 1, "first_reply_at": 1, "closed_at": 1})
    if not doc or doc.get("first_reply_at") or doc.get("closed_at") or not doc.get("opened_at"):
        return False
    now = now or datetime.now(timezone.utc)
    opened = doc["opened_at"]
    seconds = max(0, int((now.replace(tzinfo=None) - (opened.replace(tzinfo=None) if opened.tzinfo else opened)).total_seconds()))
    res = db.ticket_stats.update_one(
        {"_id": cid, "first_reply_at": None},
        {"$set": {"first_reply_at": now.replace(tzinfo=None), "first_reply_seconds": seconds,
                  "first_reply_by_id": str(staff_id), "first_reply_by_name": staff_name}})
    return res.modified_count == 1


# ---------------------------------------------------------------------------
# Names for the ticket list (only for tickets that don't have one saved)
# ---------------------------------------------------------------------------
_names: dict = {}


def _lookup_name(guild_id, uid) -> str:
    key = (str(guild_id), str(uid))
    hit = _names.get(key)
    if hit and time.monotonic() - hit[0] < _NAME_TTL:
        return hit[1]
    name = None
    try:
        r = _api("GET", f"/guilds/{int(guild_id)}/members/{int(uid)}")
        if r.status_code == 200:
            d = _json(r)
            u = d.get("user") or {}
            name = d.get("nick") or u.get("global_name") or u.get("username")
        elif r.status_code == 404:
            r = _api("GET", f"/users/{int(uid)}")
            if r.status_code == 200:
                d = _json(r)
                name = d.get("global_name") or d.get("username")
    except Exception as e:
        print(f"[live] name lookup failed: {e}")
    name = name or f"user {uid}"
    _names[key] = (time.monotonic(), name)
    return name


def resolve_names(guild_id, user_ids) -> dict:
    ids = sorted({str(u) for u in user_ids if str(u).isdigit()})
    if not ids:
        return {}
    with ThreadPoolExecutor(max_workers=min(6, len(ids))) as pool:
        return dict(zip(ids, pool.map(lambda u: _lookup_name(guild_id, u), ids)))
