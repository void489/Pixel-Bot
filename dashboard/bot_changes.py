
"""Data for the dashboard's "Bot Changes" page (sidebar -> Bot Changes).

EVERY change made to this bot gets an entry here - whoever makes it (a person or an AI).
Newest first: add the new entry at the TOP of CHANGES. See CLAUDE.md for the rules.

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
        "title": "Promotions, demotions and strikes ping the member (no DMs)",
        "tags": ["Staff", "Dashboard"],
        "summary": "Promotion, demotion and strike cards now ping the member in the type's channel, above the embed. Nobody is DMed for these any more.",
        "details": [
            "The ping is the message text above the card, so it really notifies. Only that one member can be pinged, never @everyone or roles.",
            "Covers /promote, /demote, accepted applications, staff automation, demotions from the strike rules, /strike add, activity-check strikes, and the \"strike removed\" card.",
            "Strike DMs are gone (the strike, strike-removed and activity-check DMs), so \"couldn't DM them\" warnings no longer appear.",
            "Strikes tab: the \"DM the member when a strike expires\" option is removed. Expired strikes are still deleted automatically and shown as \"expired\".",
            "If a type has no channel set on the Changes page, nothing is announced (so nobody is pinged), as before.",
            "A strike that triggers a demotion posts two cards, so the member is pinged on each.",
        ],
        "commands": ["/strike add", "/strike remove", "/promote", "/demote"],
        "files": [
            "cogs/changes.py", "cogs/staff_strikes.py", "cogs/staff_activity.py", "utils/strikes_core.py",
            "dashboard/routes.py", "dashboard/templates/strikes.html", "README.md",
        ],
    },
    {
        "date": "2026-11-24",
        "title": "Admin page tabs, and support link + contact email moved to the admin page",
        "tags": ["Admin", "Dashboard", "Members"],
        "summary": "/admin now has two tabs: Main (bot activity and the new Links & contact section) and Servers (the server list). The support server link and contact email are no longer hardcoded: they are set on the admin page.",
        "details": [
            "Servers tab: the same list, filter, Manage, Get Invite and Leave as before. Discord is only asked for the server list when this tab is open, so Main loads faster.",
            "Links & contact (Main tab): Support server link (must be https) and Contact email. Leave one empty to hide it. Both apply within seconds.",
            "The Support link shows in /botinfo, the dashboard sidebar and footer, and the Setup Guide. The email shows on the public Privacy Policy and Terms pages; with no email they point to the support link instead.",
            "The old SUPPORT_URL and CONTACT_EMAIL environment variables and their built-in defaults are gone. Until you set them on the admin page, the Support link and the email are hidden.",
            "Stored in Mongo bot_settings (_id = site). New file utils/site_settings.py holds the checks.",
        ],
        "files": [
            "dashboard/admin.py", "dashboard/templates/admin.html", "dashboard/db.py", "dashboard/routes.py",
            "dashboard/templates/privacy.html", "dashboard/templates/terms.html", "cogs/member_tools.py",
            "database/models.py", "utils/site_settings.py", "config/settings.py", ".env.example", "README.md",
        ],
    },
    {
        "date": "2026-11-24",
        "title": "Dashboard menu reordered",
        "tags": ["Dashboard"],
        "summary": "The left menu is grouped by topic instead of one long mixed list. Setup Guide is first and Bot Changes second. No page was added, removed or renamed.",
        "details": [
            "Order: Setup Guide, Bot Changes, then server settings (Config & Logs, Command Permissions, Bot Profile, Security, Auto-Mod, Moderation, Blacklist Requests, Audit Log).",
            "Then Join & Leave, Join Ad, Announcements; all the ticket pages together; Applications and Submissions; community pages (Vouches, Spawners, Role Panels, Leveling); and the staff pages last.",
        ],
        "files": ["dashboard/templates/_sidebar.html"],
    },
    {
        "date": "2026-11-24",
        "title": "Join & Leave tab and Ping on Join (ghost ping)",
        "tags": ["Dashboard", "Members"],
        "summary": "The Welcome & Auto Role tab is now called Join & Leave, since it holds Welcome, Leaves, Ping on Join and Auto Role. New Ping on Join section: pick channels, and when someone joins the bot pings them in each one and deletes the message right away.",
        "details": [
            "New section sits between Leaves and Auto Role. Pick one or more channels (up to 10); leave empty to turn it off.",
            "On join the bot sends the member's ping in every picked channel and deletes it at once, so they get the notification and the channels stay clean.",
            "Only the joiner is pinged (never @everyone or roles). Bots that join are skipped. A channel the bot can't use is skipped and the others still run.",
            "The bot needs View Channel, Send Messages and Manage Messages in each picked channel.",
            "Servers saved before this change have Ping on Join off. The page address and the Command Permissions page are unchanged.",
        ],
        "files": [
            "cogs/welcome.py", "utils/welcome.py", "database/models.py", "dashboard/routes.py",
            "dashboard/templates/welcome.html", "dashboard/templates/_sidebar.html",
        ],
    },
    {
        "date": "2026-11-24",
        "title": "Leaves: a leave message with the same three types",
        "tags": ["Dashboard", "Members"],
        "summary": "Welcome & Auto Role has a new Leaves section under Welcome. It posts when a member leaves and has the same Normal, Embed and Image types as Welcome.",
        "details": [
            "Its own channel and its own text for each type, saved separately from Welcome.",
            "Nobody is pinged (the member is gone), so {user} is just their name. {membercount} is the member count after they left.",
            "Image type: the banner says GOODBYE and has its own uploadable background (same limits and fallbacks as Welcome).",
            "Defaults: \"{username} left {server}\" with a red colour bar for the embed. Bots leaving are ignored.",
        ],
        "files": [
            "cogs/welcome.py", "utils/welcome.py", "utils/welcome_image.py", "database/models.py",
            "dashboard/routes.py", "dashboard/templates/welcome.html",
        ],
    },
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
