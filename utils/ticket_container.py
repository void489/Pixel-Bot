"""Helpers that work on a ticket's container: a TEXT CHANNEL or a PRIVATE THREAD.

Panels can open tickets as private threads (per-panel "thread mode", utils/ticket_settings.py).
Everything that acts on "the ticket channel" goes through these helpers, so close, claim,
add, remove, rename, priority, transcripts, ratings and auto-close share one code path.

Thread facts the code is built around:
  * A thread can't be moved to another parent, so /ticket move doesn't apply to threads.
  * Roles can't be added to a thread. Support roles instead get Manage Threads on the parent
    channel (ensure_staff_access), which lets them see every private thread in it.
  * Archived threads are not in Discord's cache, so a ticket must be looked up by ID
    (get_container) or it would look deleted. Posting in an archived thread unarchives it.
  * Auto-archive is only 60 / 1440 / 4320 / 10080 minutes. The longest is the default so the
    inactivity auto-close (hours) acts first.
  * Permission overwrites don't exist on threads, so "only the claimer can see the ticket"
    does nothing in a thread.
"""
import discord

# permission attribute -> label, for the parent channel (checked before creating a thread)
THREAD_PERMS = {
    "view_channel": "View Channel",
    "create_private_threads": "Create Private Threads",
    "send_messages_in_threads": "Send Messages in Threads",
    "manage_threads": "Manage Threads",
}


def is_thread(channel) -> bool:
    return isinstance(channel, discord.Thread)


def is_ticket_container(channel) -> bool:
    return isinstance(channel, (discord.TextChannel, discord.Thread))


async def get_container(guild: discord.Guild, channel_id):
    """The ticket's text channel or thread, or None if it's gone. Looks in the cache first,
    then asks Discord, which finds archived threads too."""
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return None
    found = guild.get_channel_or_thread(cid)
    if found is not None:
        return found
    try:
        found = await guild.fetch_channel(cid)
    except discord.HTTPException:        # NotFound, Forbidden, or Discord having a bad moment
        return None
    return found if is_ticket_container(found) else None


async def resolve(guild: discord.Guild, channel):
    """`channel` from an interaction / ctx can be a bare PartialMessageable when it's an
    archived thread; swap it for the real thing so edit / delete / name work."""
    if is_ticket_container(channel) or guild is None:
        return channel
    return await get_container(guild, getattr(channel, "id", None)) or channel


async def ensure_unarchived(channel) -> None:
    """Posting into an archived thread needs it open first. Never raises."""
    if is_thread(channel) and channel.archived:
        try:
            await channel.edit(archived=False)
        except discord.HTTPException as e:
            print(f"[tickets] couldn't unarchive thread {channel.id}: {e}")


def missing_thread_perms(parent, me) -> list:
    """Labels of the permissions the bot lacks in the parent channel to run thread tickets."""
    perms = parent.permissions_for(me)
    return [label for attr, label in THREAD_PERMS.items() if not getattr(perms, attr, False)]


async def create_ticket_thread(parent: discord.TextChannel, name: str, auto_archive: int,
                               invitable: bool, reason: str) -> discord.Thread:
    """A private thread in `parent`. Private threads can't be started from a message."""
    return await parent.create_thread(
        name=name[:100], type=discord.ChannelType.private_thread,
        auto_archive_duration=auto_archive, invitable=invitable, reason=reason)


async def ensure_staff_access(parent: discord.TextChannel, roles) -> list:
    """Give each support role Manage Threads (+ view / send in threads) on the parent
    channel, once. That's how staff see private threads they weren't added to. Returns the
    roles that couldn't be updated (bot lacks Manage Permissions, or can't grant that)."""
    failed = []
    for role in roles:
        try:
            ow = parent.overwrites_for(role)
            if ow.view_channel is True and ow.manage_threads is True and ow.send_messages_in_threads is not False:
                continue
            ow.update(view_channel=True, manage_threads=True, send_messages_in_threads=True)
            await parent.set_permissions(role, overwrite=ow, reason="Support role for thread tickets")
        except discord.HTTPException:
            failed.append(role)
    return failed


async def add_member(channel, member: discord.Member, reason: str = "Added to ticket") -> None:
    """Let `member` see the ticket: a thread adds them as a member, a channel gives them an
    overwrite. May raise discord.HTTPException (the caller reports it)."""
    if is_thread(channel):
        await ensure_unarchived(channel)
        await channel.add_user(member)
    else:
        await channel.set_permissions(
            member, view_channel=True, send_messages=True, read_message_history=True, reason=reason)


async def remove_member(channel, member: discord.Member, reason: str = "Removed from ticket") -> None:
    if is_thread(channel):
        await ensure_unarchived(channel)
        await channel.remove_user(member)
    else:
        await channel.set_permissions(member, overwrite=None, reason=reason)


async def delete_container(channel, reason: str) -> bool:
    """Delete the ticket channel / thread. If a thread can't be deleted (no Manage Threads),
    it is archived and locked instead. False when nothing could be done."""
    try:
        await channel.delete(reason=reason)
        return True
    except discord.HTTPException as e:
        if not is_thread(channel):
            return False
        print(f"[tickets] couldn't delete thread {channel.id} ({e}); archiving it instead")
    try:
        await channel.edit(archived=True, locked=True, reason=reason)
        return True
    except discord.HTTPException:
        return False
