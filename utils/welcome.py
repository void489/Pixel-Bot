"""Shared bits for the welcome message + auto role feature (bot and dashboard)."""
import re

DEFAULT_WELCOME_MESSAGE = "Welcome to **{server}**, {user}! You are member #{membercount}."
MAX_WELCOME_LEN = 2000
GHOSTPING_MAX_CHANNELS = 10     # Ping on Join: most channels a server can pick

# A server uses exactly ONE welcome type at a time. A missing / unknown value means
# "normal", so servers saved before types existed keep working unchanged.
WELCOME_TYPES = ("normal", "embed", "image")
DEFAULT_WELCOME_TYPE = "normal"

# Embed type: the joiner is pinged above an embed (joiner's avatar = right thumbnail).
DEFAULT_EMBED = {
    "title": "Welcome to {server}, {username}!",
    "description": "You are member #{membercount}.\nGlad to have you here!",
    "footer": "{server}",
    "color": "#C6F432",
}
EMBED_TITLE_MAX = 256
EMBED_DESC_MAX = MAX_WELCOME_LEN
EMBED_FOOTER_MAX = 200

# Image type: a text line (the joiner is always pinged) + a generated wide image.
DEFAULT_IMAGE_TEXT = "Welcome {user} to **{server}**!"
IMAGE_TEXT_MAX = 300
# Leaves section: same three types, posted when a member leaves. Nobody is pinged
# (the member is gone), so {user} is just their name there.
DEFAULT_LEAVE_MESSAGE = "**{username}** left **{server}**. We are now {membercount} members."
DEFAULT_LEAVE_EMBED = {
    "title": "{username} left {server}",
    "description": "We are now {membercount} members.",
    "footer": "{server}",
    "color": "#FF6270",
}
DEFAULT_LEAVE_IMAGE_TEXT = "**{username}** left **{server}**."
LEAVE_PLACEHOLDERS = [
    ("{user}", "their name (they left, so no ping)"),
    ("{username}", "their username"),
    ("{server}", "the server name"),
    ("{membercount}", "total members now, without them"),
]
BG_MAX_UPLOAD = 5 * 1024 * 1024         # bytes the dashboard accepts for a background upload
BG_MAX_PIXELS = 25_000_000              # refuse huge images before decoding them
BG_UPLOAD_FORMATS = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}

# (placeholder, what it becomes) - shown on the dashboard page.
PLACEHOLDERS = [
    ("{user}", "mentions the new member"),
    ("{username}", "their username (no ping)"),
    ("{server}", "the server name"),
    ("{membercount}", "total members, including them"),
]

# Roles with any of these permissions are never handed out automatically: anyone who
# can join the server would get them. (Admin, Manage Server/Roles/Channels/Webhooks/
# Messages, Kick, Ban, Timeout, Mention Everyone.)
_DANGEROUS_BITS = (1, 2, 3, 4, 5, 13, 17, 28, 29, 40)
DANGEROUS_PERMS_MASK = sum(1 << b for b in _DANGEROUS_BITS)

_PLACEHOLDER_RE = re.compile(r"\{(username|user|server|membercount)\}")


def render_welcome(template: str, *, user_mention: str, username: str,
                   server: str, member_count) -> str:
    """Fill the placeholders in one pass (so a username can't inject another one)."""
    values = {
        "user": user_mention,
        "username": username,
        "server": server,
        "membercount": str(member_count if member_count is not None else ""),
    }
    return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], str(template or ""))


def role_is_dangerous(permission_value: int) -> bool:
    return bool(int(permission_value or 0) & DANGEROUS_PERMS_MASK)


_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")


def clean_hex_color(value, default: str = DEFAULT_EMBED["color"]) -> str:
    """'#RRGGBB' (upper-case) or the default. Never raises."""
    m = _HEX_RE.match(str(value or "").strip())
    return ("#" + m.group(1).upper()) if m else default


def clean_welcome_type(value) -> str:
    value = str(value or "").strip().lower()
    return value if value in WELCOME_TYPES else DEFAULT_WELCOME_TYPE


def _text_or_default(raw, default: str, limit: int) -> str:
    # None = never saved -> default. An empty string is a real choice (e.g. no footer).
    return default if raw is None else str(raw)[:limit]


def normalize_welcome(welcome, kind: str = "welcome") -> dict:
    """The saved guild_settings["welcome"] (or ["leave"], kind="leave") dict - may be
    missing, empty or old - as a full config with safe defaults. Used by the bot AND the
    dashboard so they always agree."""
    leave = kind == "leave"
    d_embed = DEFAULT_LEAVE_EMBED if leave else DEFAULT_EMBED
    welcome = welcome if isinstance(welcome, dict) else {}
    embed = welcome.get("embed") if isinstance(welcome.get("embed"), dict) else {}
    image = welcome.get("image") if isinstance(welcome.get("image"), dict) else {}
    return {
        "channel_id": welcome.get("channel_id"),
        "type": clean_welcome_type(welcome.get("type")),
        "message": welcome.get("message") or "",
        "embed": {
            "title": _text_or_default(embed.get("title"), d_embed["title"], EMBED_TITLE_MAX),
            "description": _text_or_default(embed.get("description"), d_embed["description"], EMBED_DESC_MAX),
            "footer": _text_or_default(embed.get("footer"), d_embed["footer"], EMBED_FOOTER_MAX),
            "color": clean_hex_color(embed.get("color"), d_embed["color"]),
        },
        "image_text": _text_or_default(
            image.get("text"), DEFAULT_LEAVE_IMAGE_TEXT if leave else DEFAULT_IMAGE_TEXT, IMAGE_TEXT_MAX),
    }
