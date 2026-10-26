# Ticket system upgrade: changelog

Everything below is optional and off or equal to today's behaviour until you edit a panel or
turn something on. No migration is needed: new fields are read with safe defaults.

## 1. Overflow categories
- Each ticket type has a main category plus up to 5 spare categories (Tickets page). A spare is
  typed by name: used if it exists, created when first needed, and deleted again when empty,
  exactly like the main category.
- A server-wide block (Tickets page) sets the main + spare categories for claim, spawner and
  application tickets, and for types without a category of their own.
- Every ticket kind uses it, under `category_lock`. If every category is full the member gets a
  short message and staff are told in the ticket log channel (once per 10 minutes).
- `utils/ticket_core.py`

## 2. Per-panel settings (on the ticket type)
- Channel name template (`{panel}` `{number}` `{username}` `{id}` ...), required roles, max open
  tickets per member per panel, welcome message and "ticket created" reply with placeholders,
  custom panel / close / claim / unclaim buttons, claiming switch, "only the claimer sees the
  ticket", transcript switch, linked panels. "Support roles" is the existing Allow Roles field.
- `utils/ticket_settings.py`, `dashboard/templates/tickets.html`

## 3. Priorities
- Low / Normal / High / Urgent: Priority button, `/ticket priority`, welcome-embed field,
  transcript + list column and filter, optional channel rename (emoji or word prefix).

## 4. Transcripts
- Search by member, staff, panel, text, dates, rating, priority. Rating, claimer and closer shown.
- Expiring, revocable share links (1 hour to 30 days), public read-only page `/t/<token>`.
- Per-panel access: everyone with dashboard access (default) / staff roles only / staff + opener.
- `dashboard/transcript_search.py`, `utils/transcript_access.py`

## 5. Analytics
- Ticket Analytics page: volume, first-reply time, close time, ratings, busiest panels, staff
  workload, date / kind / panel filters. Stored per ticket in `ticket_stats` at ticket events;
  one-click import of past tickets from transcripts.
- `utils/ticket_stats.py`, `dashboard/ticket_analytics.py`

## 6. Thread mode (per panel)
- Open tickets as private threads in a chosen channel. Close, claim, unclaim, add, remove,
  rename, priority, transcripts, ratings, auto-close and analytics work in both modes.
- Support roles get Manage Threads on the parent channel (a role can't be added to a thread).
- `utils/ticket_container.py`. Spawner / application / claim tickets stay channels.

## 7. Live web reply + automations
- **Live Tickets** (dashboard): open tickets list, live conversation view that updates by itself,
  reply box. Replies are posted by the bot as an embed with the staff member's name and avatar,
  with mentions limited to (optionally) the opener. Per-panel switch "Live replies" (off by
  default). A web reply counts as the first staff reply for analytics.
- **Automations** (dashboard + `cogs/ticket_automations.py`): trigger (ticket opened / keyword /
  no staff reply after N minutes) then up to 5 steps (set priority, rename, move panel, send
  message, add member). Once per ticket, never triggered by bots, failures skipped and logged.
- `dashboard/ticket_live.py`, `utils/ticket_automation.py`

## Phone / tablet layout fixes (dashboard)
Checked in a real browser at 360, 390, 768, 1024 and 1280 px wide.
- Tables on the ticket pages (Transcripts, Latest transcripts on the Tickets page, Analytics, Automations
  runs, share links, Live Tickets) are now small cards on phones and tablets (up to 820 px), the same idea as
  the Ticket Ratings page. Wider than that, a table that doesn't fit scrolls inside its box.
- Shared styles are in `dashboard/static/style.css` (section "Phones and tablets"): `table.cards`
  with `data-label`s, `.filters` bars, date pickers, `.tbl-scroll`.
- Transcript page: the menu is now the same swipeable strip as other pages (it used to wrap into a tall
  block), the tall header no longer sticks, long attachment names wrap.
- Live ticket view: compact header, long names wrap, the reply box stays at the bottom of the screen.
- Panel settings: button rows, toggles and dropdown texts fit a phone; secondary / delete buttons use the
  grey / red styles like the rest of the dashboard.
- Filter bars: compact on desktop, one control per line on phones.
- Already too wide in your original upload and fixed here: the Transcripts list and the Tickets page's
  "Latest transcripts" table.

## Data added (all optional)
- Ticket type: overflow_category_ids, button_label, max_open_tickets, required_role_ids,
  welcome_message, created_message, claiming_enabled, claim_only_visible, save_transcripts,
  linked_type_ids, buttons, transcript_access, thread_mode, thread_parent_id, thread_auto_archive,
  thread_invitable, web_reply, priority_enabled, priority_rename.
- Server settings: tickets.fixed_category_id, tickets.overflow_category_ids.
- Ticket record: number, priority, priority_prefix, opener_name, is_thread, thread_parent_id,
  automations_run. Transcript: priority, ticket_type_id. Rating record: channel_id.
- New collections: ticket_counters, ticket_stats, ticket_stats_meta, transcript_shares,
  ticket_automations, ticket_automation_runs.
- New command: `/ticket priority` (hybrid, `@command_check()`, in help and Command Permissions).

## Known limits
- Staff points / taskbook counters don't count replies sent from the dashboard (bot's own messages).
- Analytics only covers tickets from when it started, plus anything you import from transcripts.
- Imported tickets have no first-reply time. Old ratings (no channel id) aren't linked to analytics.
- Discord allows about 2 channel renames per 10 minutes; priority and automation renames can hit it.
- In thread mode: no `/ticket move`, no "only the claimer sees it", no categories.
- Live view polls every few seconds; text replies only (no attachments from the web).

## Found in the original upload (not changed)
- `cogs/staff_points.py` imports `staff_points_col`, `staff_ticket_stats_col` and
  `staff_points_board_col` from `database/mongo.py`, which doesn't define them, so that cog fails
  to load. Thread-ticket support was added to it but can't act until it loads.

## Check in a real server (can't be verified offline)
- Members need View Channel on the thread parent channel to see their thread.
- Manage Threads on the parent makes support roles see every ticket thread.
- The bot needs Create Private Threads, Send Messages in Threads and Manage Threads there, and
  Manage Permissions to set the staff overwrite itself.
