# Deployment

## Railway (current setup)

One Railway service runs the whole thing. `main.py` starts the Discord bot on
the main asyncio loop and the Flask dashboard in a background thread, so a
single service is enough - no worker/web split needed.

- **Start command**: `python main.py` (the `Procfile` in this repo says the
  same thing; if your service already has a start command configured there, it
  wins).
- **Port**: the dashboard binds to whichever env var the platform injects, in
  this order: `SERVER_PORT`, then `PORT`, then `DASHBOARD_PORT`.
- **Variables**: everything in `.env.example` - at minimum `DISCORD_TOKEN`,
  `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `MONGO_URI`, and a long random
  `DASHBOARD_SECRET_KEY` (16+ chars; without it every restart logs everyone
  out). Set `ADMIN_USERNAME` / `ADMIN_PASSWORD` to turn the `/admin` page on.
- **Logs**: all modules log through `logging` with timestamps and levels to
  stdout, so the Railway log viewer can be filtered. `LOG_LEVEL` (DEBUG /
  INFO / WARNING / ERROR, default INFO) changes the verbosity. ERROR-level
  records also land in the admin page's **Errors** tab (kept 7 days).

## Tests

```
pip install -r requirements.txt pytest
python -m pytest
```

CI runs the same suite on every push (`.github/workflows/tests.yml`). The
tests cover the pure-logic modules that need no network or database:
spawner price parsing, audit-log diffs and the guessflag answer matcher.

## Docker (optional)

`Dockerfile.example` is a working single-container build. It is deliberately
not named `Dockerfile`: Railway builds with a Dockerfile automatically when it
exists, which would silently change your current deployment path. To adopt it,
rename it to `Dockerfile`, commit, and watch the first build.

## After any change

Per `CLAUDE.md`: run `python -m py_compile` on every file you touched, and add
an entry to the Bot Changes page (`dashboard/bot_changes.py`).
