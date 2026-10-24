"""
Anti-nuke + honey trap system.

Anti-nuke: detects destructive bursts from a single non-admin actor and
optionally punishes. See the class docstring for details.

Honey trap: a configured channel that softbans anyone who posts in it (except
Admins, guild owner, and members with the configured bypass role). The bot posts
a panel with a disabled button showing the running kick count, and edits that
button after every new kick so the number stays accurate.

Zero commands — everything is configured on the dashboard.
"""
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import commands, tasks

from database import models
from utils.embeds import base_embed
from config import constants


ALERT_COOLDOWN_SECONDS = 60


def _audit(name: str):
    return getattr(discord.AuditLogAction, name, None)


_ACTION_SOURCES = {
    "ban":                 ["ban"],
    "kick":                ["kick"],
    "channel_delete":      ["channel_delete"],
    "channel_create":      ["channel_create"],
    "role_delete":         ["role_delete"],
    "role_create":         ["role_create"],
    "webhook_create":      ["webhook_create"],
    "emoji_delete":        ["emoji_delete"],
    "member_prune":        ["member_prune"],
    "guild_update":        ["guild_update"],
    "automod_rule_delete": ["automod_rule_delete"],
    "bot_add":             ["bot_add"],
    "bot_remove":          ["bot_remove", "integration_delete"],
}

ACTION_TO_AUDIT = {}
for _key, _names in _ACTION_SOURCES.items():
    resolved = [a for a in (_audit(n) for n in _names) if a is not None]
    if resolved:
        ACTION_TO_AUDIT[_key] = resolved

AUDIT_TO_ACTION = {}
for _key, _actions in ACTION_TO_AUDIT.items():
    for _a in _actions:
        AUDIT_TO_ACTION.setdefault(_a, _key)

REVERT_ACTION_MAP = {
    "bot_add":        "bot_add",
    "channel_create": "channel_create",
    "role_create":    "role_create",
    "webhook_create": "webhook_create",
}

print("[anti_nuke] tracked actions: " + ", ".join(sorted(ACTION_TO_AUDIT.keys())))


def _normalize_actions(actions: dict) -> dict:
    out = {}
    for k, v in actions.items():
        out[k] = {
            "enabled": bool(v.get("enabled", False)),
            "count": max(int(v.get("count") or 1), 1),
            "window_seconds": max(int(v.get("window_seconds") or 1), 1),
        }
    return out


def _is_admin(member) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return member.guild_permissions.administrator


# ---------------------------------------------------------------------------
# Honey trap panel helpers
# ---------------------------------------------------------------------------

def _honeypot_panel_embed(kick_count: int, guild: discord.Guild = None) -> discord.Embed:
    embed = base_embed(
        title="DO NOT SEND MESSAGES IN THIS CHANNEL",
        description=(
            "This channel is used to catch spam bots.\n"
            "Any message sent here will result in a **softban**."
        ),
        color=constants.COLOR_WARNING,
    )
    # The server's own icon as the thumbnail (none if the server has no icon).
    if guild is not None and guild.icon:
        embed.set_thumbnail(url=guild.icon.replace(size=128).url)
    return embed


def _honeypot_panel_view(kick_count: int) -> discord.ui.View:
    """A view with one disabled button showing the running kick count. The
    button cannot be clicked — it's just a visual counter."""
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(
        label=f"Kicks: {kick_count}",
        emoji="🍯",
        style=discord.ButtonStyle.secondary,
        custom_id="honeypot_panel_counter",
        disabled=True,
    ))
    return view


class AntiNuke(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._punished_until = defaultdict(dict)
        self._alert_last_sent = defaultdict(dict)
        # Honeypot per-user cooldown so we don't re-process on spam.
        self._honeypot_cooldown = defaultdict(dict)  # [guild_id][user_id] = ts
        self._prune_events.start()
        self._poll_dashboard_tasks.start()

    def cog_unload(self):
        self._prune_events.cancel()
        self._poll_dashboard_tasks.cancel()

    async def cog_load(self):
        print(f"[anti_nuke] discord.py version: {discord.__version__}")
        for guild in self.bot.guilds:
            me = guild.me
            if me is None:
                continue
            has = me.guild_permissions.view_audit_log
            if has:
                print(f"[anti_nuke] startup check OK: {guild.name} ({guild.id}) can read audit log")
            else:
                print(
                    f"[anti_nuke] WARNING: missing View Audit Log in "
                    f"{guild.name} ({guild.id}) — anti-nuke cannot detect anything there"
                )

    # ------------------------------------------------------------------
    # Audit-log listener (anti-nuke)
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_audit_log_entry_create(self, entry: discord.AuditLogEntry):
        print(
            f"[anti_nuke] listener fired: action={entry.action} "
            f"user={getattr(entry.user, 'id', None)}"
        )
        try:
            await self._handle_entry(entry)
        except Exception as e:
            print(f"[anti_nuke] handler error: {type(e).__name__}: {e}")

    async def _handle_entry(self, entry: discord.AuditLogEntry):
        guild = entry.guild
        if guild is None:
            return

        action_key = AUDIT_TO_ACTION.get(entry.action)
        if action_key is None:
            print(f"[anti_nuke] skip: action {entry.action} is not tracked")
            return

        print(f"[anti_nuke] mapped audit {entry.action} -> internal '{action_key}'")

        actor = entry.user
        if actor is None:
            print("[anti_nuke] skip: entry has no actor")
            return

        if actor.id == self.bot.user.id:
            print("[anti_nuke] skip: actor is me")
            return

        cfg = await models.get_anti_nuke_config(guild.id)
        if not cfg["enabled"]:
            print(f"[anti_nuke] skip: enabled=False in guild {guild.id}")
            return

        per_action = _normalize_actions(cfg["actions"])
        rule = per_action.get(action_key)
        if not rule:
            print(f"[anti_nuke] skip: no rule for '{action_key}' in config")
            return
        if not rule["enabled"]:
            print(f"[anti_nuke] skip: action '{action_key}' is disabled in config")
            return

        if actor.id == guild.owner_id:
            print("[anti_nuke] skip: actor is guild owner (exempt)")
            return
        actor_member = guild.get_member(actor.id)
        if _is_admin(actor_member):
            print(f"[anti_nuke] skip: actor {actor.id} is admin (exempt)")
            return

        whitelisted = self._is_whitelisted(guild, actor, cfg)
        if whitelisted:
            print(f"[anti_nuke] actor {actor.id} is whitelisted (still detected, no punish)")

        threshold = rule["count"]
        window = rule["window_seconds"]

        print(
            f"[anti_nuke] config in use for guild {guild.id}: "
            f"enabled={cfg['enabled']} punishment={cfg['punishment']} "
            f"dry_run={cfg['dry_run']} auto_revert={cfg.get('auto_revert')} "
            f"alert_channel={cfg['security_alert_channel_id']}"
        )
        active_rules = ", ".join(
            f"{k}={v['count']}/{v['window_seconds']}s"
            for k, v in sorted(per_action.items()) if v["enabled"]
        )
        print(f"[anti_nuke] enabled rules: {active_rules or '(none)'}")

        since = datetime.now(timezone.utc) - timedelta(seconds=window)
        count = await self._recount(guild, action_key, actor.id, since)
        print(
            f"[anti_nuke] recount: actor={actor.id} action={action_key} "
            f"count={count} threshold={threshold} window={window}s"
        )

        if count < threshold:
            print(f"[anti_nuke] skip: below threshold ({count} < {threshold})")
            return

        print(f"[anti_nuke] 🚨 THRESHOLD CROSSED: {count} {action_key} in {window}s by {actor}")

        now_ts = datetime.now(timezone.utc).timestamp()
        re_punish_at = self._punished_until[guild.id].get(actor.id, 0)
        already_punished = now_ts < re_punish_at

        punishment = cfg["punishment"] if not whitelisted else "alert_only"
        dry_run = bool(cfg.get("dry_run", False))
        auto_revert = bool(cfg.get("auto_revert", True))

        punishment_applied = False
        punishment_error = None
        revert_summary = None

        if dry_run:
            print("[anti_nuke] dry-run is on, skipping punishment and revert")
        elif punishment == "alert_only":
            print("[anti_nuke] punishment is alert_only, skipping punishment and revert")
        elif already_punished:
            print("[anti_nuke] already punished recently, skipping re-punish")
        else:
            if auto_revert and action_key in REVERT_ACTION_MAP:
                try:
                    revert_summary = await self._revert(
                        guild, action_key, actor, since, window
                    )
                    print(f"[anti_nuke] revert summary: {revert_summary}")
                except Exception as e:
                    print(f"[anti_nuke] revert error: {type(e).__name__}: {e}")
                    revert_summary = {"attempted": 0, "ok": 0, "failed": 0,
                                      "errors": [f"{type(e).__name__}: {e}"]}
            elif auto_revert:
                print(f"[anti_nuke] no revert defined for action '{action_key}'")

            applied, err = await self._apply_punishment(
                guild, actor, punishment, action_key, count, window
            )
            punishment_applied = applied
            punishment_error = err
            if applied:
                self._punished_until[guild.id][actor.id] = now_ts + window
                print(f"[anti_nuke] punishment applied: {punishment}")
            else:
                print(f"[anti_nuke] punishment failed: {err}")

        try:
            await models.log_nuke_event(
                guild_id=guild.id,
                actor_id=actor.id,
                actor_name=str(actor),
                is_bot=actor.bot,
                action=action_key,
                count=count,
                window_seconds=window,
                threshold=threshold,
                punishment=punishment,
                punishment_applied=punishment_applied,
                punishment_error=punishment_error,
            )
            print("[anti_nuke] nuke_events row written")
        except Exception as e:
            print(f"[anti_nuke] log_nuke_event failed: {type(e).__name__}: {e}")

        last_alert = self._alert_last_sent[guild.id].get(actor.id, 0)
        if now_ts - last_alert < ALERT_COOLDOWN_SECONDS:
            print("[anti_nuke] alert suppressed (rate limit)")
            return
        self._alert_last_sent[guild.id][actor.id] = now_ts

        await self._send_alert(
            guild=guild, cfg=cfg, actor=actor,
            action_key=action_key, count=count, window=window, threshold=threshold,
            punishment=punishment, dry_run=dry_run, auto_revert=auto_revert,
            punishment_applied=punishment_applied, punishment_error=punishment_error,
            revert_summary=revert_summary, whitelisted=whitelisted,
        )

    async def _recount(self, guild: discord.Guild, action_key: str,
                       actor_id: int, since: datetime) -> int:
        total = 0
        for audit_action in ACTION_TO_AUDIT[action_key]:
            try:
                async for log in guild.audit_logs(limit=100, action=audit_action, after=since):
                    if log.user is not None and log.user.id == actor_id:
                        total += 1
                        if total >= 1000:
                            break
            except discord.Forbidden as e:
                print(f"[anti_nuke] audit_logs Forbidden for action={audit_action}: {e}")
                return 0
            except discord.HTTPException as e:
                print(f"[anti_nuke] audit_logs HTTPException for action={audit_action}: {e}")
                return 0
        return total

    async def _revert(self, guild: discord.Guild, action_key: str, actor,
                      since: datetime, window: int):
        summary = {"attempted": 0, "ok": 0, "failed": 0, "errors": []}
        audit_name = REVERT_ACTION_MAP[action_key]
        audit_action = _audit(audit_name)
        if audit_action is None:
            summary["errors"].append(f"audit action {audit_name} not available")
            return summary

        entries = []
        try:
            async for log in guild.audit_logs(limit=100, action=audit_action, after=since):
                if log.user is not None and log.user.id == actor.id:
                    entries.append(log)
        except discord.Forbidden as e:
            summary["errors"].append(f"Forbidden reading audit log: {e}")
            return summary
        except discord.HTTPException as e:
            summary["errors"].append(f"HTTPException reading audit log: {e}")
            return summary

        reason = f"Anti-nuke revert of {action_key} by {actor}"

        if action_key == "bot_add":
            for log in entries:
                target = log.target
                target_id = getattr(target, "id", None)
                if target_id is None:
                    summary["failed"] += 1
                    summary["errors"].append("bot_add entry missing target id")
                    continue
                if target_id == self.bot.user.id:
                    continue
                member = guild.get_member(target_id)
                if member is None:
                    continue
                if guild.owner_id == member.id:
                    continue
                if _is_admin(member):
                    continue
                summary["attempted"] += 1
                try:
                    await member.kick(reason=reason)
                    summary["ok"] += 1
                except (discord.Forbidden, discord.HTTPException) as e:
                    summary["failed"] += 1
                    summary["errors"].append(f"kick {member.id}: {e}")

        elif action_key == "channel_create":
            for log in entries:
                target = log.target
                target_id = getattr(target, "id", None)
                if target_id is None:
                    summary["failed"] += 1
                    continue
                channel = guild.get_channel(target_id)
                if channel is None:
                    continue
                summary["attempted"] += 1
                try:
                    await channel.delete(reason=reason)
                    summary["ok"] += 1
                except (discord.Forbidden, discord.HTTPException) as e:
                    summary["failed"] += 1
                    summary["errors"].append(f"channel {target_id}: {e}")

        elif action_key == "role_create":
            for log in entries:
                target = log.target
                target_id = getattr(target, "id", None)
                if target_id is None:
                    summary["failed"] += 1
                    continue
                role = guild.get_role(target_id)
                if role is None or role.is_default():
                    continue
                summary["attempted"] += 1
                try:
                    await role.delete(reason=reason)
                    summary["ok"] += 1
                except (discord.Forbidden, discord.HTTPException) as e:
                    summary["failed"] += 1
                    summary["errors"].append(f"role {target_id}: {e}")

        elif action_key == "webhook_create":
            for log in entries:
                target = log.target
                target_id = getattr(target, "id", None)
                if target_id is None:
                    summary["failed"] += 1
                    continue
                summary["attempted"] += 1
                try:
                    webhook = await self.bot.fetch_webhook(target_id)
                    await webhook.delete(reason=reason)
                    summary["ok"] += 1
                except discord.NotFound:
                    summary["ok"] += 1
                except (discord.Forbidden, discord.HTTPException) as e:
                    summary["failed"] += 1
                    summary["errors"].append(f"webhook {target_id}: {e}")

        return summary

    async def _apply_punishment(self, guild: discord.Guild, actor, punishment: str,
                                action_key: str, count: int, window: int):
        reason = f"Anti-nuke: {count} {action_key} in {window}s"
        member = guild.get_member(actor.id)
        if member is None:
            return False, "Actor is no longer in the guild."
        if guild.owner_id == member.id:
            return False, "Actor is the guild owner."
        if _is_admin(member):
            return False, "Actor is an admin."

        me = guild.me
        if me is None or member.top_role >= me.top_role:
            return False, "Actor's top role is above the bot's top role."

        if punishment in ("revoke_roles", "revoke_and_ban"):
            try:
                await member.edit(roles=[], reason=reason)
            except (discord.Forbidden, discord.HTTPException) as e:
                return False, f"revoke failed: {e}"

        if punishment in ("ban", "revoke_and_ban"):
            try:
                await member.ban(reason=reason, delete_message_days=0)
            except (discord.Forbidden, discord.HTTPException) as e:
                return False, f"ban failed: {e}"

        return True, None

    def _is_whitelisted(self, guild: discord.Guild, actor, cfg: dict) -> bool:
        if str(actor.id) in cfg["whitelist_user_ids"]:
            return True
        if actor.bot and str(actor.id) in cfg["whitelist_bot_ids"]:
            return True
        member = guild.get_member(actor.id)
        if member is None:
            return False
        role_ids = {str(r.id) for r in member.roles}
        return bool(role_ids & set(cfg["whitelist_role_ids"]))

    async def _send_alert(self, guild: discord.Guild, cfg: dict, actor,
                          action_key: str, count: int, window: int, threshold: int,
                          punishment: str, dry_run: bool, auto_revert: bool,
                          punishment_applied: bool, punishment_error: str,
                          revert_summary: dict, whitelisted: bool):
        channel_id = cfg.get("security_alert_channel_id")
        if not channel_id:
            print(f"[anti_nuke] no security alert channel set for guild {guild.id}")
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            print(f"[anti_nuke] security alert channel {channel_id} not found in guild {guild.id}")
            return

        color = constants.COLOR_ERROR
        if dry_run or punishment == "alert_only":
            color = constants.COLOR_WARNING

        lines = [
            f"**Actor:** <@{actor.id}> (`{actor.id}`)",
            f"**Action:** `{action_key}`",
            f"**Detected:** {count} in {window}s (threshold: {threshold})",
            f"**Punishment setting:** `{punishment}`",
        ]
        if whitelisted:
            lines.append("**Whitelisted:** yes — punishment skipped, logged only.")
        if dry_run:
            lines.append("**Dry-run:** on — no action taken.")
        if punishment_applied:
            lines.append(f"**Applied:** ✅ {punishment}")
        elif punishment_error:
            lines.append(f"**Applied:** ❌ {punishment_error}")

        if revert_summary is not None:
            attempted = revert_summary.get("attempted", 0)
            ok = revert_summary.get("ok", 0)
            failed = revert_summary.get("failed", 0)
            errors = revert_summary.get("errors") or []
            noun = {
                "bot_add": "bot(s) kicked",
                "channel_create": "channel(s) deleted",
                "role_create": "role(s) deleted",
                "webhook_create": "webhook(s) deleted",
            }.get(action_key, "object(s) reverted")
            if attempted == 0:
                lines.append(f"**Reverted:** nothing to revert ({noun})")
            elif failed == 0:
                lines.append(f"**Reverted:** {ok}/{attempted} {noun}")
            else:
                err_text = "; ".join(errors[:2])
                if len(errors) > 2:
                    err_text += f" (+{len(errors) - 2} more)"
                lines.append(f"**Reverted:** {ok}/{attempted} {noun} ({failed} failed: {err_text})")
        elif auto_revert is False:
            lines.append("**Reverted:** auto-revert is off")

        embed = base_embed(
            title=f"⚠️ Anti-Nuke triggered — {action_key}",
            description="\n".join(lines),
            color=color,
        )
        embed.set_footer(text=f"Guild: {guild.name} · {guild.id}")

        content = None
        mentions = [
            f"<@&{rid}>"
            for rid in (cfg.get("alert_role_ids") or [])
            if guild.get_role(int(rid))
        ]
        if mentions:
            content = " ".join(mentions)

        try:
            await channel.send(content=content, embed=embed)
            print(f"[anti_nuke] alert sent to #{channel.name} ({channel.id})")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[anti_nuke] could not send alert: {type(e).__name__}: {e}")

    # ------------------------------------------------------------------
    # Honey trap
    # ------------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        try:
            cfg = await models.get_honeypot_config(message.guild.id)
        except Exception as e:
            print(f"[honeypot] config read failed: {e}")
            return

        if not cfg["enabled"] or not cfg["channel_id"]:
            return
        if str(message.channel.id) != str(cfg["channel_id"]):
            return

        guild = message.guild
        author = message.author

        # Bypass: owner, admins, bypass role.
        if author.id == guild.owner_id:
            return
        member = guild.get_member(author.id)
        if _is_admin(member):
            return
        if cfg["bypass_role_id"] and member is not None:
            if any(str(r.id) == str(cfg["bypass_role_id"]) for r in member.roles):
                return

        # Per-user cooldown so we don't re-process on spam.
        now_ts = datetime.now(timezone.utc).timestamp()
        last = self._honeypot_cooldown[guild.id].get(author.id, 0)
        if now_ts - last < 60:
            print(f"[honeypot] skip {author.id} (cooldown)")
            return
        self._honeypot_cooldown[guild.id][author.id] = now_ts

        print(f"[honeypot] triggered by {author} ({author.id}) in #{message.channel}")

        # 1. Capture message content info BEFORE deletion.
        content_preview = (message.content or "")[:200]
        content_types = []
        if message.content:
            content_types.append("text")
        if message.attachments:
            for att in message.attachments:
                ct = (att.content_type or "").lower()
                if ct.startswith("image"):
                    if "image" not in content_types:
                        content_types.append("image")
                elif ct.startswith("video"):
                    if "video" not in content_types:
                        content_types.append("video")
                elif ct.startswith("audio"):
                    if "audio" not in content_types:
                        content_types.append("audio")
                else:
                    if "attachment" not in content_types:
                        content_types.append("attachment")
        if message.stickers:
            content_types.append("sticker")
        if message.embeds:
            content_types.append("embed")
        if not content_types:
            content_types.append("empty")

        # 2. Delete this message and the previous N in the same channel by the
        #    same author (default 1 = just this one; we don't have a config field
        #    for N per your spec, so we do 1).
        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

        # 3. Softban (ban with 1 day of message deletion, then immediate unban).
        softban_ok = False
        softban_error = None
        try:
            await guild.ban(
                author,
                reason="Honey trap: posted in trap channel",
                delete_message_days=1,
            )
            await guild.unban(author, reason="Honey trap: softban")
            softban_ok = True
            print(f"[honeypot] softbanned {author.id}")
        except discord.Forbidden:
            softban_error = "Missing permissions or role hierarchy."
        except discord.HTTPException as e:
            softban_error = f"Discord error: {e}"
        except Exception as e:
            softban_error = f"{type(e).__name__}: {e}"

        # 4. Log to DB and bump counter.
        try:
            await models.log_honeypot_kick(
                guild_id=guild.id,
                user_id=author.id,
                user_name=str(author),
                channel_id=message.channel.id,
                message_id=message.id,
                content_preview=content_preview,
                content_types=content_types,
            )
        except Exception as e:
            print(f"[honeypot] log failed: {e}")

        new_count = 0
        try:
            new_count = await models.bump_honeypot_kick_count(guild.id)
        except Exception as e:
            print(f"[honeypot] counter bump failed: {e}")

        # 5. DM the user (best effort).
        try:
            dm_text = (cfg.get("dm_message") or "").replace("{guild}", guild.name)
            if dm_text:
                try:
                    await author.send(embed=base_embed(
                        title=f"🍯 Removed from {guild.name}",
                        description=dm_text[:4000],
                        color=constants.COLOR_WARNING,
                    ))
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except Exception as e:
            print(f"[honeypot] dm failed: {e}")

        # 6. Refresh the panel button label in place.
        await self._refresh_honeypot_panel(guild, cfg, new_count)

        # 7. Log alert to the security channel.
        await self._send_honeypot_alert(guild, author, content_preview,
                                        content_types, softban_ok, softban_error)

    async def _refresh_honeypot_panel(self, guild: discord.Guild, cfg: dict,
                                      kick_count: int):
        """Edit the panel in place. If the message is gone, repost once."""
        channel = guild.get_channel(int(cfg["channel_id"]))
        if channel is None:
            print(f"[honeypot] trap channel {cfg['channel_id']} not found")
            return

        embed = _honeypot_panel_embed(kick_count, guild)
        view = _honeypot_panel_view(kick_count)

        message_id = cfg.get("panel_message_id")
        if message_id:
            try:
                msg = await channel.fetch_message(int(message_id))
                await msg.edit(embed=embed, view=view)
                return
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        # Repost.
        try:
            posted = await channel.send(embed=embed, view=view)
            await models.set_honeypot_panel_message(guild.id, posted.id)
            print(f"[honeypot] panel (re)posted in #{channel.name}")
        except (discord.Forbidden, discord.HTTPException) as e:
            print(f"[honeypot] could not post panel: {e}")

    async def _send_honeypot_alert(self, guild: discord.Guild, author,
                                   content_preview: str, content_types: list,
                                   softban_ok: bool, softban_error):
        cfg = await models.get_anti_nuke_config(guild.id)
        channel_id = cfg.get("security_alert_channel_id")
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return

        embed = base_embed(
            title="🍯 Honey trap triggered",
            description=(
                f"**User:** <@{author.id}> (`{author.id}`)\n"
                f"**Content types:** {', '.join(content_types) or 'unknown'}\n"
                f"**Preview:** {content_preview[:1000] or '*(no text)*'}"
            ),
            color=constants.COLOR_ERROR,
        )
        if softban_ok:
            embed.add_field(name="Action", value="✅ Softbanned", inline=False)
        else:
            embed.add_field(name="Action", value=f"❌ Softban failed: {softban_error}", inline=False)
        embed.set_footer(text=f"Guild: {guild.name} · {guild.id}")

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    # ------------------------------------------------------------------
    # Dashboard task poller (honeypot panel posting from the dashboard)
    # ------------------------------------------------------------------
    @tasks.loop(seconds=3)
    async def _poll_dashboard_tasks(self):
        try:
            batch = await models.take_pending_dashboard_tasks(
                limit=10, types=["post_honeypot_panel"]
            )
            for t in batch:
                try:
                    await self._run_dashboard_task(t)
                except Exception as e:
                    print(f"[anti_nuke] dashboard task {t.get('_id')} failed: {e}")
        except Exception as e:
            print(f"[anti_nuke] dashboard poller error: {e}")

    @_poll_dashboard_tasks.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def _run_dashboard_task(self, t: dict):
        if t.get("type") != "post_honeypot_panel":
            return
        guild_id = int(t["guild_id"])
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        cfg = await models.get_honeypot_config(guild_id)
        if not cfg["channel_id"]:
            print(f"[honeypot] post panel requested but no trap channel in {guild_id}")
            return
        await self._refresh_honeypot_panel(guild, cfg, cfg["kick_count"])

    # ------------------------------------------------------------------
    # Daily prune
    # ------------------------------------------------------------------
    @tasks.loop(hours=24)
    async def _prune_events(self):
        try:
            for guild in self.bot.guilds:
                try:
                    await models.prune_nuke_events(guild.id, older_than_days=30)
                except Exception as e:
                    print(f"[anti_nuke] prune {guild.id} failed: {e}")
        except Exception as e:
            print(f"[anti_nuke] prune loop error: {e}")

    @_prune_events.before_loop
    async def _before_prune(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(AntiNuke(bot))
