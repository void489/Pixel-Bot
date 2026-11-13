"""Data for the dashboard's "Bot Changes" page (sidebar -> Bot Changes).

EVERY change made to this bot gets an entry here - whoever makes it (a person or an AI).
Newest first: add the new entry at the TOP of CHANGES. See AGENTS.md for the rules.

Entry fields:
    date      "YYYY-MM-DD"
    title     short, plain words
    tags      any of: "Commands", "Dashboard", "Admin", "Staff", "Members", "Fix", "Database"
    summary   one or two sentences: what changed and why it matters
    details   list of short bullets (behaviour, settings, limits)       [optional]
    commands  list of command names, written like "/applications setqueue"  [optional]
    files     list of files touched                                     [optional]
"""

CHANGES = [
    {
        "date": "2026-11-13",
        "title": "Ticket transcript images are deleted after 7 days",
        "tags": ["Database", "Admin"],
        "summary": "Saved transcript images now expire after 7 days to keep the free MongoDB under its size limit.",
        "details": [
            "On startup and every 6 hours the bot deletes transcript images older than 7 days (all old ones are removed on the first run).",
            "Transcript text, messages and ratings are kept; only the images go.",
            "Old transcripts on the dashboard simply show no image instead of a broken one.",
        ],
        "files": ["cogs/transcript_cleanup.py", "database/models.py", "main.py"],
    },
    {
        "date": "2026-11-13",
        "title": "Bot Changes page",
        "tags": ["Dashboard"],
        "summary": "New sidebar tab that lists every change made to the bot, newest first.",
        "details": [
            "Every future change must be added to dashboard/bot_changes.py (rules in AGENTS.md).",
            "Search box and tag filter at the top of the page.",
            "AGENTS.md (also copied to CLAUDE.md, .cursorrules and .github/copilot-instructions.md) tells any AI editing this project to log changes here, register commands with @command_check() and update help.",
        ],
        "files": ["dashboard/bot_changes.py", "dashboard/routes.py",
                  "dashboard/templates/bot_changes.html", "dashboard/templates/_sidebar.html",
                  "AGENTS.md", "CLAUDE.md", ".cursorrules", ".github/copilot-instructions.md"],
    },
    {
        "date": "2026-11-13",
        "title": "Bot activity can show server and member counts",
        "tags": ["Admin"],
        "summary": "In the /admin page's activity text you can now use {servers} and {members}.",
        "details": [
            "{servers} = how many servers the bot is in.",
            "{members} = total members across all of those servers.",
            "Example: \"{members} members in {servers} servers\".",
            "Numbers refresh every 5 minutes (only when the text uses one of them).",
        ],
        "files": ["cogs/admin_tools.py", "dashboard/templates/admin.html"],
    },
    {
        "date": "2026-11-13",
        "title": "Application queue",
        "tags": ["Commands", "Staff"],
        "summary": "Review applications from ONE embed with a dropdown instead of one message per application.",
        "details": [
            "Pick an application in the dropdown to open it privately (only you see it), with the same question/answer embed and the same Accept / Deny / reason / History / Ticket buttons.",
            "Decided applications drop out of the dropdown. Dropdown shows the oldest 25.",
            "Off by default: with no queue set, applications still post to their submit channel.",
            "removequeue is blocked while applications are still pending in the queue.",
        ],
        "commands": ["/applications setqueue", "/applications removequeue"],
        "files": ["cogs/applications.py", "utils/review_queue_core.py", "database/models.py"],
    },
    {
        "date": "2026-11-13",
        "title": "Blacklist queue",
        "tags": ["Commands", "Staff"],
        "summary": "Review blacklist / unblacklist requests from ONE embed with a dropdown.",
        "details": [
            "Same private view and Accept / Deny buttons as before.",
            "Only Admins and the blacklist staff role can open items.",
            "With the queue on, decided requests are still logged in the old request channel (if set).",
            "Off by default: with no queue set, requests post to the request channel as before.",
        ],
        "commands": ["/blacklist setqueue", "/blacklist removequeue"],
        "files": ["cogs/utilities.py", "utils/review_queue_core.py", "database/models.py"],
    },
    {
        "date": "2026-11-13",
        "title": "Scam report queue",
        "tags": ["Commands", "Staff"],
        "summary": "Review /report submissions from ONE embed with a dropdown.",
        "details": [
            "Same private view and Accept Report / Reject Report buttons as before.",
            "Only Admins and the report staff role can open items.",
            "With the queue on, decided reports are still logged in the old report channel (if set).",
            "Off by default: with no queue set, reports post to the report channel as before.",
        ],
        "commands": ["/vouches setreportqueue", "/vouches removereportqueue"],
        "files": ["cogs/vouches.py", "utils/review_queue_core.py", "database/models.py"],
    },
    {
        "date": "2026-11-13",
        "title": "Admin help and Command Permissions for the new queue commands",
        "tags": ["Commands", "Admin"],
        "summary": "The six queue commands show in /admin help (new Blacklists page) and on the dashboard's Command Permissions page.",
        "details": [
            "They use @command_check(), so they register on Command Permissions by themselves and are admin-only until turned on.",
        ],
        "files": ["cogs/help.py"],
    },
]
