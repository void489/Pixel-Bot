"""Rank card + level-up card images (Pillow). Pure functions: bytes in, PNG bytes out,
so the cog can run them in a worker thread.

Both cards share one look: dark panel, round avatar with an accent ring, accent
gradient progress bar. The accent colour and an optional background image come from
the dashboard's Leveling page.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFont

_FONT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")
_FALLBACKS = (
    "/usr/share/fonts/truetype/dejavu/{name}",
    "/usr/share/fonts/dejavu/{name}",
)

W = 934
BG = (24, 25, 28)
PANEL = (32, 34, 38)
TRACK = (58, 61, 67)
DEFAULT_ACCENT = (88, 101, 242)
DEFAULT_ACCENT_2 = (139, 92, 246)
WHITE = (244, 245, 247)
MUTED = (148, 155, 164)
MAX_BG_PIXELS = 40_000_000


def _font(size: int, bold: bool = False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in [os.path.join(_FONT_DIR, name)] + [p.format(name=name) for p in _FALLBACKS]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                pass
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1: scalable built-in font
    except TypeError:
        return ImageFont.load_default()


def _fit(draw, text, font_size, max_width, bold=True, min_size=30):
    """Shrink text down to min_size to fit max_width, then truncate with an ellipsis."""
    size = font_size
    while size >= min_size:
        f = _font(size, bold)
        if draw.textlength(text, font=f) <= max_width:
            return text, f
        size -= 2
    f = _font(min_size, bold)
    while text and draw.textlength(text + "…", font=f) > max_width:
        text = text[:-1]
    return text.rstrip() + "…", f


def _theme(accent_hex):
    """(accent, accent_2). The default accent keeps the original blue -> purple look."""
    try:
        h = str(accent_hex or "").lstrip("#")
        a = (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)) if len(h) == 6 else DEFAULT_ACCENT
    except ValueError:
        a = DEFAULT_ACCENT
    if a == DEFAULT_ACCENT:
        return DEFAULT_ACCENT, DEFAULT_ACCENT_2
    return a, tuple(int(c + (255 - c) * 0.3) for c in a)


def _canvas(h: int, bg_bytes, scale: int):
    """Background + rounded panel (optionally with a darkened background image)."""
    S = lambda v: int(v * scale)
    img = Image.new("RGB", (W * scale, h * scale), BG)
    d = ImageDraw.Draw(img)
    box = (S(14), S(14), S(W - 14), S(h - 14))
    pw, ph = box[2] - box[0], box[3] - box[1]

    bg = None
    if bg_bytes:
        try:
            im = Image.open(io.BytesIO(bg_bytes))
            if im.width * im.height <= MAX_BG_PIXELS:
                bg = im.convert("RGB")
        except Exception:
            bg = None
    if bg is not None:
        ratio = max(pw / bg.width, ph / bg.height)  # "cover" crop
        bg = bg.resize((max(1, int(bg.width * ratio)), max(1, int(bg.height * ratio))), Image.LANCZOS)
        left, top = (bg.width - pw) // 2, (bg.height - ph) // 2
        bg = bg.crop((left, top, left + pw, top + ph))
        bg = Image.blend(bg, Image.new("RGB", bg.size, (12, 12, 14)), 0.68)  # keep text readable
        mask = Image.new("L", bg.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, pw - 1, ph - 1), radius=S(26), fill=255)
        img.paste(bg, (box[0], box[1]), mask)
    else:
        d.rounded_rectangle(box, radius=S(26), fill=PANEL)
    return img, d


def _avatar(img, d, avatar_bytes, ax, ay, asz, accent, scale):
    S = lambda v: int(v * scale)
    av = None
    if avatar_bytes:
        try:
            av = Image.open(io.BytesIO(avatar_bytes)).convert("RGBA").resize((S(asz), S(asz)), Image.LANCZOS)
        except Exception:
            av = None
    if av is None:
        av = Image.new("RGBA", (S(asz), S(asz)), TRACK)
    mask = Image.new("L", (S(asz), S(asz)), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, S(asz) - 1, S(asz) - 1), fill=255)
    d.ellipse((S(ax - 5), S(ay - 5), S(ax + asz + 5), S(ay + asz + 5)), fill=accent)
    img.paste(av, (S(ax), S(ay)), mask)


def _bar(img, bx0, by0, bx1, by1, frac, accent, accent_2, scale):
    S = lambda v: int(v * scale)
    d = ImageDraw.Draw(img)
    r = (by1 - by0) // 2
    d.rounded_rectangle((S(bx0), S(by0), S(bx1), S(by1)), radius=S(r), fill=TRACK)
    frac = max(0.0, min(1.0, frac))
    if frac <= 0:
        return
    fill_w = max(S(by1 - by0 + 2), int(S(bx1 - bx0) * frac))
    bar = Image.new("RGB", (fill_w, S(by1 - by0)))
    bd = ImageDraw.Draw(bar)
    for i in range(fill_w):  # horizontal gradient
        t = i / max(1, fill_w - 1)
        bd.line((i, 0, i, S(by1 - by0)), fill=tuple(int(accent[k] + (accent_2[k] - accent[k]) * t) for k in range(3)))
    bmask = Image.new("L", (fill_w, S(by1 - by0)), 0)
    ImageDraw.Draw(bmask).rounded_rectangle((0, 0, fill_w - 1, S(by1 - by0) - 1), radius=S(r), fill=255)
    img.paste(bar, (S(bx0), S(by0)), bmask)


def _finish(img, h):
    out = io.BytesIO()
    img.resize((W, h), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()


def _pill(d, right_x, baseline_y, text, accent, scale):
    """Small accent pill, right aligned (used for the XP boost tag)."""
    S = lambda v: int(v * scale)
    f = _font(18, True)
    tw = d.textlength(text, font=f)
    pad = 14
    x1, x0 = S(right_x), S(right_x) - int(tw) - S(pad * 2)
    d.rounded_rectangle((x0, S(baseline_y - 24), x1, S(baseline_y + 8)), radius=S(16), fill=accent)
    d.text((x0 + S(pad), S(baseline_y)), text, font=f, fill=WHITE, anchor="ls")
    return x0 / scale


# ---------------------------------------------------------------------------
# /rank
# ---------------------------------------------------------------------------

def render(avatar_bytes, name: str, level: int, rank: int, xp_into: int, xp_need: int,
           total_xp: int, *, accent=None, bg_bytes=None, next_reward=None,
           boost=None, max_level: bool = False) -> bytes:
    """Rank card.

    next_reward: text such as "Next reward: VIP at level 15 (3 levels away)", or None.
    boost:       text such as "2x XP", or None.
    max_level:   True when there is no next level to work towards.
    """
    scale = 2
    S = lambda v: int(v * scale)
    ACCENT, ACCENT_2 = _theme(accent)
    has_row_c = bool(next_reward or boost)
    H = 342 if has_row_c else 300

    img, d = _canvas(H, bg_bytes, scale)
    asz = 180
    _avatar(img, d, avatar_bytes, 44, (H - asz) // 2, asz, ACCENT, scale)

    left, right = 44 + asz + 44, W - 52

    # name
    text, f_name = _fit(d, name, 46, S(right - left))
    d.text((S(left), S(96)), text, font=f_name, fill=WHITE, anchor="ls")

    # level / rank (right aligned, top)
    f_big, f_lab = _font(46, True), _font(22, True)
    x = S(right)
    for value, label, color in ((str(level), "LEVEL", ACCENT), (f"#{rank}", "RANK", WHITE)):
        vw = d.textlength(value, font=f_big)
        lw = d.textlength(label + " ", font=f_lab)
        x -= vw
        d.text((x, S(146)), value, font=f_big, fill=color, anchor="ls")
        x -= lw
        d.text((x, S(146)), label + " ", font=f_lab, fill=MUTED, anchor="ls")
        x -= S(26)

    f_xp = _font(22, False)
    f_pct = _font(22, True)
    frac = 1.0 if max_level else (0 if xp_need <= 0 else xp_into / xp_need)
    pct = 100 if max_level else int(max(0, min(100, frac * 100)))

    # row A: this level's progress (full numbers) + percent
    row_a = "MAX LEVEL" if max_level else f"{xp_into:,} / {xp_need:,} XP"
    d.text((S(left), S(188)), row_a, font=f_xp, fill=WHITE, anchor="ls")
    d.text((S(right), S(188)), f"{pct}%", font=f_pct, fill=MUTED, anchor="rs")

    _bar(img, left, 202, right, 236, frac, ACCENT, ACCENT_2, scale)

    # row B: XP until next level + total XP
    d = ImageDraw.Draw(img)
    row_b = "You've reached the top!" if max_level else f"{max(0, xp_need - xp_into):,} XP until level {level + 1}"
    d.text((S(left), S(268)), row_b, font=f_xp, fill=MUTED, anchor="ls")
    d.text((S(right), S(268)), f"{total_xp:,} total XP", font=f_xp, fill=MUTED, anchor="rs")

    # row C: next reward + boost tag
    if has_row_c:
        limit = right
        if boost:
            limit = _pill(d, right, 304, boost, ACCENT, scale) - 16
        if next_reward:
            t, f = _fit(d, next_reward, 20, S(limit - left), bold=False, min_size=16)
            d.text((S(left), S(304)), t, font=f, fill=MUTED, anchor="ls")

    return _finish(img, H)


# ---------------------------------------------------------------------------
# level-up
# ---------------------------------------------------------------------------

def render_levelup(avatar_bytes, name: str, old_level: int, new_level: int, rank: int,
                   xp_into: int, xp_need: int, *, accent=None, bg_bytes=None,
                   extra=None, max_level: bool = False) -> bytes:
    """Level-up card: "LEVEL UP!", old -> new level, progress to the next level.

    extra: one line under the bar, e.g. "Unlocked: VIP" or "Next reward: VIP at level 15".
    """
    scale = 2
    S = lambda v: int(v * scale)
    ACCENT, ACCENT_2 = _theme(accent)
    H = 350 if extra else 320

    img, d = _canvas(H, bg_bytes, scale)
    asz = 180
    _avatar(img, d, avatar_bytes, 44, (H - asz) // 2 - (8 if extra else 0), asz, ACCENT, scale)

    left, right = 44 + asz + 44, W - 52

    d.text((S(left), S(78)), "LEVEL UP!", font=_font(26, True), fill=ACCENT, anchor="ls")
    text, f_name = _fit(d, name, 42, S(right - left), min_size=26)
    d.text((S(left), S(124)), text, font=f_name, fill=WHITE, anchor="ls")

    # big "12 -> 13"
    f_old, f_new, f_arrow = _font(60, True), _font(84, True), _font(54, True)
    x, y = S(left), S(206)
    old_s, new_s = str(old_level), str(new_level)
    d.text((x, y), old_s, font=f_old, fill=MUTED, anchor="ls")
    x += d.textlength(old_s, font=f_old) + S(22)
    d.text((x, y - S(4)), "→", font=f_arrow, fill=ACCENT, anchor="ls")
    x += d.textlength("→", font=f_arrow) + S(22)
    d.text((x, y), new_s, font=f_new, fill=WHITE, anchor="ls")

    frac = 1.0 if max_level else (0 if xp_need <= 0 else xp_into / xp_need)
    _bar(img, left, 226, right, 252, frac, ACCENT, ACCENT_2, scale)

    d = ImageDraw.Draw(img)
    f_xp = _font(22, False)
    row = "MAX LEVEL" if max_level else f"{max(0, xp_need - xp_into):,} XP until level {new_level + 1}"
    d.text((S(left), S(284)), row, font=f_xp, fill=MUTED, anchor="ls")
    d.text((S(right), S(284)), f"Rank #{rank}", font=_font(22, True), fill=WHITE, anchor="rs")

    if extra:
        t, f = _fit(d, extra, 22, S(right - left), bold=True, min_size=16)
        d.text((S(left), S(322)), t, font=f, fill=ACCENT_2, anchor="ls")

    return _finish(img, H)
