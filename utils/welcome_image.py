"""Welcome image: wide banner with the joiner's round avatar, "WELCOME" and their name.

Pure Pillow, no Discord imports, so the bot (real avatar) and the dashboard preview
(sample avatar) use the same code. Blocking: call it with asyncio.to_thread on the bot.

Nothing here may break a join: a bad / missing background falls back to the shipped
default, and if even that is missing a flat colour is used. render_welcome_image() only
raises if Pillow itself is broken - the caller wraps it anyway.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont, ImageOps

from utils.welcome import BG_MAX_PIXELS

W, H = 1000, 400
AVATAR = 176
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")
DEFAULT_BG_PATH = os.path.join(ASSETS, "welcome", "default_bg.png")
FONT_BOLD = os.path.join(ASSETS, "fonts", "DejaVuSans-Bold.ttf")

FLAT_BG = (12, 13, 15)            # last-resort background (same as the dashboard's base)
INK = (244, 246, 238)
SHADE = (12, 13, 15)
NAME_MAX = 32


def _font(size: int):
    try:
        return ImageFont.truetype(FONT_BOLD, size)
    except Exception:
        try:
            return ImageFont.load_default(size)   # Pillow >= 10.1
        except Exception:
            return ImageFont.load_default()


def fit_background(img: Image.Image) -> Image.Image:
    """Any image -> exactly W x H RGB (cover-crop, EXIF rotation applied)."""
    img = ImageOps.exif_transpose(img)
    return ImageOps.fit(img.convert("RGB"), (W, H), Image.LANCZOS)


def process_upload(raw: bytes, allowed_formats) -> bytes:
    """Validate + normalise an uploaded background. Returns JPEG bytes (W x H).
    Raises ValueError with a short code: 'format' | 'big' | 'bad'."""
    try:
        img = Image.open(io.BytesIO(raw))
        fmt = img.format
    except Exception:
        raise ValueError("bad")
    if fmt not in allowed_formats:
        raise ValueError("format")
    w, h = img.size
    if w < 1 or h < 1 or w * h > BG_MAX_PIXELS:
        raise ValueError("big")
    try:
        img.load()                         # full decode: catches truncated / corrupt files
        out = io.BytesIO()
        fit_background(img).save(out, "JPEG", quality=88, optimize=True)   # also strips metadata
        return out.getvalue()
    except Exception:
        raise ValueError("bad")


def _load_background(custom: bytes | None) -> Image.Image:
    """Custom upload if it works, else the shipped default, else a flat colour."""
    if custom:
        try:
            img = Image.open(io.BytesIO(bytes(custom)))
            if img.size[0] * img.size[1] <= BG_MAX_PIXELS:
                img.load()
                return fit_background(img)
        except Exception:
            pass                           # broken upload -> default
    try:
        img = Image.open(DEFAULT_BG_PATH)
        img.load()
        return fit_background(img)
    except Exception:
        return Image.new("RGB", (W, H), FLAT_BG)


def _has_glyph(font, ch: str, notdef: bytes) -> bool:
    try:
        return bytes(font.getmask(ch)) != notdef
    except Exception:
        return False


def clean_name(name: str, font) -> str:
    """Upper-case, only characters the font can draw (no empty boxes), max NAME_MAX."""
    name = " ".join(str(name or "").split()).upper()
    try:
        notdef = bytes(font.getmask("￿"))
    except Exception:
        notdef = b""
    out = "".join(c for c in name if c == " " or _has_glyph(font, c, notdef))
    return " ".join(out.split())[:NAME_MAX].strip()


def _circle_avatar(avatar: bytes | None, fallback_letter: str) -> Image.Image:
    size = AVATAR * 3                      # draw big, shrink = smooth edge
    img = None
    if avatar:
        try:
            img = ImageOps.fit(Image.open(io.BytesIO(avatar)).convert("RGBA"), (size, size), Image.LANCZOS)
        except Exception:
            img = None
    if img is None:                        # no / broken avatar: flat block with the first letter
        img = Image.new("RGBA", (size, size), (39, 43, 50, 255))
        d = ImageDraw.Draw(img)
        f = _font(size // 2)
        d.text((size / 2, size / 2), (fallback_letter or "?")[:1].upper(), font=f, fill=INK, anchor="mm")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
    img.putalpha(mask)
    return img.resize((AVATAR, AVATAR), Image.LANCZOS)


def render_welcome_image(name: str, avatar: bytes | None = None, background: bytes | None = None,
                         word: str = "WELCOME") -> bytes:
    """PNG bytes of the banner. word = the big text ("WELCOME", or "GOODBYE" for leaves)."""
    bg = _load_background(background).convert("RGBA")
    # flat dark veil so white text stays readable on any background
    bg = Image.alpha_composite(bg, Image.new("RGBA", (W, H), SHADE + (110,)))
    d = ImageDraw.Draw(bg)

    # avatar: dark ring, white ring, picture
    cx, top = W // 2, 28
    ring = 10
    box = (cx - AVATAR // 2 - ring, top, cx + AVATAR // 2 + ring, top + AVATAR + 2 * ring)
    d.ellipse(box, fill=INK)
    d.ellipse((box[0] + 4, box[1] + 4, box[2] - 4, box[3] - 4), fill=SHADE)
    d.ellipse((box[0] + 7, box[1] + 7, box[2] - 7, box[3] - 7), fill=INK)
    bg.alpha_composite(_circle_avatar(avatar, name[:1]), (cx - AVATAR // 2, top + ring))

    # big word
    big = _font(78)
    d.text((cx, 262), word, font=big, fill=INK, anchor="mm", stroke_width=5, stroke_fill=SHADE)

    # name: shrink until it fits, never wider than 90% of the image
    font = _font(46)
    text = clean_name(name, font) or "NEW MEMBER"
    size = 46
    while size > 20 and d.textlength(text, font=font) > W * 0.9:
        size -= 2
        font = _font(size)
    d.text((cx, 338), text, font=font, fill=INK, anchor="mm", stroke_width=4, stroke_fill=SHADE)

    out = io.BytesIO()
    bg.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()


def sample_avatar() -> bytes:
    """Flat pixel-block avatar for the dashboard preview and tests."""
    img = Image.new("RGB", (256, 256), (39, 43, 50))
    d = ImageDraw.Draw(img)
    cells = ["..XXXX..", ".XXXXXX.", "XX.XX.XX", "XXXXXXXX", "X.XXXX.X", "XX....XX", ".XXXXXX.", "..XXXX.."]
    for y, row in enumerate(cells):
        for x, c in enumerate(row):
            if c == "X":
                d.rectangle((x * 32, y * 32, x * 32 + 31, y * 32 + 31), fill=(198, 244, 50))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def build_default_background() -> Image.Image:
    """Pixel-grid look matching the dashboard (used once to create assets/welcome/default_bg.png)."""
    import random
    rnd = random.Random(489)
    img = Image.new("RGB", (W, H), (16, 18, 21))
    d = ImageDraw.Draw(img)
    cell = 20
    for gy in range(H // cell):
        for gx in range(W // cell):
            r = rnd.random()
            if r < 0.10:
                col = (27, 30, 35)
            elif r < 0.14:
                col = (39, 43, 50)
            elif r < 0.17:
                col = (86, 108, 12)
            elif r < 0.185:
                col = (198, 244, 50)
            else:
                continue
            d.rectangle((gx * cell, gy * cell, gx * cell + cell - 1, gy * cell + cell - 1), fill=col)
    return img
