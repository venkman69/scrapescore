"""
Analytics dashboard for job_score.
All keyword data is sourced from job_details.search_term — no profile config dependency.
"""

import logging

from fasthtml.common import *
import fasthtml.common as _fh
from monsterui.all import *

logger = logging.getLogger(__name__)

_NativeSelect = _fh.Select  # native <select>; monsterui.all overwrites Select with custom dropdown

from .common import NavigationLayout, get_auth_user
from .db import (
    get_search_terms_from_jobs,
    get_keyword_quality,
    get_keyword_site_breakdown,
    get_job_funnel_stats,
    get_source_effectiveness,
    get_applications_timeline,
)


analytics_rt = APIRouter(prefix="/analytics")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stat_card(label: str, value, icon: str, cls_accent: str = "text-primary"):
    return Card(
        DivVStacked(
            DivLAligned(
                UkIcon(icon, ratio=1.0, cls=f"{cls_accent} mr-2"),
                Span(label, cls="text-xs text-muted-foreground font-medium uppercase tracking-wide leading-tight"),
            ),
            P(str(value), cls=f"text-2xl font-bold {cls_accent} mt-1"),
        ),
        cls="p-3",
    )


def _th(*cols):
    return Thead(Tr(*[Th(c, cls="text-xs font-semibold text-muted-foreground py-2 px-3") for c in cols]))


def _td(val, cls=""):
    return Td(str(val) if val is not None else "—", cls=f"py-1.5 px-3 text-sm {cls}")


def _num(val):
    return f"{val:,}" if val else "0"


def _pct(a, b):
    return f"{round(100 * a / b, 1)}%" if b else "—"


def _bar(pct: float, cls: str = "bg-primary") -> FT:
    w = min(round(pct), 100)
    return Div(
        Div(cls=f"{cls} h-1.5 rounded-full", style=f"width:{w}%"),
        cls="w-20 bg-muted rounded-full h-1.5",
    )


def _mobile_row_card(*children) -> FT:
    return Div(*children, cls="border border-border rounded-lg p-3 mb-2 space-y-1.5")


def _mobile_stat_row(label: str, val: str, accent: str = "") -> FT:
    return Div(
        Span(label, cls="text-xs text-muted-foreground"),
        Span(val, cls=f"text-xs font-semibold {accent}"),
        cls="flex justify-between items-center",
    )


def _mobile_bar_row(label: str, pct_str: str, pct_val: float, bar_cls: str, accent: str) -> FT:
    return Div(
        Div(
            Span(label, cls="text-xs text-muted-foreground w-10 shrink-0"),
            Span(pct_str, cls=f"text-xs font-semibold {accent} w-12 text-right shrink-0"),
            Div(
                Div(cls=f"{bar_cls} h-1.5 rounded-full", style=f"width:{min(round(pct_val), 100)}%"),
                cls="flex-1 bg-muted rounded-full h-1.5",
            ),
        ),
        cls="flex items-center gap-2",
    )


# ---------------------------------------------------------------------------
# Keyword Quality summary card
# ---------------------------------------------------------------------------

def _keyword_quality_card(owning_user: str) -> FT:
    rows_data = get_keyword_quality(owning_user)

    if not rows_data:
        return Card(
            H3("Keyword Quality", cls="text-base font-semibold mb-1"),
            P("No keyword-attributed jobs yet. Keyword attribution populates after the next scraper run.",
              cls="text-sm text-muted-foreground"),
            cls="p-4 mb-4",
        )

    table_rows = []
    mobile_cards = []

    for r in rows_data:
        total = r["total"] or 0
        high  = r["high_compat"] or 0
        med   = r["medium_compat"] or 0
        low   = r["low_compat"] or 0
        high_pct = (100 * high / total) if total else 0
        med_pct  = (100 * med  / total) if total else 0
        low_pct  = (100 * low  / total) if total else 0

        # Desktop table row
        table_rows.append(Tr(
            _td(r["keyword"]),
            _td(_num(total), cls="text-right tabular-nums"),
            Td(
                Div(
                    Span(_pct(high, total), cls="text-xs text-green-600 font-semibold w-14 text-right"),
                    _bar(high_pct, "bg-green-500"),
                    cls="flex items-center gap-2 justify-end",
                ),
                cls="py-1.5 px-3",
            ),
            Td(
                Div(
                    Span(_pct(med, total), cls="text-xs text-yellow-600 w-14 text-right"),
                    _bar(med_pct, "bg-yellow-400"),
                    cls="flex items-center gap-2 justify-end",
                ),
                cls="py-1.5 px-3",
            ),
            Td(
                Div(
                    Span(_pct(low, total), cls="text-xs text-muted-foreground w-14 text-right"),
                    _bar(low_pct, "bg-muted-foreground"),
                    cls="flex items-center gap-2 justify-end",
                ),
                cls="py-1.5 px-3",
            ),
        ))

        # Mobile card per keyword
        mobile_cards.append(_mobile_row_card(
            Div(
                Span(r["keyword"], cls="text-sm font-semibold"),
                Span(f"{_num(total)} jobs", cls="text-xs text-muted-foreground"),
                cls="flex justify-between items-center mb-1",
            ),
            _mobile_bar_row("High", _pct(high, total), high_pct, "bg-green-500", "text-green-600"),
            _mobile_bar_row("Med",  _pct(med,  total), med_pct,  "bg-yellow-400", "text-yellow-600"),
            _mobile_bar_row("Low",  _pct(low,  total), low_pct,  "bg-muted-foreground", "text-muted-foreground"),
        ))

    return Card(
        H3("Keyword Quality", cls="text-base font-semibold mb-1"),
        P("How effectively each keyword surfaces high-compatibility job titles.",
          cls="text-xs text-muted-foreground mb-3"),
        # Desktop table
        Div(
            Table(
                _th("Keyword", "Jobs in DB", "High", "Medium", "Low"),
                Tbody(*table_rows),
                cls="w-full text-left border-collapse",
            ),
            cls="overflow-x-auto hidden md:block",
        ),
        # Mobile cards
        Div(*mobile_cards, cls="md:hidden"),
        cls="p-4 mb-0",
    )


# ---------------------------------------------------------------------------
# Keyword site breakdown (HTMX target)
# ---------------------------------------------------------------------------

def _site_breakdown_table(owning_user: str, keyword: str) -> FT:
    rows = get_keyword_site_breakdown(owning_user, keyword, "all")

    if not rows:
        msg = "No keyword-attributed jobs yet." if not keyword else f'No jobs found for "{keyword}".'
        return Div(P(msg, cls="text-muted-foreground text-sm p-4"), id="site-breakdown-body")

    table_rows = []
    mobile_cards = []

    for r in rows:
        table_rows.append(Tr(
            _td(r["site"].title()),
            _td(_num(r["total"]),       cls="text-right tabular-nums"),
            _td(_num(r["high_compat"]), cls="text-right tabular-nums text-green-600 font-medium"),
            _td(_pct(r["high_compat"], r["total"]), cls="text-right tabular-nums"),
        ))

        mobile_cards.append(_mobile_row_card(
            Span(r["site"].title(), cls="text-sm font-semibold"),
            Div(
                _mobile_stat_row("Total", _num(r["total"])),
                _mobile_stat_row("High Compat", _num(r["high_compat"]), "text-green-600"),
                _mobile_stat_row("Hit Rate", _pct(r["high_compat"], r["total"])),
                cls="space-y-1",
            ),
        ))

    return Div(
        # Desktop table
        Div(
            Table(
                _th("Site", "Total Jobs", "High Compat", "High %"),
                Tbody(*table_rows),
                cls="w-full text-left border-collapse",
            ),
            cls="overflow-x-auto hidden md:block",
        ),
        # Mobile cards
        Div(*mobile_cards, cls="md:hidden"),
        cls="",
        id="site-breakdown-body",
    )


# ---------------------------------------------------------------------------
# Main page
# ---------------------------------------------------------------------------

@analytics_rt("/")
def get(auth, sess):
    user = get_auth_user(auth)
    user_info = sess.get("user_info", {})

    keywords = get_search_terms_from_jobs(user)
    default_kw = keywords[0] if keywords else ""

    funnel = get_job_funnel_stats(user)
    sources = get_source_effectiveness()
    timeline = get_applications_timeline(user)

    # --- Funnel summary row ---
    funnel_row = Div(
        _stat_card("High Compat Available", _num(funnel.get("high_unreviewed", 0)), "star", "text-green-500"),
        _stat_card("Saved",    _num(funnel.get("saved",    0)), "bookmark",   "text-blue-500"),
        _stat_card("Applied",  _num(funnel.get("applied",  0)), "send",        "text-primary"),
        _stat_card("Rejected", _num(funnel.get("rejected", 0)), "x-circle",   "text-muted-foreground"),
        cls="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4",
    )

    # --- Keyword quality summary ---
    kw_quality_card = _keyword_quality_card(user)

    # --- Keyword site breakdown card ---
    kw_options = [Option(k, value=k, selected=(k == default_kw)) for k in keywords]
    kw_selector = _NativeSelect(
        *kw_options,
        name="keyword",
        hx_get="/analytics/site-breakdown",
        hx_trigger="change",
        hx_target="#site-breakdown-body",
        hx_swap="outerHTML",
        hx_include="[name='user']",
        cls="uk-select text-sm w-auto max-w-xs",
    )

    breakdown_card = Card(
        DivLAligned(
            H3("Keyword Performance by Site", cls="text-base font-semibold m-0 mr-4"),
            kw_selector,
            Input(type="hidden", name="user", value=user),
            cls="mb-3 flex-wrap gap-2 items-center",
        ),
        _site_breakdown_table(user, default_kw),
        cls="p-4 mb-0",
    )

    # --- Source effectiveness card ---
    source_table_rows = []
    source_mobile_cards = []

    for r in sources:
        source_table_rows.append(Tr(
            _td(r["site"].title()),
            _td(_num(r["total"]),       cls="text-right tabular-nums"),
            _td(_num(r["high_compat"]), cls="text-right tabular-nums text-green-600 font-medium"),
            _td(_pct(r["high_compat"], r["total"]), cls="text-right tabular-nums"),
        ))
        source_mobile_cards.append(_mobile_row_card(
            Span(r["site"].title(), cls="text-sm font-semibold"),
            Div(
                _mobile_stat_row("Total", _num(r["total"])),
                _mobile_stat_row("High Compat", _num(r["high_compat"]), "text-green-600"),
                _mobile_stat_row("Hit Rate", _pct(r["high_compat"], r["total"])),
                cls="space-y-1",
            ),
        ))

    source_card = Card(
        H3("Source Effectiveness", cls="text-base font-semibold mb-3"),
        # Desktop table
        Div(
            Table(
                _th("Site", "Total Jobs", "High Compat", "Hit Rate"),
                Tbody(*source_table_rows),
                cls="w-full text-left border-collapse",
            ),
            cls="overflow-x-auto hidden md:block",
        ),
        # Mobile cards
        Div(*source_mobile_cards, cls="md:hidden"),
        cls="p-4 mb-4",
    )

    # --- Applications timeline card ---
    # Find max for bar scaling
    max_applied = max((r["applied"] or 0 for r in timeline), default=1) or 1

    timeline_table_rows = []
    for r in timeline:
        applied = r["applied"] or 0
        bar_pct = 100 * applied / max_applied
        timeline_table_rows.append(Tr(
            _td(r["month"]),
            # Bar column: desktop only
            Td(
                _bar(bar_pct, "bg-primary"),
                cls="py-1.5 px-3 hidden md:table-cell",
            ),
            _td(_num(applied), cls="text-right tabular-nums font-medium"),
        ))

    if not timeline_table_rows:
        timeline_table_rows = [Tr(Td("No applications yet.", colspan="3", cls="text-muted-foreground text-sm py-3 px-3"))]

    # Desktop header includes bar column; mobile hides it via CSS on the cell
    timeline_card = Card(
        H3("Applications Over Time", cls="text-base font-semibold mb-3"),
        Div(
            Table(
                Thead(Tr(
                    Th("Month",   cls="text-xs font-semibold text-muted-foreground py-2 px-3"),
                    Th("",        cls="text-xs font-semibold text-muted-foreground py-2 px-3 hidden md:table-cell"),
                    Th("Applied", cls="text-xs font-semibold text-muted-foreground py-2 px-3 text-right"),
                )),
                Tbody(*timeline_table_rows),
                cls="w-full text-left border-collapse",
            ),
            cls="overflow-x-auto",
        ),
        cls="p-4 mb-4",
    )

    content = Div(
        funnel_row,
        # Keyword quality + keyword performance: side-by-side on lg+, stacked below
        Div(
            kw_quality_card,
            breakdown_card,
            cls="grid grid-cols-1 lg:grid-cols-2 gap-4 mb-4 items-start",
        ),
        Div(source_card, timeline_card, cls="grid grid-cols-1 md:grid-cols-2 gap-4 items-start"),
    )

    return NavigationLayout(
        content,
        title="Analytics",
        current_path="/analytics",
        user_info=user_info,
    )


# ---------------------------------------------------------------------------
# HTMX partial: keyword + compat filter → site breakdown table
# ---------------------------------------------------------------------------

@analytics_rt("/site-breakdown")
def get(user: str = "", keyword: str = ""):
    return _site_breakdown_table(user, keyword)
