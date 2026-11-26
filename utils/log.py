"""Shared logging setup for the bot and the dashboard.

One call in main.py configures the root logger; every module gets its own
named logger via get_logger(__name__). ERROR-level records are what the
admin page's Errors tab shows (the _ErrorCapture handler in
cogs/admin_tools.py), so real failures should be logged with
logger.error / logger.exception instead of printed.

Replaces the old bare print() calls: same console output, but with
timestamps, levels and logger names, and a LOG_LEVEL env override
(DEBUG / INFO / WARNING / ERROR; default INFO).
"""
import logging
import os
import sys

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def setup_logging(level: str = None) -> logging.Logger:
    """Configure the root logger once (safe to call more than once)."""
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(_FMT, datefmt=_DATEFMT))
        root.addHandler(handler)
        root.setLevel((level or os.getenv("LOG_LEVEL") or "INFO").upper())
        _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, making sure logging is configured first."""
    setup_logging()
    return logging.getLogger(name)
