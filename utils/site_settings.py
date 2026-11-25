"""Public contact details set on the /admin page (Main tab): the support server link and the
contact email. Stored in Mongo `bot_settings`, _id = "site". Nothing is hardcoded: until the
admin sets them they are empty and everything that shows them simply hides them.

Used by the dashboard (templates, privacy/terms pages), the admin page and /botinfo.
"""
import re
from urllib.parse import urlparse

SUPPORT_URL_MAX = 200
EMAIL_MAX = 120
_EMAIL_RE = re.compile(r"^[^@\s<>\"'()\[\],;:\\]+@[^@\s<>\"'()\[\],;:\\]+\.[^@\s<>\"'()\[\],;:\\]+$")


def valid_support_url(url: str) -> bool:
    """Empty is fine (= none). Otherwise it must be an https link."""
    if not url:
        return True
    u = urlparse(url)
    return len(url) <= SUPPORT_URL_MAX and u.scheme == "https" and bool(u.hostname)


def valid_email(email: str) -> bool:
    """Empty is fine (= none)."""
    if not email:
        return True
    return len(email) <= EMAIL_MAX and bool(_EMAIL_RE.match(email))


def normalize_site(doc) -> dict:
    """The saved doc (missing / empty / odd) as {"support_url", "contact_email"} strings.
    A stored value that fails validation is treated as not set."""
    doc = doc if isinstance(doc, dict) else {}
    url = str(doc.get("support_url") or "").strip()
    email = str(doc.get("contact_email") or "").strip()
    return {
        "support_url": url if valid_support_url(url) else "",
        "contact_email": email if valid_email(email) else "",
    }
