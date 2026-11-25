"""Flask app factory / entrypoint for dashboard (run via gunicorn on Railway: dashboard.app:app)."""
import os
import secrets
from datetime import timedelta
from flask import Flask, jsonify, redirect, request, session, url_for
from flask_wtf.csrf import CSRFProtect, CSRFError
from werkzeug.middleware.proxy_fix import ProxyFix
from config import settings
from dashboard.auth import init_discord_oauth
from dashboard.routes import register_routes
from dashboard.admin import admin_bp

# Allow OAuth over plain http for LOCAL testing only. This is OFF by default and only
# ever switches on for a localhost redirect URL, so a real (https) deployment can never
# end up accepting insecure OAuth. To test locally, set DASHBOARD_ALLOW_INSECURE_LOCAL=1.
_redirect = settings.DASHBOARD_REDIRECT_URI or ""
if os.getenv("DASHBOARD_ALLOW_INSECURE_LOCAL", "0") == "1" and _redirect.startswith(
        ("http://localhost", "http://127.0.0.1")):
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

app = Flask(__name__)

# The session key signs the login cookie, so it must be a long, fixed, secret value
# (Railway > Variables > DASHBOARD_SECRET_KEY). If it is missing or weak we do NOT use it
# (anyone could forge a login with "change-me"); we use a random temporary key instead.
# That is safe, but everyone gets logged out on each restart until a real key is set.
_key = settings.DASHBOARD_SECRET_KEY or ""
if len(_key) < 16 or _key.strip().lower() in {"change-me", "changeme", "secret", "password"}:
    print("[dashboard] WARNING: DASHBOARD_SECRET_KEY is missing, too short (<16) or a default "
          "value. Using a random temporary key - everyone is logged out on every restart. "
          "Set DASHBOARD_SECRET_KEY to a long random string in your Railway variables.")
    _key = secrets.token_hex(32)
app.secret_key = _key

# --- Stay logged in -------------------------------------------------------
# By default Flask's session cookie is deleted when the browser closes, which is
# why you had to log in again every time. Making the session permanent gives the
# cookie a real expiry, and it is renewed on every visit, so you only get logged
# out after this many days without opening the dashboard (or if you press Logout).
# NOTE: DASHBOARD_SECRET_KEY must be a fixed value in your env vars. If it changes
# between restarts/deploys, every saved login becomes invalid.
_SESSION_DAYS = int(os.getenv("DASHBOARD_SESSION_DAYS", "30"))
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(days=_SESSION_DAYS),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",  # still sent when Discord redirects back to /callback
    SESSION_COOKIE_SECURE=(settings.DASHBOARD_REDIRECT_URI or "").startswith("https://"),
)


@app.before_request
def _keep_session_permanent():
    session.permanent = True

# Railway (and most PaaS providers) terminate HTTPS at their edge proxy and forward
# requests to gunicorn as plain HTTP internally, adding X-Forwarded-Proto: https.
# Without this, Flask/oauthlib see the request as insecure http and OAuth login
# fails with "InsecureTransportError: OAuth 2 MUST utilize https", even though the
# real public-facing connection is https. ProxyFix makes Flask trust that header
# so request.url correctly reports https behind Railway's proxy.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

discord_session = init_discord_oauth(app)
register_routes(app, discord_session)
app.register_blueprint(admin_bp)

# --- CSRF protection ------------------------------------------------------
# Every POST form carries a hidden csrf_token (added in the templates) and the one fetch()
# POST sends it as the X-CSRFToken header. The /admin blueprint keeps its own check.
app.config.update(WTF_CSRF_TIME_LIMIT=None, WTF_CSRF_SSL_STRICT=False)
csrf = CSRFProtect(app)
csrf.exempt(admin_bp)


@app.errorhandler(CSRFError)
def _csrf_failed(_err):
    msg = "Your session expired or the page was open too long. Go back, refresh the page and try again."
    if request.is_json:
        return jsonify(ok=False, error=msg), 400
    return ("<!doctype html><meta charset=utf-8><title>Security check failed</title>"
            "<body style=\"font-family:system-ui;background:#0b0c0e;color:#d4d4da;padding:60px 24px;"
            "max-width:560px;margin:auto\"><h2 style=\"color:#fafafa\">Security check failed</h2>"
            f"<p>{msg}</p><p><a style=\"color:#86efac\" href=\"{url_for('dashboard.index')}\">Back to the dashboard</a></p>",
            400)

# If Discord rejects the saved token (revoked, or the refresh token finally
# expired), send the user through login again instead of showing an error page.
try:
    from flask_discord.exceptions import Unauthorized as _DiscordUnauthorized

    @app.errorhandler(_DiscordUnauthorized)
    def _reauthenticate(_err):
        session.clear()
        return redirect(url_for("dashboard.login"))
except ImportError:
    pass

if __name__ == "__main__":
    # Local use only. The debugger lets anyone who can reach it run code, so it is opt-in.
    app.run(host="0.0.0.0", port=settings.DASHBOARD_PORT, debug=os.getenv("DASHBOARD_DEBUG") == "1")

