"""Ticket panel look: which style a panel is posted in and where its optional image goes.

Two styles (Discord decides what each can do):
  embed - a normal embed; the buttons / dropdown sit in a row under it ("outside").
          Image: right (small picture) or bottom (big picture).
  box   - one container box with the text, image and buttons / dropdown all inside it
          (Discord "Components V2"). Image: up, down or right (small picture).
Left is not possible in either style.

No discord import here: the dashboard (sync) and the bot (async) both use it.
"""
import io
from urllib.parse import urlparse

PANEL_STYLES = [("embed", "Outside: embed, buttons under it"), ("box", "Inside: one box with everything")]
IMAGE_POSITIONS = {
    "embed": [("right", "Right (small picture)"), ("bottom", "Bottom (big picture)")],
    "box": [("up", "Up (top)"), ("down", "Down (below the text)"), ("right", "Right (small picture)")],
}
DEFAULT_POSITION = {"embed": "right", "box": "up"}
IMAGE_MAX_UPLOAD = 4 * 1024 * 1024
IMAGE_MAX_PIXELS = 40_000_000
IMAGE_MAX_SIDE = 1600
URL_MAX = 500


def clean_style(value) -> str:
    return value if value in dict(PANEL_STYLES) else "embed"


def clean_position(style: str, value) -> str:
    style = clean_style(style)
    return value if value in dict(IMAGE_POSITIONS[style]) else DEFAULT_POSITION[style]


def clean_image_url(value) -> str:
    """An https picture link, or "" if empty. Raises ValueError if it can't be used."""
    v = str(value or "").strip()
    if not v:
        return ""
    u = urlparse(v)
    if u.scheme != "https" or not u.hostname or len(v) > URL_MAX or any(c.isspace() for c in v):
        raise ValueError("url")
    return v


def panel_look(panel: dict) -> dict:
    """The saved look of a panel with safe defaults (old panels: embed, no image)."""
    panel = panel or {}
    style = clean_style(panel.get("panel_style"))
    try:
        url = clean_image_url(panel.get("image_url"))
    except ValueError:
        url = ""
    return {"style": style, "image_url": url, "image_position": clean_position(style, panel.get("image_position"))}


def process_image(raw: bytes):
    """Check an uploaded picture and shrink it. Returns (bytes, extension, mime).
    Raises ValueError when it is not a usable PNG, JPG, WEBP or GIF."""
    from PIL import Image, ImageOps
    try:
        img = Image.open(io.BytesIO(raw))
        if img.format not in ("PNG", "JPEG", "WEBP", "GIF"):
            raise ValueError("format")
        if img.width * img.height > IMAGE_MAX_PIXELS:
            raise ValueError("pixels")
        img.seek(0)
        img = ImageOps.exif_transpose(img)
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        out = io.BytesIO()
        if img.mode in ("RGBA", "LA", "P"):
            img.convert("RGBA").save(out, "PNG", optimize=True)
            return out.getvalue(), "png", "image/png"
        img.convert("RGB").save(out, "JPEG", quality=88, optimize=True)
        return out.getvalue(), "jpg", "image/jpeg"
    except ValueError:
        raise
    except Exception as e:
        raise ValueError("bad image") from e
