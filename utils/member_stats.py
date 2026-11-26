"""Server growth stats: the numbers and the chart behind /stats combined | joins | leaves.

No Discord code in here, so it can be tested on its own (see the bottom of the file).

Days are UTC days. "Today" is the current UTC day, "Last 7 Days" and "Last 30 Days" are the last
7 / 30 UTC days including today, so the card numbers always add up to what the chart shows.

The chart is drawn with Pillow (already used for the welcome banners): no matplotlib.
It is drawn at twice the size and scaled down, which gives smooth lines.
"""
import io
import math
import os
from datetime import timedelta

from PIL import Image, ImageDraw, ImageFont

from utils.log import get_logger
logger = get_logger(__name__)

CHART_DAYS = 30
MODES = ("combined", "joins", "leaves")

TITLES = {
    "combined": "Member Joins and Leaves History",
    "joins": "Member Joins History",
    "leaves": "Member Leaves History",
}
CARD_TITLES = {
    "combined": "Join and Leave Members Server Stats",
    "joins": "Join Members Server Stats",
    "leaves": "Leave Members Server Stats",
}

_FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets", "fonts")

# colours (RGB)
BG = (10, 10, 12)
CYAN = (0, 200, 210)
GRID = (120, 120, 128)
GREEN = (144, 238, 144)
RED = (255, 105, 97)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def build_series(buckets: dict, today, days: int = CHART_DAYS) -> tuple:
    """(dates, joins, leaves): one value per UTC day, oldest first, ending today.
    `buckets` is {"YYYY-MM-DD": {"join": n, "leave": n}} (missing days count as 0)."""
    dates = [today - timedelta(days=days - 1 - i) for i in range(days)]
    joins = [int((buckets.get(d.isoformat()) or {}).get("join", 0)) for d in dates]
    leaves = [int((buckets.get(d.isoformat()) or {}).get("leave", 0)) for d in dates]
    return dates, joins, leaves


def window(series: list, days: int) -> int:
    """The total of the last `days` days of a series."""
    return sum(series[-days:])


def card_value(mode: str, joins: int, leaves: int) -> str:
    """What one field of the card says, e.g. **+168** (+194, -26)."""
    if mode == "joins":
        return f"**+{joins}**" if joins else "**0**"
    if mode == "leaves":
        return f"**-{leaves}**" if leaves else "**0**"
    net = joins - leaves
    return f"**{'+' if net >= 0 else '-'}{abs(net)}** (+{joins}, -{leaves})"


# ---------------------------------------------------------------------------
# Chart
# ---------------------------------------------------------------------------

def _font(size: int, bold: bool = False):
    path = os.path.join(_FONTS, "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        try:
            return ImageFont.load_default(size)         # Pillow >= 10.1
        except TypeError:
            return ImageFont.load_default()


def _nice_step(top: float) -> int:
    """A tidy tick spacing (1, 2, 5, 10, 20, 50 ...) that gives about five lines."""
    if top <= 0:
        return 1
    raw = top / 5
    mag = 10 ** math.floor(math.log10(raw))
    for m in (1, 2, 5, 10):
        if raw <= m * mag:
            return max(1, int(round(m * mag)))
    return max(1, int(round(10 * mag)))


def render_chart(mode: str, dates: list, joins: list, leaves: list) -> bytes:
    """The history chart as PNG bytes. mode: combined | joins | leaves."""
    S = 2                                                # drawn at 2x, scaled down at the end
    W, H = 800 * S, 420 * S
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    lines = []
    if mode in ("combined", "joins"):
        lines.append((joins, GREEN))
    if mode in ("combined", "leaves"):
        lines.append((leaves, RED))

    top_val = max([max(v) for v, _c in lines] + [0])
    step = _nice_step(top_val)
    y_min = -step                                        # one line below zero, like the example
    y_max = (math.ceil(top_val / step) + (1 if top_val % step == 0 else 0)) * step
    if y_max <= 0:
        y_max = step

    left, right = 72 * S, W - 26 * S
    top, bottom = 58 * S, H - 96 * S
    n = len(dates)

    def x_at(i):
        return left + (right - left) * (i / (n - 1) if n > 1 else 0.5)

    def y_at(v):
        return bottom - (v - y_min) / (y_max - y_min) * (bottom - top)

    # title
    title_font = _font(18 * S)
    title = TITLES.get(mode, TITLES["combined"])
    tw = d.textlength(title, font=title_font)
    d.text(((W - tw) / 2, 16 * S), title, font=title_font, fill=CYAN)

    # horizontal grid + y labels
    label_font = _font(12 * S)
    tick = y_min
    while tick <= y_max:
        y = y_at(tick)
        d.line([(left, y), (right, y)], fill=GRID, width=1 * S)
        text = str(tick)
        d.text((left - 10 * S - d.textlength(text, font=label_font), y - 8 * S), text,
               font=label_font, fill=CYAN)
        tick += step

    # vertical grid + x labels (every second day, ending on today), rotated 45 degrees
    for i in range(n):
        if (n - 1 - i) % 2:
            continue
        x = x_at(i)
        d.line([(x, top), (x, bottom)], fill=GRID, width=1 * S)
        text = dates[i].strftime("%d/%m/%Y")
        tw = int(d.textlength(text, font=label_font)) + 6
        th = 16 * S
        tmp = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
        ImageDraw.Draw(tmp).text((2, 0), text, font=label_font, fill=CYAN + (255,))
        rot = tmp.rotate(45, expand=True, resample=Image.BICUBIC)
        img.paste(rot, (int(x + 4 * S - rot.width), int(bottom + 6 * S)), rot)

    # the lines and their dots
    for values, colour in lines:
        pts = [(x_at(i), y_at(v)) for i, v in enumerate(values)]
        if len(pts) > 1:
            d.line(pts, fill=colour, width=2 * S, joint="curve")
        for x, y in pts:
            d.ellipse([x - 3 * S, y - 3 * S, x + 3 * S, y + 3 * S], fill=colour)

    img = img.resize((800, 420), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


if __name__ == "__main__":                              # quick manual test: python -m utils.member_stats
    import random
    from datetime import date
    random.seed(4)
    buckets = {(date.today() - timedelta(days=i)).isoformat():
               {"join": random.randint(5, 60) if i % 7 else 150, "leave": random.randint(5, 40)}
               for i in range(30)}
    ds, js, ls = build_series(buckets, date.today())
    logger.info(" ".join(str(x) for x in (card_value("combined", window(js, 1), window(ls, 1)), card_value("combined", window(js, 7), window(ls, 7)), card_value("combined", window(js, 30), window(ls, 30)))))
    for m in MODES:
        open(f"/tmp/stats_{m}.png", "wb").write(render_chart(m, ds, js, ls))
    logger.info("wrote /tmp/stats_*.png")
