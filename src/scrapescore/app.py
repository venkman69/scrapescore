"""
FastHTML application for job_score with Google OAuth.

Launch:
    PYTHONPATH="./src" uv run python -m scrapescore.app

Google OAuth setup:
    1. Go to https://console.cloud.google.com/
    2. Create a project (or select existing)
    3. Enable "People API"
    4. Go to Credentials > Create Credentials > OAuth 2.0 Client ID
    5. Application type: "Web application"
    6. Add authorized redirect URI: http://localhost:5001/redirect
    7. Download the JSON file and save to path configured in
       config.yaml under storage_dirs.google_oauth_secrets_path
"""

import asyncio
import logging
import sys
from pathlib import Path

from fasthtml.common import *
from fasthtml.oauth import GoogleAppClient, OAuth
from monsterui.all import *

from scrapescore.lib import utils as _log_utils
from scrapescore.lib.config import APP_CONFIG, RESOURCE_CONFIG, get_storage_dir_config
from scrapescore.lib.utils import BASE_PREFIX

# Configure logging early — before sub-module imports so migration and route
# module log calls are captured from the start.
_log_utils.config_logger("scrapescore.log", Path(get_storage_dir_config("logs_dir")))
logger = logging.getLogger(__name__)
logger.info("job_score app initializing (BASE_PREFIX=%r)", BASE_PREFIX)

from scrapescore.analytics import analytics_rt
from scrapescore.applied import applied_rt
from scrapescore.common import NavigationLayout
from scrapescore.configs import ar as configs_router
from scrapescore.profiles import ar as profiles_router
from scrapescore.saved import saved_rt
from scrapescore.score import score_rt
from scrapescore.search import search_rt

# Load Google OAuth client from secrets file
secrets_path = get_storage_dir_config("google_oauth_secrets_path")
if not secrets_path or not Path(secrets_path).exists():
    print(f"ERROR: Google OAuth secrets file not found: {secrets_path}")
    print("Configure 'storage_dirs.google_oauth_secrets_path' in config.yaml")
    print("See app.py docstring for setup instructions.")
    sys.exit(1)

google_client = GoogleAppClient.from_file(secrets_path)

_FAVICON_URI = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E"
    "%3Crect width='32' height='32' rx='6' fill='%231e3a8a'/%3E"
    "%3Ccircle cx='13' cy='13' r='7.2' fill='%231e40af' stroke='%237dd3fc' stroke-width='2'/%3E"
    "%3Cline x1='18.5' y1='18.5' x2='25.5' y2='25.5' stroke='%237dd3fc' stroke-width='2.5' stroke-linecap='round'/%3E"
    "%3Crect x='9' y='12' width='8' height='5' rx='1' fill='%23bfdbfe'/%3E"
    "%3Cpath d='M11 12 L11 11 Q11 10 12 10 L14 10 Q15 10 15 11 L15 12' fill='none' stroke='%23bfdbfe' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E"
    "%3Cline x1='9' y1='14.5' x2='17' y2='14.5' stroke='%2393c5fd' stroke-width='0.8'/%3E"
    "%3Cpath d='M25.5 5 L26.3 7.2 L28.5 7.8 L26.3 8.4 L25.5 10.5 L24.7 8.4 L22.5 7.8 L24.7 7.2 Z' fill='%23fbbf24'/%3E"
    "%3C/svg%3E"
)

_COMPACT_FORM_CSS = Style("""
.uk-btn           { height: auto !important; padding: 4px 12px !important; font-size: 0.8125rem !important; line-height: 1.25rem !important; min-height: unset !important; }
.uk-input         { height: 32px !important; padding: 0 8px !important; font-size: 0.8125rem !important; }
.uk-input-fake    { height: 32px !important; padding: 0 8px !important; font-size: 0.8125rem !important; }
.uk-textarea      { padding: 4px 8px !important; font-size: 0.8125rem !important; }
input[type="date"] { -webkit-appearance: auto !important; appearance: auto !important; overflow: visible !important; padding-right: 4px !important; }
input[type="date"]::-webkit-calendar-picker-indicator { opacity: 1 !important; display: block !important; cursor: pointer; }
""")

_MARKDOWN_CSS = Style("""
.marked h1 { font-size: 1.875rem; font-weight: 700; margin: 1.5rem 0 0.75rem; line-height: 1.2; }
.marked h2 { font-size: 1.5rem;   font-weight: 700; margin: 1.75rem 0 0.5rem;  line-height: 1.3; border-bottom: 1px solid hsl(var(--border)); padding-bottom: 0.25rem; }
.marked h3 { font-size: 1.2rem;   font-weight: 600; margin: 1.25rem 0 0.4rem; }
.marked p  { margin-bottom: 0.85rem; line-height: 1.75; }
.marked ul { list-style: disc;    padding-left: 1.75rem; margin-bottom: 0.85rem; }
.marked ol { list-style: decimal; padding-left: 1.75rem; margin-bottom: 0.85rem; }
.marked li { margin-bottom: 0.35rem; line-height: 1.7; }
.marked hr { margin: 1.75rem 0; border: none; border-top: 1px solid hsl(var(--border)); }
.marked strong { font-weight: 700; }
.marked em     { font-style: italic; }
.marked code   { font-family: monospace; background: hsl(var(--muted)); padding: 0.15em 0.4em; border-radius: 0.25rem; font-size: 0.875em; }
.marked pre    { background: hsl(var(--muted)); padding: 1rem; border-radius: 0.5rem; overflow-x: auto; margin-bottom: 1rem; }
.marked pre code { background: none; padding: 0; }
.marked table  { border-collapse: collapse; width: 100%; margin-bottom: 1rem; }
.marked th, .marked td { border: 1px solid hsl(var(--border)); padding: 0.5rem 0.75rem; }
.marked th     { font-weight: 600; background: hsl(var(--muted)); }
.marked a      { text-decoration: underline; color: hsl(var(--primary)); }
.marked blockquote { border-left: 4px solid hsl(var(--border)); padding-left: 1rem; margin: 1rem 0; opacity: 0.8; }
""")

# Create FastHTML app with MonsterUI theme
app, rt = fast_app(
    hdrs=(*Theme.slate.headers(), MarkdownJS(), _MARKDOWN_CSS, _COMPACT_FORM_CSS,
          Link(rel="icon", type="image/svg+xml", href=_FAVICON_URI)),
    bodycls="bg-background text-foreground",
)
app.router.redirect_slashes = False


@rt("/api/test-auth")
def get(sess):
    sess["auth"] = "test-user@testdomain.com"
    sess["user_info"] = {
        "email": "test-user@testdomain.com",
        "name": "Test User",
        "picture": "",
    }
    return {"session_cookie": create_test_session_cookie()}


class MyAppOAuth(OAuth):
    def __init__(self, app, cli, **kwargs):
        redir_path = kwargs.get('redir_path', '/redirect')

        # Register our redirect handler FIRST — Starlette uses first-match-wins,
        # so this takes priority over the handler super().__init__ also registers.
        # This lets us use self.redir_url() (which includes BASE_PREFIX) as the
        # redirect_uri in the token exchange, matching the authorization request.
        @app.get(redir_path)
        async def _redirect(req, sess, code: str = None, error: str = None, state: str = None):
            if not code:
                sess['oauth_error'] = error
                return RedirectResponse(self.error_path, status_code=303)
            redirect_uri = self.redir_url(req)
            info = await cli.retr_info_async(code, redirect_uri)
            ident = info.get(cli.id_key) if info else None
            if not ident:
                return self.redir_login(sess)
            res = self.get_auth(info, ident, sess, state)
            if asyncio.iscoroutine(res):
                res = await res
            if not res:
                return self.redir_login(sess)
            req.scope['auth'] = sess['auth'] = ident
            return res

        super().__init__(app, cli, **kwargs)

    def get_auth(self, info, ident, session, state):
        session["user_info"] = {
            "email": info.get("email", ""),
            "name": info.get("name", ""),
            "picture": info.get("picture", ""),
        }
        logger.info("OAuth login: %s (%s)", info.get("email", ""), info.get("name", ""))
        return RedirectResponse(f"{BASE_PREFIX}/", status_code=303)

    def redir_login(self, session):
        return RedirectResponse(f"{BASE_PREFIX}/login", status_code=303)

    def redir_url(self, req):
        url = super().redir_url(req)
        if BASE_PREFIX:
            url = url.replace(self.redir_path, f"{BASE_PREFIX}{self.redir_path}", 1)
        return url

    def logout(self, session):
        logger.info("Logout: %s", session.get("user_info", {}).get("email", "unknown"))
        session.pop("user_info", None)
        return self.redir_login(session)


oauth = MyAppOAuth(app, google_client,
    skip=[f"{BASE_PREFIX}{p}" for p in ("/api/test-auth", "/login", "/redirect")]
)


def create_test_session_cookie():
    """Create a test session cookie for authentication without OAuth."""
    import base64
    import json

    import itsdangerous
    from fasthtml.core import get_key

    secret_key = get_key()
    signer = itsdangerous.TimestampSigner(secret_key)
    session_data = {
        "auth": "test-user@testdomain.com",
        "user_info": {
            "email": "test-user@testdomain.com",
            "name": "Test User",
            "picture": "",
        },
    }
    serialized = base64.b64encode(json.dumps(session_data).encode()).decode()
    return signer.sign(serialized).decode()


def _help_content():
    return RESOURCE_CONFIG.get("help_content", "")


@rt("/")
def get(auth, sess):
    user_info = sess.get("user_info", {})
    logger.debug("Home: %s", auth)
    name = user_info.get("name", "User")
    help_md = _help_content()
    return NavigationLayout(
        Div(
            DivCentered(H1(f"Welcome, {name}"), cls="mt-8"),
            Div(help_md, cls="marked max-w-3xl mx-auto mt-6 px-4") if help_md else None,
        ),
        title="Home",
        current_path="/",
        user_info=user_info,
    )


@rt("/help")
def get(auth, sess):
    user_info = sess.get("user_info", {})
    help_md = _help_content()
    return NavigationLayout(
        Div(
            help_md,
            cls="marked max-w-3xl mx-auto mt-6 px-4",
        ) if help_md else DivCentered(P("No help content configured."), cls="mt-20"),
        title="Help",
        current_path="/help",
        user_info=user_info,
    )




# Register score routes
score_rt.to_app(app)

# Register search routes
search_rt.to_app(app)

# Register saved routes
saved_rt.to_app(app)

# Register applied routes
applied_rt.to_app(app)

# Register analytics routes
analytics_rt.to_app(app)

# Register config routes
configs_router.to_app(app)


@rt("/login")
def get(req):
    login_url = oauth.login_link(req)
    return Title("Login"), Container(
        DivCentered(
            Card(
                Button(
                    DivLAligned(UkIcon("logo-google"), P("Sign in with Google")),
                    cls=ButtonT.primary,
                    onclick=f"window.location.href='{login_url}'",
                ),
                header=(H3("Scrape Score Job Finder"), Subtitle("Sign in to continue")),
                cls="max-w-sm",
            ),
            cls="mt-20",
        ),
    )


# Register profile routes
profiles_router.to_app(app)


@rt()
def get(request):
    return Body()


# When a prefix is configured, mount the app so /prefix/* routes reach it.
# main_app is what uvicorn actually serves; without a prefix it's identical to app.
if BASE_PREFIX:
    from starlette.routing import Mount, Router
    main_app = Router(routes=[Mount(BASE_PREFIX, app=app)], redirect_slashes=False)
else:
    main_app = app


if __name__ == "__main__":
    import uvicorn
    port = int(APP_CONFIG["server"]["port"])
    logger.info("Starting uvicorn on port %d", port)
    uvicorn.run(
        "scrapescore.app:main_app",
        host="0.0.0.0",
        reload=True,
        reload_dirs=["src/scrapescore"],
        port=port,
        log_config=None,
    )
