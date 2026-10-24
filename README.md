# Discord Bot

Base structure for a multi-system Discord bot (giveaways, applications, security,
vouch/counter-vouch, spawner panel, anti-nuke, anti-raid) with a
web dashboard and MongoDB. Deployed on bot-hosting (startup file: main.py).

## Structure
- `main.py` - bot entrypoint, loads cogs
- `cogs/` - one file per feature/system
- `utils/` - shared helpers (embeds, checks, API wrapper, logger)
- `database/` - MongoDB connection + models
- `config/` - settings & constants
- `dashboard/` - web dashboard (Flask)

## Setup
1. Copy `.env.example` to `.env` and fill in values.
2. `pip install -r requirements.txt`
3. `python main.py`

## Spawner panel
Buy/sell price panel with tickets (`cogs/spawner_panel.py`).

- `/spawner add <type> <emoji>` - add a spawner type
- `/spawner setprice <type> <sell|buy> <price>` - `sell` is what the customer receives
  when they sell to the server, `buy` is what they pay. Accepts `8.2m`, `1.5b`, `100k`,
  `8600000`. `off` stops a side. Open tickets for that spawner + side are recalculated
  and their openers pinged.
- `/spawner panel` - post the live panel (edits itself when prices or notes change)
- `/spawner remove <type>`

Category, allow roles, ping roles and the notes shown under the prices are set on the
dashboard's **Spawners** tab. Like every command, the `/spawner` commands are admin-only
until enabled on the **Command Permissions** page.

## Bot profile (per server)
Dashboard -> Bot Profile sets the bot's nickname, bio, avatar and banner for one server.
Saved in Mongo (`bot_profiles`), applied by `cogs/bot_profile.py`, and re-applied on startup
only if what's saved differs from what was last applied. Needs the Change Nickname permission
for the nickname.

## Giveaway games, polls, embeds, application blacklist
- `/giveaway membercount members prize [claim_time] [winners]` has no timer - it ends when
  the server hits that member count. `/giveaway fastestfinger prize time` (entrants race to
  type a code / press a button), `/giveaway guessflag prize` (first to name the flag in any
  channel) and `/giveaway doubleornothing prize time` (winner doubles or takes; odds 50% ->
  40% -> 30% -> 20% -> 10%). `/giveaway end` works on all of them; `/giveaway reroll` works on
  create, membercount, fastestfinger and doubleornothing (not rps, splitsteal or guessflag).
- `/ticket request-close [reason]` pings the ticket opener with Close Ticket / Don't Close buttons.
- `/giveaway rps` and `/giveaway splitsteal` take the same flags as `/giveaway create`
  (`prize`, `time`, `claim_time`; `winners` is ignored - games always have 2). When entries
  close, 2 winners play with hidden picks, then the winner(s) get the normal Claim button.
  The pick timer for each game is set on the dashboard's **Claim Tickets** page.
  Reroll is blocked for game giveaways.
- Claim Tickets page: **Ping mode** is either "Ping roles" (opener + roles) or "Ping host"
  (opener + the giveaway host). Allow Roles is unaffected.
- `/poll create` posts "Option N" buttons (one changeable vote each, live counts, stored in
  Mongo). `/poll end <message_id>` closes one.
- `/embeds create` (the old `/embeds`) and `/embeds edit <message_id or link>`. The old
  `embeds` permission is copied to `embeds create` automatically the first time the bot starts.
- `/blacklist add|remove` type is a dropdown: giveaway, ticket or application. Set the
  **Application blacklist role** on Config & Logs; those members can't open applications.

## Extra member / staff commands
All are `@command_check()` commands, so each shows on the dashboard's **Command Permissions**
page (admin-only until enabled) and in `/help`, `/staffhelp` or `/admin help` according to
`config/command_groups.py`.

- **Members** (`cogs/member_tools.py`): `/serverinfo`, `/userinfo`, `/avatar`, `/banner`, `/ping`,
  `/botinfo`, `/profile`, `/remindme`, `/translate`, `/rolelist`, `/suggest`, `/giveaways`,
  `/ticketstatus`, `/birthday set|view|remove`, `/coinflip`, `/roll`, `/8ball`, `/choose`,
  `/vouches top`, `/spawnercalc`.
- **Staff**: `/slowmode`, `/history`, `/clearwarns`, `/note add|list|remove`, `/staffprofile`,
  `/suggestions approve|deny` (`cogs/staff_tools.py`).
- **Admin**: `/nuke`, `/lockdown`, `/unlockdown`, `/suggestions setchannel`, `/birthday setchannel`.
- Suggestions and birthdays need their channel set once with the two `setchannel` commands.
  `/botinfo` shows a Support button if `SUPPORT_URL` is set in the environment.

## Auto-mod, invite tracker, tempban, stat channels, reaction role modes
All are `@command_check()` commands: they show on the dashboard's **Command Permissions** page
(admin-only until you enable them there) and in `/help`, `/staffhelp` or `/admin help` according
to `config/command_groups.py` and `ADMIN_CATEGORIES` in `cogs/help.py`.

- **Auto-mod** (`cogs/automod.py`) is **dashboard only - there are no `/automod` commands.** Everything is on
  the dashboard's **Auto-Mod** tab: master switch (off by default), log channel, the six rules (spam, mentions,
  invites, links, caps, badwords) with their limits, the bad-word list, allowed link domains, exempt roles /
  channels and the strike ladder. Changes apply within ~30s. Every violation deletes the message, saves an
  `automod` mod case (visible in `/history`) and adds a strike; the ladder (default 3 strikes -> 10 min timeout,
  kick/ban off) is set on the same tab. Staff with Manage Messages / Administrator are never checked.
  **Strikes are stored in MongoDB** (`automod_strikes`, idle docs expire after 25h), so a restart or redeploy no
  longer wipes them. The tab lists everyone with active strikes and lets you clear one member or everyone.
  The old `/automod ...` rows are removed from Command Permissions automatically on startup.
- **Tempban** (`cogs/moderation.py`, **admin help -> Moderation**; it is an *Admin* command on the Command
  Permissions page, not a Staff one): `/tempban <member> <duration> [reason]` (up to 365d) and `/tempbans`.
  Expiry is checked every 30s from the `temp_bans` collection, so it survives restarts; `/unban` cancels a
  pending expiry. Permissions are stored per command name, so anything you
  already enabled for `/tempban` keeps working - it just moved groups.
- **Invite tracker** (`cogs/invite_tracker.py`): members get `/invites [member]` and `/topinviters`
  (help -> Community); admins use `/invitetracker toggle|channel|status` (admin help -> Roles & utilities).
  Needs the **Manage Server** permission. Off until `/invitetracker toggle on`; only joins after that are known.
- **Stat channels** (`cogs/utilities.py`, admin help -> Roles & utilities): `/statchannel add|remove|list|
  refresh`. Stats: members, humans, bots, boosts, roles, channels, voice. Updated every 10 minutes
  (Discord's rename limit). Needs Manage Channels.
- **Reaction role modes** (`cogs/utilities.py`): `/reactionrole mode <message_id> <normal|unique|verify>`
  and `/reactionrole clear <message_id>`. `unique` = one role per message (picking another swaps it and
  removes the old reaction - needs Manage Messages); `verify` = un-reacting keeps the role.

## Role panels (buttons / dropdown) - the newer style
`cogs/role_panels.py`, set up on the dashboard's **Role Panels** tab (no commands). The older emoji
`/reactionrole` system is unchanged and still works.

- **Style**: *Buttons* (one per role - click to get it, click again to remove it) or a *Dropdown* menu (members
  pick the roles they want and the menu applies exactly that choice; picking nothing removes them all).
- **Mode**: *Multiple* (any number of the panel's roles) or *One only* (picking another role swaps it).
- Per role: label, emoji (normal or custom), a description (dropdown) and a button colour. Up to 25 roles per
  panel, and any number of panels. Embed title / text / colour are editable, with an optional list of the roles
  under the text.
- **Save & post** queues a task; the bot posts the message (or edits it in place if it was already posted, or
  moves it if you changed the channel) and shows the result - posted, or the exact error - on the panel.
  Deleting a panel also deletes its message.
- Components use fixed custom IDs and are re-registered at startup, so panels keep working after restarts.
  The roles a click affects are read from the database at click time.
- The bot needs **Manage Roles** and its top role must be above every role on the panel (managed/bot roles
  can't be given). The dashboard warns about roles the bot can't hand out.

## Staff strikes (per Change type)
`cogs/staff_strikes.py` + the dashboard's **Strikes** tab. Strikes use the same **types** as the
Changes page (staff, builder, ...), so a strike only counts inside its own type and the card is
posted in that type's announce channel.

- `/strike add <member> <type> [reason]` - give a strike (red card in the type's channel + a DM to the
  member; closed DMs are fine). Works on members on LOA and on people who already left.
- `/strike remove <number> [reason]` - delete one strike by its number.
- `/strike list <member> [type]` - a member's strikes, grouped by type. `/staffprofile` also shows
  their strike counts.
- Like every command these are admin-only until enabled on **Command Permissions** (admin help ->
  Staff management).

Per type, on the **Strikes** tab:
- **Limits**: "at N strikes -> demote X ranks". The highest limit reached wins. Limits only run when a
  *new* strike is added, never when you edit them. After a limit demotes someone their strikes in that
  type are reset to 0. Members on LOA (ranks are paused), people not in the server, and failed
  demotions are skipped with a note - the strike still counts.
- **Reset strikes on promotion / on demotion** ticks (both off by default). They live in the shared
  promote/demote code in `cogs/changes.py`, so `/promote`, `/demote`, accepted applications and strike
  limits all behave the same.
- Delete a single strike, reset one member, or reset the whole type. Deleted / reset strikes are gone
  for good; a small trail is kept in `staff_strike_audit` and shown at the bottom of the tab.

Storage: strikes are one document each in `staff_strikes` (with `type_id` + a snapshot of the type
name, so deleting a type never loses data); settings are in `guild_settings.strikes.types.<type_id>`.

## Privacy Policy & Terms of Service (Discord verification)
Public pages, no login needed: `<your dashboard URL>/privacy` and `<your dashboard URL>/terms`
(`dashboard/templates/privacy.html` and `terms.html`, shared layout `_legal_base.html`).
Links to both are in the footer of the dashboard home page and admin login, and at the bottom of the
sidebar on every server page. Paste the two URLs into the Discord Developer Portal (your application
-> General Information -> Terms of Service URL / Privacy Policy URL).

- The contact email shown on both pages is `CONTACT_EMAIL` (env var, defaults to the owner's email in
  `config/settings.py`). No phone number is shown.
- "Last updated" is the `LEGAL_UPDATED` string next to the routes in `dashboard/routes.py` - change it
  whenever you edit the wording.
- If you add a feature that stores new kinds of data or uses a new third-party service, add it to
  section 1 / 5 of the privacy policy so it stays accurate.

## Live help menus & Command Permissions
- **All help commands live under Admin** on the Command Permissions page (`/help`, `/staffhelp`,
  `/admin`, `/admin help` - see `HELP_COMMAND_GROUPS` in `config/command_groups.py`). Note the Admin
  group's bulk switch also affects them unless you tick "not affected by group changes" on a command;
  to let everyone use `/help`, enable that one command and pick its roles.
- **Posted help menus update themselves.** A menu posted by an admin (e.g. in a bot-info channel) is
  remembered (`help_panels` collection) and edited automatically - at most every 5 minutes, and only if
  its categories/counts changed - when commands are added or removed. Menus that were posted before this
  feature become live the first time an admin picks a category from them. Up to 10 menus per type are
  tracked per server (newest kept); members running `/help` don't create tracked menus.
- The same 5-minute loop re-registers new/removed commands on the Command Permissions page, so it no
  longer needs a restart to catch up. Each category's contents were already read live when someone
  clicks it.

## Auto nick change, ticket ratings
- **Auto nick change** (Changes page, each type's Settings): promotions, accepted applications and
  demotions rename the member to `Rank nick | Display name`; demoted to #0 resets the nickname.
  The bot also fixes nicknames whenever a ladder role is added/removed by hand, and each type has a
  **Sync nicknames** button to fix everyone already ranked. No commands.
- **Ticket ratings** (`cogs/ticket_ratings.py`): closing a claimed ticket puts 1-5 buttons in the
  opener's close DM, credited to the claimer. Shown on the staff member's taskbook, in
  `/staffprofile` and on the dashboard's **Ticket Ratings** page. `/taskbook reset` zeroes only the
  taskbook's rating figures; the dashboard keeps the history.

## Transcripts page, /ticket lookup
- Dashboard -> **Transcripts**: search closed tickets by user, staff, ticket name, reason or user ID;
  filter by rating and date; optional search inside messages; paginated. New transcripts also
  store who claimed the ticket and its rating (older ones just show "-"). The Tickets page now only
  lists the latest 10 and links here.
- `/ticket lookup <member or text>` (staff group, so it shows in `/staffhelp` and on Command
  Permissions): a member's past tickets (mention / ID) or a search by name, staff or reason, with
  dashboard links. Replies only to the person who ran it.
- The transcript viewer now uses the dashboard's green theme (very old HTML-stored transcripts keep their original look).
