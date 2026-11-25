"""Generic dashboard audit hook: records WHO saved WHICH page and WHAT changed.

For every POST on a /guild/<id>/... page we snapshot the guild's settings document and a few
config collections before the handler runs, and again after it answers. The difference is
turned into short readable lines (utils/audit_diff.py) and saved with the page name and the
person. Form values themselves are never stored, only the before/after of real settings, and
secret-looking keys (token, webhook, password...) are shown as "(hidden)".

Routes that already write their own specific entry (log_audit, e.g. a deleted rating) get the
before/after lines attached to that entry instead of a second one, and failed saves (error redirects, 4xx/5xx) are not recorded.
"""
import re

from flask import g, request

from utils import audit_diff as ad

# Per-guild config collections worth diffing. Each doc has guild_id (string or int).
WATCHED = ["ticket_panels", "ticket_types", "role_panels", "applications",
           "application_panels", "command_permissions", "staff_activity_configs",
           "scheduled_announcements"]
MAX_DOCS = 500
ACTION_FIELDS = ("action", "section", "form_type")


def _snapshot(db, guild_id: str) -> dict:
    snap = {"settings": db.guild_settings.find_one({"_id": guild_id}) or {}, "colls": {}}
    gids = [guild_id, int(guild_id)] if str(guild_id).isdigit() else [guild_id]
    for coll in WATCHED:
        try:
            docs = db[coll].find({"guild_id": {"$in": gids}}).limit(MAX_DOCS)
            snap["colls"][coll] = {str(d["_id"]): d for d in docs}
        except Exception:
            snap["colls"][coll] = {}
    return snap


def register(bp, dashboard_db, audit_actor, discord_session, names_for):
    """Attach the hooks to the dashboard blueprint."""

    def _is_guild_post() -> bool:
        return (request.method == "POST" and bool(request.view_args)
                and str(request.view_args.get("guild_id", "")).isdigit())

    @bp.before_request
    def _audit_before():
        g.audit_before = None
        g.audit_last_id = None
        if not _is_guild_post():
            return
        try:
            g.audit_before = _snapshot(dashboard_db.get_db(), str(request.view_args["guild_id"]))
        except Exception as e:
            print(f"[dashboard] audit snapshot failed: {e}")

    @bp.after_request
    def _audit_after(resp):
        try:
            before = getattr(g, "audit_before", None)
            if before is None:
                return resp
            if resp.status_code >= 400:
                return resp
            if resp.status_code in (301, 302, 303, 307) and "error=" in (resp.headers.get("Location") or ""):
                return resp                                     # the save was refused
            guild_id = str(request.view_args["guild_id"])
            after = _snapshot(dashboard_db.get_db(), guild_id)

            raw = ad.diff_lines(before["settings"], after["settings"])
            for coll in WATCHED:
                raw += ad.diff_collection(coll, before["colls"][coll], after["colls"][coll])
            if any(re.search(r"\b\d{17,20}\b", ln) for ln in raw):     # show #channel / @role names
                names = names_for(guild_id)
                lines = ad.diff_lines(before["settings"], after["settings"], names)
                for coll in WATCHED:
                    lines += ad.diff_collection(coll, before["colls"][coll], after["colls"][coll], names)
            else:
                lines = raw

            form_action = next((request.form.get(f) for f in ACTION_FIELDS if request.form.get(f)), "")
            if not lines and not form_action:
                return resp                                     # saved without changing anything
            page = ad.page_label(request.endpoint)
            last_id = getattr(g, "audit_last_id", None)
            if last_id is not None:
                # The route already wrote its own entry (e.g. "deleted a rating"): add the
                # readable before/after to it instead of making a second entry.
                if lines:
                    dashboard_db.get_db().dashboard_audit.update_one(
                        {"_id": last_id}, {"$set": {"page": page, "lines": ad.cap(lines)}})
                return resp
            actor_id, actor_name = audit_actor(discord_session)
            summary = (f"Saved {page}" if lines else f"{page}: {form_action}")
            dashboard_db.log_audit(guild_id, actor_id, actor_name, "dashboard_save", summary,
                                   page=page, lines=ad.cap(lines))
        except Exception as e:
            print(f"[dashboard] audit hook failed: {e}")      # never break a save
        return resp
