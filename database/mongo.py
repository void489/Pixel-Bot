"""Motor (async) MongoDB client connection, shared across the whole bot + dashboard."""
import motor.motor_asyncio
from config import settings

_client = None
_db = None


def get_client():
    global _client
    if _client is None:
        _client = motor.motor_asyncio.AsyncIOMotorClient(settings.MONGO_URI)
    return _client


def get_db():
    global _db
    if _db is None:
        _db = get_client()[settings.MONGO_DB_NAME]
    return _db


# Collection shortcuts
def guild_settings_col():
    return get_db()["guild_settings"]


def command_permissions_col():
    return get_db()["command_permissions"]


def tickets_col():
    return get_db()["tickets"]


def ticket_types_col():
    return get_db()["ticket_types"]


def ticket_panels_col():
    return get_db()["ticket_panels"]


def transcripts_col():
    return get_db()["transcripts"]


def giveaways_col():
    return get_db()["giveaways"]


def polls_col():
    return get_db()["polls"]


def vouches_col():
    return get_db()["vouches"]


def reports_col():
    return get_db()["reports"]


def scammers_col():
    return get_db()["scammers"]


def applications_col():
    return get_db()["applications"]


def application_submissions_col():
    return get_db()["application_submissions"]


def application_panels_col():
    return get_db()["application_panels"]


def application_sessions_col():
    return get_db()["application_sessions"]


def dashboard_tasks_col():
    return get_db()["dashboard_tasks"]


def mod_cases_col():
    return get_db()["mod_cases"]


def nuke_events_col():
    return get_db()["nuke_events"]


def stickies_col():
    return get_db()["stickies"]


def count_games_col():
    return get_db()["count_games"]


def afk_col():
    return get_db()["afk"]


def blacklist_requests_col():
    return get_db()["blacklist_requests"]


def honeypot_kicks_col():
    return get_db()["honeypot_kicks"]


def reaction_roles_col():
    return get_db()["reaction_roles"]





def taskbooks_col():
    return get_db()["taskbooks"]


def bot_profiles_col():
    return get_db()["bot_profiles"]


def bot_settings_col():
    """Global (not per-server) bot settings, e.g. the activity set from /admin."""
    return get_db()["bot_settings"]


def staff_activity_checks_col():
    return get_db()["staff_activity_checks"]


def staff_activity_configs_col():
    return get_db()["staff_activity_configs"]


def levels_col():
    return get_db()["levels"]


def temp_roles_col():
    return get_db()["temp_roles"]


def reminders_col():
    return get_db()["reminders"]


def suggestions_col():
    return get_db()["suggestions"]


def mod_notes_col():
    return get_db()["mod_notes"]


def birthdays_col():
    return get_db()["birthdays"]


def temp_bans_col():
    return get_db()["temp_bans"]


def invite_joins_col():
    return get_db()["invite_joins"]


def automod_strikes_col():
    return get_db()["automod_strikes"]


def role_panels_col():
    return get_db()["role_panels"]


def loa_settings_col():
    return get_db()["loa_settings"]


def loa_requests_col():
    return get_db()["loa_requests"]


def staff_strikes_col():
    return get_db()["staff_strikes"]


def staff_strike_audit_col():
    return get_db()["staff_strike_audit"]


def help_panels_col():
    return get_db()["help_panels"]



def ticket_ratings_col():
    return get_db()["ticket_ratings"]


def promotion_history_col():
    return get_db()["promotion_history"]


def staff_suggestions_col():
    return get_db()["staff_suggestions"]


def staff_probation_col():
    return get_db()["staff_probation"]


def staff_report_state_col():
    return get_db()["staff_report_state"]


def staff_report_snapshots_col():
    return get_db()["staff_report_snapshots"]


def scheduled_announcements_col():
    return get_db()["scheduled_announcements"]


def ticket_counters_col():
    """One doc per server: {_id: guild_id, seq: last ticket number} (ticket {number})."""
    return get_db()["ticket_counters"]


def ticket_stats_col():
    """One small doc per ticket (_id = channel ID), filled in at ticket events for the
    dashboard's Ticket Analytics page. See utils/ticket_stats.py."""
    return get_db()["ticket_stats"]


def ticket_automations_col():
    """Ticket automation rules (see utils/ticket_automation.py)."""
    return get_db()["ticket_automations"]


def ticket_automation_runs_col():
    """Log of automation runs (kept 30 days)."""
    return get_db()["ticket_automation_runs"]
