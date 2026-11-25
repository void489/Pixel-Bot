# Rules for anyone (or any AI) changing this bot

Read this before editing. These rules apply to every change, big or small.

## 1. Log every change on the Bot Changes page (required)
The dashboard has a sidebar tab **Bot Changes**. Its data is the `CHANGES` list in
`dashboard/bot_changes.py`.

After you change anything in this project, add ONE entry at the TOP of `CHANGES`
(newest first). Fields: `date` (YYYY-MM-DD), `title`, `tags`, `summary`, and where they
apply `details`, `commands`, `files`. The field list is at the top of that file.
A change without an entry is not finished. Also tell the user which entry you added.

## 2. New commands: use the cog's auto-registration, and update help
- Put the command in the cog that owns the feature (applications -> `cogs/applications.py`,
  blacklists -> `cogs/utilities.py`, vouches and reports -> `cogs/vouches.py`, and so on).
  Do not make a new cog for something an existing cog already owns.
- Put `@command_check()` (from `utils/permissions.py`) on every hybrid command and
  subcommand, below the `.command(...)` decorator. That alone:
  - shows the command on the dashboard's **Command Permissions** page (auto-registered,
    never add it by hand),
  - makes it admin-only until it is turned on there.
- Admin help (`/admin help`, `cogs/help.py`) lists every `@command_check()` command that
  is not in `config/command_groups.py`, grouped by the command's first word:
  - If the first word is new, add it to `ADMIN_CATEGORIES` in `cogs/help.py`
    (and to `ADMIN_ORDER` / `EMOJI` if it is a new page).
  - Add a clear one-line description to `OVERRIDES` in `cogs/help.py` when the short
    description is not enough.
  - A command for everyone or for staff goes in `MEMBER_COMMANDS` / `STAFF_COMMANDS` in
    `config/command_groups.py` instead (that file is also what Command Permissions uses).
- Mention the command in the "commands" list of your Bot Changes entry.

## 3. Review queues (applications, blacklist, scam reports)
There are three separate queues, one per type, and each belongs to its own cog.
Shared plumbing only is in `utils/review_queue_core.py`. Do not merge them into one.
Rules of thumb: queue off = the old per-channel behavior must keep working; the queue's
buttons are the same persistent buttons the old messages used.

## 4. Dashboard pages
- Add the route in `dashboard/routes.py` (start with the `_is_authed` and
  `_require_guild_access` checks like every other route) and a template in
  `dashboard/templates/`.
- Add ONE line to `items` in `dashboard/templates/_sidebar.html` to get the sidebar tab.
- New settings read and write through `dashboard/db.py` (sync) and `database/models.py`
  (async, used by the bot). New fields must have safe defaults so old data still works.

## 5. Persistent buttons and dropdowns
Buttons that must survive restarts are `discord.ui.DynamicItem`s registered in the cog's
`cog_load` with `bot.add_dynamic_items(...)`. A persistent dropdown view is registered with
`bot.add_view(...)` in `cog_load`. Never rely on in-memory state for them.

## 6. Before you finish
- Run `python -m py_compile` on every Python file you touched.
- Keep changes small and in the existing style. Do not reformat or rewrite files you
  are not changing.
- If a request is unclear, ask first instead of guessing.
- Never commit secrets (`.env`, tokens, passwords).
