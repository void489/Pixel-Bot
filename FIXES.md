# What was fixed (2026-11-26)

Follow-up to the project analysis report. Every fix below is also recorded on
the dashboard's **Bot Changes** page (`dashboard/bot_changes.py`), as CLAUDE.md
requires.

## Fixed

1. **Structured logging** (was: 311+ bare `print()` calls)
   - New `utils/log.py`: one root setup, `logger = get_logger(__name__)` per module,
     timestamps + level + logger name on stdout, `LOG_LEVEL` env override.
   - Every print call in `cogs/ utils/ database/ dashboard/ config/ main.py` was
     converted (314 calls in 45 files), keeping each message's wording.
     Error-looking messages log at ERROR (they now show up on the admin **Errors**
     tab like the rest of discord.py's error output), warnings at WARNING, the rest at INFO.

2. **Global error handlers** (was: unhandled command errors gave users no feedback)
   - `cogs/events.py` now installs a global `on_command_error` listener and takes over
     `bot.tree.on_error` for slash commands (restored on cog unload).
   - Unhandled errors are logged with traceback (ERROR level → admin Errors tab) and the
     user gets a short ephemeral "something went wrong" embed instead of Discord's
     generic response. Check failures (maintenance mode, switched-off features, command
     permissions) are untouched - those checks already message the user.

3. **Test suite + CI** (was: no tests at all)
   - `tests/` with 57 pytest cases over the pure-logic modules: spawner price parsing
     (`utils/spawner_math.py`), audit-log change rendering (`utils/audit_diff.py`) and
     guessflag matching (`utils/flags_data.py`).
   - `pytest.ini`, root `conftest.py`, and `.github/workflows/tests.yml` running the
     suite on every push.

4. **Deployment files** (was: build/start config lived only in Railway's UI)
   - `Procfile` (`web: python main.py`), `DEPLOYMENT.md` (Railway setup, ports, env vars,
     logging, testing), `Dockerfile.example` - deliberately *not* named `Dockerfile`
     so Railway's current build path doesn't silently change; rename to adopt.

5. **`.env.example` sanitized** (was: carried live deployment identifiers)
   - Placeholder client id, placeholder dashboard URL, `MONGO_DB_NAME=pixel_bot`
     (was the old project name). Live deployments are unaffected - values stay in env vars.

## Checked and left as-is

- **Admin login brute-force protection** - already implemented in `dashboard/admin.py`
  (per-IP failed-attempt tracking with lockout, timing-safe comparisons). An earlier
  review missed this; no change needed.
- **3-second dashboard task pollers** - consolidating them into one dispatcher or change
  streams is a behaviour-affecting refactor that needs live testing against a real bot
  run; not safe to do blind. (Review recommendation #5 remains open.)
- **Flask-Discord replacement** - OAuth flows cannot be verified without live Discord
  credentials; replacing the library blind would risk locking everyone out of the dashboard.
- **routes.py / models.py split** - pure refactor with regression risk and no user-facing
  benefit; better done incrementally when those files are next touched.
- **`default_member_permissions` for admin-only commands** - intentionally NOT applied:
  commands are enabled per guild (and for non-admin roles) from the dashboard, and a
  Discord-level permission would hide them from those roles.

## Verification

- `python -m py_compile` on all 93 Python files: pass
- `python -m pytest`: 57 passed
- Import smoke test of `main` + all 31 cogs + core dashboard modules: pass
- The bot itself was not started (needs a real Discord token + MongoDB), so the
  error handlers and logging conversion are verified statically (compile/import)
  and the converted log lines were reviewed in place.
