"""Promotion / demotion card image (Pillow). Pure function: bytes in, PNG bytes out,
so the cog can run it in a worker thread. Same look as the rank card."""
import io

from PIL import Image, ImageDraw

from utils.rankcard import _font, _fit, BG, PANEL, TRACK, WHITE, MUTED

W, H = 934, 330
GREEN = (87, 242, 135)
RED = (237, 66, 69)
DARK = (24, 25, 28)

# kind -> (label shown on the card, colour, is_up)
KINDS = {
    "promotion": ("PROMOTION", GREEN, True),
    "promoted":  ("PROMOTED", GREEN, True),   # used when an application is accepted
    "demotion":  ("DEMOTION", RED, False),
}


def _trunc(draw, text, font, max_w):
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _chip(img, d, S, x, y, h, text, fill, text_fill, max_w):
    """Rounded chip with centred text. Returns the x where it ends (final px)."""
    text, f = _fit(d, text, 38, S(max_w - 44), bold=True, min_size=18)
    tw = d.textlength(text, font=f)
    w = int(tw / S(1)) + 44
    d.rounded_rectangle((S(x), S(y), S(x + w), S(y + h)), radius=S(h / 2), fill=fill)
    d.text((S(x + w / 2), S(y + h / 2) + 1), text, font=f, fill=text_fill, anchor="mm")
    return x + w


def render(avatar_bytes, username: str, kind: str, old_label: str, new_label: str,
           reason: str = None, footer: str = None) -> bytes:
    title, color, is_up = KINDS.get(kind, KINDS["promotion"])
    scale = 2
    img = Image.new("RGB", (W * scale, H * scale), BG)
    d = ImageDraw.Draw(img)
    S = lambda v: int(v * scale)

    d.rounded_rectangle((S(14), S(14), S(W - 14), S(H - 14)), radius=S(26), fill=PANEL)

    # avatar with a ring in the promotion/demotion colour
    asz = 160
    ax, ay = 46, (H - asz) // 2
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
    d.ellipse((S(ax - 5), S(ay - 5), S(ax + asz + 5), S(ay + asz + 5)), fill=color)
    img.paste(av, (S(ax), S(ay)), mask)

    left = ax + asz + 42
    right = W - 52

    # title pill (PROMOTION / DEMOTION / PROMOTED) with an arrow
    f_pill = _font(26, True)
    pw = int(d.textlength(title, font=f_pill) / scale) + 30 + 26
    d.rounded_rectangle((S(left), S(44), S(left + pw), S(78)), radius=S(17), fill=color)
    cx, cy = left + 22, 61
    if is_up:
        d.polygon([(S(cx), S(cy - 8)), (S(cx + 8), S(cy + 6)), (S(cx - 8), S(cy + 6))], fill=DARK)
    else:
        d.polygon([(S(cx), S(cy + 8)), (S(cx + 8), S(cy - 6)), (S(cx - 8), S(cy - 6))], fill=DARK)
    d.text((S(left + 40), S(61) + 1), title, font=f_pill, fill=DARK, anchor="lm")

    # username
    text, f_name = _fit(d, username, 64, S(right - left), bold=True, min_size=32)
    d.text((S(left), S(142)), text, font=f_name, fill=WHITE, anchor="ls")

    # old role  ->  new role
    chip_h, chip_y = 50, 168
    arrow_w = 54
    max_chip = (right - left - arrow_w) // 2
    x_end = _chip(img, d, S, left, chip_y, chip_h, old_label, TRACK, WHITE, max_chip)
    ay_mid = chip_y + chip_h / 2
    x0 = x_end + 14
    d.line((S(x0), S(ay_mid), S(x0 + 24), S(ay_mid)), fill=MUTED, width=S(4))
    d.polygon([(S(x0 + 34), S(ay_mid)), (S(x0 + 20), S(ay_mid - 9)), (S(x0 + 20), S(ay_mid + 9))], fill=MUTED)
    _chip(img, d, S, x0 + 34 + 14, chip_y, chip_h, new_label, color, DARK, max_chip)

    # reason
    if reason:
        f_r = _font(26, False)
        line = _trunc(d, "Reason: " + " ".join(reason.split()), f_r, S(right - left))
        d.text((S(left), S(256)), line, font=f_r, fill=MUTED, anchor="ls")

    # footer (who did it)
    if footer:
        f_f = _font(22, False)
        d.text((S(right), S(298)), _trunc(d, footer, f_f, S(right - left)), font=f_f, fill=MUTED, anchor="rs")

    out = io.BytesIO()
    img.resize((W, H), Image.LANCZOS).save(out, "PNG", optimize=True)
    return out.getvalue()
