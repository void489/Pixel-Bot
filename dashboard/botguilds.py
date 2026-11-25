"""Which servers is the bot in? (asked from Discord's API with the bot token.)

Used by the dashboard to:
  * only let people open servers the bot is actually in,
  * show "Add bot" buttons for servers it isn't in,
  * power the /admin page (server list, leave, invite).

The list is cached for ~60 seconds so a join/leave shows up quickly without
hammering Discord. If Discord can't be reached, the last known list is kept.
"""
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests

from config import settings

DISCORD_API = "https://discord.com/api/v10"
_TTL = 60  # seconds

# Permissions used when building an invite link ourselves (no BOT_INVITE_URL set):
# Administrator keeps it simple; change BOT_INVITE_URL in Railway to use your own.
_DEFAULT_PERMISSIONS = 8

_lock = threading.Lock()
_cache = {"t": 0.0, "guilds": None}  # guilds: list[dict] | None (None = never fetched)


def _headers():
    return {"Authorization": f"Bot {settings.DISCORD_TOKEN}"}


def _fetch_all():
    """Page through GET /users/@me/guilds. Raises on any failure."""
    out, after = [], None
    while True:
        params = {"limit": 200, "with_counts": "true"}
        if after:
            params["after"] = after
        resp = requests.get(f"{DISCORD_API}/users/@me/guilds",
                            headers=_headers(), params=params, timeout=15)
        resp.raise_for_status()
        page = resp.json()
        out.extend(page)
        if len(page) < 200:
            return out
        after = page[-1]["id"]


def get_bot_guilds(force: bool = False):
    """List of guild dicts the bot is in, or None if it has never been fetched
    successfully (callers should then NOT lock people out)."""
    now = time.time()
    with _lock:
        if not force and _cache["guilds"] is not None and now - _cache["t"] < _TTL:
            return _cache["guilds"]
        if not settings.DISCORD_TOKEN:
            return _cache["guilds"]
        try:
            _cache["guilds"] = _fetch_all()
            _cache["t"] = now
        except Exception as e:
            print(f"[botguilds] couldn't fetch the bot's servers: {e}")
            # keep the last known list; retry again in a few seconds, not every request
            _cache["t"] = now - _TTL + 10
        return _cache["guilds"]


def bot_in_guild(guild_id):
    """True / False, or None when we can't tell (Discord unreachable, nothing cached)."""
    guilds = get_bot_guilds()
    if guilds is None:
        return None
    return any(str(g["id"]) == str(guild_id) for g in guilds)


_member_roles_cache = {}
_MEMBER_ROLES_TTL = 60


def get_member_role_ids(guild_id, user_id):
    """Set of role IDs (strings) the member has in that server, or None when we can't tell
    (not a member, bot lacks access, Discord unreachable). Cached for a minute."""
    key = (str(guild_id), str(user_id))
    hit = _member_roles_cache.get(key)
    if hit and time.time() - hit[0] < _MEMBER_ROLES_TTL:
        return hit[1]
    roles = None
    try:
        resp = requests.get(f"{DISCORD_API}/guilds/{int(guild_id)}/members/{int(user_id)}",
                            headers=_headers(), timeout=8)
        if resp.status_code == 200:
            roles = {str(r) for r in (resp.json().get("roles") or [])}
    except Exception as e:
        print(f"[dashboard] member role lookup failed: {e}")
    if roles is not None:                 # only cache real answers
        _member_roles_cache[key] = (time.time(), roles)
    return roles


def forget():
    """Drop the cache (after leaving a server)."""
    with _lock:
        _cache["t"] = 0.0


# ---------------------------------------------------------------------------
# Invite link for adding the bot
# ---------------------------------------------------------------------------

def add_bot_url(guild_id=None) -> str:
    """The 'Add the bot' link. Uses BOT_INVITE_URL, else builds one from the client id.
    With guild_id, the server is pre-selected in Discord's invite screen."""
    base = settings.BOT_INVITE_URL
    if not base:
        if not settings.DISCORD_CLIENT_ID:
            return ""
        base = (f"https://discord.com/oauth2/authorize?client_id={settings.DISCORD_CLIENT_ID}"
                f"&permissions={_DEFAULT_PERMISSIONS}&scope=bot%20applications.commands")
    if guild_id:
        parts = urlparse(base)
        q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in ("guild_id", "disable_guild_select")]
        q += [("guild_id", str(guild_id)), ("disable_guild_select", "true")]
        base = urlunparse(parts._replace(query=urlencode(q)))
    return base


# ---------------------------------------------------------------------------
# Admin actions (called directly against Discord so the result is immediate)
# ---------------------------------------------------------------------------

def leave_guild(guild_id) -> tuple:
    """(ok, message)"""
    try:
        resp = requests.delete(f"{DISCORD_API}/users/@me/guilds/{int(guild_id)}",
                               headers=_headers(), timeout=15)
    except Exception as e:
        return False, f"Request failed: {e}"
    if resp.status_code in (200, 204):
        forget()
        return True, "Left the server."
    return False, f"Discord said {resp.status_code}: {resp.text[:200]}"


def create_invite(guild_id) -> tuple:
    """Make a 24h single-use invite from the first channel the bot may invite from.
    Returns (ok, url_or_error)."""
    try:
        resp = requests.get(f"{DISCORD_API}/guilds/{int(guild_id)}/channels",
                            headers=_headers(), timeout=15)
    except Exception as e:
        return False, f"Request failed: {e}"
    if resp.status_code != 200:
        return False, f"Couldn't read the server's channels (Discord said {resp.status_code})."

    # text (0), announcement (5), voice (2) and stage (13) channels can all hold invites
    channels = [c for c in resp.json() if c.get("type") in (0, 5, 2, 13)]
    channels.sort(key=lambda c: (c.get("type") not in (0, 5), c.get("position", 0)))
    if not channels:
        return False, "That server has no channel an invite can be made from."

    tried = 0
    for ch in channels[:15]:
        tried += 1
        try:
            r = requests.post(
                f"{DISCORD_API}/channels/{ch['id']}/invites",
                headers=_headers(), timeout=15,
                json={"max_age": 86400, "max_uses": 1, "unique": True, "temporary": False},
            )
        except Exception as e:
            return False, f"Request failed: {e}"
        if r.status_code == 200:
            return True, f"https://discord.gg/{r.json()['code']}"
        if r.status_code == 429:
            return False, "Discord is rate-limiting invites right now - try again in a moment."
        # 403 = no Create Invite permission in this channel; try the next one

    return False, (f"The bot can't create invites in this server (tried {tried} channel"
                   f"{'s' if tried != 1 else ''}). It needs the Create Invite permission "
                   f"in at least one channel.")
