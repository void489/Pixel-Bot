"""Shared "box or embed" posting for panels (spawner, applications, role panels, help).

Every panel can be posted in one of two looks (see utils/panel_style.py):
  embed - a normal embed, the buttons / dropdown in a row under it ("outside")
  box   - one container with the text, picture and buttons / dropdown inside it ("inside",
          Discord Components V2, needs discord.py 2.6+)

What this file gives the cogs:
  BoxView        a box built from text + optional picture + rows of buttons / one dropdown
  box_rows()     split buttons into rows of 5 (a dropdown always gets a row of its own)
  picture()      (url, discord.File) for a panel: an uploaded picture wins over the link
  put_panel()    post a panel, or edit its message in place, in its saved look. If the look
                 changed (embed <-> box) the old message is replaced, because Discord can't
                 turn one kind of message into the other. If the box can't be built or
                 Discord refuses it, the panel is still posted as an embed.

Uploaded pictures live in the `ticket_panel_images` collection (same as ticket panels), under
an id that says what they belong to: "spawner:<guild>", "apps:<guild>", "rp:<panel id>".
"""
import io

import discord

from database import models
from utils import panel_style

from utils.log import get_logger
logger = get_logger(__name__)

_BOX_PARTS = ("LayoutView", "Container", "TextDisplay", "Section", "Thumbnail", "MediaGallery",
              "Separator", "ActionRow")

_LayoutBase = getattr(discord.ui, "LayoutView", discord.ui.View)

BOX_TEXT_MAX = 3900          # Discord allows 4000 characters of text in one message


def box_supported() -> bool:
    """The one-box style needs Discord's Components V2 classes (discord.py 2.6+)."""
    return all(hasattr(discord.ui, n) for n in _BOX_PARTS)


def box_rows(items: list) -> list:
    """Buttons -> rows of up to 5. A select menu takes a whole row, so it gets its own."""
    rows, current = [], []
    for item in items:
        if isinstance(item, discord.ui.Select):
            if current:
                rows.append(current)
                current = []
            rows.append([item])
            continue
        current.append(item)
        if len(current) == 5:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return rows


class BoxView(_LayoutBase):
    """One container: [picture up] text [picture right / down], then the components.

    `rows` is a list of lists of items (see box_rows). Pass `timeout=None` views only:
    the buttons / dropdown inside must keep working after a restart."""

    def __init__(self, *, text: str, colour: int = 0x5865F2, image_url: str = None,
                 position: str = "up", rows: list = (), title: str = None):
        super().__init__(timeout=None)
        ui = discord.ui
        body = (f"## {title}\n" if title else "") + (text or "")
        body = body[:BOX_TEXT_MAX] or "\u200b"

        children = []
        if image_url and position == "up":
            children.append(self._gallery(image_url))
        if image_url and position == "right":
            children.append(ui.Section(ui.TextDisplay(body), accessory=ui.Thumbnail(image_url)))
        else:
            children.append(ui.TextDisplay(body))
        if image_url and position == "down":
            children.append(self._gallery(image_url))
        if rows:
            children.append(ui.Separator())
            for row_items in rows:
                row = ui.ActionRow()
                for item in row_items:
                    row.add_item(item)
                children.append(row)
        self.add_item(ui.Container(*children, accent_colour=discord.Colour(int(colour or 0x5865F2))))

    @staticmethod
    def _gallery(url: str):
        gallery = discord.ui.MediaGallery()
        gallery.add_item(media=url)
        return gallery


async def picture(image_key: str, look: dict):
    """(url for the message, discord.File or None). An uploaded picture wins over the link;
    it is attached to the message and referenced as attachment://."""
    uploaded = await models.get_ticket_panel_image(str(image_key))
    if uploaded:
        data, ext = uploaded
        name = f"panel.{ext}"
        return f"attachment://{name}", discord.File(io.BytesIO(data), filename=name)
    return (look.get("image_url") or None), None


def embed_picture(embed: discord.Embed, image_url: str, position: str) -> discord.Embed:
    """Put the picture on an embed: right = small thumbnail, bottom/down = big picture."""
    if image_url:
        if position in ("bottom", "down"):
            embed.set_image(url=image_url)
        else:
            embed.set_thumbnail(url=image_url)
    return embed


async def _write(channel, msg, *, box: bool, kwargs: dict, file):
    """Edit `msg` in place when it is the same kind of message, otherwise replace it."""
    if msg is not None and bool(msg.flags.components_v2) == box:
        try:
            return await msg.edit(attachments=[file] if file else [], **kwargs)
        except discord.NotFound:
            pass
    elif msg is not None:
        try:
            await msg.delete()          # an embed message can't become a box (or the reverse)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    send_kwargs = dict(kwargs)
    if file:
        send_kwargs["files"] = [file]
    return await channel.send(**send_kwargs)


async def put_panel(channel, message_id, *, look: dict, image_key: str, make_box, make_embed):
    """Post a panel in its saved look, or update its existing message.

    look        panel_style.panel_look(doc)
    image_key   where an uploaded picture is stored (see the module docstring)
    make_box    make_box(image_url) -> BoxView           (called fresh every time)
    make_embed  make_embed(image_url, position) -> (embed, view)

    Returns (message, posted_style, note). `note` says what went differently from what
    was asked ("" when everything went as planned). Raises discord.Forbidden when the bot
    isn't allowed to post there."""
    note = ""
    image_url, file = await picture(image_key, look)

    msg = None
    if message_id:
        try:
            msg = await channel.fetch_message(int(message_id))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException, ValueError):
            msg = None

    if look["style"] == "box":
        if box_supported():
            try:
                posted = await _write(channel, msg, box=True,
                                      kwargs={"view": make_box(image_url)}, file=file)
                return posted, "box", ""
            except discord.Forbidden:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error(f"[panel_box] box style failed in #{getattr(channel, 'name', '?')}: "
                      f"{type(e).__name__}: {e}")
                note = "The one-box style failed, so it was posted as an embed instead."
                # the old message may be a box that is still fine to replace below
                msg = await _refetch(channel, msg)
        else:
            note = "The one-box style needs a newer discord.py, so it was posted as an embed instead."

    embed, view = make_embed(image_url, look["image_position"])
    try:
        posted = await _write(channel, msg, box=False,
                              kwargs={"embed": embed, "view": view}, file=file)
        return posted, "embed", note
    except discord.HTTPException as e:
        if isinstance(e, discord.Forbidden) or e.status != 400 or not image_url:
            raise
        # Discord didn't accept the picture (bad link): post without it
        embed, view = make_embed(None, look["image_position"])
        msg = await _refetch(channel, msg)
        posted = await _write(channel, msg, box=False,
                              kwargs={"embed": embed, "view": view}, file=None)
        return posted, "embed", (note + " Discord rejected the picture, so it was posted without it.").strip()


async def _refetch(channel, msg):
    """The same message again (it may have been deleted meanwhile), or None."""
    if msg is None:
        return None
    try:
        return await channel.fetch_message(msg.id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        return None


def is_box_message(message) -> bool:
    """True if a posted message is a Components V2 box (not an embed message)."""
    try:
        return bool(message.flags.components_v2)
    except AttributeError:
        return False


def find_select(message):
    """The first dropdown on a posted message, whether it is an embed message or a box
    (boxes keep their components inside a container). None if there is none."""
    def walk(components):
        for c in components or []:
            if getattr(c, "options", None) is not None and getattr(c, "custom_id", None):
                return c
            found = walk(getattr(c, "children", None))
            if found is not None:
                return found
        return None
    try:
        return walk(message.components)
    except Exception:  # noqa: BLE001
        return None


def panel_look(doc: dict) -> dict:
    """The saved look of any panel document (re-exported so cogs import one module)."""
    return panel_style.panel_look(doc)
