"""Dashboard routes: home, ticket types + panels manager, transcripts, permissions,
config/logs, vouches, applications, moderation, auto-mod, role panels, security,
blacklist requests."""
import re
import hashlib
import io
import json
import time
import requests
from flask import (
    Blueprint, Response, render_template, request, redirect, url_for, session, abort, jsonify, make_response,
)

from dashboard import db as dashboard_db
from dashboard import botguilds
from dashboard.admin import admin_logged_in
from dashboard.auth import user_can_manage_guild, user_is_guild_admin
from dashboard import transcript_search
from dashboard import ticket_analytics
from dashboard import ticket_live
from utils import ticket_settings as ts_cfg
from utils import ticket_automation as ta_rules
from utils import transcript_access as ta
from dashboard.transcript_render import render_markdown, prepare_messages
from utils.helpers import (
    get_text_channels, get_categories, get_guild_roles, get_guild_channels,
    get_guild_member, get_guild_members, member_display_name,
)
from utils.spawner_math import format_short, parse_price, parse_amount
from config.command_groups import GROUP_ORDER, GROUP_INFO, group_for
from utils import leveling_core as lv
from utils.welcome import (
    DEFAULT_WELCOME_MESSAGE, MAX_WELCOME_LEN, PLACEHOLDERS, role_is_dangerous,
)
from utils import join_ads as ja
from database.automod_config import (
    AUTOMOD_RULES, AUTOMOD_LIMIT_BOUNDS, AUTOMOD_STRIKE_BOUNDS, MAX_BADWORDS,
    MAX_ALLOWED_DOMAINS, clean_domain,
)

bp = Blueprint("dashboard", __name__)

_GUILD_CACHE_TTL = 300  # seconds

SPAWNER_NOTES_MAX = 2000
MAX_SPAWNER_TYPES = 25  # a Discord select menu holds 25 options (same as the bot)
_SP_CUSTOM_EMOJI = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")
_SP_TRAILING = re.compile(r"\s*spawners?\s*$", re.IGNORECASE)


def _spawner_key(name: str) -> str:
    """Same slug the bot's /spawner add uses, so both sides edit the same type."""
    return re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")[:32]


def _sorted_spawner_types(types: dict) -> list:
    """[(key, type), ...] in panel order (same rule as the bot's sort_spawner_types)."""
    items = list((types or {}).items())
    items.sort(key=lambda kv: kv[1].get("order") if isinstance(kv[1].get("order"), int) else 10**9)
    return items


def _spawner_emoji_ok(text: str) -> bool:
    text = (text or "").strip()
    if _SP_CUSTOM_EMOJI.match(text):
        return True
    return 0 < len(text) <= 16 and not any(c.isalnum() or c.isspace() for c in text)
# Pre-filled in the Spawners tab until notes have been saved once (it's just text -
# edit or clear it on the page).
DEFAULT_SPAWNER_NOTES = (
    "**WE ARE NOT SELLING ANY OTHER SPAWNERS THAT AREN'T LISTED!**\n\n"
    "*TO BUY OR SELL MAKE A TICKET*\n\n"
    "-# Only /tpaccept if the username is provided in the ticket (not DMs). "
    "We are not responsible for scams."
)

LOG_EVENTS = [
    ("message_delete",  "Message Deleted",       "#ED4245"),
    ("message_edit",    "Message Edited",        "#FEE75C"),
    ("member_join",     "Member Joined",         "#57F287"),
    ("member_leave",    "Member Left",           "#EB459E"),
    ("role_update",     "Member Roles Updated",  "#5865F2"),
    ("nickname_change", "Nickname Changed",      "#5865F2"),
    ("username_change", "Username Changed",      "#5865F2"),
    ("avatar_change",   "Avatar Changed",        "#5865F2"),
    ("timeout",         "Timeout Added/Removed", "#E67E22"),
    ("ban_add",         "Member Banned",         "#E67E22"),
    ("ban_remove",      "Member Unbanned",       "#57F287"),
    ("channel_create",  "Channel Created",       "#1ABC9C"),
    ("channel_delete",  "Channel Deleted",       "#ED4245"),
    ("channel_update",  "Channel Updated",       "#1ABC9C"),
    ("role_create",     "Role Created",          "#5865F2"),
    ("role_delete",     "Role Deleted",          "#ED4245"),
    ("voice_join",      "Voice Join",            "#57F287"),
    ("voice_leave",     "Voice Leave",           "#EB459E"),
]


class _GuildShim:
    __slots__ = ("id", "name", "owner", "permissions")

    def __init__(self, d):
        self.id = int(d["id"])
        self.name = d["name"]
        self.owner = d["owner"]
        self.permissions = d["permissions"]


def _get_manageable_guilds(discord_session):
    now = time.time()
    cached = session.get("_guilds_cache")
    if cached and now - cached.get("t", 0) < _GUILD_CACHE_TTL:
        raw = cached["g"]
    else:
        raw = []
        for g in discord_session.fetch_guilds():
            perms_obj = getattr(g, "permissions", None)
            perms_value = getattr(perms_obj, "value", perms_obj)
            try:
                perms_value = int(perms_value) if perms_value is not None else None
            except (TypeError, ValueError):
                perms_value = None
            raw.append({
                "id": str(g.id),
                "name": g.name,
                "owner": bool(getattr(g, "owner", False)),
                "permissions": perms_value,
            })
        session["_guilds_cache"] = {"t": now, "g": raw}

    return [_GuildShim(d) for d in raw]


def _is_authed(discord_session) -> bool:
    """Logged in with Discord, or with the /admin login."""
    return admin_logged_in() or discord_session.authorized


def _audit_actor(discord_session):
    """(id, name) of whoever is using the dashboard right now, for the audit log."""
    if admin_logged_in():
        return "admin", "Admin login"
    try:
        u = discord_session.fetch_user()
        return str(u.id), str(u.name)
    except Exception:
        return "unknown", "Unknown user"


def _require_guild_access(discord_session, guild_id):
    """Gate for every /guild/<id>/... page.
      * the bot must actually be in that server (if we can't tell, we let it through),
      * the /admin login can open any server the bot is in,
      * everyone else needs Manage Server / Administrator there.
    """
    admin = admin_logged_in()

    if botguilds.bot_in_guild(guild_id) is False:
        if admin:
            abort(redirect(url_for("admin.admin_page", error="notinserver")))
        abort(redirect(url_for("dashboard.index", notice="nobot")))

    if admin:
        name = next((g.get("name") for g in (botguilds.get_bot_guilds() or [])
                     if str(g["id"]) == str(guild_id)), None) or str(guild_id)
        try:
            return _GuildShim({"id": guild_id, "name": name, "owner": True, "permissions": 8})
        except (TypeError, ValueError):
            abort(404)

    for g in _get_manageable_guilds(discord_session):
        if str(g.id) == str(guild_id) and user_can_manage_guild(g):
            return g
    abort(403)


def _hex_to_int(hex_str: str, default: int = 0x5865F2) -> int:
    try:
        return int(hex_str.lstrip("#"), 16)
    except (ValueError, AttributeError):
        return default


def _parse_id_list(raw: str) -> list:
    if not raw:
        return []
    parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
    out = []
    for p in parts:
        if p and p.isdigit():
            out.append(p)
    return out


def _looks_like_emoji(text: str) -> bool:
    """A custom emoji like <:name:123> / <a:name:123>, or a short unicode emoji (no letters)."""
    import re
    if re.fullmatch(r"<a?:\w{2,32}:\d{15,25}>", text):
        return True
    return len(text) <= 12 and not any(ch.isascii() and ch.isalnum() for ch in text)


def register_routes(app, discord_session):
    app.jinja_env.filters["discord_md"] = render_markdown
    app.jinja_env.filters["short_num"] = format_short
    app.jinja_env.filters["dur"] = ticket_analytics.duration_label
    from config import settings as _settings
    app.jinja_env.globals["support_url"] = _settings.SUPPORT_URL

    @bp.route("/")
    def index():
        base = dict(
            add_bot_url=botguilds.add_bot_url(),
            notice=request.args.get("notice"),
            is_admin=admin_logged_in(),
        )
        if not discord_session.authorized:
            try:
                _bg = botguilds.get_bot_guilds()
                base["guild_count"] = len(_bg) if _bg else 0
            except Exception:
                base["guild_count"] = 0
            return render_template("index.html", **base)

        manageable = [g for g in _get_manageable_guilds(discord_session)
                      if user_can_manage_guild(g)]
        bot_guilds = botguilds.get_bot_guilds()
        if bot_guilds is None:
            # Discord couldn't be asked and nothing is cached: don't lock anyone out.
            with_bot, without_bot = manageable, []
        else:
            bot_ids = {str(g["id"]) for g in bot_guilds}
            with_bot = [g for g in manageable if str(g.id) in bot_ids]
            without_bot = [(g, botguilds.add_bot_url(g.id))
                           for g in manageable if str(g.id) not in bot_ids]
        # Servers whose Setup Guide this browser has already opened go straight to the dashboard.
        seen_setup = {str(g.id) for g in with_bot if request.cookies.get(f"setup_seen_{g.id}")}
        return render_template("index.html", guilds=with_bot, without_bot=without_bot,
                               seen_setup=seen_setup, logged_in=True, **base)

    # Public legal pages (no login) - the URLs Discord asks for when verifying the bot.
    LEGAL_UPDATED = "October 15, 2026"

    @bp.route("/privacy")
    def privacy():
        from config import settings
        return render_template("privacy.html", contact_email=settings.CONTACT_EMAIL,
                               updated=LEGAL_UPDATED)

    @bp.route("/terms")
    def terms():
        from config import settings
        return render_template("terms.html", contact_email=settings.CONTACT_EMAIL,
                               updated=LEGAL_UPDATED)

    # Setup guide: a public copy (/setup) for people deciding whether to add the bot, and a
    # per-server copy inside the dashboard whose steps link straight to that server's pages.
    @bp.route("/setup")
    def setup_public():
        return render_template("setup.html", guild_id=None, add_bot_url=botguilds.add_bot_url())

    @bp.route("/guild/<guild_id>/setup")
    def setup_guild_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)
        resp = make_response(render_template("setup.html", guild_id=guild_id,
                                             add_bot_url=botguilds.add_bot_url(guild_id)))
        resp.set_cookie(f"setup_seen_{guild_id}", "1", max_age=60 * 60 * 24 * 365,
                        samesite="Lax", httponly=True)
        return resp

    @bp.route("/login")
    def login():
        return discord_session.create_session(scope=["identify", "guilds"])

    @bp.route("/callback")
    def callback():
        discord_session.callback()
        session.pop("_guilds_cache", None)
        return redirect(url_for("dashboard.index"))

    @bp.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("dashboard.index"))

    # -----------------------------------------------------------------
    # Ticket types + panels
    # -----------------------------------------------------------------
    def _category_entry(guild_id, raw):
        """A category typed in a form -> what gets stored: its ID if a category with that
        name (or that ID) exists, else "__new__:Name" (created when first needed and
        removed again when empty, like a ticket type's main category). Blank -> None."""
        raw = (raw or "").strip()
        if not raw:
            return None
        known = get_categories(int(guild_id))
        for c in known:
            if raw == str(c["id"]) or raw.lower() == str(c["name"]).strip().lower():
                return str(c["id"])
        return "__new__:" + raw[:100]

    def _overflow_from_form(guild_id, primary_id=None):
        """The spare categories typed in a form (max 5, no duplicates, never the main one)."""
        from utils.ticket_settings import clean_overflow_ids
        entries = [_category_entry(guild_id, v) for v in request.form.getlist("overflow_category_names")]
        return clean_overflow_ids([e for e in entries if e], primary_id)

    def _role_ids(guild_id):
        return {str(r["id"]) for r in get_guild_roles(int(guild_id))}

    def _ids_from_form(field, known):
        """IDs picked in a multi-select, limited to ones that really exist (if the list of
        what exists couldn't be loaded, the submitted IDs are kept)."""
        picked = [str(v) for v in request.form.getlist(field)]
        return [v for v in picked if v in known] if known else picked

    @bp.route("/guild/<guild_id>/tickets", methods=["GET", "POST"])
    def tickets_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            form_type = request.form.get("form_type")

            if form_type == "save_ticket_type":
                allow_roles = request.form.getlist("allow_roles")
                pairs = [(request.form.get(f"q{i}", "").strip(),
                          request.form.get(f"r{i}") == "on") for i in range(5)]
                pairs = [(q, r) for q, r in pairs if q]   # blank questions drop out
                questions = [q for q, _ in pairs]
                questions_required = [r for _, r in pairs]
                color_hex = request.form.get("color", "#5865f2")

                category_mode = request.form.get("category_mode", "existing")
                category_id = None
                if category_mode == "new":
                    category_new_name = (request.form.get("category_new_name") or "").strip()
                    if category_new_name:
                        category_id = f"__new__:{category_new_name}"
                else:
                    category_id = request.form.get("category_id") or None

                # Thread mode: tickets are private threads in one chosen text channel, so a
                # category isn't needed - but that channel must be a real text channel.
                thread_mode = request.form.get("thread_mode") == "on"
                thread_parent_id = (request.form.get("thread_parent_id") or "").strip()
                if thread_mode:
                    known = {str(c["id"]) for c in get_text_channels(int(guild_id))}
                    if not thread_parent_id.isdigit() or (known and thread_parent_id not in known):
                        return redirect(url_for(
                            "dashboard.tickets_page", guild_id=guild_id, error="thread_channel_required"
                        ))
                elif not category_id:
                    return redirect(url_for(
                        "dashboard.tickets_page", guild_id=guild_id, error="category_required"
                    ))

                data = {
                    "name": request.form.get("name", "").strip(),
                    "emoji": request.form.get("emoji", "").strip(),
                    "button_style": request.form.get("button_style", "primary"),
                    "description": request.form.get("description", "").strip(),
                    "color": _hex_to_int(color_hex),
                    "color_hex": color_hex,
                    "category_id": category_id,
                    "ping_role_id": request.form.get("ping_role_id") or None,
                    "allow_role_ids": allow_roles,
                    "questions": questions,
                    "questions_required": questions_required,
                    "auto_rename": request.form.get("auto_rename", "").strip(),
                    "allow_blacklisted": request.form.get("allow_blacklisted") == "on",
                    "overflow_category_ids": _overflow_from_form(guild_id, category_id),
                    # per-panel settings (see utils/ticket_settings.py)
                    "advanced": True,
                    "button_label": request.form.get("button_label", ""),
                    "max_open_tickets": request.form.get("max_open_tickets", "0"),
                    "required_role_ids": _ids_from_form("required_role_ids", _role_ids(guild_id)),
                    "welcome_message": request.form.get("welcome_message", ""),
                    "created_message": request.form.get("created_message", ""),
                    "claiming_enabled": request.form.get("claiming_enabled") == "on",
                    "claim_only_visible": request.form.get("claim_only_visible") == "on",
                    "transcript_access": request.form.get("transcript_access", "managers"),
                    "thread_mode": thread_mode,
                    "thread_parent_id": thread_parent_id,
                    "thread_auto_archive": request.form.get("thread_auto_archive", ""),
                    "thread_invitable": request.form.get("thread_invitable") == "on",
                    "web_reply": request.form.get("web_reply") == "on",
                    "priority_enabled": request.form.get("priority_enabled") == "on",
                    "priority_rename": request.form.get("priority_rename", "off"),
                    "save_transcripts": request.form.get("save_transcripts") == "on",
                    "linked_type_ids": _ids_from_form(
                        "linked_type_ids",
                        {str(t["_id"]) for t in dashboard_db.get_ticket_types(guild_id)}),
                    "buttons": {
                        key: {
                            "label": request.form.get(f"btn_{key}_label", ""),
                            "emoji": request.form.get(f"btn_{key}_emoji", ""),
                            "style": request.form.get(f"btn_{key}_style", ""),
                        } for key in ("close", "claim", "unclaim")
                    },
                }
                dashboard_db.upsert_ticket_type(
                    guild_id, data, request.form.get("type_id") or None
                )

            elif form_type == "save_ticket_categories":
                # Server-wide categories: used by claim / spawner / application tickets
                # (and by any type without a category). Typed names, like a ticket type's
                # category. Dotted keys, so nothing else under "tickets" is touched.
                fixed = _category_entry(guild_id, request.form.get("fixed_category_name"))
                dashboard_db.update_guild_settings(guild_id, {
                    "tickets.fixed_category_id": fixed,
                    "tickets.overflow_category_ids": _overflow_from_form(guild_id, fixed),
                })

            elif form_type == "delete_ticket_type":
                dashboard_db.delete_ticket_type(request.form.get("type_id"))

            elif form_type == "save_panel":
                color_hex = request.form.get("panel_color", "#5865f2")
                type_ids = request.form.getlist("panel_type_ids")
                data = {
                    "name": request.form.get("panel_name", "").strip(),
                    "title": request.form.get("panel_title", "").strip(),
                    "description": request.form.get("panel_desc", "").strip(),
                    "color": _hex_to_int(color_hex),
                    "color_hex": color_hex,
                    "ticket_type_ids": type_ids,
                    "display_style": request.form.get("panel_display_style", "buttons"),
                    "placeholder": request.form.get("panel_placeholder", "").strip(),
                    "auto_close_warn_hours": request.form.get("auto_close_warn_hours", "0"),
                    "auto_close_grace_hours": request.form.get("auto_close_grace_hours", "24"),
                    "auto_close_claimed": request.form.get("auto_close_claimed", "longer"),
                }
                dashboard_db.upsert_ticket_panel(
                    guild_id, data, request.form.get("panel_id") or None
                )

            elif form_type == "delete_panel":
                dashboard_db.delete_ticket_panel(request.form.get("panel_id"))

            elif form_type == "post_panel":
                panel_id = request.form.get("panel_id")
                channel_id = request.form.get("channel_id")
                panel = dashboard_db.get_ticket_panel(panel_id)
                if panel:
                    dashboard_db.upsert_ticket_panel(
                        guild_id,
                        {
                            "name": panel["name"],
                            "title": panel.get("title"),
                            "description": panel.get("description"),
                            "color": panel.get("color"),
                            "color_hex": panel.get("color_hex"),
                            "ticket_type_ids": panel.get("ticket_type_ids", []),
                            "display_style": panel.get("display_style", "buttons"),
                            "placeholder": panel.get("placeholder", ""),
                            "panel_channel_id": channel_id,
                        },
                        panel_id,
                    )

            return redirect(url_for("dashboard.tickets_page", guild_id=guild_id, saved=1))

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        ticket_types = dashboard_db.get_ticket_types(guild_id)
        panels = dashboard_db.get_ticket_panels(guild_id)

        types_by_id = {str(t["_id"]): t for t in ticket_types}
        for p in panels:
            p["assigned_types"] = [
                types_by_id[tid] for tid in p.get("ticket_type_ids", [])
                if tid in types_by_id
            ]

        # names shown in the server-wide category inputs (an ID -> its name, "__new__:X" -> X)
        _cat_names = {str(c["id"]): c["name"] for c in get_categories(int(guild_id))}
        _entry_name = lambda e: (str(e)[len("__new__:"):] if str(e).startswith("__new__:")
                                 else _cat_names.get(str(e), ""))
        _tcfg = settings_doc.get("tickets") or {}
        server_main_name = _entry_name(_tcfg.get("fixed_category_id") or "")
        server_spare_names = [n for n in (_entry_name(e) for e in (_tcfg.get("overflow_category_ids") or [])) if n]

        return render_template(
            "tickets.html",
            guild_id=guild_id,
            server_main_name=server_main_name,
            server_spare_names=server_spare_names,
            settings=settings_doc,
            ticket_types=ticket_types,
            panels=panels,
            channels=get_text_channels(int(guild_id)),
            categories=get_categories(int(guild_id)),
            roles=get_guild_roles(int(guild_id)),
            transcripts=dashboard_db.get_transcripts(guild_id),
            saved=request.args.get("saved"),
            error=request.args.get("error"),
        )

    # ---- who may open which transcript (per-panel setting `transcript_access`) ----------
    def _viewer(guild_id):
        """What the current person is on this server. Never aborts."""
        if admin_logged_in():
            return {"is_admin": True, "is_manager": True, "user_id": None}
        is_admin = is_manager = False
        for g in _get_manageable_guilds(discord_session):
            if str(g.id) == str(guild_id):
                is_manager = user_can_manage_guild(g)
                is_admin = user_is_guild_admin(g)
        try:
            user_id = str(discord_session.fetch_user().id)
        except Exception:
            user_id = None
        return {"is_admin": is_admin, "is_manager": is_manager, "user_id": user_id}

    def _staff_role_ids(guild_id, type_doc):
        ids = {str(r) for r in ((type_doc or {}).get("allow_role_ids") or [])}
        cfg = dashboard_db.get_guild_settings(guild_id).get("tickets") or {}
        return ids | {str(r) for r in (cfg.get("support_role_ids") or [])}

    def _has_staff_role(guild_id, viewer, type_doc):
        if not viewer["user_id"]:
            return False
        roles = botguilds.get_member_role_ids(guild_id, viewer["user_id"])
        return bool(roles and roles & _staff_role_ids(guild_id, type_doc))

    def _type_of_transcript(guild_id, transcript):
        tid = transcript.get("ticket_type_id")
        doc = dashboard_db.get_ticket_type(tid) if tid else None
        return doc if doc and str(doc.get("guild_id")) == str(guild_id) else None

    def _transcript_perms(guild_id, transcript):
        """(can_open, can_share) for the current person and this transcript."""
        viewer = _viewer(guild_id)
        type_doc = _type_of_transcript(guild_id, transcript)
        mode = ta.clean_mode((type_doc or {}).get("transcript_access"))
        staff = mode != "managers" and not viewer["is_admin"] and _has_staff_role(guild_id, viewer, type_doc)
        is_opener = bool(viewer["user_id"]) and str(transcript.get("opener_id")) == viewer["user_id"]
        kw = dict(is_admin=viewer["is_admin"], is_manager=viewer["is_manager"], has_staff_role=staff)
        return (ta.can_open(mode, is_opener=is_opener, **kw), ta.can_share(mode, **kw))

    def _denied_type_ids(guild_id):
        """Ticket types whose transcripts the current person may NOT open (hidden from the
        list). Only panels with a staff / opener rule can be denied."""
        viewer = _viewer(guild_id)
        if viewer["is_admin"]:
            return []
        denied = []
        for t in dashboard_db.get_ticket_types(guild_id):
            mode = ta.clean_mode(t.get("transcript_access"))
            if mode == "managers":
                continue
            staff = _has_staff_role(guild_id, viewer, t)
            if not ta.can_open(mode, is_admin=False, is_manager=viewer["is_manager"],
                               has_staff_role=staff, is_opener=False):
                denied.append(str(t["_id"]))
        return denied

    def _open_transcript(guild_id, transcript_id, need_share=False):
        """Login + 'bot is in the server' + per-panel access, then (transcript, can_share).
        Aborts with a redirect / 403 / 404 otherwise."""
        if not _is_authed(discord_session):
            abort(redirect(url_for("dashboard.login")))
        if botguilds.bot_in_guild(guild_id) is False:
            if admin_logged_in():
                abort(redirect(url_for("admin.admin_page", error="notinserver")))
            abort(redirect(url_for("dashboard.index", notice="nobot")))
        transcript = dashboard_db.get_transcript(transcript_id)
        if not transcript or transcript.get("guild_id") != str(guild_id):
            abort(404)
        can_open, can_share = _transcript_perms(guild_id, transcript)
        if not can_open or (need_share and not can_share):
            abort(403)
        return transcript, can_share

    _TRANSCRIPT_CSP = ("default-src 'none'; style-src 'unsafe-inline'; img-src 'self' https: data:; "
                       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

    @bp.route("/guild/<guild_id>/transcripts")
    def transcripts_page(guild_id):
        """Search / filter / page through every closed-ticket transcript."""
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        PER_PAGE = 25
        try:
            page = max(int(request.args.get("page", 1)), 1)
        except (TypeError, ValueError):
            page = 1

        query, f = transcript_search.build(guild_id, request.args, _denied_type_ids(guild_id))
        rows, total = dashboard_db.search_transcripts(query, page, PER_PAGE)
        pages = max((total + PER_PAGE - 1) // PER_PAGE, 1)
        args = {k: v for k, v in (
            ("q", f["q"]), ("member", f["member"]), ("staff", f["staff"]), ("panel", f["panel"]),
            ("text", f["text"]), ("rating", f["rating"]), ("priority", f["priority"]),
            ("from", f["date_from"]), ("to", f["date_to"]), ("days", f["days"] or ""),
            ("in_msgs", "1" if f["in_msgs"] else "")) if v}

        def page_url(n):
            return url_for("dashboard.transcripts_page", guild_id=guild_id, page=n, **args)

        return render_template(
            "transcripts.html", guild_id=guild_id, rows=rows, total=total, page=page, pages=pages,
            f=f, panels=dashboard_db.transcript_panel_names(guild_id), filtered=bool(args),
            prev_url=page_url(page - 1) if page > 1 else None,
            next_url=page_url(page + 1) if page < pages else None,
        )

    def _render_transcript(transcript, file_url, **ctx):
        """The read-only transcript page (shared by the dashboard view and share links)."""
        created = transcript.get("created_at")
        resp = Response(render_template(
            "transcript.html", t=transcript,
            messages=prepare_messages(transcript.get("messages"), file_url),
            closed_at=created.strftime("%Y-%m-%d %H:%M UTC") if created else "", **ctx,
        ), mimetype="text/html")
        resp.headers["Content-Security-Policy"] = _TRANSCRIPT_CSP
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @bp.route("/guild/<guild_id>/transcripts/<transcript_id>")
    def view_transcript(guild_id, transcript_id):
        transcript, can_share = _open_transcript(guild_id, transcript_id)

        # Old-style transcripts were stored as a raw HTML string. Serve them
        # sandboxed (no scripts) since their message text was never escaped.
        if "messages" not in transcript:
            return Response(
                transcript.get("html", ""), mimetype="text/html",
                headers={"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"},
            )

        def file_url(file_id):
            return url_for("dashboard.transcript_file", guild_id=guild_id,
                           transcript_id=transcript_id, file_id=file_id)

        return _render_transcript(
            transcript, file_url, guild_id=guild_id, share=False, can_share=can_share,
            share_page=url_for("dashboard.transcript_share_page", guild_id=guild_id,
                               transcript_id=transcript_id),
        )

    _IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}

    @bp.route("/guild/<guild_id>/transcripts/<transcript_id>/file/<file_id>")
    def transcript_file(guild_id, transcript_id, file_id):
        _open_transcript(guild_id, transcript_id)
        f = dashboard_db.get_transcript_file(file_id)
        meta = (getattr(f, "metadata", None) or {}) if f is not None else {}
        if (f is None or meta.get("guild_id") != str(guild_id)
                or meta.get("transcript_id") != str(transcript_id)
                or meta.get("content_type") not in _IMAGE_TYPES):
            abort(404)
        return Response(f.read(), mimetype=meta["content_type"], headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    # -----------------------------------------------------------------
    # Transcript share links (expiring, revocable)
    # -----------------------------------------------------------------
    _SHARE_UNITS = {"hours": 3600, "days": 86400}

    @bp.route("/guild/<guild_id>/transcripts/<transcript_id>/share", methods=["GET", "POST"])
    def transcript_share_page(guild_id, transcript_id):
        transcript, _ = _open_transcript(guild_id, transcript_id, need_share=True)
        message = error = None

        if request.method == "POST":
            who = _audit_actor(discord_session)
            action = request.form.get("form_type")
            if action == "create_share":
                unit = request.form.get("unit", "hours")
                try:
                    amount = int(request.form.get("amount", ""))
                except ValueError:
                    amount = 0
                seconds = amount * _SHARE_UNITS.get(unit, 0)
                if not (dashboard_db.SHARE_MIN_SECONDS <= seconds <= dashboard_db.SHARE_MAX_SECONDS):
                    error = "Pick between 1 hour and 30 days."
                else:
                    dashboard_db.create_transcript_share(guild_id, transcript_id, *who, seconds)
                    dashboard_db.log_audit(
                        guild_id, *who, "transcript_share_created",
                        f"#{transcript.get('channel_name')} - link valid for {amount} {unit}")
                    message = "Link created. Anyone who has it can read this transcript until it expires."
            elif action == "revoke_share":
                if dashboard_db.revoke_transcript_share(guild_id, request.form.get("share_id", ""), who[1]):
                    dashboard_db.log_audit(guild_id, *who, "transcript_share_revoked",
                                           f"#{transcript.get('channel_name')}")
                    message = "Link revoked. It stops working right away."
                else:
                    error = "That link is already revoked or doesn't exist."

        shares = []
        for sh in dashboard_db.get_transcript_shares(guild_id, transcript_id):
            sh["state"] = dashboard_db.share_state(sh)
            sh["url"] = url_for("dashboard.shared_transcript", token=sh["token"], _external=True)
            shares.append(sh)
        return render_template(
            "transcript_share.html", guild_id=guild_id, t=transcript, shares=shares,
            message=message, error=error,
            back_url=url_for("dashboard.view_transcript", guild_id=guild_id, transcript_id=transcript_id),
        )

    def _share_gone():
        """Same page for expired, revoked and unknown links, so a link can't be probed."""
        resp = Response(
            "<!doctype html><meta charset=utf-8><meta name=robots content=noindex>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Link unavailable</title><body style=\"font-family:system-ui,sans-serif;"
            "background:#0b0b0d;color:#e4e4e7;display:grid;place-items:center;height:100vh;margin:0\">"
            "<div style=\"text-align:center;padding:24px\"><h1 style=\"font-size:22px\">This link isn't available</h1>"
            "<p style=\"color:#8c8c95\">It has expired or was revoked.</p></div>",
            status=404, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'"
        return resp

    @bp.route("/t/<token>")
    def shared_transcript(token):
        """Public, read-only transcript for a share link. No login; works until it expires
        or is revoked. Opens only the one transcript the link was made for."""
        share = dashboard_db.get_active_share(token)
        transcript = dashboard_db.get_transcript(share["transcript_id"]) if share else None
        if not transcript or transcript.get("guild_id") != share["guild_id"]:
            return _share_gone()
        dashboard_db.count_share_view(share["_id"])

        if "messages" not in transcript:
            resp = Response(transcript.get("html", ""), mimetype="text/html")
            resp.headers["Content-Security-Policy"] = "sandbox"
        else:
            def file_url(file_id):
                return url_for("dashboard.shared_transcript_file", token=token, file_id=file_id)
            resp = _render_transcript(transcript, file_url, guild_id=share["guild_id"],
                                      share=True, can_share=False, share_page=None)
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Robots-Tag"] = "noindex, nofollow"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp

    @bp.route("/t/<token>/file/<file_id>")
    def shared_transcript_file(token, file_id):
        share = dashboard_db.get_active_share(token)
        if not share:
            return _share_gone()
        f = dashboard_db.get_transcript_file(file_id)
        meta = (getattr(f, "metadata", None) or {}) if f is not None else {}
        if (f is None or meta.get("guild_id") != share["guild_id"]
                or meta.get("transcript_id") != share["transcript_id"]
                or meta.get("content_type") not in _IMAGE_TYPES):
            abort(404)
        return Response(f.read(), mimetype=meta["content_type"], headers={
            "Cache-Control": "no-store",          # a revoked link must stop showing images
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

    # -----------------------------------------------------------------
    # Claim tickets
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/claim-tickets", methods=["GET", "POST"])
    def claim_tickets_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            category_id = None
            if request.form.get("category_mode", "existing") == "new":
                new_name = (request.form.get("category_new_name") or "").strip()
                if new_name:
                    category_id = f"__new__:{new_name}"
            else:
                category_id = request.form.get("category_id") or None

            server_cat = (dashboard_db.get_guild_settings(guild_id).get("tickets") or {}).get(
                "fixed_category_id")
            if not category_id and not server_cat:
                return redirect(url_for(
                    "dashboard.claim_tickets_page", guild_id=guild_id, error="category_required"
                ))

            questions = [request.form.get(f"q{i}", "").strip()[:45] for i in range(5)]
            questions = [q for q in questions if q]
            if not questions:
                return redirect(url_for(
                    "dashboard.claim_tickets_page", guild_id=guild_id, error="question_required"
                ))

            ping_mode = request.form.get("ping_mode")
            if ping_mode not in ("roles", "host"):
                ping_mode = "roles"

            def _timer(name):
                try:
                    return max(10, min(int(request.form.get(name, 60)), 600))
                except (TypeError, ValueError):
                    return 60

            dashboard_db.update_guild_settings(guild_id, {"claim_tickets": {
                "category_id": str(category_id) if category_id else None,
                "ping_mode": ping_mode,
                "ping_role_ids": [str(r) for r in request.form.getlist("ping_role_ids")],
                "allow_role_ids": [str(r) for r in request.form.getlist("allow_role_ids")],
                "questions": questions,
                "rps_timer": _timer("rps_timer"),
                "splitsteal_timer": _timer("splitsteal_timer"),
            }})
            return redirect(url_for("dashboard.claim_tickets_page", guild_id=guild_id, saved=1))

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        cfg = settings_doc.get("claim_tickets") or {}
        return render_template(
            "claim_tickets.html",
            guild_id=guild_id,
            cfg=cfg,
            configured=bool(cfg.get("category_id")),
            server_category=(settings_doc.get("tickets") or {}).get("fixed_category_id"),
            categories=get_categories(int(guild_id)),
            roles=get_guild_roles(int(guild_id)),
            saved=request.args.get("saved"),
            error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Spawners (price panel + spawner tickets). Prices and spawner types are
    # managed here AND in Discord with /spawner; this page also holds the rest.
    # NOTE: every field is saved with a dotted key ("spawners.notes") so saving here
    # never wipes the types / prices / panel list the bot keeps under "spawners".
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/spawners", methods=["GET", "POST"])
    def spawners_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            category_id = None
            if request.form.get("category_mode", "existing") == "new":
                new_name = (request.form.get("category_new_name") or "").strip()
                if new_name:
                    category_id = f"__new__:{new_name}"
            else:
                category_id = request.form.get("category_id") or None

            notes = (request.form.get("notes") or "").replace("\r\n", "\n").replace("\r", "\n")
            notes = notes.strip()[:SPAWNER_NOTES_MAX]

            dashboard_db.update_guild_settings(guild_id, {
                "spawners.category_id": str(category_id) if category_id else None,
                "spawners.ping_role_ids": [str(r) for r in request.form.getlist("ping_role_ids")],
                "spawners.allow_role_ids": [str(r) for r in request.form.getlist("allow_role_ids")],
                "spawners.notes": notes,
            })
            # Tell the bot to re-draw the live panel(s) with the new notes.
            dashboard_db.enqueue_task("refresh_spawner_panels", guild_id)
            return redirect(url_for("dashboard.spawners_page", guild_id=guild_id, saved=1))

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        cfg = settings_doc.get("spawners") or {}
        notes = cfg.get("notes")
        if notes is None:
            notes = DEFAULT_SPAWNER_NOTES
        return render_template(
            "spawners.html",
            guild_id=guild_id,
            cfg=cfg,
            notes=notes,
            notes_max=SPAWNER_NOTES_MAX,
            types=[dict(v, key=k) for k, v in _sorted_spawner_types(cfg.get("types"))],
            panel_count=len(cfg.get("panels") or []),
            categories=get_categories(int(guild_id)),
            roles=get_guild_roles(int(guild_id)),
            saved=request.args.get("saved"),
            error=request.args.get("error"),
        )

    # Add / edit / remove spawner types + prices from the dashboard (same data as the
    # /spawner add | setprice | remove commands, which keep working).
    @bp.route("/guild/<guild_id>/spawners/type", methods=["POST"])
    def spawner_type_save(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        def back(**kw):
            return redirect(url_for("dashboard.spawners_page", guild_id=guild_id, **kw))

        action = request.form.get("action", "save")
        cfg = (dashboard_db.get_guild_settings(guild_id).get("spawners") or {})
        types = cfg.get("types") or {}

        if action in ("up", "down"):
            keys = [k for k, _ in _sorted_spawner_types(types)]
            key = request.form.get("key", "")
            if key in keys:
                i = keys.index(key)
                j = i - 1 if action == "up" else i + 1
                if 0 <= j < len(keys):
                    keys[i], keys[j] = keys[j], keys[i]
                    dashboard_db.update_guild_settings(guild_id, {
                        f"spawners.types.{k}.order": n for n, k in enumerate(keys)})
                    dashboard_db.enqueue_task("refresh_spawner_panels", guild_id)
            return back(saved=1)

        if action == "delete":
            key = request.form.get("key", "")
            if key in types:
                dashboard_db.get_db().guild_settings.update_one(
                    {"_id": str(guild_id)}, {"$unset": {f"spawners.types.{key}": ""}})
                dashboard_db.enqueue_task("refresh_spawner_panels", guild_id)
            return back(saved=1)

        # save = add (no key) or edit (key of an existing type)
        key = (request.form.get("key") or "").strip()
        editing = bool(key)
        name = _SP_TRAILING.sub("", (request.form.get("name") or "").strip()).strip()
        emoji = (request.form.get("emoji") or "").strip()
        if editing:
            if key not in types:
                return back(error="That spawner no longer exists.")
        else:
            key = _spawner_key(name)
            if not key:
                return back(error="Give the spawner a name with letters or numbers in it.")
            if key not in types and len(types) >= MAX_SPAWNER_TYPES:
                return back(error=f"You can have at most {MAX_SPAWNER_TYPES} spawner types.")
        if not name:
            name = (types.get(key) or {}).get("name") or ""
        if not name or len(name) > 40:
            return back(error="Name is required and must be under 40 characters.")
        if not _spawner_emoji_ok(emoji):
            return back(error="Emoji must be an actual emoji (like 💀) or a custom <:name:id> emoji.")

        existing = types.get(key) or {}
        sets = {f"spawners.types.{key}.name": name, f"spawners.types.{key}.emoji": emoji}
        if key not in types:  # new types go to the bottom of the panel
            sets[f"spawners.types.{key}.order"] = len(types)
        unsets = {}
        price_changes = []
        for side in ("sell", "buy"):
            raw = (request.form.get(f"{side}_price") or "").strip()
            field = f"{side}_price"
            old = existing.get(field)
            if raw.lower() in ("", "off", "none", "clear", "remove"):
                if existing.get(field):
                    unsets[f"spawners.types.{key}.{field}"] = ""
                continue
            try:
                new = parse_price(raw)
            except ValueError as e:
                return back(error=f"{side.title()} price: {e}")
            sets[f"spawners.types.{key}.{field}"] = int(new)
            if new != (int(old) if old else None):
                price_changes.append({"key": key, "side": side})

        # Minimum amount per order, separately for each side (empty / 0 / off = no minimum).
        for side in ("sell", "buy"):
            raw = (request.form.get(f"{side}_min") or "").strip()
            field = f"{side}_min"
            if raw.lower() in ("", "0", "off", "none", "clear", "remove"):
                if existing.get(field):
                    unsets[f"spawners.types.{key}.{field}"] = ""
                continue
            try:
                sets[f"spawners.types.{key}.{field}"] = int(parse_amount(raw))
            except ValueError as e:
                return back(error=f"{side.title()} minimum: {e}")

        # Pause ticks: keep the price, just stop that side from being ordered / shown as live.
        for side in ("sell", "buy"):
            sets[f"spawners.types.{key}.{side}_paused"] = request.form.get(f"{side}_paused") == "on"

        update = {"$set": sets}
        if unsets:
            update["$unset"] = unsets
        dashboard_db.get_db().guild_settings.update_one(
            {"_id": str(guild_id)}, update, upsert=True)
        dashboard_db.enqueue_task("refresh_spawner_panels", guild_id,
                                  {"price_changes": price_changes})
        return back(saved=1)

    # -----------------------------------------------------------------
    # Welcome message + auto role (dashboard only - no commands)
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/welcome", methods=["GET", "POST"])
    def welcome_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        gid = int(guild_id)
        # Text + announcement channels the bot can see.
        channels = sorted(
            (c for c in get_guild_channels(gid) if c.get("type") in (0, 5)),
            key=lambda c: c.get("position", 0),
        )
        roles = get_guild_roles(gid)

        if request.method == "POST":
            update = {}

            message = (request.form.get("message") or "").replace("\r\n", "\n").replace("\r", "\n")
            update["welcome.message"] = message.strip()[:MAX_WELCOME_LEN]

            # If Discord's channel/role lists couldn't be fetched, leave that part
            # of the saved config alone rather than wiping it.
            if channels:
                wanted = request.form.get("channel_id") or None
                update["welcome.channel_id"] = (
                    wanted if wanted in {str(c["id"]) for c in channels} else None
                )
            if roles:
                by_id = {str(r["id"]): r for r in roles}
                chosen = []
                for rid in request.form.getlist("autorole_ids"):
                    r = by_id.get(str(rid))
                    if (r and not r.get("managed") and not role_is_dangerous(r.get("permissions"))
                            and str(rid) not in chosen):
                        chosen.append(str(rid))
                update["autorole.role_ids"] = chosen

            dashboard_db.update_guild_settings(guild_id, update)
            return redirect(url_for("dashboard.welcome_page", guild_id=guild_id, saved=1))

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        welcome = settings_doc.get("welcome") or {}
        autorole = settings_doc.get("autorole") or {}
        role_rows = [
            {
                "id": str(r["id"]),
                "name": r.get("name", ""),
                "managed": bool(r.get("managed")),
                "dangerous": role_is_dangerous(r.get("permissions")),
            }
            for r in sorted(roles, key=lambda r: -int(r.get("position", 0)))
        ]
        return render_template(
            "welcome.html",
            guild_id=guild_id,
            channels=channels,
            roles=role_rows,
            channel_id=str(welcome.get("channel_id") or ""),
            message=welcome.get("message") or DEFAULT_WELCOME_MESSAGE,
            autorole_ids=[str(r) for r in (autorole.get("role_ids") or [])],
            placeholders=PLACEHOLDERS,
            message_max=MAX_WELCOME_LEN,
            saved=request.args.get("saved"),
        )

    # -----------------------------------------------------------------
    # Join Ad (DM new members an ad - dashboard only, no commands)
    # Config: guild_settings.join_ads   Counters: join_ad_stats collection
    # -----------------------------------------------------------------
    def _join_ad_stats(guild_id):
        """{ad_id: {sent, failed, recent_failures[]}} plus '_global' skip counters."""
        out = {}
        try:
            docs = list(dashboard_db.get_db().join_ad_stats.find({"guild_id": str(guild_id)}))
        except Exception:
            docs = []
        for d in docs:
            fails = []
            for f in (d.get("recent_failures") or [])[::-1]:
                at = f.get("at")
                fails.append({
                    "user": f.get("user", ""), "reason": f.get("reason", ""),
                    "ad": f.get("ad", ""),
                    "at": at.strftime("%Y-%m-%d %H:%M UTC") if hasattr(at, "strftime") else "",
                })
            out[d.get("ad_id")] = {
                "sent": int(d.get("sent") or 0), "failed": int(d.get("failed") or 0),
                "skipped": int(d.get("skipped") or 0),
                "reasons": d.get("reasons") or {}, "recent_failures": fails,
            }
        return out

    @bp.route("/guild/<guild_id>/join-ads", methods=["GET", "POST"])
    def join_ads_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        gid = int(guild_id)
        channels = sorted(
            (c for c in get_guild_channels(gid) if c.get("type") in (0, 5)),
            key=lambda c: c.get("position", 0),
        )
        roles = get_guild_roles(gid)
        settings_doc = dashboard_db.get_guild_settings(guild_id)
        stored = settings_doc.get("join_ads") or {}

        if request.method == "POST":
            raw_payload = request.form.get("payload") or ""
            try:
                if len(raw_payload) > 600_000:
                    raise ValueError("too big")
                raw = json.loads(raw_payload)
            except ValueError:
                return redirect(url_for("dashboard.join_ads_page", guild_id=guild_id, error=1))

            cfg, restarted = ja.sanitize_config(
                raw, stored,
                channel_ids={str(c["id"]) for c in channels} if channels else None,
                role_ids={str(r["id"]) for r in roles} if roles else None,
            )
            db = dashboard_db.get_db()
            kept = {a["id"] for a in cfg["ads"]}
            gone = [a.get("id") for a in (stored.get("ads") or []) if a.get("id") not in kept]
            reset = list(restarted) + gone
            if reset:
                db.join_ad_stats.delete_many(
                    {"_id": {"$in": [f"{guild_id}:{i}" for i in reset]}})
            dashboard_db.update_guild_settings(guild_id, {"join_ads": cfg})
            return redirect(url_for("dashboard.join_ads_page", guild_id=guild_id, saved=1))

        cfg = ja.load_config(stored)
        stats = _join_ad_stats(guild_id)
        now = ja.utcnow()
        status = {}
        for a in cfg["ads"]:
            sent = (stats.get(a["id"]) or {}).get("sent", 0)
            state, reason = ja.campaign_state(a, sent, now)
            left = ja.days_left(a, now)
            status[a["id"]] = {
                "state": state, "reason": reason,
                "days_left": None if left is None else round(left, 1),
            }
        boot = {
            "cfg": cfg,
            "stats": stats,
            "status": status,
            "placeholders": PLACEHOLDERS,
            "skip_reasons": ja.SKIP_REASONS,
            "limits": {"max_ads": ja.MAX_ADS, "max_buttons": ja.MAX_BUTTONS,
                       "content": ja.MAX_CONTENT, "desc": ja.MAX_DESC},
            "default_content": ja.DEFAULT_AD_CONTENT,
        }
        role_rows = [
            {"id": str(r["id"]), "name": r.get("name", "")}
            for r in sorted(roles, key=lambda r: -int(r.get("position", 0)))
        ]
        return render_template(
            "join_ads.html",
            guild_id=guild_id,
            boot=boot,
            channels=[{"id": str(c["id"]), "name": c.get("name", "")} for c in channels],
            roles=role_rows,
            delay_choices=ja.DELAY_CHOICES,
            pace_choices=ja.PACE_CHOICES,
            saved=request.args.get("saved"),
            error=request.args.get("error"),
        )

    @bp.route("/guild/<guild_id>/join-ads/test", methods=["POST"])
    def join_ads_test(guild_id):
        """Send one ad to the logged-in person as a test DM (not counted in stats)."""
        if not _is_authed(discord_session):
            return jsonify(ok=False, error="Not logged in."), 401
        g = _require_guild_access(discord_session, guild_id)
        if not request.is_json:
            return jsonify(ok=False, error="Bad request."), 400
        if not discord_session.authorized:
            return jsonify(ok=False, error="Test DMs need a normal Discord login - the /admin "
                                           "login has no Discord account to send to."), 400

        ad = ja.sanitize_ad((request.get_json(silent=True) or {}).get("ad") or {})
        if not ja.ad_is_sendable(ad):
            return jsonify(ok=False, error="This ad has nothing to send yet - add a message "
                                           "(plain) or a title/description (embed)."), 400
        try:
            user = discord_session.fetch_user()
        except Exception:
            return jsonify(ok=False, error="Couldn't read your Discord account - log in again."), 401

        from utils.helpers import DISCORD_API, _headers as bot_headers
        count = 128
        try:
            r = requests.get(f"https://discord.com/api/v10/guilds/{guild_id}",
                             headers=bot_headers(),
                             params={"with_counts": "true"}, timeout=10)
            if r.ok:
                count = r.json().get("approximate_member_count") or count
        except requests.RequestException:
            pass

        msg = ja.build_message(ad, user_mention=f"<@{user.id}>", username=user.name,
                               server=getattr(g, "name", "Your Server"), member_count=count)
        try:
            ch = requests.post(f"{DISCORD_API}/users/@me/channels", headers=bot_headers(),
                               json={"recipient_id": str(user.id)}, timeout=15)
            if not ch.ok:
                return jsonify(ok=False, error="Discord wouldn't let the bot open a DM with you."), 502
            res = requests.post(f"{DISCORD_API}/channels/{ch.json()['id']}/messages",
                                headers=bot_headers(), json=ja.to_rest_payload(msg), timeout=15)
        except requests.RequestException:
            return jsonify(ok=False, error="Couldn't reach Discord. Try again."), 502

        if res.ok:
            return jsonify(ok=True)
        try:
            body = res.json()
        except ValueError:
            body = {}
        if body.get("code") == 50007:
            return jsonify(ok=False, error="Your DMs are closed to this bot. Turn on Direct "
                                           "Messages for this server in your Privacy settings."), 400
        return jsonify(ok=False, error=f"Discord rejected it: {body.get('message') or res.status_code}"), 400

    def _change_types(guild_id):
        """[{id, name}] of this server's Changes types (for the Applications dropdown)."""
        from utils import changes_core as cc
        doc = dashboard_db.get_guild_settings(guild_id)
        return [{"id": t["id"], "name": t["name"]}
                for t in cc.normalize_types((doc.get("changes") or {}).get("types"))]

    def _valid_change_type(guild_id, raw):
        """The submitted type id if it still exists, else None."""
        raw = (raw or "").strip()
        return raw if raw and any(t["id"] == raw for t in _change_types(guild_id)) else None

    # -----------------------------------------------------------------
    # Changes (promotion / demotion ladders, one per type)
    # Stored in guild_settings.changes.types as
    #   [{"id", "name", "channel_id", "ladder": [{"index", "role_id", "nick"}, ...],
    #     "extra_role_ids": [...]}]
    # index 0 = base rank (Member). Indexes are renumbered on every save.
    # A role may be on several ladders only as index 0 (see utils/changes_core.py).
    # Extra roles (max 10 per type) are given on 0 -> 1 and removed on 1 -> 0; they can
    # never also be ladder roles, in either direction.
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/changes", methods=["GET", "POST"])
    def changes_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from utils import changes_core as cc

        gid = int(guild_id)
        roles = get_guild_roles(gid)
        by_id = {str(r["id"]): r for r in roles}
        channels = sorted(get_text_channels(gid), key=lambda c: c.get("position", 0))
        channel_ids = {str(c["id"]) for c in channels}

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        types = cc.normalize_types((settings_doc.get("changes") or {}).get("types"))

        def _back(type_id=None, **kw):
            if type_id:
                kw["_anchor"] = f"t-{type_id}"
            return redirect(url_for("dashboard.changes_page", guild_id=guild_id, **kw))

        def _save(new_types):
            if not cc.ladders_valid(new_types):
                return False
            dashboard_db.update_guild_settings(guild_id, {"changes.types": new_types})
            return True

        if request.method == "POST":
            action = request.form.get("action")
            tid = (request.form.get("type_id") or "").strip()
            ctype = cc.find_type(types, tid) if tid else None

            if action == "create_type":
                name = cc.clean_type_name(request.form.get("name"))
                channel_id = request.form.get("channel_id") or ""
                if name is None:
                    return _back(error="name_invalid")
                if any(t["name"].lower() == name.lower() for t in types):
                    return _back(error="name_duplicate")
                if channel_id not in channel_ids:
                    return _back(error="channel_invalid")
                if len(types) >= cc.MAX_TYPES:
                    return _back(error="types_full")
                new_id = cc.new_type_id()
                types.append({"id": new_id, "name": name, "channel_id": channel_id, "ladder": []})
                _save(types)
                return _back(new_id, saved=1)

            if ctype is None:
                return _back(error="type_missing")

            if action == "save_type":
                name = cc.clean_type_name(request.form.get("name"))
                channel_id = request.form.get("channel_id") or ""
                if name is None:
                    return _back(tid, error="name_invalid")
                if any(t["id"] != ctype["id"] and t["name"].lower() == name.lower() for t in types):
                    return _back(tid, error="name_duplicate")
                if channel_id not in channel_ids:
                    return _back(tid, error="channel_invalid")
                ctype["name"], ctype["channel_id"] = name, channel_id
                ctype["auto_nick"] = request.form.get("auto_nick") == "on"
                # Nick priority: needs auto nick on, and only ONE type can hold it
                # (ticking it here clears it everywhere else).
                ctype["nick_priority"] = (ctype["auto_nick"]
                                          and request.form.get("nick_priority") == "on")
                # Custom nick font: off by default, needs auto nick on. The chosen font is
                # remembered even while the tick is off.
                ctype["nick_custom_font"] = (ctype["auto_nick"]
                                             and request.form.get("nick_custom_font") == "on")
                # Staff Automation type: only ONE type can hold it (ticking it clears the others).
                ctype["staff_automation"] = request.form.get("staff_automation") == "on"
                if ctype["staff_automation"]:
                    for other in types:
                        if other["id"] != ctype["id"]:
                            other["staff_automation"] = False
                if request.form.get("nick_font") is not None:
                    ctype["nick_font"] = cc.clean_nick_font(request.form.get("nick_font"))
                if ctype["nick_priority"]:
                    for other in types:
                        if other["id"] != ctype["id"]:
                            other["nick_priority"] = False
                _save(types)
                return _back(ctype["id"], saved=1)

            if action == "sync_nicks":
                dashboard_db.enqueue_task("changes_sync_nicks", guild_id, {"type_id": ctype["id"]})
                return _back(ctype["id"], queued="sync")

            if action == "delete_type":
                types = [t for t in types if t["id"] != ctype["id"]]
                _save(types)
                dashboard_db.get_db().applications.update_many(
                    {"guild_id": str(gid), "change_type_id": ctype["id"]},
                    {"$set": {"change_type_id": None}})
                return _back(deleted=1)

            if action == "save_extras":
                wanted = []
                for r in request.form.getlist("extra_role_ids"):
                    r = (r or "").strip()
                    if r and r not in wanted:
                        wanted.append(r)
                if len(wanted) > cc.MAX_EXTRAS:
                    return _back(tid, error="extras_full")
                ladder_ids = cc.all_ladder_role_ids(types)
                for r in wanted:
                    role = by_id.get(r)
                    if role is None or r == str(gid):
                        return _back(tid, error="extra_invalid")
                    if role.get("managed"):
                        return _back(tid, error="role_managed")
                    if r in ladder_ids:
                        return _back(tid, error="extra_is_ladder")
                ctype["extra_role_ids"] = wanted
                if not _save(types):
                    return _back(tid, error="extra_is_ladder")
                return _back(tid, saved=1)

            if action == "save_exceptions":
                wanted = []
                for r in request.form.getlist("exception_role_ids"):
                    r = (r or "").strip()
                    if r and r not in wanted:
                        wanted.append(r)
                if len(wanted) > cc.MAX_EXCEPTIONS:
                    return _back(tid, error="exceptions_full")
                for r in wanted:
                    role = by_id.get(r)
                    if role is None or r == str(gid):
                        return _back(tid, error="exception_invalid")
                ctype["exception_role_ids"] = wanted
                _save(types)
                return _back(tid, saved=1)

            # ---- ladder editing ----
            ladder = [dict(e) for e in ctype["ladder"]]
            rid = (request.form.get("role_id") or "").strip()
            pos = next((i for i, e in enumerate(ladder) if e["role_id"] == rid), None)
            err = None

            if action == "add":
                nick = (request.form.get("nick") or "").strip()[:cc.MAX_NICK]
                r = by_id.get(rid)
                if r is None:
                    err = "role_invalid"
                elif r.get("managed"):
                    err = "role_managed"
                elif pos is not None:
                    err = "role_duplicate"
                elif rid in cc.all_extra_role_ids(types):
                    err = "role_is_extra"
                elif len(ladder) >= cc.MAX_LADDER:
                    err = "ladder_full"
                else:
                    ladder.append({"role_id": rid, "nick": nick})
            elif action == "remove" and pos is not None:
                ladder.pop(pos)
            elif action == "up" and pos is not None and pos < len(ladder) - 1:
                ladder[pos], ladder[pos + 1] = ladder[pos + 1], ladder[pos]
            elif action == "down" and pos is not None and pos > 0:
                ladder[pos], ladder[pos - 1] = ladder[pos - 1], ladder[pos]
            elif action == "nick" and pos is not None:
                ladder[pos]["nick"] = (request.form.get("nick") or "").strip()[:cc.MAX_NICK]

            if err is None:
                ctype["ladder"] = cc.normalize_ladder(
                    [{"index": i, **e} for i, e in enumerate(ladder)])
                if not _save(types):
                    err = "role_conflict"
            if err is None:
                return _back(ctype["id"], saved=1)
            return _back(ctype["id"], error=err)

        # ---- GET: build the view model ----
        cards = []
        all_ladder_ids = cc.all_ladder_role_ids(types)
        all_extra_ids = cc.all_extra_role_ids(types)
        for t in types:
            in_ladder = {e["role_id"] for e in t["ladder"]}
            rows = []
            for e in t["ladder"]:
                r = by_id.get(e["role_id"])
                color = int(r.get("color") or 0) if r else 0
                rows.append({
                    "index": e["index"], "role_id": e["role_id"], "nick": e["nick"],
                    "name": r.get("name") if r else None,
                    "color": "#%06x" % color if color else "",
                })
            rows.reverse()  # highest index on top
            cards.append({
                "id": t["id"], "name": t["name"],
                "auto_nick": bool(t.get("auto_nick")),
                "nick_priority": bool(t.get("nick_priority")),
                "nick_custom_font": bool(t.get("nick_custom_font")),
                "nick_font": cc.clean_nick_font(t.get("nick_font")),
                "staff_automation": bool(t.get("staff_automation")),
                "channel_id": t.get("channel_id") or "",
                "channel_name": next(
                    (c.get("name") for c in channels if str(c["id"]) == str(t.get("channel_id") or "")), None),
                "rows": rows,
                "available": [
                    {"id": str(r["id"]), "name": r.get("name", "")}
                    for r in sorted(roles, key=lambda r: -int(r.get("position", 0)))
                    if not r.get("managed") and str(r["id"]) not in in_ladder
                    and str(r["id"]) not in all_extra_ids
                ],
                # Extra roles: any assignable role that isn't on a ladder. Roles already
                # picked for this type are marked selected; deleted ones are listed apart.
                "extra_options": [
                    {"id": str(r["id"]), "name": r.get("name", ""),
                     "selected": str(r["id"]) in (t.get("extra_role_ids") or [])}
                    for r in sorted(roles, key=lambda r: -int(r.get("position", 0)))
                    if not r.get("managed") and str(r["id"]) != str(gid)
                    and str(r["id"]) not in all_ladder_ids
                ],
                "extra_missing": [x for x in (t.get("extra_role_ids") or []) if x not in by_id],
                "extra_count": len(t.get("extra_role_ids") or []),
                # Exception roles: any real role (ladder roles allowed). Deleted ones listed apart.
                "exception_options": [
                    {"id": str(r["id"]), "name": r.get("name", ""),
                     "selected": str(r["id"]) in (t.get("exception_role_ids") or [])}
                    for r in sorted(roles, key=lambda r: -int(r.get("position", 0)))
                    if str(r["id"]) != str(gid)
                ],
                "exception_missing": [x for x in (t.get("exception_role_ids") or []) if x not in by_id],
                "exception_count": len(t.get("exception_role_ids") or []),
            })
        return render_template(
            "changes.html",
            guild_id=guild_id,
            cards=cards,
            channels=channels,
            roles_visible=bool(roles),
            max_nick=cc.MAX_NICK, max_types=cc.MAX_TYPES, max_name=cc.MAX_NAME,
            max_extras=cc.MAX_EXTRAS,
            nick_fonts=[{"key": k, "label": v[0], "sample": cc.apply_font("Trial Mod", k)}
                        for k, v in cc.NICK_FONTS.items()],
            max_exceptions=cc.MAX_EXCEPTIONS,
            saved=request.args.get("saved"),
            deleted=request.args.get("deleted"),
            queued=request.args.get("queued"),
            error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Ticket ratings. The bot writes "ticket_ratings" when an opener rates a closed ticket
    # from the close DM (cogs/ticket_ratings.py). Individual reviews can be deleted here
    # (handy for test ratings): that also takes the rating back out of the staff member's
    # taskbook totals and off the ticket's transcript.
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # Live Tickets: read and answer open tickets from the dashboard
    # -----------------------------------------------------------------
    def _live_allowed(guild_id, type_doc, viewer) -> bool:
        """Who may use live replies on a panel: administrators; Manage Server holders; and
        members with the panel's support roles. With "staff roles only" / "+ opener"
        transcript access, Manage Server alone isn't enough (the same privacy rule)."""
        if viewer["is_admin"]:
            return True
        staff = _has_staff_role(guild_id, viewer, type_doc)
        if ta.clean_mode(type_doc.get("transcript_access")) == "managers":
            return viewer["is_manager"] or staff
        return staff

    def _live_fail(api, status, code, text):
        if api:
            abort(make_response(jsonify(error=code, message=text), status))
        abort(status)

    def _live_gate(guild_id, channel_id, api=False):
        """Login + server + panel setting + per-person access. -> (ticket, type_doc).
        Pages abort with a redirect / status; API calls answer with JSON."""
        if not _is_authed(discord_session):
            if api:
                _live_fail(True, 401, "login", "Please log in again.")
            abort(redirect(url_for("dashboard.login")))
        if botguilds.bot_in_guild(guild_id) is False:
            _live_fail(api, 404, "no_bot", "The bot isn't in this server.")
        ticket = dashboard_db.get_db().tickets.find_one({"guild_id": str(guild_id), "channel_id": str(channel_id)})
        if not ticket:
            _live_fail(api, 404, "not_found", "That ticket doesn't exist.")
        type_doc = dashboard_db.get_ticket_type(ticket.get("ticket_type_id")) if ticket.get("ticket_type_id") else None
        if (not type_doc or str(type_doc.get("guild_id")) != str(guild_id)
                or not ts_cfg.settings_for(type_doc)["web_reply"]):
            _live_fail(api, 403, "off", "Live replies are off for this panel.")
        if not _live_allowed(guild_id, type_doc, _viewer(guild_id)):
            _live_fail(api, 403, "denied", "You can't open this ticket.")
        return ticket, type_doc

    def _staff_identity():
        """(id, name, avatar url) of whoever is logged in, for labelling a reply."""
        uid, name = _audit_actor(discord_session)
        avatar = ""
        try:
            avatar = discord_session.fetch_user().avatar_url or ""
        except Exception:
            pass
        return uid, name, avatar

    _PRIORITY_RANK = {"urgent": 3, "high": 2, "normal": 1, "low": 0}

    @bp.route("/guild/<guild_id>/live-tickets")
    def live_tickets_page(guild_id):
        """Open tickets of the panels that have Live replies on, most urgent first."""
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        if botguilds.bot_in_guild(guild_id) is False:
            return redirect(url_for("dashboard.index", notice="nobot"))
        db = dashboard_db.get_db()
        viewer = _viewer(guild_id)
        panels = [t for t in dashboard_db.get_ticket_types(guild_id) if ts_cfg.settings_for(t)["web_reply"]]
        allowed = [t for t in panels if _live_allowed(guild_id, t, viewer)]
        if not allowed and not (viewer["is_admin"] or viewer["is_manager"]):
            abort(403)

        tickets = list(db.tickets.find({
            "guild_id": str(guild_id), "status": "open",
            "ticket_type_id": {"$in": [str(t["_id"]) for t in allowed]}}).sort("created_at", 1).limit(200)) if allowed else []
        stats = {d["_id"]: d for d in db.ticket_stats.find({"_id": {"$in": [t["channel_id"] for t in tickets]}})} if tickets else {}
        wanted = set()
        for t in tickets:
            if not t.get("opener_name"):
                wanted.add(t["opener_id"])
            st = stats.get(t["channel_id"], {})
            if st.get("claimed_by_id") and not st.get("claimed_by_name"):
                wanted.add(st["claimed_by_id"])
        names = ticket_live.resolve_names(guild_id, wanted)
        rows = []
        for t in tickets:
            st = stats.get(t["channel_id"], {})
            prio = ts_cfg.clean_priority(t.get("priority"), "normal")
            rows.append({
                "channel_id": t["channel_id"], "panel": t.get("panel_name"), "number": ts_cfg.format_number(t.get("number")),
                "opener": t.get("opener_name") or names.get(t["opener_id"], t["opener_id"]),
                "priority": prio, "priority_label": ts_cfg.priority_label(prio), "rank": _PRIORITY_RANK[prio],
                "claimed": (st.get("claimed_by_name") or names.get(st.get("claimed_by_id"), "")) if st.get("claimed_by_id") else "",
                "waiting": bool(st) and not st.get("first_reply_at"), "created_at": t.get("created_at"),
                "thread": bool(t.get("is_thread")),
            })
        rows.sort(key=lambda r: (-r["rank"], r["created_at"] or 0))
        return render_template("live_tickets.html", guild_id=guild_id, rows=rows, has_panels=bool(panels),
                               limited=len(tickets) >= 200)

    @bp.route("/guild/<guild_id>/live-tickets/<channel_id>")
    def live_ticket_page(guild_id, channel_id):
        ticket, type_doc = _live_gate(guild_id, channel_id)
        stats = dashboard_db.get_db().ticket_stats.find_one({"_id": str(channel_id)}) or {}
        transcript = None
        if ticket.get("status") != "open":
            transcript = dashboard_db.get_db().transcripts.find_one(
                {"guild_id": str(guild_id), "channel_id": str(channel_id)}, {"_id": 1})
        names = ticket_live.resolve_names(guild_id, [ticket["opener_id"]]) if not ticket.get("opener_name") else {}
        prio = ts_cfg.clean_priority(ticket.get("priority"), "normal")
        return render_template(
            "live_ticket.html", guild_id=guild_id, channel_id=str(channel_id), is_open=ticket.get("status") == "open",
            panel=ticket.get("panel_name"), number=ts_cfg.format_number(ticket.get("number")),
            opener=ticket.get("opener_name") or names.get(ticket["opener_id"], ticket["opener_id"]),
            priority=ts_cfg.priority_label(prio), claimed=stats.get("claimed_by_name") or "",
            is_thread=bool(ticket.get("is_thread")), max_reply=ticket_live.MAX_REPLY,
            transcript_url=url_for("dashboard.view_transcript", guild_id=guild_id, transcript_id=str(transcript["_id"])) if transcript else None,
        )

    @bp.route("/guild/<guild_id>/live-tickets/<channel_id>/messages")
    def live_ticket_messages(guild_id, channel_id):
        ticket, _ = _live_gate(guild_id, channel_id, api=True)
        if ticket.get("status") != "open":
            return jsonify(messages=[], closed=True)
        messages, err = ticket_live.fetch_messages(channel_id, after=request.args.get("after"))
        if err == "gone":
            return jsonify(messages=[], closed=True)
        if err:
            return jsonify(error=err, messages=[]), 429 if err == "rate_limited" else 502
        return jsonify(messages=messages, closed=False)

    @bp.route("/guild/<guild_id>/live-tickets/<channel_id>/reply", methods=["POST"])
    def live_ticket_reply(guild_id, channel_id):
        ticket, _ = _live_gate(guild_id, channel_id, api=True)
        if ticket.get("status") != "open":
            return jsonify(error="closed", message="This ticket is closed."), 409
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        raw = data.get("text")
        text = ticket_live.clean_reply(raw) if isinstance(raw, str) else ""
        if not text:
            return jsonify(error="empty", message="Write something first."), 400
        uid, name, avatar = _staff_identity()
        if not ticket_live.allow_reply(guild_id, uid):
            return jsonify(error="slow_down", message="You're sending too fast. Wait a moment."), 429
        ok, err = ticket_live.send_reply(channel_id, name, avatar, text,
                                         ping_user_id=ticket["opener_id"] if data.get("ping") else None)
        if not ok:
            msg = {"gone": "The ticket channel no longer exists.", "forbidden": "I can't post in that ticket.",
                   "rate_limited": "Discord is rate limiting me. Try again in a moment."}.get(err, "Couldn't send. Try again.")
            return jsonify(error=err, message=msg), {"gone": 404, "forbidden": 403, "rate_limited": 429}.get(err, 502)
        ticket_live.record_web_first_reply(dashboard_db.get_db(), channel_id, uid, name)
        number = ts_cfg.format_number(ticket.get("number"))
        dashboard_db.log_audit(guild_id, uid, name, "ticket_web_reply",
                               f"{ticket.get('panel_name') or 'ticket'} {('#' + number) if number else ticket['channel_id']}: {len(text)} characters")
        return jsonify(ok=True)

    @bp.route("/guild/<guild_id>/ticket-automations", methods=["GET", "POST"])
    def ticket_automations_page(guild_id):
        """Rules the bot runs on tickets: a trigger, then up to 5 steps, once per ticket."""
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        types_ = dashboard_db.get_ticket_types(guild_id)
        roles = get_guild_roles(int(guild_id))
        who = _audit_actor(discord_session)
        error = draft = None

        if request.method == "POST":
            action = request.form.get("form_type")
            aid = request.form.get("automation_id") or None
            if action == "save_automation":
                raw = {
                    "name": request.form.get("name", ""), "enabled": request.form.get("enabled") == "on",
                    "trigger": request.form.get("trigger"), "panel_ids": request.form.getlist("panel_ids"),
                    "keywords": request.form.get("keywords", ""), "from": request.form.get("from"),
                    "minutes": request.form.get("minutes"),
                    "steps": [{
                        "type": request.form.get(f"step{i}_type", ""),
                        "level": request.form.get(f"step{i}_level"), "template": request.form.get(f"step{i}_template"),
                        "type_id": request.form.get(f"step{i}_type_id"), "text": request.form.get(f"step{i}_text"),
                        "role_id": request.form.get(f"step{i}_role_id"), "user_id": request.form.get(f"step{i}_user_id"),
                    } for i in range(ta_rules.MAX_STEPS)],
                }
                fields, error = ta_rules.normalize(raw, {str(t["_id"]) for t in types_}, {str(r["id"]) for r in roles})
                if fields:
                    saved = dashboard_db.save_ticket_automation(guild_id, fields, aid)
                    if saved:
                        dashboard_db.log_audit(guild_id, *who, "automation_saved" if aid else "automation_created", fields["name"])
                        return redirect(url_for("dashboard.ticket_automations_page", guild_id=guild_id, saved=1))
                    error = ta_rules.ERRORS["too_many"] if not aid else "That automation no longer exists."
                # keep what was typed so the form reopens filled in
                draft = {"_id": aid or "", "name": raw["name"], "enabled": raw["enabled"],
                         "trigger": {"type": raw["trigger"] or "opened", "panel_ids": raw["panel_ids"],
                                     "keywords": ta_rules.clean_keywords(raw["keywords"]), "from": raw["from"] or "opener",
                                     "minutes": raw["minutes"] or ""},
                         "steps": [s for s in raw["steps"] if s["type"]]}
            elif action == "delete_automation":
                if dashboard_db.delete_ticket_automation(guild_id, aid):
                    dashboard_db.log_audit(guild_id, *who, "automation_deleted", request.form.get("name", ""))
                return redirect(url_for("dashboard.ticket_automations_page", guild_id=guild_id, saved=1))
            elif action == "toggle_automation":
                on = request.form.get("enabled") == "1"
                if dashboard_db.set_ticket_automation_enabled(guild_id, aid, on):
                    dashboard_db.log_audit(guild_id, *who, "automation_enabled" if on else "automation_disabled",
                                           request.form.get("name", ""))
                return redirect(url_for("dashboard.ticket_automations_page", guild_id=guild_id, saved=1))

        names = {str(t["_id"]): t["name"] for t in types_}
        automations = dashboard_db.get_ticket_automations(guild_id)
        for a in automations:
            a["summary"] = ta_rules.describe(a, names)
            a["id"] = str(a["_id"])
        return render_template(
            "ticket_automations.html", guild_id=guild_id, automations=automations,
            runs=dashboard_db.get_automation_runs(guild_id), ticket_types=types_, roles=roles,
            error=error, draft=draft, saved=request.args.get("saved"),
            limits=ta_rules, priorities=("low", "normal", "high", "urgent"),
        )

    @bp.route("/guild/<guild_id>/ticket-analytics", methods=["GET", "POST"])
    def ticket_analytics_page(guild_id):
        """Ticket volume, reply / close times, staff workload and busiest panels. Everything
        comes from the per-ticket stats the bot saves at ticket events."""
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)
        from datetime import datetime, timezone
        db = dashboard_db.get_db()

        notice = None
        if request.method == "POST" and request.form.get("form_type") == "import_history":
            added = ticket_analytics.import_history(db, guild_id)
            # remembered so the button goes away (live tickets are tracked from now on, so the
            # old ones only ever need importing once)
            db.ticket_stats_meta.update_one(
                {"_id": str(guild_id)},
                {"$set": {"history_imported_at": datetime.now(timezone.utc)}, "$inc": {"history_added": added}},
                upsert=True)
            dashboard_db.log_audit(guild_id, *_audit_actor(discord_session), "ticket_stats_import",
                                   f"Imported {added} past ticket(s) from transcripts into analytics")
            notice = (f"Imported {added} past ticket{'' if added == 1 else 's'}."
                      if added else "Nothing new to import.")

        start, end, f = ticket_analytics.parse_range(request.args)
        data = ticket_analytics.compute(db.ticket_stats, guild_id, start, end, f["kind"], f["panel"])
        can_import = (db.ticket_stats_meta.find_one({"_id": str(guild_id)}) is None
                      and db.transcripts.count_documents({"guild_id": str(guild_id)}, limit=1) > 0)
        panel_names = sorted((n for n in db.ticket_stats.distinct("panel_name", {"guild_id": str(guild_id)}) if n),
                             key=str.lower)
        return render_template(
            "ticket_analytics.html", guild_id=guild_id, d=data, f=f, kinds=ticket_analytics.KINDS,
            panel_names=panel_names, notice=notice, can_import=can_import,
        )

    @bp.route("/guild/<guild_id>/ticket-ratings", methods=["GET", "POST"])
    def ticket_ratings_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from datetime import datetime, timedelta, timezone
        db = dashboard_db.get_db()

        if request.method == "POST":
            keep_days = request.form.get("days") or 30
            if request.form.get("form_type") == "delete_rating":
                from bson import ObjectId
                try:
                    oid = ObjectId(request.form.get("rating_id") or "")
                except Exception:
                    oid = None
                gone = db.ticket_ratings.find_one_and_delete(
                    {"_id": oid, "guild_id": guild_id}) if oid else None
                if gone and gone.get("rating") is not None:
                    r = int(gone["rating"])
                    dashboard_db.log_audit(
                        guild_id, *_audit_actor(discord_session), "rating_delete",
                        f"Deleted a {r}-star review for {gone.get('staff_name') or gone.get('staff_id')}"
                        + (" (it had a comment)" if gone.get("comment") else ""))
                    # Take it back out of the taskbook totals (only if that can't go negative,
                    # e.g. the taskbook was reset after this rating came in).
                    tb = db.taskbooks.update_one(
                        {"guild_id": guild_id, "user_id": str(gone.get("staff_id")),
                         "rating_count": {"$gte": 1}, "rating_sum": {"$gte": r}},
                        {"$inc": {"rating_sum": -r, "rating_count": -1}})
                    # lifetime rating totals follow, so a deleted rating doesn't linger there
                    db.taskbooks.update_one(
                        {"guild_id": guild_id, "user_id": str(gone.get("staff_id")),
                         "life_rating_count": {"$gte": 1}, "life_rating_sum": {"$gte": r}},
                        {"$inc": {"life_rating_sum": -r, "life_rating_count": -1}})
                    if tb.modified_count:
                        dashboard_db.enqueue_task("taskbook_refresh_all", guild_id, {})
                    # ...and off the ticket's transcript.
                    try:
                        tid = ObjectId(gone.get("transcript_id") or "")
                        db.transcripts.update_one(
                            {"_id": tid}, {"$unset": {"rating": "", "rating_comment": ""}})
                    except Exception:
                        pass
                return redirect(url_for("dashboard.ticket_ratings_page",
                                        guild_id=guild_id, days=keep_days, deleted=1))
            return redirect(url_for("dashboard.ticket_ratings_page", guild_id=guild_id))

        try:
            days = int(request.args.get("days", 30))
        except (TypeError, ValueError):
            days = 30
        if days not in (7, 30, 90, 0):
            days = 30
        since = datetime.now(timezone.utc) - timedelta(days=days) if days else None

        rated_q = {"guild_id": guild_id, "rating": {"$ne": None}}
        sent_q = {"guild_id": guild_id}
        if since:
            rated_q["rated_at"] = {"$gte": since}
            sent_q["created_at"] = {"$gte": since}
        sent = db.ticket_ratings.count_documents(sent_q)

        # The database does the counting (one row per staff member comes back), so the page
        # stays fast however many ratings there are. Indexes: see cogs/ticket_ratings.py.
        def _count(star):
            return {"$sum": {"$cond": [{"$eq": ["$rating", star]}, 1, 0]}}

        rows, total, grand = [], 0, 0
        for g in db.ticket_ratings.aggregate([
            {"$match": rated_q},
            {"$sort": {"rated_at": -1}},   # so $first below is the most recent staff name
            {"$group": {"_id": "$staff_id", "name": {"$first": "$staff_name"},
                        "count": {"$sum": 1}, "total": {"$sum": "$rating"},
                        "d1": _count(1), "d2": _count(2), "d3": _count(3),
                        "d4": _count(4), "d5": _count(5)}},
        ], allowDiskUse=True):
            dist = {n: g.get(f"d{n}", 0) for n in (1, 2, 3, 4, 5)}
            rows.append({
                "staff_id": g["_id"], "name": g.get("name") or g["_id"],
                "count": g["count"], "total": g["total"], "dist": dist,
                "avg": g["total"] / g["count"], "max_dist": max(dist.values()) or 1,
            })
            total += g["count"]
            grand += g["total"]
        rows.sort(key=lambda r: (-r["avg"], -r["count"]))

        reviews = [
            {"id": str(d["_id"]), "staff": d.get("staff_name") or d["staff_id"],
             "rating": int(d["rating"]), "text": d.get("comment") or "",
             "ticket": d.get("channel_name") or "", "when": d.get("rated_at")}
            for d in db.ticket_ratings.find(rated_q).sort("rated_at", -1).limit(50)
        ]
        overall = (grand / total) if total else None
        return render_template(
            "ticket_ratings.html",
            guild_id=guild_id, rows=rows, reviews=reviews, days=days,
            total=total, sent=sent, overall=overall, deleted=request.args.get("deleted"),
        )

    # -----------------------------------------------------------------
    # Strikes (per Change type): rules, reset ticks, and managing strikes.
    # Settings: guild_settings.strikes.types.<type_id> (see utils/strikes_core.py).
    # Strikes themselves live in the `staff_strikes` collection (the bot writes them).
    # Deleting a strike / resetting a member or a whole type is logged in `staff_strike_audit`.
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # Promotion history (read-only; the bot writes it in cogs/changes.py)
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/promotion-history")
    def promotion_history_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from utils import changes_core as cc
        settings_doc = dashboard_db.get_guild_settings(guild_id)
        types = cc.normalize_types((settings_doc.get("changes") or {}).get("types"))

        search = (request.args.get("q") or "").strip()[:100]
        type_id = (request.args.get("type") or "").strip()
        kind = (request.args.get("kind") or "").strip()
        rows = dashboard_db.get_promotion_history(guild_id, search, type_id, kind)

        # One member's timeline: a search that matches exactly one person.
        members = {r["member_id"] for r in rows}
        timeline_of = None
        if len(members) == 1 and (search or type_id):
            timeline_of = rows[0]["member_name"]
        return render_template(
            "promotion_history.html",
            guild_id=guild_id, rows=rows, types=types, search=search,
            type_id=type_id, kind=kind, timeline_of=timeline_of,
            limit_hit=len(rows) >= 200,
        )

    # -----------------------------------------------------------------
    # Scheduled announcements (the bot's loop in cogs/announcements.py posts them)
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # Staff automation: promotion suggestions, probation, weekly report
    # (the bot's cogs/staff_automation.py does the work; settings live in
    #  guild_settings.staff_automation, see utils/staff_automation_core.py)
    # -----------------------------------------------------------------
    # -----------------------------------------------------------------
    # Dashboard audit log: who changed what, on which page (hook: dashboard/audit.py)
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/audit-log")
    def audit_log_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)
        actor = (request.args.get("actor") or "").strip()[:60]
        page_f = (request.args.get("page") or "").strip()[:60]
        q = (request.args.get("q") or "").strip()[:80]
        try:
            pg = max(1, int(request.args.get("p") or 1))
        except ValueError:
            pg = 1
        per = 50
        rows, total = dashboard_db.search_audit(guild_id, actor, page_f, q, (pg - 1) * per, per)
        return render_template(
            "audit_log.html", guild_id=guild_id, rows=rows, total=total, page_no=pg,
            pages_total=max(1, -(-total // per)), actor=actor, page_f=page_f, q=q,
            pages=dashboard_db.audit_pages(guild_id),
            retention=dashboard_db.AUDIT_RETENTION_DAYS)

    @bp.route("/guild/<guild_id>/staff-automation", methods=["GET", "POST"])
    def staff_automation_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from datetime import datetime, timezone, timedelta
        from utils import changes_core as cc
        from utils import staff_automation_core as sac
        from utils import announce_core

        gid = int(guild_id)
        db = dashboard_db.get_db()
        settings_doc = dashboard_db.get_guild_settings(guild_id)
        all_types = cc.normalize_types((settings_doc.get("changes") or {}).get("types"))
        # Only the type ticked "Show in Staff Automation" on the Changes page (or every type
        # while none is). Hidden types keep their saved settings; saving never touches them.
        types = cc.automation_types(all_types)
        only_type = types[0]["name"] if len(types) == 1 and any(
            t.get("staff_automation") for t in all_types) else ""
        cfg = sac.normalize(settings_doc.get("staff_automation"))
        channels = get_text_channels(gid)
        roles = get_guild_roles(gid)
        role_names = {str(r["id"]): r["name"] for r in roles}

        def _back(**kw):
            return redirect(url_for("dashboard.staff_automation_page", guild_id=guild_id, **kw))

        if request.method == "POST":
            f = request.form
            section = f.get("section")
            chan_ids = {str(c["id"]) for c in channels}
            role_ids = {str(r["id"]) for r in roles}

            if section == "general":
                ch, rl = f.get("mgmt_channel_id") or "", f.get("ping_role_id") or ""
                cfg["mgmt_channel_id"] = ch if ch in chan_ids else ""
                cfg["ping_role_id"] = rl if rl in role_ids else ""
            elif section == "suggestions":
                cfg["suggestions"]["enabled"] = f.get("enabled") == "on"
                for t in types:
                    rungs = {}
                    for k in range(1, len(t["ladder"])):
                        pre = f"sug__{t['id']}__{k}__"
                        rungs[str(k)] = {"handled": f.get(pre + "handled"), "closed": f.get(pre + "closed"),
                                         "min_rating": f.get(pre + "min_rating"),
                                         "min_ratings": f.get(pre + "min_ratings")}
                    cfg["suggestions"]["types"][t["id"]] = {
                        "use_lifetime": f.get(f"sug__{t['id']}__lifetime") == "on", "rungs": rungs}
            elif section == "probation":
                cfg["probation"]["enabled"] = f.get("enabled") == "on"
                for t in types:
                    cfg["probation"]["types"][t["id"]] = f.get(f"prob__{t['id']}")
            elif section == "report":
                rep = cfg["report"]
                rep["enabled"] = f.get("enabled") == "on"
                ch = f.get("channel_id") or ""
                rep["channel_id"] = ch if ch in chan_ids else ""
                rep["weekday"], rep["hour"] = f.get("weekday"), f.get("hour")
                tz = (f.get("tz") or "UTC").strip()
                if sac.get_tz(tz) is None:
                    return _back(error="tz")
                rep["tz"] = tz
                if rep["enabled"] and not rep["channel_id"]:
                    return _back(error="channel")
            elif section == "report_now":
                if not cfg["report"]["channel_id"]:
                    return _back(error="channel")
                dashboard_db.enqueue_task("staff_report_now", guild_id, {})
                dashboard_db.log_audit(guild_id, *_audit_actor(discord_session),
                                       "staff_report_now", "Posted the weekly staff report now")
                return _back(queued=1)
            elif section == "backdate":
                uid, tid = (f.get("user_id") or "").strip(), f.get("type_id") or ""
                if not uid.isdigit() or tid not in {t["id"] for t in types}:
                    return _back(error="member")
                raw = (f.get("date") or "").strip()
                key = {"guild_id": guild_id, "user_id": uid, "type_id": tid}
                if raw:
                    try:
                        when = datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                    except ValueError:
                        return _back(error="date")
                    if when > datetime.now(timezone.utc):
                        return _back(error="date")
                    db.staff_probation.update_one(key, {"$set": {"backdate": when},
                                                        "$setOnInsert": {"status": "active"}}, upsert=True)
                else:
                    db.staff_probation.update_one(key, {"$unset": {"backdate": ""}})
                dashboard_db.log_audit(guild_id, *_audit_actor(discord_session), "probation_backdate",
                                       f"Probation start for {uid} set to {raw or 'taskbook date'}")
                return _back(saved=1)
            else:
                return _back()

            dashboard_db.update_guild_settings(guild_id, {"staff_automation": sac.normalize(cfg)})
            dashboard_db.log_audit(guild_id, *_audit_actor(discord_session),
                                   "staff_automation", f"Saved staff automation: {section}")
            return _back(saved=1)

        # ---- GET: suggestion rows (one per type per rung above the base)
        sug_types = []
        for t in types:
            tcfg = cfg["suggestions"]["types"].get(t["id"]) or {"use_lifetime": True, "rungs": {}}
            rungs = []
            for e in t["ladder"][1:]:
                req = tcfg["rungs"].get(str(e["index"])) or {"handled": 0, "closed": 0,
                                                              "min_rating": 0, "min_ratings": 0}
                rungs.append({"k": e["index"], "label": cc.label(e, role_names.get(e["role_id"])),
                              "req": req})
            sug_types.append({"id": t["id"], "name": t["name"], "rungs": rungs,
                              "lifetime": tcfg["use_lifetime"]})
        prob_types = [{"id": t["id"], "name": t["name"],
                       "days": cfg["probation"]["types"].get(t["id"], 0)} for t in types]

        # ---- GET: who is on probation right now
        now = datetime.now(timezone.utc)
        on_prob = []
        books = {b["user_id"]: b for b in db.taskbooks.find({"guild_id": guild_id})}
        pdocs = {(d["user_id"], d["type_id"]): d for d in db.staff_probation.find({"guild_id": guild_id})}
        for t in types:
            days = cfg["probation"]["types"].get(t["id"], 0)
            if days <= 0:
                continue
            for uid, tb in books.items():
                m = get_guild_member(gid, uid)
                if not m:
                    continue
                if cc.highest_index(m.get("roles") or [], t["ladder"]) < 1:
                    continue
                pd = pdocs.get((uid, t["id"])) or {}
                if pd.get("status") == "passed":
                    continue
                start = pd.get("backdate") or tb.get("created_at")
                end = sac.probation_end(start, days, pd.get("extended_until"))
                if end is None:
                    continue
                on_prob.append({
                    "user_id": uid, "name": member_display_name(m, uid), "type_id": t["id"],
                    "type_name": t["name"], "start": sac.aware(start).strftime("%Y-%m-%d"),
                    "backdated": bool(pd.get("backdate")), "end": end.strftime("%Y-%m-%d"),
                    "over": end <= now, "in_review": pd.get("status") == "review"})
        on_prob.sort(key=lambda r: r["end"])

        return render_template(
            "staff_automation.html",
            guild_id=guild_id, cfg=cfg, channels=channels, roles=roles, types=types,
            only_type=only_type, sug_types=sug_types, prob_types=prob_types, on_prob=on_prob,
            weekdays=sac.WEEKDAYS, timezones=announce_core.COMMON_TIMEZONES,
            saved=request.args.get("saved"), queued=request.args.get("queued"),
            error=request.args.get("error"),
        )

    @bp.route("/guild/<guild_id>/announcements", methods=["GET", "POST"])
    def announcements_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from datetime import datetime, timezone
        from bson import ObjectId
        from utils import announce_core as ac

        gid = int(guild_id)
        col = dashboard_db.get_db().scheduled_announcements

        def _back(**kw):
            return redirect(url_for("dashboard.announcements_page", guild_id=guild_id, **kw))

        def _oid(raw):
            # ObjectId(None) would silently make a NEW id, so only accept a real 24-hex string.
            raw = (raw or "").strip()
            if not raw or not ObjectId.is_valid(raw):
                return None
            return ObjectId(raw)

        if request.method == "POST":
            action = request.form.get("action")
            now = datetime.now(timezone.utc)

            if action in ("cancel", "delete"):
                oid = _oid(request.form.get("ann_id"))
                if oid is None:
                    return _back(error="missing")
                if action == "delete":
                    col.delete_one({"_id": oid, "guild_id": guild_id})
                else:
                    col.update_one({"_id": oid, "guild_id": guild_id, "status": "scheduled"},
                                   {"$set": {"status": "cancelled", "updated_at": now}})
                dashboard_db.log_audit(guild_id, *_audit_actor(discord_session),
                                       f"announcement_{action}", f"{action.title()} announcement")
                return _back(saved=1)

            if action == "save":
                channels = {str(c["id"]) for c in get_text_channels(gid)}
                roles = {str(r["id"]) for r in get_guild_roles(gid)}
                channel_id = request.form.get("channel_id") or ""
                if channel_id not in channels:
                    return _back(error="channel")
                role_id = request.form.get("ping_role_id") or ""
                use_embed = request.form.get("use_embed") == "on"
                message = (request.form.get("message") or "").strip()
                title = (request.form.get("title") or "").strip()[:ac.MAX_TITLE]
                limit = ac.MAX_EMBED_TEXT if use_embed else ac.MAX_TEXT
                if not message:
                    return _back(error="empty")
                if len(message) > limit:
                    return _back(error="long")
                tzname = (request.form.get("tz") or "UTC").strip()
                repeat = request.form.get("repeat") or "once"
                first_local = (request.form.get("when") or "").strip()[:16]
                if ac.get_tz(tzname) is None or repeat not in ac.REPEATS:
                    return _back(error="time")
                local = ac.parse_local(first_local)
                if local is None:
                    return _back(error="time")
                nxt = ac.next_run(first_local, tzname, repeat, now)
                if nxt is None:
                    return _back(error="past")      # a one-off in the past
                data = {
                    "guild_id": guild_id, "channel_id": channel_id,
                    "ping_role_id": role_id if role_id in roles else None,
                    "use_embed": use_embed, "title": title if use_embed else "",
                    "message": message,
                    "color": ac.clean_color(request.form.get("color")),
                    "image_url": ac.clean_image_url(request.form.get("image_url")),
                    "first_local": local.strftime("%Y-%m-%dT%H:%M"), "tz": tzname,
                    "repeat": repeat, "next_run": nxt, "status": "scheduled",
                    "last_error": "", "updated_at": now,
                }
                oid = _oid(request.form.get("ann_id"))
                if oid is not None:
                    res = col.update_one({"_id": oid, "guild_id": guild_id}, {"$set": data})
                    if not res.matched_count:
                        return _back(error="missing")
                else:
                    if col.count_documents({"guild_id": guild_id, "status": "scheduled"}) >= ac.MAX_ACTIVE:
                        return _back(error="full")
                    data.update({"created_at": now, "run_count": 0,
                                 "created_by": _audit_actor(discord_session)[1]})
                    col.insert_one(data)
                dashboard_db.log_audit(
                    guild_id, *_audit_actor(discord_session), "announcement_save",
                    f"Scheduled an announcement ({repeat}) for {first_local} {tzname}")
                return _back(saved=1)
            return _back()

        rows = list(col.find({"guild_id": guild_id}).sort("next_run", 1))
        chan_names = {str(c["id"]): c["name"] for c in get_text_channels(gid)}
        role_names = {str(r["id"]): r["name"] for r in get_guild_roles(gid)}
        upcoming, past = [], []
        for r in rows:
            r["id"] = str(r["_id"])
            r["channel_name"] = chan_names.get(str(r.get("channel_id")), "deleted channel")
            r["role_name"] = role_names.get(str(r.get("ping_role_id") or ""), "")
            r["next_local"] = ac.to_local_text(r["next_run"], r.get("tz"))
            r["last_local"] = (ac.to_local_text(r["last_sent_at"], r.get("tz"))
                               if r.get("last_sent_at") else "")
            r["edit"] = {k: r.get(k) for k in ("message", "title", "use_embed", "image_url",
                                                "first_local", "tz", "repeat")}
            r["edit"].update({"id": r["id"], "channel_id": str(r.get("channel_id")),
                              "ping_role_id": str(r.get("ping_role_id") or ""),
                              "color": "#%06x" % int(r.get("color") or 0x5865F2)})
            (upcoming if r["status"] == "scheduled" else past).append(r)
        past.sort(key=lambda r: r.get("updated_at") or r["next_run"], reverse=True)
        return render_template(
            "announcements.html",
            guild_id=guild_id, upcoming=upcoming, past=past[:25],
            channels=get_text_channels(gid), roles=get_guild_roles(gid),
            timezones=ac.COMMON_TIMEZONES, max_text=ac.MAX_TEXT, max_embed=ac.MAX_EMBED_TEXT,
            saved=request.args.get("saved"), error=request.args.get("error"),
        )

    @bp.route("/guild/<guild_id>/strikes", methods=["GET", "POST"])
    def strikes_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from utils import changes_core as cc
        from utils import strikes_core as sc

        gid = int(guild_id)
        settings_doc = dashboard_db.get_guild_settings(guild_id)
        types = cc.normalize_types((settings_doc.get("changes") or {}).get("types"))

        def _back(type_id=None, **kw):
            if type_id:
                kw["_anchor"] = f"t-{type_id}"
            return redirect(url_for("dashboard.strikes_page", guild_id=guild_id, **kw))

        def _actor():
            try:
                return discord_session.fetch_user().name
            except Exception:
                return "dashboard"

        if request.method == "POST":
            action = request.form.get("action")
            tid = (request.form.get("type_id") or "").strip()
            ctype = cc.find_type(types, tid) if tid else None
            if ctype is None:
                return _back(error="type_missing")
            cfg = dashboard_db.get_strike_type_config(guild_id, ctype["id"])
            uid = (request.form.get("user_id") or "").strip()

            if action == "save_options":
                cfg["reset_on_promotion"] = request.form.get("reset_on_promotion") == "on"
                cfg["reset_on_demotion"] = request.form.get("reset_on_demotion") == "on"
                old_days = cfg.get("expire_days", 0)
                cfg["expire_days"] = sc.clean_expire_days(request.form.get("expire_days"))
                cfg["expire_dm"] = request.form.get("expire_dm") == "on"
                dashboard_db.save_strike_type_config(guild_id, ctype["id"], cfg)
                if cfg["expire_days"] != old_days:
                    dashboard_db.log_audit(
                        guild_id, *_audit_actor(discord_session), "strike_expiry",
                        f"{ctype['name']}: strikes expire after "
                        f"{cfg['expire_days'] or 'never'} day(s) (was {old_days or 'never'})")
                return _back(ctype["id"], saved=1)

            if action == "add_rule":
                try:
                    at = int(request.form.get("at", ""))
                    demote = int(request.form.get("demote", ""))
                except ValueError:
                    return _back(ctype["id"], error="rule_invalid")
                max_demote = max(1, len(ctype["ladder"]) - 1)
                if not (1 <= at <= sc.MAX_STRIKES_AT) or not (1 <= demote <= max_demote):
                    return _back(ctype["id"], error="rule_invalid")
                rules = [r for r in cfg["rules"] if r["at"] != at]   # same threshold = replace
                if len(rules) >= sc.MAX_RULES:
                    return _back(ctype["id"], error="rules_full")
                rules.append({"at": at, "demote": demote})
                cfg["rules"] = sc.normalize_rules(rules)
                dashboard_db.save_strike_type_config(guild_id, ctype["id"], cfg)
                return _back(ctype["id"], saved=1)

            if action == "remove_rule":
                try:
                    at = int(request.form.get("at", ""))
                except ValueError:
                    return _back(ctype["id"], error="rule_invalid")
                cfg["rules"] = [r for r in cfg["rules"] if r["at"] != at]
                dashboard_db.save_strike_type_config(guild_id, ctype["id"], cfg)
                return _back(ctype["id"], saved=1)

            if action == "delete_strike":
                try:
                    number = int(request.form.get("number", ""))
                except ValueError:
                    return _back(ctype["id"], error="strike_missing")
                doc = dashboard_db.delete_strike(guild_id, number, ctype["id"])
                if doc is None:      # not found, or it belongs to a different type
                    return _back(ctype["id"], error="strike_missing")
                dashboard_db.add_strike_audit(
                    guild_id, "delete_strike", ctype["id"], ctype["name"], doc["user_id"],
                    _actor(), f"Strike #{doc['number']}")
                return _back(ctype["id"], cleared=1)

            if action == "reset_member":
                if not uid.isdigit():
                    return _back(ctype["id"], error="strike_missing")
                n = dashboard_db.clear_strikes(guild_id, ctype["id"], uid)
                if n:
                    dashboard_db.add_strike_audit(
                        guild_id, "reset_member", ctype["id"], ctype["name"], uid,
                        _actor(), f"{n} strike(s) cleared")
                return _back(ctype["id"], cleared=1)

            if action == "reset_type":
                n = dashboard_db.clear_strikes(guild_id, ctype["id"])
                if n:
                    dashboard_db.add_strike_audit(
                        guild_id, "reset_type", ctype["id"], ctype["name"], None,
                        _actor(), f"{n} strike(s) cleared for everyone")
                return _back(ctype["id"], cleared=1)

            return _back(ctype["id"], error="type_missing")

        # ---- GET: build the view model ----
        cards = []
        for t in types:
            cfg = dashboard_db.get_strike_type_config(guild_id, t["id"])
            rows = dashboard_db.get_strike_rows(guild_id, t["id"])
            for row in rows[:40]:        # names for the first rows only (one REST call each, cached)
                m = get_guild_member(gid, row["user_id"])
                row["name"] = member_display_name(m, "") if m else ""
            cards.append({
                "id": t["id"], "name": t["name"], "cfg": cfg,
                "rungs": len(t["ladder"]), "max_demote": max(1, len(t["ladder"]) - 1),
                "rows": rows, "total": sum(r["count"] for r in rows),
                "expiring": dashboard_db.count_expiring_strikes(
                    guild_id, t["id"], cfg.get("expire_days", 0)),
            })
        return render_template(
            "strikes.html",
            guild_id=guild_id,
            cards=cards,
            audit=dashboard_db.get_strike_audit(guild_id),
            max_at=sc.MAX_STRIKES_AT, max_rules=sc.MAX_RULES, max_expire=sc.MAX_EXPIRE_DAYS,
            saved=request.args.get("saved"),
            cleared=request.args.get("cleared"),
            error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # LOA (leave of absence): the role that gets given + the request channel
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/loa", methods=["GET", "POST"])
    def loa_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        gid = int(guild_id)
        db = dashboard_db.get_db()
        roles = sorted(get_guild_roles(gid), key=lambda r: r.get("position", 0), reverse=True)
        channels = sorted(get_text_channels(gid), key=lambda c: c.get("position", 0))

        if request.method == "POST":
            role_id = request.form.get("role_id") or ""
            channel_id = request.form.get("channel_id") or ""
            if role_id not in {str(r["id"]) for r in roles}:
                return redirect(url_for("dashboard.loa_page", guild_id=guild_id, error="role"))
            if channel_id not in {str(c["id"]) for c in channels}:
                return redirect(url_for("dashboard.loa_page", guild_id=guild_id, error="channel"))
            db.loa_settings.update_one(
                {"guild_id": gid},
                {"$set": {"role_id": int(role_id), "channel_id": int(channel_id)}},
                upsert=True)
            return redirect(url_for("dashboard.loa_page", guild_id=guild_id, saved=1))

        settings_doc = db.loa_settings.find_one({"guild_id": gid}) or {}
        role_names = {str(r["id"]): r["name"] for r in roles}
        channel_names = {str(c["id"]): c["name"] for c in channels}
        active = list(db.loa_requests.find({"guild_id": gid, "status": "active"}).sort("end", 1))
        pending = list(db.loa_requests.find({"guild_id": gid, "status": "pending"}).sort("requested_at", 1))
        recent = list(db.loa_requests.find(
            {"guild_id": gid, "status": {"$in": ["denied", "ended", "expired", "removed"]}}
        ).sort("requested_at", -1).limit(15))

        names = {}
        for doc in active + pending + recent:
            uid = str(doc["user_id"])
            if uid not in names:
                m = get_guild_member(gid, uid)
                names[uid] = member_display_name(m, uid) if m else uid

        return render_template(
            "loa.html",
            guild_id=guild_id, roles=roles, channels=channels,
            role_names=role_names, channel_names=channel_names,
            settings=settings_doc, active=active, pending=pending, recent=recent, names=names,
            saved=request.args.get("saved"), error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Staff activity (any number of checks, each for its own role)
    # -----------------------------------------------------------------
    MAX_EXEMPT_ROLES = 25
    MAX_ACTIVITY_CHECKS = 10

    @bp.route("/guild/<guild_id>/staff-activity", methods=["GET", "POST"])
    def staff_activity_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from bson import ObjectId
        from datetime import datetime, timezone

        gid = int(guild_id)
        db = dashboard_db.get_db()
        configs_col = db.staff_activity_configs
        roles = sorted(get_guild_roles(gid), key=lambda r: r.get("position", 0), reverse=True)
        channels = sorted(get_text_channels(gid), key=lambda c: c.get("position", 0))
        change_types = [
            {"id": str(t.get("id")), "name": t.get("name")}
            for t in ((dashboard_db.get_guild_settings(guild_id).get("changes") or {}).get("types") or [])
            if isinstance(t, dict) and t.get("id") and t.get("name")
        ]

        def _oid(raw):
            try:
                return ObjectId(raw)
            except Exception:
                return None

        def _back(**kw):
            return redirect(url_for("dashboard.staff_activity_page", guild_id=guild_id, **kw))

        if request.method == "POST":
            form_type = request.form.get("form_type")

            if form_type == "save_check":
                role_ids = {str(r["id"]) for r in roles}
                channel_ids = {str(c["id"]) for c in channels}
                name = (request.form.get("name") or "").strip()[:60]
                role_id = request.form.get("role_id") or ""
                channel_id = request.form.get("channel_id") or ""
                if not name:
                    return _back(error="name")
                if role_id not in role_ids:
                    return _back(error="role")
                if channel_id not in channel_ids:
                    return _back(error="channel")
                strike_on = request.form.get("strike_on_miss") == "on"
                strike_type = request.form.get("strike_type_id") or ""
                if strike_on and strike_type not in {t["id"] for t in change_types}:
                    return _back(error="striketype")
                def _num(field, default, lo, hi):
                    try:
                        return max(lo, min(hi, int(request.form.get(field) or default)))
                    except ValueError:
                        return default

                data = {
                    "name": name,
                    "enabled": request.form.get("enabled") == "on",
                    "role_id": int(role_id),
                    "channel_id": int(channel_id),
                    "interval_days": _num("interval_days", 2, 1, 90),
                    "window_hours": _num("window_hours", 24, 1, 168),
                    "emoji": (request.form.get("emoji") or "").strip()[:8] or "\U0001F48E",
                    "strike_on_miss": strike_on,
                    "strike_type_id": strike_type if strike_on else None,
                }
                check_id = _oid(request.form.get("check_id")) if request.form.get("check_id") else None
                if check_id:
                    configs_col.update_one({"_id": check_id, "guild_id": gid}, {"$set": data})
                else:
                    if configs_col.count_documents({"guild_id": gid}) >= MAX_ACTIVITY_CHECKS:
                        return _back(error="limit")
                    data.update({
                        "guild_id": gid,
                        "last_opened_at": None,
                        "created_at": datetime.now(timezone.utc),
                    })
                    configs_col.insert_one(data)
                return _back(saved=1)

            if form_type == "toggle_check":
                oid = _oid(request.form.get("check_id"))
                if oid:
                    configs_col.update_one(
                        {"_id": oid, "guild_id": gid},
                        {"$set": {"enabled": request.form.get("enabled") == "1"}})
                return _back(saved=1)

            if form_type == "delete_check":
                oid = _oid(request.form.get("check_id"))
                if oid:
                    configs_col.delete_one({"_id": oid, "guild_id": gid})
                return _back(deleted=1)

            return _back()

        role_names = {str(r["id"]): r["name"] for r in roles}
        channel_names = {str(c["id"]): c["name"] for c in channels}
        configs = list(configs_col.find({"guild_id": gid}).sort("created_at", 1))
        recent = list(db.staff_activity_checks.find({"guild_id": gid}).sort("opened_at", -1).limit(10))

        return render_template(
            "staff_activity.html",
            guild_id=guild_id,
            configs=configs,
            roles=roles,
            channels=channels,
            role_names=role_names,
            channel_names=channel_names,
            change_types=change_types,
            type_names={t["id"]: t["name"] for t in change_types},
            recent=recent,
            saved=request.args.get("saved"),
            deleted=request.args.get("deleted"),
            error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Leveling
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/leveling", methods=["GET", "POST"])
    def leveling_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        from datetime import datetime, timedelta, timezone

        gid = int(guild_id)
        db = dashboard_db.get_db()
        roles = sorted(get_guild_roles(gid), key=lambda r: r.get("position", 0), reverse=True)
        all_channels = get_guild_channels(gid)
        # text, voice, announcement, stage, forum + categories (ignoring a category ignores its channels)
        channels = sorted(
            [c for c in all_channels if c.get("type") in (0, 2, 4, 5, 13, 15)],
            key=lambda c: (c.get("type") != 4, c.get("position", 0)))
        role_ids = {str(r["id"]) for r in roles}
        channel_ids = {str(c["id"]) for c in channels}
        text_ids = {str(c["id"]) for c in all_channels if c.get("type") in (0, 5)}

        def _cfg():
            return lv.normalize_config(dashboard_db.get_guild_settings(guild_id).get("leveling"))

        def _save(update):
            dashboard_db.update_guild_settings(guild_id, {f"leveling.{k}": v for k, v in update.items()})

        def _back(**kw):
            return redirect(url_for("dashboard.leveling_page", guild_id=guild_id, **kw))

        def _f(name, default=None):
            return request.form.get(name, default)

        if request.method == "POST":
            ft = _f("form_type")
            cfg = _cfg()

            if ft == "save_settings":
                announce_ch = _f("announce_channel_id") or ""
                raw = {
                    "enabled": _f("enabled") == "on",
                    "xp_min": _f("xp_min"), "xp_max": _f("xp_max"),
                    "cooldown_seconds": _f("cooldown_seconds"),
                    "min_length": _f("min_length"),
                    "voice_enabled": _f("voice_enabled") == "on",
                    "voice_xp_per_minute": _f("voice_xp_per_minute"),
                    "voice_require_company": _f("voice_require_company") == "on",
                    "voice_ignore_muted": _f("voice_ignore_muted") == "on",
                    "announce_mode": _f("announce_mode"),
                    "announce_channel_id": announce_ch if announce_ch in text_ids else None,
                    "announce_message": _f("announce_message"),
                    "announce_style": _f("announce_style"),
                    "card_accent": _f("card_accent"),
                    "card_background_url": _f("card_background_url"),
                    "role_mode": _f("role_mode"),
                    "ignored_channel_ids": [c for c in request.form.getlist("ignored_channel_ids") if c in channel_ids],
                    "ignored_role_ids": [r for r in request.form.getlist("ignored_role_ids") if r in role_ids],
                }
                clean = lv.normalize_config(raw)
                _save({k: clean[k] for k in raw})
                return _back(saved=1)

            if ft == "add_reward":
                rid = _f("role_id") or ""
                try:
                    level = int(_f("level"))
                except (TypeError, ValueError):
                    return _back(error="level")
                if rid not in role_ids or not 1 <= level <= lv.MAX_LEVEL:
                    return _back(error="reward")
                rewards = [r for r in cfg["rewards"] if r["role_id"] != rid]
                rewards.append({"level": level, "role_id": rid})
                _save({"rewards": lv.normalize_config({"rewards": rewards})["rewards"]})
                return _back(saved=1)

            if ft == "delete_reward":
                _save({"rewards": [r for r in cfg["rewards"] if r["role_id"] != _f("role_id")]})
                return _back(saved=1)

            if ft == "add_multiplier":
                kind = _f("kind")
                target = _f("target") or ""
                try:
                    mult = float(_f("mult"))
                except (TypeError, ValueError):
                    return _back(error="mult")
                if not 0 <= mult <= 100:
                    return _back(error="mult")
                if kind == "role" and target in role_ids:
                    items = [m for m in cfg["role_multipliers"] if m["role_id"] != target]
                    items.append({"role_id": target, "mult": mult})
                    _save({"role_multipliers": items})
                elif kind == "channel" and target in channel_ids:
                    items = [m for m in cfg["channel_multipliers"] if m["channel_id"] != target]
                    items.append({"channel_id": target, "mult": mult})
                    _save({"channel_multipliers": items})
                else:
                    return _back(error="mult")
                return _back(saved=1)

            if ft == "delete_multiplier":
                kind, target = _f("kind"), _f("target")
                if kind == "role":
                    _save({"role_multipliers": [m for m in cfg["role_multipliers"] if m["role_id"] != target]})
                elif kind == "channel":
                    _save({"channel_multipliers": [m for m in cfg["channel_multipliers"] if m["channel_id"] != target]})
                return _back(saved=1)

            if ft == "start_event":
                try:
                    mult = float(_f("event_multiplier"))
                    hours = int(_f("event_hours"))
                except (TypeError, ValueError):
                    return _back(error="event")
                if not (0 < mult <= 100 and 1 <= hours <= 24 * 30):
                    return _back(error="event")
                _save({"event_multiplier": mult,
                       "event_ends_at": datetime.now(timezone.utc) + timedelta(hours=hours)})
                return _back(saved=1)

            if ft == "end_event":
                _save({"event_multiplier": 1.0, "event_ends_at": None})
                return _back(saved=1)

            return _back()

        cfg = _cfg()
        role_names = {str(r["id"]): r["name"] for r in roles}
        channel_names = {str(c["id"]): c for c in channels}

        top = list(db.levels.find({"guild_id": guild_id, "xp": {"$gt": 0}}).sort("xp", -1).limit(25))
        board = [{
            "rank": i, "name": t.get("name") or t["user_id"], "xp": int(t["xp"]),
            "level": lv.level_from_xp(t["xp"]),
            "messages": int(t.get("messages", 0)),
            "voice_minutes": int(t.get("voice_seconds", 0)) // 60,
        } for i, t in enumerate(top, 1)]
        ranked = db.levels.count_documents({"guild_id": guild_id, "xp": {"$gt": 0}})

        return render_template(
            "leveling.html",
            guild_id=guild_id, cfg=cfg, roles=roles, channels=channels,
            announce_placeholders=lv.ANNOUNCE_PLACEHOLDERS,
            role_names=role_names, channel_names=channel_names,
            text_channels=[c for c in channels if c.get("type") in (0, 5)],
            board=board, ranked=ranked,
            event_active=lv.event_active(cfg),
            saved=request.args.get("saved"), error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Taskbooks
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/taskbooks", methods=["GET", "POST"])
    def taskbooks_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        gid = int(guild_id)
        db = dashboard_db.get_db()
        roles = get_guild_roles(gid)
        channels = get_text_channels(gid)
        role_ids = {str(r["id"]) for r in roles}
        channel_ids = {str(c["id"]) for c in channels}

        def _tb_settings():
            raw = dashboard_db.get_guild_settings(guild_id).get("taskbooks") or {}
            return {
                "channel_id": str(raw["channel_id"]) if raw.get("channel_id") else "",
                "staff_role_ids": [str(r) for r in (raw.get("staff_role_ids") or [])],
                "allow_role_ids": [str(r) for r in (raw.get("allow_role_ids") or [])],
            }

        if request.method == "POST":
            form_type = request.form.get("form_type")

            if form_type == "save_settings":
                channel_id = request.form.get("channel_id") or ""
                if channel_id not in channel_ids:
                    channel_id = ""
                dashboard_db.update_guild_settings(guild_id, {
                    "taskbooks.channel_id": channel_id or None,
                    "taskbooks.staff_role_ids": [r for r in request.form.getlist("staff_role_ids") if r in role_ids],
                    "taskbooks.allow_role_ids": [r for r in request.form.getlist("allow_role_ids") if r in role_ids],
                })
                return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, saved=1))

            if form_type == "add_taskbook":
                user_id = (request.form.get("user_id") or "").strip()
                cfg = _tb_settings()
                if not cfg["channel_id"]:
                    return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, error="no_channel"))
                if not user_id.isdigit():
                    return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, error="no_user"))
                if db.taskbooks.find_one({"guild_id": guild_id, "user_id": user_id}):
                    return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, error="exists"))
                dashboard_db.enqueue_task("taskbook_create", guild_id, {"user_id": user_id})
                return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, creating=1))

            if form_type == "import_blacklists":
                dashboard_db.enqueue_task("taskbook_import_blacklists", guild_id, {})
                return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, queued="import"))

            if form_type == "refresh_taskbooks":
                dashboard_db.enqueue_task("taskbook_refresh_all", guild_id, {})
                return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, queued="refresh"))

            if form_type == "reset_taskbook":
                number = request.form.get("number", type=int)
                if number is not None:
                    dashboard_db.enqueue_task("taskbook_reset", guild_id, {"target": str(number)})
                    dashboard_db.log_audit(guild_id, *_audit_actor(discord_session),
                                           "taskbook_reset", f"Reset taskbook #{number} to zero")
                return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, queued="reset"))

            if form_type == "reset_all_taskbooks":
                dashboard_db.enqueue_task("taskbook_reset", guild_id, {"target": "all"})
                dashboard_db.log_audit(guild_id, *_audit_actor(discord_session),
                                       "taskbook_reset_all", "Reset ALL taskbooks to zero")
                return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, queued="reset"))

            if form_type == "delete_taskbook":
                number = request.form.get("number", type=int)
                tb = db.taskbooks.find_one_and_delete({"guild_id": guild_id, "number": number})
                if tb and tb.get("thread_id"):
                    dashboard_db.enqueue_task("taskbook_delete", guild_id, {"thread_id": tb["thread_id"]})
                if tb:
                    dashboard_db.log_audit(guild_id, *_audit_actor(discord_session), "taskbook_delete",
                                           f"Deleted taskbook #{number} ({tb.get('user_name') or tb.get('user_id')})")
                return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id, deleted=1))

            return redirect(url_for("dashboard.taskbooks_page", guild_id=guild_id))

        cfg = _tb_settings()
        if not cfg["channel_id"]:  # suggest a channel called "taskbooks" until one is saved
            guess = next((c for c in channels if (c.get("name") or "").lower() == "taskbooks"), None)
            cfg["channel_id"] = str(guess["id"]) if guess else ""

        books = list(db.taskbooks.find({"guild_id": guild_id}).sort("number", 1))
        have = {b["user_id"] for b in books}

        candidates = []
        if cfg["staff_role_ids"]:
            wanted = set(cfg["staff_role_ids"])
            for m in get_guild_members(gid):
                user = m.get("user") or {}
                if user.get("bot") or str(user.get("id")) in have:
                    continue
                if wanted & {str(r) for r in (m.get("roles") or [])}:
                    candidates.append({"id": str(user["id"]), "name": member_display_name(m, str(user["id"]))})
            candidates.sort(key=lambda c: c["name"].lower())

        from datetime import datetime, timedelta, timezone
        recent = datetime.now(timezone.utc) - timedelta(minutes=10)
        errors = [
            t["error"] for t in db.dashboard_tasks.find(
                {"guild_id": guild_id,
                 "type": {"$in": ["taskbook_create", "taskbook_import_blacklists",
                                  "taskbook_refresh_all", "taskbook_reset"]},
                 "done": True, "created_at": {"$gte": recent},
                 "error": {"$nin": [None, ""]}}
            ).sort("created_at", -1).limit(3)
        ]
        last_task = db.dashboard_tasks.find_one(
            {"guild_id": guild_id,
             "type": {"$in": ["taskbook_import_blacklists", "taskbook_refresh_all", "taskbook_reset"]},
             "done": True, "created_at": {"$gte": recent}, "result": {"$nin": [None, ""]}},
            sort=[("created_at", -1)])
        task_result = (last_task or {}).get("result") or ""
        # accepted old `add` requests not counted yet, whose submitter has a taskbook
        uncounted = db.blacklist_requests.count_documents({
            "guild_id": guild_id, "status": "Accepted", "action": "add",
            "taskbook_counted": {"$ne": True}, "applicant_id": {"$in": list(have)},
        }) if have else 0

        return render_template(
            "taskbooks.html",
            guild_id=guild_id,
            cfg=cfg,
            roles=roles,
            channels=channels,
            books=books,
            candidates=candidates,
            errors=errors,
            task_result=task_result,
            uncounted=uncounted,
            fmt=format_short,
            saved=request.args.get("saved"),
            creating=request.args.get("creating"),
            deleted=request.args.get("deleted"),
            queued=request.args.get("queued"),
            error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Bot Profile (per-server nickname / bio / avatar / banner)
    # -----------------------------------------------------------------
    _BP_MAX_NICK = 32
    _BP_MAX_BIO = 190
    _BP_MAX_IMG = 4 * 1024 * 1024
    _BP_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}

    @bp.route("/guild/<guild_id>/bot-profile", methods=["GET", "POST"])
    def bot_profile_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        col = dashboard_db.get_db().bot_profiles
        gid = str(guild_id)

        def _back(**kw):
            return redirect(url_for("dashboard.bot_profile_page", guild_id=guild_id, **kw))

        if request.method == "POST":
            nick = (request.form.get("nick") or "").strip()
            bio = (request.form.get("bio") or "").strip()
            if len(nick) > _BP_MAX_NICK:
                return _back(error="nick_long")
            if len(bio) > _BP_MAX_BIO:
                return _back(error="bio_long")

            from datetime import datetime, timezone
            update = {"nick": nick or None, "bio": bio or None,
                      "guild_id": gid, "updated_at": datetime.now(timezone.utc)}

            for kind in ("avatar", "banner"):
                if request.form.get(f"remove_{kind}") == "on":
                    update[kind] = None
                f = request.files.get(kind)
                if f and f.filename:
                    raw = f.read(_BP_MAX_IMG + 1)
                    if len(raw) > _BP_MAX_IMG:
                        return _back(error=f"{kind}_big")
                    try:
                        from PIL import Image
                        img = Image.open(io.BytesIO(raw))
                        fmt = img.format
                        img.verify()
                    except Exception:
                        return _back(error=f"{kind}_bad")
                    if fmt not in _BP_MIME:
                        return _back(error=f"{kind}_bad")
                    update[kind] = {"data": raw, "mime": _BP_MIME[fmt],
                                    "hash": hashlib.sha1(raw).hexdigest()}

            col.update_one({"_id": gid}, {"$set": update}, upsert=True)
            dashboard_db.enqueue_task("bot_profile_apply", gid, {})
            return _back(queued=1)

        doc = col.find_one({"_id": gid}) or {}
        applied = doc.get("applied") or {}

        def _h(kind):
            return (doc.get(kind) or {}).get("hash")

        in_sync = bool(doc) and (
            applied.get("nick", "\0") == (doc.get("nick") or None)
            and applied.get("bio", "\0") == (doc.get("bio") or None)
            and applied.get("avatar_hash", "\0") == _h("avatar")
            and applied.get("banner_hash", "\0") == _h("banner")
        )
        view = {
            "nick": doc.get("nick") or "",
            "bio": doc.get("bio") or "",
            "avatar_hash": _h("avatar"),
            "banner_hash": _h("banner"),
        }
        return render_template(
            "bot_profile.html",
            guild_id=guild_id,
            doc=view,
            last=doc.get("last_result"),
            saved_before=bool(doc),
            in_sync=in_sync,
            max_nick=_BP_MAX_NICK,
            max_bio=_BP_MAX_BIO,
            max_mb=_BP_MAX_IMG // (1024 * 1024),
            queued=request.args.get("queued"),
            error=request.args.get("error"),
        )

    @bp.route("/guild/<guild_id>/bot-profile/image/<kind>")
    def bot_profile_image(guild_id, kind):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)
        if kind not in ("avatar", "banner"):
            abort(404)
        doc = dashboard_db.get_db().bot_profiles.find_one({"_id": str(guild_id)}) or {}
        img = doc.get(kind)
        if not img:
            abort(404)
        resp = Response(bytes(img["data"]), mimetype=img.get("mime") or "image/png")
        resp.headers["Cache-Control"] = "private, max-age=3600"
        return resp

    # -----------------------------------------------------------------
    # Vouches
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/vouches", methods=["GET", "POST"])
    def vouches_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            channel_ids = [str(c) for c in request.form.getlist("vouch_channel_ids")]
            report_channel_id = request.form.get("report_channel_id") or None
            scammer_role_id = request.form.get("scammer_role_id") or None
            report_staff_role_id = request.form.get("report_staff_role_id") or None
            dashboard_db.update_guild_settings(guild_id, {"vouches": {
                "channel_ids": channel_ids,
                "report_channel_id": str(report_channel_id) if report_channel_id else None,
                "scammer_role_id": str(scammer_role_id) if scammer_role_id else None,
                "report_staff_role_id": (
                    str(report_staff_role_id) if report_staff_role_id else None
                ),
            }})
            return redirect(url_for("dashboard.vouches_page", guild_id=guild_id, saved=1))

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        cfg = settings_doc.get("vouches") or {}
        return render_template(
            "vouches.html",
            guild_id=guild_id,
            cfg={
                "channel_ids": [str(c) for c in (cfg.get("channel_ids") or [])],
                "report_channel_id": (
                    str(cfg["report_channel_id"]) if cfg.get("report_channel_id") else None
                ),
                "scammer_role_id": (
                    str(cfg["scammer_role_id"]) if cfg.get("scammer_role_id") else None
                ),
                "report_staff_role_id": (
                    str(cfg["report_staff_role_id"]) if cfg.get("report_staff_role_id") else None
                ),
            },
            channels=get_text_channels(int(guild_id)),
            roles=get_guild_roles(int(guild_id)),
            saved=request.args.get("saved"),
        )

    # -----------------------------------------------------------------
    # Applications
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/applications", methods=["GET", "POST"])
    def applications_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            form_type = request.form.get("form_type")

            if form_type == "save_application":
                app_id = request.form.get("application_id") or None
                questions = [q.strip() for q in request.form.getlist("questions")]
                while questions and not questions[-1]:
                    questions.pop()

                if request.form.get("ticket_category_mode", "existing") == "new":
                    _new_cat = (request.form.get("ticket_category_new_name") or "").strip()
                    ticket_category_id = f"__new__:{_new_cat}" if _new_cat else ""
                else:
                    ticket_category_id = request.form.get("ticket_category_id") or ""

                required = {
                    "name": request.form.get("name", "").strip(),
                    "submit_channel_id": request.form.get("submit_channel_id") or "",
                    "accepted_channel_id": request.form.get("accepted_channel_id") or "",
                    "denied_channel_id": request.form.get("denied_channel_id") or "",
                    "ticket_category_id": ticket_category_id,
                }
                missing = [k for k, v in required.items() if not v]
                if missing:
                    return redirect(url_for(
                        "dashboard.applications_page",
                        guild_id=guild_id,
                        error=f"missing_{missing[0]}",
                    ))
                if not questions:
                    return redirect(url_for(
                        "dashboard.applications_page",
                        guild_id=guild_id,
                        error="missing_questions",
                    ))

                data = {
                    "name": required["name"],
                    "emoji": request.form.get("emoji", "").strip(),
                    "button_label": request.form.get("button_label", "").strip(),
                    "button_style": request.form.get("button_style", "primary"),
                    "is_open": request.form.get("is_open") == "on",
                    "submit_channel_id": str(required["submit_channel_id"]),
                    "accepted_channel_id": str(required["accepted_channel_id"]),
                    "denied_channel_id": str(required["denied_channel_id"]),
                    "ticket_category_id": str(required["ticket_category_id"]),
                    "decision_role_ids": [str(r) for r in request.form.getlist("decision_role_ids")],
                    "ping_role_ids": [str(r) for r in request.form.getlist("ping_role_ids")],
                    "ticket_ping_role_ids": [str(r) for r in request.form.getlist("ticket_ping_role_ids")],
                    "ticket_allow_role_ids": [str(r) for r in request.form.getlist("ticket_allow_role_ids")],
                    "change_type_id": _valid_change_type(guild_id, request.form.get("change_type_id")),
                    "accept_message": request.form.get("accept_message", "").strip(),
                    "deny_message": request.form.get("deny_message", "").strip(),
                    "questions": [q for q in questions if q],
                }
                dashboard_db.upsert_application(guild_id, data, app_id)

            elif form_type == "delete_application":
                dashboard_db.delete_application(guild_id, request.form.get("application_id"))

            elif form_type == "toggle_application":
                dashboard_db.set_application_open(
                    guild_id,
                    request.form.get("application_id"),
                    request.form.get("is_open") == "1",
                )

            elif form_type == "save_panel":
                panel_data = {
                    "channel_id": request.form.get("panel_channel_id") or None,
                    "title": request.form.get("panel_title", "").strip(),
                    "description": request.form.get("panel_description", "").strip(),
                    "footer": request.form.get("panel_footer", "").strip(),
                    "color": _hex_to_int(request.form.get("panel_color", "#5865f2")),
                    "color_hex": request.form.get("panel_color", "#5865f2"),
                }
                dashboard_db.upsert_application_panel(guild_id, panel_data)

            elif form_type == "post_panel":
                dashboard_db.enqueue_task("post_application_panel", guild_id)

            elif form_type == "save_guild_config":
                try:
                    timeout = max(1, min(int(request.form.get("dm_timeout_minutes", "30")), 1440))
                except (TypeError, ValueError):
                    timeout = 30
                dashboard_db.update_guild_settings(guild_id, {
                    "applications.dm_timeout_minutes": timeout,
                })

            return redirect(url_for("dashboard.applications_page", guild_id=guild_id, saved=1))

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        apps = dashboard_db.get_applications(guild_id)
        panel = dashboard_db.get_application_panel(guild_id) or {}

        return render_template(
            "applications.html",
            guild_id=guild_id,
            apps=apps,
            panel=panel,
            dm_timeout_minutes=(settings_doc.get("applications") or {}).get("dm_timeout_minutes", 30),
            channels=get_text_channels(int(guild_id)),
            categories=get_categories(int(guild_id)),
            roles=get_guild_roles(int(guild_id)),
            change_types=_change_types(guild_id),
            saved=request.args.get("saved"),
            error=request.args.get("error"),
        )

    @bp.route("/guild/<guild_id>/applications/submissions")
    def application_submissions_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        app_id = request.args.get("application_id") or None
        status = request.args.get("status") or None
        subs = dashboard_db.get_submissions_for_guild(guild_id, application_id=app_id, status=status)
        apps = dashboard_db.get_applications(guild_id)
        apps_by_id = {str(a["_id"]): a for a in apps}
        return render_template(
            "application_submissions.html",
            guild_id=guild_id,
            submissions=subs,
            apps=apps,
            apps_by_id=apps_by_id,
            selected_application_id=app_id,
            selected_status=status,
        )

    @bp.route("/guild/<guild_id>/applications/submission/<submission_id>")
    def application_submission_page(guild_id, submission_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        sub = dashboard_db.get_application_submission(submission_id)
        if not sub or str(sub.get("guild_id")) != str(guild_id):
            abort(404)
        app = dashboard_db.get_application(guild_id, sub.get("application_id"))
        return render_template(
            "application_submission.html",
            guild_id=guild_id,
            submission=sub,
            app=app or {},
        )

    # -----------------------------------------------------------------
    # Moderation
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/moderation", methods=["GET", "POST"])
    def moderation_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            log_channel_id = request.form.get("log_channel_id") or None
            dashboard_db.update_guild_settings(guild_id, {
                "moderation.log_channel_id": (
                    str(log_channel_id) if log_channel_id else None
                ),
            })
            return redirect(url_for("dashboard.moderation_page", guild_id=guild_id, saved=1))

        action = request.args.get("action") or None
        target_id = request.args.get("target_id") or None
        try:
            page = max(int(request.args.get("page", "0")), 0)
        except (TypeError, ValueError):
            page = 0

        cases, total, total_pages = dashboard_db.get_mod_cases(
            guild_id, action=action, target_id=target_id, page=page, per_page=50,
        )

        return render_template(
            "moderation.html",
            guild_id=guild_id,
            cfg=dashboard_db.get_moderation_config(guild_id),
            cases=cases,
            total=total,
            page=page,
            total_pages=total_pages,
            selected_action=action,
            selected_target_id=target_id,
            channels=get_text_channels(int(guild_id)),
            saved=request.args.get("saved"),
        )

    # -----------------------------------------------------------------
    # Auto-mod (dashboard only - the bot has no /automod commands)
    # -----------------------------------------------------------------
    AUTOMOD_RULE_INFO = {
        "spam":     ("Spam",          "Too many messages in 5 seconds.", "messages per 5s"),
        "mentions": ("Mass mentions", "Too many user + role mentions in one message.", "max mentions"),
        "invites":  ("Invite links",  "Invites to OTHER servers (this server's own invites are fine).", None),
        "links":    ("Links",         "Any link whose domain isn't in the allowed list below.", None),
        "caps":     ("Excess caps",   "Messages that are mostly capital letters (10+ letters).", "% capitals"),
        "badwords": ("Bad words",     "Anything on the bad-word list below.", None),
    }

    def _lines(raw: str, commas: bool = False) -> list:
        """One entry per line (bad words may be phrases, so commas only split when asked)."""
        raw = (raw or "").replace(",", "\n") if commas else (raw or "")
        return [ln.strip() for ln in raw.splitlines() if ln.strip()]

    @bp.route("/guild/<guild_id>/automod", methods=["GET", "POST"])
    def automod_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        def _back(**kw):
            return redirect(url_for("dashboard.automod_page", guild_id=guild_id, **kw))

        if request.method == "POST":
            action = request.form.get("action") or "save"

            if action == "clear_strikes":
                uid = (request.form.get("user_id") or "").strip()
                dashboard_db.clear_automod_strikes(guild_id, uid if uid.isdigit() else None)
                return _back(cleared=1)

            # ---- save everything ----
            cfg = {"enabled": request.form.get("enabled") == "on"}
            cfg["log_channel_id"] = request.form.get("log_channel_id") or None

            rules = {}
            for name in AUTOMOD_RULES:
                rule = {"enabled": request.form.get(f"rule_{name}") == "on"}
                bounds = AUTOMOD_LIMIT_BOUNDS.get(name)
                if bounds:
                    try:
                        val = int(request.form.get(f"limit_{name}", ""))
                    except ValueError:
                        return _back(error=f"bad_limit_{name}")
                    if not bounds[0] <= val <= bounds[1]:
                        return _back(error=f"bad_limit_{name}")
                    rule["limit"] = val
                else:
                    rule["limit"] = 0
                rules[name] = rule
            cfg["rules"] = rules

            words, seen = [], set()
            for w in _lines(request.form.get("badwords", "")):
                w = w.lower()
                if 2 <= len(w) <= 50 and w not in seen:
                    seen.add(w)
                    words.append(w)
            cfg["badwords"] = words[:MAX_BADWORDS]

            domains = []
            for d in _lines(request.form.get("link_allow", ""), commas=True):
                d = clean_domain(d)
                if "." in d and len(d) <= 100 and d not in domains:
                    domains.append(d)
            cfg["link_allow"] = domains[:MAX_ALLOWED_DOMAINS]

            cfg["exempt_role_ids"] = [str(r) for r in request.form.getlist("exempt_role_ids") if r.isdigit()]
            cfg["exempt_channel_ids"] = [str(c) for c in request.form.getlist("exempt_channel_ids") if c.isdigit()]

            strikes = {}
            for key, (lo, hi) in AUTOMOD_STRIKE_BOUNDS.items():
                try:
                    val = int(request.form.get(f"strike_{key}", ""))
                except ValueError:
                    return _back(error=f"bad_strike_{key}")
                if not lo <= val <= hi:
                    return _back(error=f"bad_strike_{key}")
                strikes[key] = val
            cfg["strikes"] = strikes

            dashboard_db.save_automod_config(guild_id, cfg)
            return _back(saved=1)

        cfg = dashboard_db.get_automod_config(guild_id)
        strikes = dashboard_db.get_automod_strikes(guild_id, cfg["strikes"]["window_minutes"])
        for row in strikes[:40]:          # names for the first rows only (one REST call each, cached)
            m = get_guild_member(int(guild_id), row["user_id"])
            row["name"] = member_display_name(m, "") if m else ""
        return render_template(
            "automod.html",
            guild_id=guild_id,
            cfg=cfg,
            rule_info=AUTOMOD_RULE_INFO,
            limit_bounds=AUTOMOD_LIMIT_BOUNDS,
            strike_bounds=AUTOMOD_STRIKE_BOUNDS,
            strikes=strikes,
            channels=get_text_channels(int(guild_id)),
            all_channels=[c for c in get_guild_channels(int(guild_id)) if c.get("type") in (0, 2, 4, 5, 13, 15)],
            roles=sorted(get_guild_roles(int(guild_id)), key=lambda r: -r.get("position", 0)),
            saved=request.args.get("saved"),
            cleared=request.args.get("cleared"),
            error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Role panels (button / dropdown self-assign roles)
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/role-panels", methods=["GET", "POST"])
    def role_panels_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        def _back(**kw):
            return redirect(url_for("dashboard.role_panels_page", guild_id=guild_id, **kw))

        roles = sorted((r for r in get_guild_roles(int(guild_id)) if not r.get("managed")),
                       key=lambda r: -r.get("position", 0))
        role_ids = {str(r["id"]) for r in roles}

        if request.method == "POST":
            action = request.form.get("action") or "save"
            panel_id = (request.form.get("panel_id") or "").strip()

            if action == "delete":
                panel = dashboard_db.get_role_panel(guild_id, panel_id) if panel_id else None
                if panel:
                    if panel.get("message_id") and panel.get("posted_channel_id"):
                        dashboard_db.enqueue_task("delete_role_panel", guild_id, {
                            "channel_id": panel["posted_channel_id"],
                            "message_id": panel["message_id"],
                        })
                    dashboard_db.delete_role_panel(guild_id, panel_id)
                return _back(deleted=1)

            if panel_id and not dashboard_db.get_role_panel(guild_id, panel_id):
                return _back(error="missing")

            style = request.form.get("style")
            mode = request.form.get("mode")
            entries, used = [], set()
            for rid, label, emoji, desc, bstyle in zip(
                request.form.getlist("entry_role"), request.form.getlist("entry_label"),
                request.form.getlist("entry_emoji"), request.form.getlist("entry_desc"),
                request.form.getlist("entry_style"),
            ):
                rid = (rid or "").strip()
                if not rid or rid in used or rid not in role_ids:
                    continue
                used.add(rid)
                emoji = (emoji or "").strip()
                if emoji and not _looks_like_emoji(emoji):
                    return _back(error="bad_emoji")
                entries.append({
                    "role_id": rid,
                    "label": (label or "").strip()[:80],
                    "emoji": emoji,
                    "description": (desc or "").strip()[:100],
                    "button_style": bstyle if bstyle in ("primary", "secondary", "success", "danger") else "secondary",
                })
            if len(entries) > 25:
                return _back(error="too_many")

            color_hex = request.form.get("color_hex") or "#5865f2"
            data = {
                "name": (request.form.get("name") or "").strip()[:60] or "Role panel",
                "style": style if style in ("buttons", "dropdown") else "buttons",
                "mode": mode if mode in ("multiple", "unique") else "multiple",
                "channel_id": request.form.get("channel_id") or None,
                "title": (request.form.get("title") or "").strip()[:256],
                "description": (request.form.get("description") or "").strip()[:2000],
                "placeholder": (request.form.get("placeholder") or "").strip()[:100],
                "show_list": request.form.get("show_list") == "on",
                "color": _hex_to_int(color_hex),
                "color_hex": color_hex,
                "entries": entries,
            }
            saved_id = dashboard_db.save_role_panel(guild_id, panel_id, data)

            if action == "post":
                if not entries:
                    return _back(error="no_roles", open=saved_id)
                if not data["channel_id"]:
                    return _back(error="no_channel", open=saved_id)
                dashboard_db.enqueue_task("post_role_panel", guild_id, {"panel_id": saved_id})
                return _back(posting=1, open=saved_id)
            return _back(saved=1, open=saved_id)

        panels = dashboard_db.get_role_panels(guild_id)
        return render_template(
            "role_panels.html",
            guild_id=guild_id,
            panels=panels,
            roles=roles,
            channels=get_text_channels(int(guild_id)),
            blank={"_id": "", "name": "", "style": "buttons", "mode": "multiple", "channel_id": None,
                   "title": "", "description": "", "placeholder": "", "show_list": True,
                   "color_hex": "#5865f2", "entries": []},
            saved=request.args.get("saved"),
            posting=request.args.get("posting"),
            deleted=request.args.get("deleted"),
            error=request.args.get("error"),
            open_id=request.args.get("open", ""),
        )

    # -----------------------------------------------------------------
    # Security (anti-nuke)
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/security", methods=["GET", "POST"])
    def security_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            form_type = request.form.get("form_type") or "save"

            if form_type == "prune":
                dashboard_db.prune_nuke_events(guild_id, older_than_days=30)
                return redirect(url_for("dashboard.security_page", guild_id=guild_id, saved=1))

            if form_type == "post_honeypot_panel":
                dashboard_db.enqueue_task("post_honeypot_panel", guild_id)
                return redirect(url_for(
                    "dashboard.security_page", guild_id=guild_id, saved=1
                ))

            enabled = request.form.get("enabled") == "on"
            dry_run = request.form.get("dry_run") == "on"
            auto_revert = request.form.get("auto_revert") == "on"
            punishment = request.form.get("punishment") or "alert_only"
            if punishment not in ("alert_only", "revoke_roles", "ban", "revoke_and_ban"):
                punishment = "alert_only"

            security_alert_channel_id = request.form.get("security_alert_channel_id") or None

            if enabled and not security_alert_channel_id:
                return redirect(url_for(
                    "dashboard.security_page",
                    guild_id=guild_id,
                    error="alert_channel_required",
                ))

            alert_role_ids = [str(r) for r in request.form.getlist("alert_role_ids")]
            whitelist_role_ids = [str(r) for r in request.form.getlist("whitelist_role_ids")]
            whitelist_user_ids = _parse_id_list(request.form.get("whitelist_user_ids", ""))
            whitelist_bot_ids = _parse_id_list(request.form.get("whitelist_bot_ids", ""))

            actions = {}
            for key in dashboard_db.ANTI_NUKE_ACTION_KEYS:
                a_enabled = request.form.get(f"act_{key}_enabled") == "on"
                try:
                    a_count = int(request.form.get(f"act_{key}_count", "5"))
                except (TypeError, ValueError):
                    a_count = 5
                try:
                    a_window = int(request.form.get(f"act_{key}_window", "10"))
                except (TypeError, ValueError):
                    a_window = 10
                a_count = max(a_count, 1)
                a_window = max(a_window, 1)
                actions[key] = {
                    "enabled": a_enabled,
                    "count": a_count,
                    "window_seconds": a_window,
                }

            dashboard_db.set_anti_nuke_config(guild_id, {
                "enabled": enabled,
                "dry_run": dry_run,
                "auto_revert": auto_revert,
                "punishment": punishment,
                "security_alert_channel_id": (
                    str(security_alert_channel_id) if security_alert_channel_id else None
                ),
                "alert_role_ids": alert_role_ids,
                "whitelist_role_ids": whitelist_role_ids,
                "whitelist_user_ids": whitelist_user_ids,
                "whitelist_bot_ids": whitelist_bot_ids,
                "actions": actions,
            })

            # --- Honeypot config ---
            honeypot_enabled = request.form.get("honeypot_enabled") == "on"
            honeypot_channel = request.form.get("honeypot_channel_id") or None
            honeypot_bypass = request.form.get("honeypot_bypass_role_id") or None
            honeypot_dm = request.form.get("honeypot_dm_message", "").strip()

            dashboard_db.update_guild_settings(guild_id, {
                "honeypot.enabled": honeypot_enabled,
                "honeypot.channel_id": (
                    str(honeypot_channel) if honeypot_channel else None
                ),
                "honeypot.bypass_role_id": (
                    str(honeypot_bypass) if honeypot_bypass else None
                ),
                "honeypot.dm_message": honeypot_dm or (
                    "You were softbanned from {guild} for posting in a trap channel. "
                    "This is an automated action. If you believe this was a mistake, "
                    "contact the server staff."
                ),
            })
            return redirect(url_for("dashboard.security_page", guild_id=guild_id, saved=1))

        cfg = dashboard_db.get_anti_nuke_config(guild_id)
        events = dashboard_db.get_nuke_events(guild_id, limit=50)
        honeypot_cfg = dashboard_db.get_honeypot_config(guild_id)
        honeypot_kicks = dashboard_db.get_honeypot_kicks(guild_id, limit=100)

        return render_template(
            "security.html",
            guild_id=guild_id,
            cfg=cfg,
            events=events,
            action_keys=dashboard_db.ANTI_NUKE_ACTION_KEYS,
            action_labels=dashboard_db.ANTI_NUKE_ACTION_LABELS,
            honeypot=honeypot_cfg,
            honeypot_kicks=honeypot_kicks,
            channels=get_text_channels(int(guild_id)),
            roles=get_guild_roles(int(guild_id)),
            saved=request.args.get("saved"),
            error=request.args.get("error"),
        )

    # -----------------------------------------------------------------
    # Blacklist requests
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/blacklist")
    def blacklist_requests_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        status = request.args.get("status") or None
        type_ = request.args.get("type") or None
        action = request.args.get("action") or None
        reqs = dashboard_db.get_blacklist_requests(
            guild_id, status=status, type_=type_, action=action
        )

        return render_template(
            "blacklist_requests.html",
            guild_id=guild_id,
            requests=reqs,
            selected_status=status,
            selected_type=type_,
            selected_action=action,
            counters=dashboard_db.get_blacklist_config(guild_id)["counters"],
        )

    @bp.route("/guild/<guild_id>/blacklist/request/<request_id>")
    def blacklist_request_detail_page(guild_id, request_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        req = dashboard_db.get_blacklist_request(request_id)
        if not req or str(req.get("guild_id")) != str(guild_id):
            abort(404)

        return render_template(
            "blacklist_request_detail.html",
            guild_id=guild_id,
            request=req,
        )

    # -----------------------------------------------------------------
    # Command permissions
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/permissions", methods=["GET", "POST"])
    def permissions_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            enabled = request.form.get("enabled") == "on"
            allowed_roles = request.form.getlist("allowed_roles")
            open_group = request.form.get("group") or ""

            if request.form.get("scope") == "group":
                # Members / Staff / Admin header form: apply to every command in the group.
                if open_group not in GROUP_ORDER:
                    abort(400)
                # Commands marked "not affected by group changes" are left alone.
                names = [
                    c["command"] for c in dashboard_db.get_all_command_permissions(guild_id)
                    if group_for(c["command"]) == open_group and not c.get("group_locked")
                ]
                dashboard_db.set_command_permissions_bulk(guild_id, names, enabled, allowed_roles)
            else:
                command_name = request.form.get("command")
                dashboard_db.set_command_permission(
                    guild_id, command_name, enabled, allowed_roles,
                    group_locked=request.form.get("group_locked") == "on")
            return redirect(url_for(
                "dashboard.permissions_page", guild_id=guild_id, saved=1, open=open_group
            ))

        commands_list = dashboard_db.get_all_command_permissions(guild_id)
        roles = get_guild_roles(int(guild_id))
        settings_doc = dashboard_db.get_guild_settings(guild_id)

        groups = []
        for key in GROUP_ORDER:
            emoji, label, blurb = GROUP_INFO[key]
            cmds = sorted(
                (c for c in commands_list if group_for(c["command"]) == key),
                key=lambda c: c["command"],
            )
            # Group badge / shared values only look at commands the group form can change.
            unlocked = [c for c in cmds if not c.get("group_locked")]
            states = {
                (bool(c.get("enabled")), tuple(sorted(str(r) for r in c.get("allowed_roles", []))))
                for c in unlocked
            }
            uniform = len(states) == 1
            shared_enabled, shared_roles = next(iter(states)) if uniform else (False, ())
            groups.append({
                "key": key, "emoji": emoji, "label": label, "blurb": blurb,
                "commands": cmds,
                "unlocked_count": len(unlocked),
                "locked_count": len(cmds) - len(unlocked),
                "enabled_count": sum(1 for c in cmds if c.get("enabled")),
                "mixed": len(states) > 1,
                "shared_enabled": shared_enabled,
                "shared_roles": list(shared_roles),
            })

        return render_template(
            "permissions.html",
            guild_id=guild_id,
            groups=groups,
            roles=roles,
            prefix=settings_doc.get("prefix", "/"),
            saved=request.args.get("saved"),
            open_group=request.args.get("open", ""),
        )

    @bp.route("/guild/<guild_id>/prefix", methods=["POST"])
    def set_prefix(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)
        prefix = request.form.get("prefix", "/").strip()[:5] or "/"
        dashboard_db.update_guild_settings(guild_id, {"prefix": prefix})
        return redirect(url_for("dashboard.permissions_page", guild_id=guild_id, saved=1))

    # -----------------------------------------------------------------
    # Config & Logs
    # -----------------------------------------------------------------
    @bp.route("/guild/<guild_id>/config", methods=["GET", "POST"])
    def config_logs_page(guild_id):
        if not _is_authed(discord_session):
            return redirect(url_for("dashboard.login"))
        _require_guild_access(discord_session, guild_id)

        if request.method == "POST":
            log_channel_id = request.form.get("log_channel_id")
            if not log_channel_id:
                return redirect(url_for(
                    "dashboard.config_logs_page", guild_id=guild_id, error="channel_required"
                ))

            events = {}
            for key, _, _ in LOG_EVENTS:
                events[key] = request.form.get(f"evt_{key}") == "on"

            giveaway_bl = request.form.get("giveaway_blacklist_role_id") or None
            ticket_bl = request.form.get("ticket_blacklist_role_id") or None
            request_channel = request.form.get("blacklist_request_channel_id") or None
            staff_role = request.form.get("blacklist_staff_role_id") or None
            application_bl = request.form.get("application_blacklist_role_id") or None

            dashboard_db.update_guild_settings(guild_id, {
                "logging": {
                    "log_channel_id": str(log_channel_id),
                    "transcript_channel_id": (
                        str(request.form.get("transcript_channel_id"))
                        if request.form.get("transcript_channel_id") else None
                    ),
                    "events": events,
                },
                "blacklist.giveaway_role_id": (
                    str(giveaway_bl) if giveaway_bl else None
                ),
                "blacklist.ticket_role_id": (
                    str(ticket_bl) if ticket_bl else None
                ),
                "blacklist.application_role_id": (
                    str(application_bl) if application_bl else None
                ),
                "blacklist.request_channel_id": (
                    str(request_channel) if request_channel else None
                ),
                "blacklist.staff_role_id": (
                    str(staff_role) if staff_role else None
                ),
            })
            return redirect(url_for("dashboard.config_logs_page", guild_id=guild_id, saved=1))

        settings_doc = dashboard_db.get_guild_settings(guild_id)
        cfg = settings_doc.get("logging") or {}
        events = {k: True for k, _, _ in LOG_EVENTS}
        events.update(cfg.get("events") or {})
        bl_cfg = dashboard_db.get_blacklist_config(guild_id)

        return render_template(
            "config_logs.html",
            guild_id=guild_id,
            cfg={
                "log_channel_id": cfg.get("log_channel_id"),
                "transcript_channel_id": cfg.get("transcript_channel_id"),
                "events": events,
            },
            blacklist=bl_cfg,
            channels=get_text_channels(int(guild_id)),
            roles=get_guild_roles(int(guild_id)),
            events_list=LOG_EVENTS,
            audit=dashboard_db.get_audit(guild_id),
            saved=request.args.get("saved"),
            error=request.args.get("error"),
        )

    def _id_names(guild_id: str) -> dict:
        """{id: '#channel' / '@role'} so the audit log shows names, not numbers."""
        names = {}
        try:
            for c in get_guild_channels(int(guild_id)):
                names[str(c["id"])] = "#" + c["name"]
            for r in get_guild_roles(int(guild_id)):
                names[str(r["id"])] = "@" + r["name"]
        except Exception:
            pass
        return names

    from dashboard import audit as _audit
    _audit.register(bp, dashboard_db, _audit_actor, discord_session, _id_names)

    app.register_blueprint(bp)