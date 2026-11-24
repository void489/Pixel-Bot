
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
        "date": "2026-11-24",
        "title": "Welcome message types: Normal, Embed and Image",
        "tags": ["Dashboard", "Members"],
        "summary": "A server now picks one of three welcome styles in Welcome & Auto Role: plain text (as before), an embed with the member's avatar, or a generated welcome image.",
        "details": [
            "Normal: unchanged. Servers saved before this change keep Normal automatically.",
            "Embed: the new member is pinged above a card with an editable title, description, footer and colour bar. Same placeholders as before ({membercount} is the member number). Their avatar is the picture on the right. In the title and footer {user} shows their name, because mentions can't render there.",
            "Image: a text line (the member is always pinged) plus a wide banner drawn by the bot with the member's round avatar, WELCOME and their name.",
            "Image background: the default is built in. A server can upload its own (PNG, JPEG or WebP, up to 5 MB); the bot crops and resizes it to 1000x400. A missing or broken file falls back to the default, and a failed image falls back to the text line, so a join never errors.",
            "Only the new member can be pinged. The settings of the types you are not using are kept when you switch.",
            "The page has a live preview for each type.",
        ],
        "files": [
            "cogs/welcome.py", "utils/welcome.py", "utils/welcome_image.py", "assets/welcome/default_bg.png",
            "database/models.py", "database/mongo.py", "dashboard/routes.py", "dashboard/templates/welcome.html",
        ],
    },
    {
        "date": "2026-11-22",
        "title": "New dashboard look and Streaming status",
        "tags": ["Dashboard", "Admin"],
        "summary": "The whole dashboard has a new look (Pixel Lab: square corners, one lime accent, pixel headings) with a light and a dark mode, and the admin page can now set a Streaming activity.",
        "details": [
            "Every dashboard page, the public home page, the legal pages and the admin pages share one design system, and phones get a scrolling top menu and card-style tables.",
            "Light / dark switch button: top of the page on phones, bottom of the left menu on desktop. The choice is remembered in the browser.",
            "Admin -> Bot activity: new activity type Streaming. It needs a https Twitch or YouTube link (Discord only shows the Streaming badge for those). The status list now calls Invisible \"Offline (invisible)\".",
            "New logo, favicon, site banner and Discord banner.",
        ],
        "files": [
            "dashboard/static/style.css", "dashboard/static/script.js", "dashboard/static/theme-init.js", "dashboard/static/logo.png",
            "dashboard/static/favicon.ico", "dashboard/templates/*.html", "dashboard/admin.py", "cogs/admin_tools.py",
        ],
    },
    {
        "date": "2026-11-14",
        "title": "Fix: deleted application types stuck in the application queue",
        "tags": ["Fix", "Staff"],
        "summary": "Pending applications whose application type was deleted no longer show in the queue.",
        "details": [
            "Those submissions can't be decided without their type, so they used to sit in the dropdown forever and block /applications removequeue.",
            "They are only hidden from the queue. Nothing is deleted; they stay in the dashboard's submission history.",
        ],
        "files": ["cogs/applications.py"],
    },
    {
        "date": "2026-11-14",
        "title": "Fix: autocomplete options sometimes not showing",
        "tags": ["Fix", "Commands", "Database"],
        "summary": "Dropdown options (giveaway end/reroll/delete, spawner types, taskbooks, change types, poll end, activity check, vouches) no longer randomly come up empty.",
        "details": [
            "Discord drops an autocomplete answer if the bot takes over about 3 seconds. The giveaway one loaded every giveaway ever made, which got slower over time.",
            "Giveaway autocomplete now searches and limits to 25 inside MongoDB, and the giveaways collection has indexes.",
            "Every autocomplete is wrapped with a 2.5 second safety limit: if it is slow or errors it logs a line starting with [autocomplete] instead of failing silently.",
        ],
        "commands": ["/giveaway end", "/giveaway reroll", "/giveaway delete", "/poll end",
                     "/strike add", "/strike list", "/promote", "/demote"],
        "files": ["utils/autocomplete.py", "database/models.py", "cogs/giveaways.py",
                  "cogs/taskbook.py", "cogs/staff_activity.py", "cogs/spawner_panel.py",
                  "cogs/staff_strikes.py", "cogs/utilities.py", "cogs/changes.py",
                  "cogs/vouches.py"],
    },
    {
        "date": "2026-11-13",
        "title": "Sidebar: Bot Changes moved under Setup Guide",
        "tags": ["Dashboard"],
        "summary": "Bot Changes is now the second sidebar tab, right below Setup Guide, and the Setup Guide no longer has an emoji.",
        "files": ["dashboard/templates/_sidebar.html", "dashboard/templates/setup.html",
                  "dashboard/bot_changes.py"],
    },
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
            "Off by default: with no queue set, requests post to the request channel as before.",
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
