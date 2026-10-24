"""Async helpers for reading/writing guild_settings.logging.

Used by the bot's Logs cog. The dashboard reads/writes the same field via its
sync wrappers (dashboard/db.py get_guild_settings / update_guild_settings).
"""
from database.mongo import guild_settings_col


# Keep this in sync with dashboard/routes.py LOG_EVENTS
DEFAULT_EVENTS = {
    "message_delete": True,
    "message_edit": True,
    "member_join": True,
    "member_leave": True,
    "role_update": True,
    "channel_create": True,
    "channel_delete": True,
    "channel_update": True,
    "role_create": True,
    "role_delete": True,
    "nickname_change": True,
    "username_change": True,
    "avatar_change": True,
    "ban_add": True,
    "ban_remove": True,
    "timeout": True,
    "voice_join": False,
    "voice_leave": False,
}


async def get_logging_config(guild_id: int) -> dict:
    doc = await guild_settings_col().find_one({"_id": str(guild_id)})
    cfg = (doc or {}).get("logging") or {}
    events = dict(DEFAULT_EVENTS)
    events.update(cfg.get("events") or {})
    return {
        "log_channel_id": cfg.get("log_channel_id"),
        "transcript_channel_id": cfg.get("transcript_channel_id"),
        "events": events,
    }