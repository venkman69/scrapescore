"""
Shared helper for obtaining a session cookie for Playwright tests.

Priority:
  1. .sesskey file in project root — real OAuth session cookie (user's own data)
  2. /api/test-auth endpoint — synthetic test-user cookie (test data only)

Note: .sesskey is also used by FastHTML as the server's signing key. The user
manages this file directly; when it contains a valid OAuth session cookie that
was signed with the current server key, it works for both purposes.

Usage:
    from tests.session_helper import add_session_cookie

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context()
        add_session_cookie(ctx)
        page = ctx.new_page()
"""

from pathlib import Path

BASE_URL = "http://localhost:5001"
_SESSKEY_PATH = Path(__file__).parent.parent / ".sesskey"


def get_session_cookie(base_url: str = BASE_URL) -> str:
    """Return a valid session_ cookie value, preferring .sesskey over test-auth."""
    if _SESSKEY_PATH.exists():
        value = _SESSKEY_PATH.read_text().strip()
        if value:
            return value

    import urllib.request
    import json
    with urllib.request.urlopen(f"{base_url}/api/test-auth") as resp:
        return json.loads(resp.read())["session_cookie"]


def add_session_cookie(context, base_url: str = BASE_URL) -> None:
    """Convenience: add the session cookie to a Playwright browser context."""
    context.add_cookies([{
        "name": "session_",
        "value": get_session_cookie(base_url),
        "domain": "localhost",
        "path": "/",
    }])
