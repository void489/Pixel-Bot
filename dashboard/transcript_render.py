"""Safe rendering helpers for ticket transcripts.

Everything user-controlled is HTML-escaped BEFORE any formatting is applied, so
message text can never inject markup/scripts into the dashboard.
"""
import re
from datetime import datetime

from markupsafe import Markup, escape

_CODEBLOCK = re.compile(r"```(?:[A-Za-z0-9_+-]*\n)?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")
# Runs on already-escaped text, so '&amp;' is allowed but '&lt;' '&gt;' '&#34;' '&#39;' end a URL.
_URL = re.compile(r"https?://(?:[^\s<>&]|&amp;)+")
_CUSTOM_EMOJI = re.compile(r"&lt;(a?):([A-Za-z0-9_]{2,32}):(\d{15,25})&gt;")
_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)
_UNDERLINE = re.compile(r"__(.+?)__", re.S)
_ITALIC = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)")
_STRIKE = re.compile(r"~~(.+?)~~", re.S)
_QUOTE = re.compile(r"(?m)^&gt; ?(.*)$")

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def safe_url(url):
    url = (url or "").strip()
    return url if url.startswith(("http://", "https://")) else None


def safe_color(value):
    return value if isinstance(value, str) and _HEX.match(value) else None


def render_markdown(text) -> Markup:
    """Discord-style markdown -> safe HTML."""
    text = str(text or "")
    stash = {}

    def keep(html: str) -> str:
        key = f"\x00{len(stash)}\x00"
        stash[key] = html
        return key

    # 1) code first (contents are escaped and never formatted)
    text = _CODEBLOCK.sub(lambda m: keep(f"<pre>{escape(m.group(1).strip(chr(10)))}</pre>"), text)
    text = _INLINE_CODE.sub(lambda m: keep(f"<code>{escape(m.group(1))}</code>"), text)

    # 2) escape everything else
    text = str(escape(text))

    # 3) custom emoji + links get stashed so formatting can't mangle them
    def emoji(m):
        ext = "gif" if m.group(1) else "png"
        return keep(
            f'<img class="emoji" src="https://cdn.discordapp.com/emojis/{m.group(3)}.{ext}?size=48" '
            f'alt=":{m.group(2)}:" title=":{m.group(2)}:">'
        )

    def link(m):
        url = m.group(0)
        tail = ""
        while url and url[-1] in ".,;:!?)":
            tail = url[-1] + tail
            url = url[:-1]
        return keep(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{url}</a>') + tail

    text = _CUSTOM_EMOJI.sub(emoji, text)
    text = _URL.sub(link, text)

    # 4) markdown
    text = _BOLD.sub(r"<strong>\1</strong>", text)
    text = _UNDERLINE.sub(r"<u>\1</u>", text)
    text = _ITALIC.sub(r"<em>\1</em>", text)
    text = _STRIKE.sub(r"<s>\1</s>", text)
    text = _QUOTE.sub(r'<span class="quote">\1</span>', text)
    text = text.replace("\n", "<br>")

    # 5) put stashed pieces back
    for key, html in stash.items():
        text = text.replace(key, html)
    return Markup(text)


def human_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def prepare_messages(messages, file_url):
    """Turn stored messages into template-ready dicts.

    file_url(file_id) -> URL of the dashboard route that serves the saved image.
    Adds date separators and groups consecutive messages from one author."""
    out = []
    prev = None
    for m in messages or []:
        try:
            at = datetime.fromisoformat(m.get("at"))
        except (TypeError, ValueError):
            at = None

        author = m.get("author") or {}
        d = {
            "author": {
                "name": author.get("name") or "Unknown",
                "avatar": safe_url(author.get("avatar")),
                "bot": bool(author.get("bot")),
                "color": safe_color(author.get("color")),
                "id": author.get("id"),
            },
            "time": at.strftime("%H:%M") if at else "",
            "full_time": at.strftime("%Y-%m-%d %H:%M:%S UTC") if at else "",
            "date_label": None,
            "compact": False,
            "edited": bool(m.get("edited")),
            "content": m.get("content") or "",
            "system": m.get("system"),
            "reply": m.get("reply"),
            "embeds": [],
            "attachments": [],
        }

        for e in m.get("embeds") or []:
            d["embeds"].append({
                "title": e.get("title"),
                "description": e.get("description"),
                "url": safe_url(e.get("url")),
                "color": safe_color(e.get("color")),
                "author": e.get("author"),
                "fields": e.get("fields") or [],
                "footer": e.get("footer"),
                "image": safe_url(e.get("image")),
                "thumbnail": safe_url(e.get("thumbnail")),
            })

        for a in m.get("attachments") or []:
            d["attachments"].append({
                "filename": a.get("filename") or "file",
                "size": human_size(a.get("size")),
                "is_image": bool(a.get("is_image")),
                "url": file_url(a["file_id"]) if a.get("file_id") else None,
            })

        # date separator + grouping (like Discord: same author within 7 minutes)
        day = at.date() if at else None
        if prev is None or day != prev["day"]:
            d["date_label"] = at.strftime("%A, %d %B %Y") if at else None
        elif (at and prev["at"] and author.get("id") == prev["author_id"]
              and (at - prev["at"]).total_seconds() < 420
              and not d["system"] and not d["reply"] and not prev["system"]):
            d["compact"] = True

        prev = {"day": day, "at": at, "author_id": author.get("id"), "system": d["system"]}
        out.append(d)
    return out
