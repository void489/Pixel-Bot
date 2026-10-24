"""
Loads .env values (tokens, mongo uri, api keys).
NOTE: No channel/role IDs live here anymore - those are all configured
per-guild through the dashboard and stored in MongoDB (guild_settings collection).
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")

# Default command prefix (only used for text/hybrid invocation, can be overridden per guild via dashboard)
DEFAULT_PREFIX = os.getenv("DEFAULT_PREFIX", "/")

# MongoDB
MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "discord_bot")

# Dashboard
DASHBOARD_SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", "change-me")
DASHBOARD_REDIRECT_URI = os.getenv("DASHBOARD_REDIRECT_URI")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", 8080))
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL") or os.getenv("DASHBOARD_BASE_URI")


def dashboard_base_url() -> str:
    """Public dashboard URL without trailing slash ('' if it can't be worked out).
    Uses DASHBOARD_BASE_URL, else derives it from DASHBOARD_REDIRECT_URI."""
    base = (DASHBOARD_BASE_URL or "").strip()
    if not base and DASHBOARD_REDIRECT_URI:
        base = DASHBOARD_REDIRECT_URI.strip().rsplit("/callback", 1)[0]
    base = base.rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = "https://" + base
    return base


# Public "Add the bot" link shown on the dashboard home page (optional - if unset, a
# standard invite is built from DISCORD_CLIENT_ID).
BOT_INVITE_URL = (os.getenv("BOT_INVITE_URL") or "").strip()

# Support server link (shown by /botinfo and on the dashboard). Override with SUPPORT_URL.
SUPPORT_URL = (os.getenv("SUPPORT_URL") or "https://discord.gg/Z27SPJFECD").strip()

# Admin page (/admin). Both must be set or the admin page stays switched off.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME") or ""
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or ""

# Contact address shown on the public Privacy Policy and Terms of Service pages.
CONTACT_EMAIL = (os.getenv("CONTACT_EMAIL") or "xvoidx53@gmail.com").strip()

