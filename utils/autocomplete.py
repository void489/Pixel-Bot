"""Safety wrapper for slash-command autocomplete callbacks.

Discord throws an autocomplete answer away if the bot takes longer than ~3 seconds, and the
user just sees an empty dropdown (nothing in the logs). This wrapper gives the callback
2.5 seconds; if it is too slow or raises, it logs one line and returns no options."""
import asyncio
import functools

AUTOCOMPLETE_TIMEOUT = 2.5


def safe_autocomplete(func):
    @functools.wraps(func)
    async def wrapper(*args, **kwargs):
        try:
            return await asyncio.wait_for(func(*args, **kwargs), AUTOCOMPLETE_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[autocomplete] {func.__qualname__} took longer than "
                  f"{AUTOCOMPLETE_TIMEOUT}s - no options shown")
        except Exception as e:
            print(f"[autocomplete] {func.__qualname__} failed: {type(e).__name__}: {e}")
        return []
    return wrapper
