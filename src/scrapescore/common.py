from fasthtml.common import *
from monsterui.all import *
from .lib.config import BASE_PREFIX

_HTMX_PREFIX_SCRIPT = Script(f"""
(function() {{
  var PREFIX = {repr(BASE_PREFIX)};
  if (!PREFIX) return;
  document.addEventListener('htmx:configRequest', function(e) {{
    var path = e.detail.path;
    if (path && path.charAt(0) === '/' && path.indexOf(PREFIX) !== 0) {{
      e.detail.path = PREFIX + path;
    }}
  }});
}})();
""")

_CONFIRM_SCRIPT = Script("""
// Replace browser confirm() for hx-confirm with UIkit's themed modal dialog
document.addEventListener('htmx:confirm', function(e) {
  if (!e.detail.question) return;
  e.preventDefault();
  UIkit.modal.confirm(e.detail.question).then(
    function() { e.detail.issueRequest(true); },
    function() {}
  );
});
""")

_TAB_STATE_SCRIPT = Script(f"""
(function() {{
  var PREFIX = {repr(BASE_PREFIX)};
  var TABS = ['/search/', '/saved/', '/applied/'].map(function(t) {{ return PREFIX + t; }});

  function currentTab() {{
    return TABS.find(function(p) {{ return location.pathname.startsWith(p); }}) || null;
  }}

  function save() {{
    var tab = currentTab();
    if (tab) sessionStorage.setItem('tabState:' + tab, location.href);
  }}

  // Patch pushState so filter changes (which call history.pushState) auto-save
  var _orig = history.pushState.bind(history);
  history.pushState = function(state, title, url) {{
    _orig(state, title, url);
    save();
  }};

  // Save on initial load and browser back/forward
  save();
  window.addEventListener('popstate', save);

  // Intercept tab nav link clicks to restore stored URL via full-page navigation
  var _tabNavBusy = false;
  document.addEventListener('click', function(e) {{
    if (_tabNavBusy) return;
    var a = e.target.closest('a[href]');
    if (!a) return;
    var href = a.getAttribute('href');
    if (TABS.indexOf(href) === -1) return;
    var stored = sessionStorage.getItem('tabState:' + href);
    if (stored) {{
      e.preventDefault();
      _tabNavBusy = true;
      var origHref = a.getAttribute('href');
      a.setAttribute('href', stored);
      a.click();
      a.setAttribute('href', origHref);
      _tabNavBusy = false;
    }}
  }}, true);
}})();
""")

_THEME_TOGGLE_SCRIPT = Script("""
function toggleTheme() {
    const html = document.documentElement;
    const f = JSON.parse(localStorage.getItem('__FRANKEN__') || '{}');
    const nowDark = html.classList.toggle('dark');
    f.mode = nowDark ? 'dark' : 'light';
    localStorage.setItem('__FRANKEN__', JSON.stringify(f));
    document.querySelectorAll('.theme-sun').forEach(el => el.classList.toggle('hidden', nowDark));
    document.querySelectorAll('.theme-moon').forEach(el => el.classList.toggle('hidden', !nowDark));
}
document.addEventListener('DOMContentLoaded', function() {
    const isDark = document.documentElement.classList.contains('dark');
    document.querySelectorAll('.theme-sun').forEach(el => el.classList.toggle('hidden', isDark));
    document.querySelectorAll('.theme-moon').forEach(el => el.classList.toggle('hidden', !isDark));
});
""")



def _avatar_dropdown(user_info: dict):
    picture = user_info.get("picture", "")
    if picture:
        trigger = Img(
            src=picture,
            alt="profile",
            referrerpolicy="no-referrer",
            cls="w-8 h-8 rounded-full object-cover cursor-pointer",
        )
    else:
        trigger = UkIcon("user", cls="w-8 h-8 cursor-pointer")

    dropdown_items = [
        ("Account", "/account/", "user-cog"),
        ("Profiles", "/profiles/", "user"),
        ("Config", "/config/", "settings"),
        ("Help", "/help", "help-circle"),
    ]

    return Div(
        Div(trigger, cls="ml-4 flex items-center"),
        Div(
            Ul(
                *[
                    Li(A(DivLAligned(UkIcon(icon, cls="mr-2"), name), href=f"{BASE_PREFIX}{path}"))
                    for name, path, icon in dropdown_items
                ],
                Li(cls="uk-nav-divider"),
                Li(
                    A(
                        DivLAligned(
                            Span(
                                Span(UkIcon("sun"), cls="theme-sun"),
                                Span(UkIcon("moon"), cls="theme-moon"),
                                cls="mr-2",
                            ),
                            "Toggle theme",
                        ),
                        href="#",
                        onclick="toggleTheme(); return false;",
                    )
                ),
                Li(cls="uk-nav-divider"),
                Li(A(DivLAligned(UkIcon("log-out", cls="mr-2"), "Logout"), href=f"{BASE_PREFIX}/logout")),
                cls="uk-nav uk-dropdown-nav",
            ),
            uk_dropdown="mode: click; pos: bottom-right",
        ),
        cls="uk-inline",
    )


def NavigationLayout(content, title="Scrape Score Job Finder", current_path="/", user_info=None):
    """
    Unified layout with responsive navigation.
    Desktop: Top NavBar
    Mobile: Bottom Navigation Bar + Top Brand Bar
    """
    if user_info is None:
        user_info = {}

    nav_items = [
        ("Search", "/search/", "search"),
        ("Saved", "/saved/", "bookmark"),
        ("Applied", "/applied/", "check-circle"),
        ("Analytics", "/analytics/", "bar-chart"),
        ("Score", "/score/", "target"),
    ]

    def _is_active(path: str) -> bool:
        return current_path.rstrip("/") == path.rstrip("/")

    # Desktop NavBar (Top)
    desktop_nav = NavBar(
        *[
            A(
                DivLAligned(UkIcon(icon, cls="mr-2"), name),
                href=f"{BASE_PREFIX}{path}",
                cls="text-primary font-semibold border-b-2 border-primary pb-1"
                if _is_active(path)
                else "text-foreground",
            )
            for name, path, icon in nav_items
        ],
        _avatar_dropdown(user_info),
        brand=H3("Scrape Score Job Finder", cls="m-0 text-foreground"),
        cls="hidden md:flex border-b bg-background",
        sticky=True,
    )

    # Mobile Top Bar (Brand + toggle + avatar dropdown)
    mobile_top = Div(
        Div(
            H3("Scrape Score Job Finder", cls="text-center py-3 m-0 text-foreground flex-1"),
            Div(_avatar_dropdown(user_info), cls="absolute right-3 top-1/2 -translate-y-1/2"),
            cls="relative flex items-center",
        ),
        cls="md:hidden border-b bg-background sticky top-0 z-50",
    )

    # Mobile Bottom Nav
    mobile_bottom = Nav(
        *[
            A(
                DivVStacked(
                    UkIcon(icon, ratio=0.8),
                    Span(name, cls="text-[10px]"),
                    cls="items-center gap-0",
                ),
                href=f"{BASE_PREFIX}{path}",
                cls="flex-1 flex flex-col items-center py-2 "
                + ("text-primary font-semibold" if _is_active(path) else "text-muted-foreground"),
            )
            for name, path, icon in nav_items
        ],
        cls="md:hidden fixed bottom-0 left-0 right-0 bg-background border-t border-border flex z-50",
    )

    content_list = list(content) if isinstance(content, (list, tuple)) else [content]

    return (
        Title(title),
        _HTMX_PREFIX_SCRIPT,
        _THEME_TOGGLE_SCRIPT,
        _TAB_STATE_SCRIPT,
        _CONFIRM_SCRIPT,
        desktop_nav,
        mobile_top,
        Container(
            *content_list,
            cls="mt-4 mb-20 md:mb-4",
        ),
        mobile_bottom,
    )


def get_auth_user(auth):
    """Extract authenticated user email from auth string or session."""
    if not auth:
        return ""
    return str(auth)
