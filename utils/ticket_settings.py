"""Pure helpers for per-panel ticket settings (no Discord or DB imports, so the dashboard
and the bot can both use them).

A "panel" here is a TICKET TYPE (dashboard -> Tickets -> Ticket Types): each one is a
button / dropdown entry that opens tickets. All settings below are optional keys on the
ticket type document. A missing key means "today's behaviour", so existing types don't
change until they are edited and saved.

    button_label        panel button text (blank = the type's name)
    max_open_tickets    0 / missing = old rule (1 open ticket per member across the server);
                        1-10 = that many open tickets per member in THIS panel only
    required_role_ids   member needs at least one of these roles to open a ticket
    welcome_message     text inside the ticket (replaces the default line in the welcome embed)
    created_message     private reply the member sees after opening a ticket
    claiming_enabled    show the Claim / Unclaim buttons (default True)
    claim_only_visible  after a claim only the claimer (and admins) can see the ticket
    save_transcripts    save a transcript on close (default True)
    linked_type_ids     related ticket types: listed in the welcome embed, and /ticket move only
                        offers these (blank = every type, like before)
    transcript_access   who may open this panel's transcripts on the dashboard:
                        "managers" (default, as before) | "staff" | "opener"
                        (see utils/transcript_access.py)
    thread_mode         open this panel's tickets as PRIVATE THREADS instead of channels
                        (default False). Needs thread_parent_id (a text channel);
                        thread_auto_archive = 60 / 1440 / 4320 / 10080 minutes (default 10080);
                        thread_invitable = members may add other people (default False)
    web_reply           staff may read and answer this panel's open tickets from the dashboard
                        (Live Tickets); default False
    priority_enabled    show the Priority button and a Priority field in the welcome embed
                        (default False; /ticket priority works on every ticket either way)
    priority_rename     "off" (default) | "emoji" | "prefix": also rename the channel when
                        the priority changes (🔴-name / urgent-name; Normal = no prefix)
    buttons             {"close"|"claim"|"unclaim": {"label", "emoji", "style"}}
    (support roles = the existing `allow_role_ids`; categories live in ticket_core / step 1)

Placeholders for the two messages: {user} {username} {server} {channel} {panel} {number}
Channel-name template (existing `auto_rename`): {q1}-{q5} {username} {user} {type} {panel}
{number} {id}   ({id} is the member's ID, as before; {panel} is the same as {type}).
"""
import re

MAX_OVERFLOW = 5                # spare categories per target (step 1)
MAX_OPEN_LIMIT = 10
WELCOME_MAX = 1500
CREATED_MAX = 500
LABEL_MAX = 80                  # Discord button label limit
MAX_LINKED = 10

STYLES = ("primary", "secondary", "success", "danger")

DEFAULT_BUTTONS = {
    "close": {"label": "Close Ticket", "emoji": "🔒", "style": "danger"},
    "claim": {"label": "Claim", "emoji": "🙋", "style": "success"},
    "unclaim": {"label": "Unclaim", "emoji": "↩️", "style": "secondary"},
}

# Ticket priorities. A ticket without a `priority` field counts as Normal.
PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_INFO = {
    "low": ("🟢", "Low"),
    "normal": ("🔵", "Normal"),
    "high": ("🟠", "High"),
    "urgent": ("🔴", "Urgent"),
}
RENAME_MODES = ("off", "emoji", "prefix")
THREAD_ARCHIVE_CHOICES = (60, 1440, 4320, 10080)      # Discord's auto-archive options (minutes)
DEFAULT_THREAD_ARCHIVE = 10080                        # the longest, so auto-close acts first

_CUSTOM_EMOJI = re.compile(r"^<a?:\w{2,32}:\d{15,25}>$")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def clean_overflow_ids(raw, primary_id=None) -> list:
    """Spare categories: each an ID (digits) or "__new__:Name" (created when needed),
    unique, never the main category itself, max 5."""
    primary = str(primary_id or "").strip()
    primary_key = primary.lower()
    out, seen = [], set()
    for v in (raw or []):
        s = str(v).strip()
        if s.startswith("__new__:"):
            name = s[len("__new__:"):].strip()[:100]
            if not name:
                continue
            s = "__new__:" + name
        elif not s.isdigit():
            continue
        if s.lower() == primary_key or s.lower() in seen:
            continue
        seen.add(s.lower())
        out.append(s)
    return out[:MAX_OVERFLOW]


def clean_emoji(raw) -> str:
    """A custom emoji (<:name:id>) or a unicode emoji; anything else becomes "".
    A single "-" is kept: it means "no emoji" for the ticket buttons."""
    s = str(raw or "").strip()
    if s == "-":
        return "-"
    if _CUSTOM_EMOJI.match(s):
        return s
    # unicode emoji: short, and not made of plain letters / digits
    if s and len(s) <= 20 and not any(c.isascii() and c.isalnum() for c in s) \
            and not any(c.isspace() for c in s):
        return s
    return ""


def clean_text(raw, limit: int) -> str:
    return str(raw or "").replace("\r\n", "\n").strip()[:limit]


def clean_id_list(raw, limit: int = 25) -> list:
    out = []
    for v in (raw or []):
        s = str(v).strip()
        if s and s not in out:
            out.append(s)
    return out[:limit]


def clean_buttons(raw) -> dict:
    """{"close": {...}, "claim": {...}, "unclaim": {...}} with only valid values.
    A blank field is stored as "" and means "use the default"."""
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for key in DEFAULT_BUTTONS:
        item = raw.get(key) if isinstance(raw.get(key), dict) else {}
        style = str(item.get("style") or "").strip().lower()
        out[key] = {
            "label": clean_text(item.get("label"), LABEL_MAX),
            "emoji": clean_emoji(item.get("emoji")),
            "style": style if style in STYLES else "",
        }
    return out


def clean_priority(raw, default=None):
    """"urgent" / "Urgent " -> "urgent"; anything that isn't a priority -> `default`."""
    s = str(raw or "").strip().lower()
    return s if s in PRIORITIES else default


def priority_label(level) -> str:
    """🔴 Urgent (Normal for anything unknown)."""
    emoji, name = PRIORITY_INFO[clean_priority(level, "normal")]
    return f"{emoji} {name}"


def priority_prefix(level, mode) -> str:
    """What gets put in front of the channel name: "🔴-" (emoji), "urgent-" (prefix).
    Normal has none, so a ticket that goes back to Normal gets its plain name back."""
    level = clean_priority(level, "normal")
    if mode not in ("emoji", "prefix") or level == "normal":
        return ""
    return f"{PRIORITY_INFO[level][0]}-" if mode == "emoji" else f"{level}-"


def clean_thread_archive(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_THREAD_ARCHIVE
    return n if n in THREAD_ARCHIVE_CHOICES else DEFAULT_THREAD_ARCHIVE


def clamp_limit(raw) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(0, min(n, MAX_OPEN_LIMIT))


def settings_for(tt: dict) -> dict:
    """The ticket type's settings with defaults filled in (today's behaviour)."""
    tt = tt or {}
    return {
        "button_label": str(tt.get("button_label") or "").strip(),
        "max_open_tickets": clamp_limit(tt.get("max_open_tickets")),
        "required_role_ids": [str(r) for r in (tt.get("required_role_ids") or [])],
        "welcome_message": str(tt.get("welcome_message") or ""),
        "created_message": str(tt.get("created_message") or ""),
        "claiming_enabled": tt.get("claiming_enabled", True) is not False,
        "claim_only_visible": bool(tt.get("claim_only_visible", False)),
        "save_transcripts": tt.get("save_transcripts", True) is not False,
        "linked_type_ids": [str(r) for r in (tt.get("linked_type_ids") or [])],
        "transcript_access": tt.get("transcript_access") if tt.get("transcript_access") in ("managers", "staff", "opener") else "managers",
        "thread_mode": bool(tt.get("thread_mode", False)) and str(tt.get("thread_parent_id") or "").isdigit(),
        "thread_parent_id": str(tt.get("thread_parent_id") or "") if str(tt.get("thread_parent_id") or "").isdigit() else "",
        "thread_auto_archive": clean_thread_archive(tt.get("thread_auto_archive")),
        "thread_invitable": bool(tt.get("thread_invitable", False)),
        "web_reply": bool(tt.get("web_reply", False)),
        "priority_enabled": bool(tt.get("priority_enabled", False)),
        "priority_rename": tt.get("priority_rename") if tt.get("priority_rename") in RENAME_MODES else "off",
    }


def button_look(tt: dict, key: str) -> dict:
    """Label / emoji / style for the close, claim or unclaim button of a ticket type.
    Emoji "" -> None (no emoji) when the member typed "-"."""
    base = DEFAULT_BUTTONS[key]
    custom = ((tt or {}).get("buttons") or {}).get(key) or {}
    emoji = str(custom.get("emoji") or "").strip()
    return {
        "label": (str(custom.get("label") or "").strip() or base["label"])[:LABEL_MAX],
        "emoji": None if emoji == "-" else (emoji or base["emoji"]),
        "style": custom.get("style") if custom.get("style") in STYLES else base["style"],
    }


def render(text: str, values: dict) -> str:
    """Fill {placeholders}; unknown ones are left exactly as typed."""
    def fill(m):
        key = m.group(1).lower()
        return str(values[key]) if key in values and values[key] is not None else m.group(0)
    return _PLACEHOLDER.sub(fill, text or "")


def format_number(n) -> str:
    """Ticket number as shown in names and messages: 0007."""
    try:
        return f"{int(n):04d}"
    except (TypeError, ValueError):
        return ""
