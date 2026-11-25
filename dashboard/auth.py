"""Discord OAuth2 login for dashboard, using Flask-Discord."""
from flask_discord import DiscordOAuth2Session
from config import settings


def init_discord_oauth(app):
    app.config["DISCORD_CLIENT_ID"] = settings.DISCORD_CLIENT_ID
    app.config["DISCORD_CLIENT_SECRET"] = settings.DISCORD_CLIENT_SECRET
    app.config["DISCORD_REDIRECT_URI"] = settings.DASHBOARD_REDIRECT_URI
    app.config["DISCORD_BOT_TOKEN"] = settings.DISCORD_TOKEN
    return DiscordOAuth2Session(app)


def user_is_guild_admin(guild) -> bool:
    """Server owner or Administrator permission (Manage Server alone is not enough)."""
    if getattr(guild, "owner", False):
        return True
    perms_obj = getattr(guild, "permissions", None)
    if perms_obj is None:
        return False
    if hasattr(perms_obj, "administrator"):
        try:
            return bool(perms_obj.administrator)
        except Exception:
            pass
    raw = getattr(perms_obj, "value", perms_obj)
    try:
        return bool(int(raw) & 0x8)
    except (TypeError, ValueError):
        return False


def user_can_manage_guild(guild) -> bool:
    """flask_discord Guild objects expose .permissions as either a raw int or a
    flask_discord Permission wrapper object (varies by version) - handle both,
    and fall back to the wrapper's own admin/manage_guild flags when present."""
    if getattr(guild, "owner", False):
        return True

    perms_obj = getattr(guild, "permissions", None)
    if perms_obj is None:
        return False

    # Newer flask_discord versions expose named boolean properties directly.
    if hasattr(perms_obj, "administrator") and hasattr(perms_obj, "manage_guild"):
        try:
            return bool(perms_obj.administrator) or bool(perms_obj.manage_guild)
        except Exception:
            pass

    # Some versions expose the raw bitfield as .value
    raw = getattr(perms_obj, "value", perms_obj)
    try:
        raw = int(raw)
    except (TypeError, ValueError):
        return False

    return bool(raw & 0x20) or bool(raw & 0x8)

