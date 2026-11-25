"""Who may open a transcript (pure: no Discord, Flask or DB).

Each ticket type has a `transcript_access` setting:
    "managers"  (default = how it always worked) anyone with dashboard access to the server
                (Manage Server / Administrator, or the /admin login)
    "staff"     staff roles only: the panel's support roles + the server's ticket support
                roles. Administrators and the server owner can always open it; someone who
                only has Manage Server needs a staff role too.
    "opener"    the same as "staff", plus the member who opened the ticket.
Transcripts of tickets without a ticket type (claim / spawner / application tickets, and
anything saved before this setting existed) always use "managers".
"""

MODES = ("managers", "staff", "opener")


def clean_mode(raw) -> str:
    return raw if raw in MODES else "managers"


def can_open(mode, *, is_admin: bool, is_manager: bool, has_staff_role: bool, is_opener: bool) -> bool:
    """is_admin: /admin login, server owner or Administrator permission."""
    if is_admin:
        return True
    if clean_mode(mode) == "managers":
        return is_manager
    if has_staff_role:
        return True
    return clean_mode(mode) == "opener" and is_opener


def can_share(mode, *, is_admin: bool, is_manager: bool, has_staff_role: bool) -> bool:
    """Creating / revoking share links: staff only, never the opener."""
    return can_open(mode, is_admin=is_admin, is_manager=is_manager,
                    has_staff_role=has_staff_role, is_opener=False)
