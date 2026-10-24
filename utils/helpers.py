"""Guild lookup helpers used by the dashboard (REST, sync) with a small TTL cache.

These fail soft on 403 so a page doesn't 500 just because the bot isn't in
(or can't see) a particular guild. Results are cached for a couple minutes so
we don't hammer Discord's API on every dashboard request.
"""
import time
import requests
from config import settings

DISCORD_API = "https://discord.com/api/v10"
_TTL = 120  # seconds

_cache = {}  # { key: (expires_at, value) }


def _headers():
    return {"Authorization": f"Bot {settings.DISCORD_TOKEN}"}


def _cached(key, producer):
    now = time.time()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1]
    value = producer()
    _cache[key] = (now + _TTL, value)
    return value


def _fetch_channels(guild_id):
    def _do():
        resp = requests.get(
            f"{DISCORD_API}/guilds/{guild_id}/channels", headers=_headers()
        )
        if resp.status_code == 403:
            return []
        resp.raise_for_status()
        return resp.json()
    return _cached(f"channels:{guild_id}", _do)


def _fetch_roles(guild_id):
    def _do():
        resp = requests.get(
            f"{DISCORD_API}/guilds/{guild_id}/roles", headers=_headers()
        )
        if resp.status_code == 403:
            return []
        resp.raise_for_status()
        return [r for r in resp.json() if r.get("name") != "@everyone"]
    return _cached(f"roles:{guild_id}", _do)


def get_guild_channels(guild_id: int) -> list:
    """Return all channels for a guild, or [] if the bot can't see it."""
    return _fetch_channels(guild_id)


def get_text_channels(guild_id: int) -> list:
    return [c for c in get_guild_channels(guild_id) if c.get("type") == 0]


def get_categories(guild_id: int) -> list:
    return [c for c in get_guild_channels(guild_id) if c.get("type") == 4]


def get_guild_roles(guild_id: int) -> list:
    return _fetch_roles(guild_id)


def get_guild_members(guild_id: int, max_members: int = 5000) -> list:
    """All members of a guild (REST, paginated, cached). [] if the bot can't list them."""
    def _do():
        out, after = [], "0"
        try:
            while len(out) < max_members:
                resp = requests.get(
                    f"{DISCORD_API}/guilds/{guild_id}/members",
                    headers=_headers(), params={"limit": 1000, "after": after}, timeout=15,
                )
                if resp.status_code != 200:
                    break
                page = resp.json()
                if not page:
                    break
                out.extend(page)
                after = page[-1]["user"]["id"]
                if len(page) < 1000:
                    break
        except requests.RequestException:
            pass
        return out
    return _cached(f"members:{guild_id}", _do)


def get_guild_member(guild_id: int, user_id):
    """One member (nick, roles, user) via REST, cached. None if they left, the bot
    can't see them, or Discord errors/rate-limits - callers just skip them."""
    def _do():
        try:
            resp = requests.get(
                f"{DISCORD_API}/guilds/{guild_id}/members/{user_id}", headers=_headers(), timeout=10
            )
            if resp.status_code != 200:
                return None
            return resp.json()
        except requests.RequestException:
            return None
    return _cached(f"member:{guild_id}:{user_id}", _do)


def member_display_name(member: dict, fallback: str = "") -> str:
    user = (member or {}).get("user") or {}
    return (member or {}).get("nick") or user.get("global_name") or user.get("username") or fallback
