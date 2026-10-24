"""Shared bits for the welcome message + auto role feature (bot and dashboard)."""
import re

DEFAULT_WELCOME_MESSAGE = "Welcome to **{server}**, {user}! You are member #{membercount}."
MAX_WELCOME_LEN = 2000

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
