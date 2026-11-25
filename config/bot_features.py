"""Features the bot owner can switch off for ALL servers from the /admin page (Main tab).

Each entry: (key, label shown on the page, the cog class names it covers).
A switched-off feature refuses its commands with a short message (see cogs/admin_tools.py).
The bot owner (the Discord application's owner) is never blocked, so you can still test.
Core cogs (tickets, moderation, help, logs, ...) are deliberately not in this list.
"""

TOGGLEABLE = [
    ("giveaways", "Giveaways", ["Giveaways"]),
    ("spawners", "Spawners", ["Spawners"]),
    ("applications", "Applications", ["Applications"]),
    ("vouches", "Vouches & reports", ["Vouches"]),
    ("leveling", "Leveling", ["Leveling"]),
    ("welcome", "Welcome", ["Welcome"]),
    ("announcements", "Announcements", ["Announcements"]),
    ("role_panels", "Role panels", ["RolePanels"]),
    ("taskbook", "Taskbook", ["Taskbook"]),
    ("loa", "Leave of absence", ["Loa"]),
    ("join_ads", "Join ads", ["JoinAds"]),
]

FEATURE_KEYS = {k for k, _, _ in TOGGLEABLE}


def cogs_for(keys) -> list:
    """The cog class names behind a list of feature keys."""
    out = []
    for key, _label, cogs in TOGGLEABLE:
        if key in keys:
            out.extend(cogs)
    return out
