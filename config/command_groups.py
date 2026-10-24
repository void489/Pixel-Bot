"""Which commands belong to the Members / Staff / Admin groups.

ONE source of truth, used by both /help (cogs/help.py) and the dashboard's Command
Permissions page, so the two can never disagree. To move a command to another group,
edit the lists below; anything not listed as Members or Staff is Admin (same as
/admin help). /tempban and /tempbans are deliberately NOT listed, so they are Admin commands
(listed under /admin help -> Moderation).
"""

# {help category: [command names]}  - the categories are the dropdown pages in /help.
MEMBER_COMMANDS = {
    "Members": ["rank", "leaderboard", "profile", "calc", "vouches view", "vouches top",
                "lookup", "report", "afk", "membercount"],
    "Info": ["serverinfo", "userinfo", "avatar", "banner", "ping", "botinfo"],
    "Utility": ["remindme", "translate", "rolelist", "spawnercalc"],
    "Community": ["suggest", "giveaways", "ticketstatus",
                  "birthday set", "birthday view", "birthday remove",
                  "invites", "topinviters"],
    "Fun": ["coinflip", "roll", "8ball", "choose"],
}

STAFF_COMMANDS = {
    "Tickets": ["ticket rename", "ticket close", "ticket request-close", "ticket priority", "ticket add",
                "ticket remove", "ticket move", "ticket lookup"],
    "Moderation": ["mute", "unmute", "warn", "warns", "warnings remove", "clearwarns",
                   "ban", "unban", "kick", "softban", "purge", "lock", "unlock", "slowmode",
                   "history", "note add", "note list", "note remove"],
    "Staff tools": ["staffprofile", "suggestions approve", "suggestions deny"],
    "LOA": ["loa request", "loa end"],
    "Blacklists": ["blacklist add", "blacklist remove"],
    "Giveaways & polls": ["giveaway create", "giveaway membercount", "giveaway fastestfinger",
                          "giveaway guessflag", "giveaway guessthelanguage",
                          "giveaway doubleornothing",
                          "giveaway rps", "giveaway splitsteal",
                          "giveaway end", "giveaway reroll", "giveaway delete",
                          "poll create", "poll end"],
}

# The help commands are hidden from the help lists but still need a group: ALL of them live
# under Admin on the Command Permissions page, whichever menu they open.
HELP_COMMAND_GROUPS = {
    "help": "admin",
    "staffhelp": "admin",
    "admin": "admin",
    "admin help": "admin",
}

GROUP_ORDER = ("members", "staff", "admin")
GROUP_INFO = {
    "members": ("🙋", "Members", "Commands for everyone (the /help list)."),
    "staff": ("🛡️", "Staff", "Day-to-day staff commands (the /staffhelp list)."),
    "admin": ("⚙️", "Admin", "Setup and management commands (the /admin help list), plus the help commands themselves."),
}

_MEMBER_NAMES = {n for names in MEMBER_COMMANDS.values() for n in names}
_STAFF_NAMES = {n for names in STAFF_COMMANDS.values() for n in names}


def group_for(command_name: str) -> str:
    """'members' | 'staff' | 'admin' for a command's qualified name."""
    if command_name in HELP_COMMAND_GROUPS:
        return HELP_COMMAND_GROUPS[command_name]
    if command_name in _MEMBER_NAMES:
        return "members"
    if command_name in _STAFF_NAMES:
        return "staff"
    return "admin"
