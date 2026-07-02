"""
Job Search Dashboard for job_score.
Two-pane layout: compact scrollable job cards on the left, detail panel on the right.
All interactions via HTMX — no page reloads.
"""

import json
import logging

from fasthtml.common import *
from .lib.config import BASE_PREFIX
# Save native HTML Select before MonsterUI overwrites it with its UIkit web component.
# MonsterUI's Select renders as a uk-select custom element which ignores onchange and
# doesn't serialize name correctly — unusable for a plain filter form.
_NativeSelect = Select
from monsterui.all import *

from .common import NavigationLayout, get_auth_user
from .db import (
    apply_job,
    get_jobs_for_user,
    get_profiles_for_user,
    get_profile,
    update_job_description,
    update_job_review_status,
    update_job_score,
    update_title_compatibility_score,
    _clearance_required_from_result,
)
from scrapescore.db_setup import get_db_connection
from scrapescore.batch import job_finder as _job_finder
from .score import render_ats_score
from scrapescore.lib.gemini_ai_runner import ats_score_analyzer_gemini
from scrapescore.lib import utils

logger = logging.getLogger(__name__)

search_rt = APIRouter(prefix="/search")

_DATE_RANGE_OPTIONS = [
    ("all", "All"),
    ("today", "Today"),
    ("yesterday", "Yesterday"),
    ("1week", "1 Week"),
    ("2weeks", "2 Weeks"),
    ("1month", "1 Month"),
]

_SORT_OPTIONS = [
    ("date_posted", "Date Posted"),
    ("score", "Score"),
    ("title", "Title"),
    ("company", "Company"),
]

_REVIEW_STATUS_OPTIONS = [
    ("all", "All"),
    ("not_reviewed", "Not Reviewed"),
    ("saved", "Saved"),
    ("applied", "Applied"),
    ("rejected", "Rejected"),
]

_CLEARANCE_OPTIONS = [
    ("all", "All"),
    ("required", "Required"),
    ("not_required", "Not Required"),
]

_COMPATIBILITY_OPTIONS = [
    ("all", "All"),
    ("high", "High"),
    ("medium", "Medium"),
    ("low", "Low"),
]


def _filter_bar(
    keyword="", date_range="1week", sort_by="score",
    compatibility="high", clearance="not_required", review_status="not_reviewed",
):
    # requestSubmit() dispatches a submit event that HTMX catches via hx-trigger="submit"
    _submit_js = "this.form.requestSubmit()"
    _keyword_js = "clearTimeout(this._t);this._t=setTimeout(()=>this.form.requestSubmit(),400)"

    def _select(name, options, selected):
        return _NativeSelect(
            *[Option(label, value=val, selected=(val == selected)) for val, label in options],
            name=name,
            id=f"filter-{name}",
            cls="text-sm bg-background text-foreground border border-input rounded px-1",
            onchange=_submit_js,
        )

    return Form(
        Div(
            Input(
                id="filter-keyword",
                name="keyword",
                value=keyword,
                placeholder="Search title or company...",
                cls="text-sm flex-1 min-w-36",
                oninput=_keyword_js,
            ),
            Div(
                Label("Date", fr="filter-date_range", cls="text-xs text-muted-foreground whitespace-nowrap"),
                _select("date_range", _DATE_RANGE_OPTIONS, date_range),
                cls="flex flex-col gap-0.5",
            ),
            Div(
                Label("Sort", fr="filter-sort_by", cls="text-xs text-muted-foreground"),
                _select("sort_by", _SORT_OPTIONS, sort_by),
                cls="flex flex-col gap-0.5",
            ),
            Div(
                Label("Status", fr="filter-review_status", cls="text-xs text-muted-foreground"),
                _select("review_status", _REVIEW_STATUS_OPTIONS, review_status),
                cls="flex flex-col gap-0.5",
            ),
            Div(
                Label("Compatibility", fr="filter-compatibility", cls="text-xs text-muted-foreground"),
                _select("compatibility", _COMPATIBILITY_OPTIONS, compatibility),
                cls="flex flex-col gap-0.5",
            ),
            Div(
                Label("Clearance", fr="filter-clearance", cls="text-xs text-muted-foreground"),
                _select("clearance", _CLEARANCE_OPTIONS, clearance),
                cls="flex flex-col gap-0.5",
            ),
            cls="flex flex-wrap items-end gap-2",
        ),
        id="filter-form",
        hx_get="/search/jobs",
        hx_target="#job-results",
        hx_trigger="submit",
        hx_indicator="#list-spinner",
    )


def _decision_badge(decision: str):
    d = decision.upper()
    if d == "PASS":
        cls = "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300"
    elif d == "CONDITIONAL":
        cls = "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300"
    else:
        cls = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300"
    return Span(d.capitalize(), cls=f"text-xs font-bold px-1.5 py-0.5 rounded {cls}")


def _score_badge(score):
    if score >= 80:
        cls = "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300"
    elif score >= 60:
        cls = "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300"
    elif score > 0:
        cls = "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300"
    else:
        cls = "bg-muted text-muted-foreground"
    return Span(str(score), cls=f"text-xs font-bold px-1.5 py-0.5 rounded {cls}")


def _status_badge(status):
    colors = {
        "saved": "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300",
        "applied": "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
        "rejected": "bg-red-100 text-red-800 dark:bg-red-800/50 dark:text-red-100",
        "not_reviewed": "bg-muted text-muted-foreground",
    }
    cls = colors.get(status, "bg-muted text-muted-foreground")
    return Span(status.replace("_", " ").title(), cls=f"text-xs px-1.5 py-0.5 rounded {cls}")


def _compat_badge(compat):
    colors = {
        "high":   "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
        "medium": "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
        "low":    "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
    }
    cls = colors.get(compat, "bg-muted text-muted-foreground")
    return Span(compat.title(), cls=f"text-xs px-1.5 py-0.5 rounded {cls}")


def _format_salary(job):
    def _fmt(v):
        if not v:
            return ""
        try:
            f = float(v)
            return str(int(f)) if f == int(f) else str(f)
        except (ValueError, TypeError):
            return str(v)

    lo = _fmt(job.get("min_amount", "") or "")
    hi = _fmt(job.get("max_amount", "") or "")
    currency = job.get("currency", "") or ""
    if lo and hi and lo != "0" and hi != "0":
        return f"{currency}{lo}–{hi}"
    if lo and lo != "0":
        return f"{currency}{lo}+"
    return ""


def _bookmark_detail_btn(job_id, status, oob=False, route_prefix="search"):
    """Detail-pane bookmark button wrapped in a stable id container for OOB swap."""
    _detail_btn = "flex items-center gap-1.5 text-sm px-3 py-1.5 rounded border hover:bg-accent transition-colors"
    is_saved = status == "saved"
    cls = _detail_btn + (
        " text-blue-600 border-blue-300 bg-blue-50 dark:text-blue-400 dark:border-blue-700 dark:bg-blue-950/60"
        if is_saved else ""
    )
    btn = Button(
        UkIcon("bookmark", ratio=0.85),
        Span("Saved" if is_saved else "Save"),
        hx_post=f"/{route_prefix}/bookmark/{job_id}",
        hx_target=f"#job-card-{job_id}",
        hx_swap="outerHTML",
        cls=cls,
    )
    attrs = {"id": f"bookmark-btn-{job_id}"}
    if oob:
        attrs["hx_swap_oob"] = "true"
    return Div(btn, **attrs)


def _job_card(job, route_prefix="search"):
    jid = job["id"]
    salary = _format_salary(job)
    clearance = "Clearance Req." if job.get("security_clearance_required") else ""
    score = job.get("job_score") or 0
    compat = job.get("title_compatibility_score") or ""
    status = job.get("review_status") or "not_reviewed"
    date_posted = (job.get("date_posted") or "")[:10]
    url = job.get("job_url") or ""

    decision = None
    travel = None
    try:
        score_data = json.loads(job.get("job_score_json") or "{}")
        decision = score_data.get("decision") or None
        tv = score_data.get("travel_required")
        if tv is not None:
            try:
                travel = f"Travel: {int(float(tv))}%"
            except (ValueError, TypeError):
                travel = f"Travel: {tv}"
    except Exception:
        pass

    _btn_base = "p-0.5"
    _stop = "event.stopPropagation()"
    is_saved = status == "saved"
    bookmark_btn = Button(
        UkIcon("bookmark", ratio=0.85),
        hx_post=f"/{route_prefix}/bookmark/{jid}",
        hx_target=f"#job-card-{jid}",
        hx_swap="outerHTML",
        hx_include="#filter-form",
        onclick=_stop,
        cls=f"{_btn_base} {'text-blue-500 dark:text-blue-400' if is_saved else 'text-muted-foreground hover:text-primary'}",
        title="Unsave" if is_saved else "Save",
    )
    apply_btn = Button(
        UkIcon("send", ratio=0.85),
        hx_post=f"/search/apply/{jid}",
        hx_target=f"#job-card-{jid}",
        hx_swap="outerHTML",
        onclick=_stop,
        cls=f"{_btn_base} hover:text-green-600",
        title="Apply",
    )
    thumbsdown_btn = Button(
        UkIcon("thumbs-down", ratio=0.85),
        hx_post=f"/search/reject/{jid}",
        hx_target=f"#job-card-{jid}",
        hx_swap="outerHTML",
        hx_include="#filter-form",
        onclick=_stop,
        cls=f"{_btn_base} text-muted-foreground hover:text-red-500 transition-colors",
        title="Reject",
    )

    # Right-side action column: bookmark → apply → reject
    action_col = Div(
        bookmark_btn, apply_btn, thumbsdown_btn,
        cls="flex flex-col items-center gap-1 shrink-0 ml-1",
    )

    return Div(
        Div(
            # Left content
            Div(
                # Row 1: title
                Span(job.get("title", ""), cls="font-semibold text-sm leading-tight line-clamp-2"),
                # Row 2: badges
                Div(
                    _decision_badge(decision) if decision else None,
                    _score_badge(score),
                    _compat_badge(compat) if compat else None,
                    _status_badge(status),
                    cls="flex flex-wrap gap-1 mt-0.5",
                ),
                # Row 3: company + date
                Div(
                    Span(job.get("company", ""), cls="text-xs text-muted-foreground truncate flex-1"),
                    Span(date_posted, cls="text-xs text-muted-foreground whitespace-nowrap"),
                    cls="flex justify-between gap-1 mt-0.5",
                ),
                # Row 4: location + travel
                Div(
                    Span(job.get("location", ""), cls="text-xs text-muted-foreground truncate flex-1"),
                    Span(travel, cls="text-xs text-muted-foreground whitespace-nowrap") if travel else None,
                    cls="flex justify-between gap-1 mt-0.5",
                ),
                # Row 5: salary + clearance
                Div(
                    Span(salary, cls="text-xs text-muted-foreground") if salary else None,
                    Span(clearance, cls="text-xs text-orange-600 dark:text-orange-400 font-medium") if clearance else None,
                    cls="flex gap-2 flex-wrap mt-0.5",
                ),
                # Row 6: URL
                A(url, href=url, target="_blank", cls="text-xs text-primary truncate block mt-0.5 hover:underline") if url else None,
                cls="flex-1 min-w-0",
            ),
            action_col,
            cls="flex gap-1 items-start",
        ),
        id=f"job-card-{jid}",
        hx_get=f"/{route_prefix}/job/{jid}",
        hx_target="#job-detail-pane",
        hx_swap="innerHTML",
        onclick=(
            "document.querySelectorAll('[id^=job-card-]').forEach(e=>e.classList.remove('card-selected'));"
            "this.classList.add('card-selected');"
            "if(window.innerWidth<768){"
            "document.getElementById('job-detail-pane').classList.add('mobile-open');"
            "document.getElementById('job-list-pane').classList.add('mobile-hidden');"
            "document.getElementById('filter-bar-container').classList.add('mobile-hidden');}"
        ),
        cls="border rounded p-2 cursor-pointer hover:bg-accent/40 transition-colors",
    )


_FILTER_DEFAULTS = dict(
    keyword="", date_range="1week", sort_by="score",
    compatibility="high", clearance="not_required", review_status="not_reviewed",
)

_CARD_SELECTED_CSS = Style("""
    .card-selected { outline: 2px solid #6366f1; outline-offset: -1px; }
    .dark .card-selected { outline: 2px solid #a5b4fc; outline-offset: -1px; }
    @media (max-width: 767px) {
        #job-detail-pane { display: none; }
        #job-detail-pane.mobile-open {
            display: block !important;
            width: 100%;
            height: auto !important;
            overflow-y: visible;
        }
        #job-list-pane.mobile-hidden { display: none; }
        #filter-bar-container.mobile-hidden { display: none; }
    }
""")

_PUSH_URL_JS = Script(f"""
(function() {{
    document.body.addEventListener('htmx:afterRequest', function(e) {{
        var cfg = e.detail && e.detail.requestConfig;
        if (cfg && cfg.path && cfg.path.indexOf('/search/jobs') !== -1) {{
            var form = document.getElementById('filter-form');
            if (form) {{
                var params = new URLSearchParams(new FormData(form));
                history.pushState(null, '', {repr(BASE_PREFIX)} + '/search/?' + params.toString());
            }}
        }}
    }});
}})();
""")


@search_rt("/")
def get(
    auth, sess,
    keyword: str = "",
    date_range: str = "1week",
    sort_by: str = "score",
    compatibility: str = "high",
    clearance: str = "not_required",
    review_status: str = "not_reviewed",
):
    from urllib.parse import urlencode
    user_info = sess.get("user_info", {})

    # Build initial load URL from current (possibly URL-restored) filter values
    initial_qs = urlencode(dict(
        keyword=keyword, date_range=date_range, sort_by=sort_by,
        compatibility=compatibility, clearance=clearance, review_status=review_status,
    ))

    content = Div(
        # Filter bar pre-populated with restored filter state
        Div(_filter_bar(keyword, date_range, sort_by, compatibility, clearance, review_status), id="filter-bar-container", cls="mb-2"),
        # Two-pane layout
        Div(
            # Left: spinner + scrollable results (counter lives inside #job-results)
            Div(
                Div(
                    Div(cls="uk-spinner", uk_spinner=True),
                    id="list-spinner",
                    cls="htmx-indicator py-1",
                ),
                Div(
                    Button(
                        UkIcon("rotate-cw", ratio=0.85),
                        Span("Refresh"),
                        type="button",
                        hx_get="/search/jobs",
                        hx_target="#job-results",
                        hx_include="#filter-form",
                        hx_indicator="#list-spinner",
                        hx_headers='{"Cache-Control": "no-cache"}',
                        cls="flex items-center gap-1.5 text-xs px-2 py-1 rounded border hover:bg-accent transition-colors text-muted-foreground hover:text-foreground",
                    ),
                    Span("", id="refresh-timestamp", cls="text-xs text-muted-foreground"),
                    cls="flex items-center gap-2 py-1",
                ),
                Div(
                    id="job-results",
                    hx_get=f"/search/jobs?{initial_qs}",
                    hx_trigger="load",
                    hx_indicator="#list-spinner",
                    cls="space-y-1.5",
                ),
                id="job-list-pane",
                cls="overflow-y-auto",
                style="height: calc(100vh - 160px)",
            ),
            # Right: detail panel
            Div(
                P("Select a job to view details.", cls="text-muted-foreground text-sm p-4"),
                id="job-detail-pane",
                cls="overflow-y-auto border rounded p-2",
                style="height: calc(100vh - 160px)",
            ),
            cls="grid gap-3",
            style="grid-template-columns: 380px 1fr",
        ),
        _PUSH_URL_JS,
        _CARD_SELECTED_CSS,
        Script("""
(function() {
    if (document._refreshTsInit) return;
    document._refreshTsInit = true;
    var lastMs = null;
    function fmt(ms) {
        var diff = Math.floor((Date.now() - ms) / 1000);
        if (diff < 10) return 'just now';
        if (diff < 60) return diff + 's ago';
        var mins = Math.floor(diff / 60);
        if (mins < 60) return mins + 'm ago';
        var hrs = Math.floor(mins / 60);
        return hrs + 'h ago';
    }
    function tick() {
        var el = document.getElementById('refresh-timestamp');
        if (lastMs !== null && el) el.textContent = 'Refreshed ' + fmt(lastMs);
    }
    document.addEventListener('htmx:afterSwap', function(e) {
        if (e.target && e.target.id === 'job-results') { lastMs = Date.now(); tick(); }
    });
    setInterval(tick, 30000);
})();
"""),
    )
    return NavigationLayout(content, title="Job Search", current_path="/search", user_info=user_info)


_PAGE_SIZE = 100


def _jobs_response(*parts):
    from starlette.responses import HTMLResponse
    html = "".join(to_xml(p) for p in parts if p is not None)
    return HTMLResponse(html, headers={"Cache-Control": "private, max-age=60"})


def _load_more_btn(keyword, date_range, sort_by, compatibility, clearance, review_status, page):
    from urllib.parse import urlencode
    qs = urlencode(dict(
        keyword=keyword, date_range=date_range, sort_by=sort_by,
        compatibility=compatibility, clearance=clearance,
        review_status=review_status, page=page,
    ))
    return Button(
        "Load More",
        hx_get=f"/search/jobs?{qs}",
        hx_target="this",
        hx_swap="outerHTML",
        hx_indicator="#list-spinner",
        cls=f"{ButtonT.default} text-sm w-full mt-2",
    )


@search_rt("/jobs", methods=["GET"])
def get_jobs(
    auth,
    keyword: str = "",
    date_range: str = "1week",
    sort_by: str = "score",
    compatibility: str = "high",
    clearance: str = "not_required",
    review_status: str = "not_reviewed",
    page: int = 1,
):
    user = get_auth_user(auth)
    jobs = get_jobs_for_user(
        user,
        keyword=keyword,
        date_range=date_range,
        sort_by=sort_by,
        compatibility=compatibility,
        clearance=clearance if clearance != "all" else "",
        review_status=review_status if review_status != "all" else "",
    )

    total = len(jobs)
    start = (page - 1) * _PAGE_SIZE
    page_jobs = jobs[start : start + _PAGE_SIZE]
    end = start + len(page_jobs)

    cards = [_job_card(j) for j in page_jobs]
    if end < total:
        cards.append(_load_more_btn(keyword, date_range, sort_by, compatibility, clearance, review_status, page + 1))

    # Counter only on first page (goes into #job-results alongside the cards)
    if page == 1:
        if not jobs:
            count_label = "0 jobs"
            return _jobs_response(P(count_label, cls="text-sm text-muted-foreground font-medium mb-1"), P("No jobs found.", cls="text-muted-foreground text-sm p-4"))
        count_label = f"{total} job{'s' if total != 1 else ''}"
        if total > _PAGE_SIZE:
            count_label += f" (showing first {_PAGE_SIZE})"
        return _jobs_response(P(count_label, cls="text-sm text-muted-foreground font-medium mb-1"), *cards)

    # Subsequent pages: replace the Load More button with new cards (+ possibly another Load More)
    return _jobs_response(*cards)


@search_rt("/job/{job_id}", methods=["GET"])
def get_job_detail(job_id: int, auth):
    user = get_auth_user(auth)
    conn = get_db_connection()
    conn.row_factory = __import__("sqlite3").Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM job_details WHERE id = ? AND owning_user = ?", (job_id, user))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return P("Job not found.", cls="text-muted-foreground text-sm")

    job = dict(row)
    profiles = get_profiles_for_user(user)
    default_profile = next((p["profile_name"] for p in profiles if p.get("is_default")), "")

    salary = _format_salary(job)
    score_int = job.get("job_score") or 0
    has_description = bool((job.get("description") or "").strip())

    # Stored score
    stored_score_html = None
    travel_detail = None
    score_json_str = job.get("job_score_json") or "{}"
    try:
        score_data = json.loads(score_json_str)
        if score_data and score_data != {}:
            stored_score_html = render_ats_score(score_data)
        tv = score_data.get("travel_required")
        if tv is not None:
            try:
                travel_detail = f"{int(float(tv))}%"
            except (ValueError, TypeError):
                travel_detail = str(tv)
    except Exception:
        pass

    _score_btn_id = f"score-btn-{job_id}"
    _desc_id = f"desc-{job_id}"
    _desc_area_id = f"desc-area-{job_id}"

    _detail_btn = "flex items-center gap-1.5 text-sm px-3 py-1.5 rounded border hover:bg-accent transition-colors"
    apply_btn = Button(
        UkIcon("send", ratio=0.85), Span("Apply"),
        hx_post=f"/search/apply/{job_id}",
        hx_target=f"#apply-result-{job_id}",
        hx_swap="innerHTML",
        cls=f"{_detail_btn} text-green-700 border-green-300 hover:bg-green-50 dark:text-green-400 dark:border-green-800 dark:hover:bg-green-950",
    )
    bookmark_btn = _bookmark_detail_btn(job_id, job.get("review_status") or "not_reviewed")
    reject_btn = Button(
        UkIcon("thumbs-down", ratio=0.85), Span("Reject"),
        hx_post=f"/search/reject/{job_id}",
        hx_target=f"#job-card-{job_id}",
        hx_swap="outerHTML",
        cls=f"{_detail_btn} text-destructive border-destructive/30 hover:bg-destructive/10 dark:text-red-400 dark:border-red-700/50 dark:hover:bg-red-900/20",
    )

    profile_options = [Option("-- Select Profile --", value="")] + [
        Option(p["profile_name"], value=p["profile_name"], selected=(p["profile_name"] == default_profile))
        for p in profiles
    ]

    _sm_btn = f"{ButtonT.default} text-xs"
    score_btn_cls = _sm_btn + (" opacity-50 cursor-not-allowed" if not has_description else "")

    is_remote = str(job.get("is_remote", "")).lower() in ("true", "1", "yes")

    return Div(
        # Back button — mobile only
        Button(
            UkIcon("arrow-left", ratio=0.85), Span("Back to results"),
            onclick=(
                "document.getElementById('job-detail-pane').classList.remove('mobile-open');"
                "document.getElementById('job-list-pane').classList.remove('mobile-hidden');"
                "document.getElementById('filter-bar-container').classList.remove('mobile-hidden');"
            ),
            cls="md:hidden flex items-center gap-1.5 text-sm mb-3 px-2 py-1 rounded border hover:bg-accent transition-colors",
        ),
        # Title + company
        H3(job.get("title", ""), cls="font-bold text-base leading-tight"),
        P(
            Span(job.get("company", ""), cls="font-medium"),
            Span(" · ", cls="text-muted-foreground"),
            Span(job.get("location", ""), cls="text-muted-foreground text-sm"),
            cls="text-sm mt-0.5",
        ),
        A(job.get("job_url", ""), href=job.get("job_url", ""), target="_blank",
          cls="text-xs text-primary hover:underline truncate block mt-0.5"),
        # Action buttons
        Div(apply_btn, bookmark_btn, reject_btn, cls="flex gap-4 mt-2 flex-wrap"),
        Div(id=f"apply-result-{job_id}", cls="mt-1 text-sm"),
        # Details grid
        Div(
            Div(Span("Remote: ", cls="font-medium text-xs"), Span("Yes" if is_remote else "No", cls="text-xs")),
            Div(Span("Travel: ", cls="font-medium text-xs"), Span(travel_detail or "N/A", cls="text-xs")),
            Div(Span("Salary: ", cls="font-medium text-xs"), Span(salary or "N/A", cls="text-xs")),
            Div(Span("Clearance: ", cls="font-medium text-xs"), Span("Required" if job.get("security_clearance_required") else "Not required", cls="text-xs")),
            cls="grid grid-cols-2 gap-x-4 gap-y-0.5 mt-2 border rounded p-2",
        ),
        # Description
        Div(
            H4("Job Description", cls="text-sm font-semibold"),
            # Wrapper targeted by Retrieve so both textarea and status update together
            Div(
                TextArea(
                    job.get("description", "") or "",
                    id=_desc_id,
                    name="description",
                    rows=10,
                    cls="text-xs w-full",
                    oninput=f"(function(ta){{var btn=document.getElementById('{_score_btn_id}');if(btn){{var has=!!ta.value.trim();btn.disabled=!has;has?btn.classList.remove('opacity-50','cursor-not-allowed'):btn.classList.add('opacity-50','cursor-not-allowed');}}}})( this)",
                ),
                Div(id=f"retrieve-status-{job_id}", cls="text-xs mt-0.5"),
                id=f"desc-area-{job_id}",
            ),
            Div(
                Button(
                    "Save Description",
                    hx_post=f"/search/save-description/{job_id}",
                    hx_include=f"#desc-{job_id}",
                    hx_target=f"#save-desc-result-{job_id}",
                    hx_swap="innerHTML",
                    cls=_sm_btn,
                ),
                Button(
                    "Retrieve Job",
                    hx_post=f"/search/retrieve/{job_id}",
                    hx_target=f"#desc-area-{job_id}",
                    hx_indicator=f"#retrieve-spinner-{job_id}",
                    hx_swap="innerHTML",
                    cls=_sm_btn,
                ),
                Div(
                    Div(cls="uk-spinner uk-spinner-small", uk_spinner=True),
                    id=f"retrieve-spinner-{job_id}",
                    cls="htmx-indicator",
                ),
                Div(id=f"save-desc-result-{job_id}", cls="text-xs"),
                cls="flex items-center gap-2 mt-1 flex-wrap",
            ),
            cls="mt-3",
        ),
        # Scoring
        Div(
            H4("Score Job", cls="text-sm font-semibold"),
            Div(
                Select(
                    *profile_options,
                    id=f"score-profile-{job_id}",
                    name="profile_name",
                    cls="text-xs flex-1",
                ),
                Button(
                    "Score Job",
                    id=_score_btn_id,
                    hx_post=f"/search/score/{job_id}",
                    hx_target=f"#score-result-{job_id}",
                    hx_indicator=f"#score-spinner-{job_id}",
                    hx_include=f"#{_desc_id}, #score-profile-{job_id}",
                    hx_swap="innerHTML",
                    disabled=not has_description,
                    cls=score_btn_cls,
                ),
                Button(
                    "Score Title",
                    hx_post=f"/search/score-title/{job_id}",
                    hx_target=f"#title-score-result-{job_id}",
                    hx_indicator=f"#score-spinner-{job_id}",
                    hx_include=f"#score-profile-{job_id}",
                    hx_swap="innerHTML",
                    cls=_sm_btn,
                ),
                cls="flex gap-2 items-center mt-1 flex-wrap",
            ),
            Div(
                Div(cls="uk-spinner uk-spinner-small", uk_spinner=True),
                id=f"score-spinner-{job_id}",
                cls="htmx-indicator mt-1",
            ),
            Div(id=f"title-score-result-{job_id}", cls="mt-1"),
            cls="mt-3",
        ),
        # Score results (pre-populated if stored, otherwise empty target)
        Div(
            stored_score_html,
            id=f"score-result-{job_id}",
            cls="mt-2",
        ),
        Script(f"""
(function(){{
    function _syncScoreBtn(){{
        var ta=document.getElementById('{_desc_id}');
        var btn=document.getElementById('{_score_btn_id}');
        if(!ta||!btn)return;
        var has=!!ta.value.trim();
        btn.disabled=!has;
        has?btn.classList.remove('opacity-50','cursor-not-allowed'):btn.classList.add('opacity-50','cursor-not-allowed');
    }}
    var da=document.getElementById('{_desc_area_id}');
    if(da)da.addEventListener('htmx:afterSwap',_syncScoreBtn);
}})();
"""),
    )


def _get_job_by_id(job_id: int, owning_user: str) -> dict | None:
    from scrapescore.db_setup import get_db_connection
    import sqlite3
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM job_details WHERE id = ? AND owning_user = ?", (job_id, owning_user))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


@search_rt("/bookmark/{job_id}", methods=["POST"])
def post_bookmark(job_id: int, auth):
    user = get_auth_user(auth)
    job = _get_job_by_id(job_id, user)
    if not job:
        return Div(id=f"job-card-{job_id}")
    current = job.get("review_status") or "not_reviewed"
    new_status = "not_reviewed" if current == "saved" else "saved"
    update_job_review_status(job_id, new_status, user)
    job = _get_job_by_id(job_id, user)
    if not job:
        return Div(id=f"job-card-{job_id}")
    return _job_card(job), _bookmark_detail_btn(job_id, new_status, oob=True)


@search_rt("/reject/{job_id}", methods=["POST"])
def post_reject(job_id: int, auth):
    user = get_auth_user(auth)
    update_job_review_status(job_id, "rejected", user)
    job = _get_job_by_id(job_id, user)
    if not job:
        return Div(id=f"job-card-{job_id}")
    return _job_card(job)


@search_rt("/apply/{job_id}", methods=["POST"])
def post_apply(job_id: int, auth):
    user = get_auth_user(auth)
    success = apply_job(job_id, user)
    # Card apply btn uses hx_swap="outerHTML" on the card — return a card-shaped replacement
    if success:
        # Job moved to applied_jobs; show a dismissed placeholder
        return Div(
            Span("✓ Applied", cls="text-green-600 text-xs font-medium"),
            id=f"job-card-{job_id}",
            cls="border rounded p-2 text-center opacity-50",
        )
    # On failure, fetch the current job and re-render its card
    job = _get_job_by_id(job_id, user)
    if job:
        return _job_card(job)
    return Div(id=f"job-card-{job_id}")


@search_rt("/save-description/{job_id}", methods=["POST"])
def post_save_description(job_id: int, description: str = "", auth=None):
    user = get_auth_user(auth)
    update_job_description(job_id, description, user)
    return Span("Saved.", cls="text-green-600")


@search_rt("/retrieve/{job_id}", methods=["POST"])
def post_retrieve(job_id: int, auth):
    user = get_auth_user(auth)
    job = _get_job_by_id(job_id, user)
    if not job:
        return _desc_area_error(job_id, job.get("description", "") if job else "", "Job not found.")
    url = job.get("job_url") or ""
    if not url:
        return _desc_area_error(job_id, job.get("description", ""), "No URL available for this job.")
    try:
        text = utils.get_markdown_from_url(url)
        if not text or not text.strip():
            return _desc_area_error(job_id, job.get("description", ""), "Could not retrieve description — no content found.")
        current_len = len((job.get("description") or "").strip())
        retrieved_len = len(text)
        if current_len > 0 and retrieved_len < current_len * 0.9:
            status = Span(f"⚠ Retrieved {retrieved_len} chars (shorter than current {current_len}). Review before saving.", cls="text-yellow-600 text-xs")
        else:
            status = Span(f"✓ Retrieved {retrieved_len} chars.", cls="text-green-600 text-xs")
        return (
            TextArea(text, id=f"desc-{job_id}", name="description", rows=10, cls="text-xs w-full"),
            Div(status, id=f"retrieve-status-{job_id}", cls="text-xs mt-0.5"),
        )
    except Exception as e:
        logger.exception("Retrieve failed")
        return _desc_area_error(job_id, job.get("description", ""), f"Retrieve failed: {e}")


def _desc_area_error(job_id: int, original_text: str, msg: str):
    return (
        TextArea(original_text, id=f"desc-{job_id}", name="description", rows=10, cls="text-xs w-full"),
        Div(Span(msg, cls="text-red-500 text-xs"), id=f"retrieve-status-{job_id}", cls="text-xs mt-0.5"),
    )


@search_rt("/score-title/{job_id}", methods=["POST"])
def post_score_title(job_id: int, profile_name: str = "", auth=None):
    user = get_auth_user(auth)
    job = _get_job_by_id(job_id, user)
    if not job:
        return Alert("Job not found.", cls=AlertT.error)

    profile = get_profile(profile_name, user) if profile_name else None
    if not profile:
        profiles = get_profiles_for_user(user)
        profile = next((p for p in profiles if p.get("is_default")), None)
    if not profile:
        return Alert("Select a profile first.", cls=AlertT.error)

    title = job.get("title") or ""
    if not title:
        return Alert("Job has no title to score.", cls=AlertT.error)

    try:
        results = _job_finder.job_title_compatibility([title], profile["desired_role_description"])
        if not results:
            return Alert("No result returned from scorer.", cls=AlertT.error)
        score = (results[0].get("score") or "").lower()
        if score:
            update_title_compatibility_score(job_id, score, user)
        return Div(
            _compat_badge(score) if score else Span("No score returned.", cls="text-xs text-muted-foreground"),
            cls="flex items-center gap-2",
        )
    except Exception as e:
        logger.exception("Score title failed")
        return Alert(f"Scoring failed: {str(e)}", cls=AlertT.error)


@search_rt("/score/{job_id}", methods=["POST"])
def post_score(job_id: int, profile_name: str = "", description: str = "", auth=None):
    user = get_auth_user(auth)
    if not profile_name or not description:
        return Alert("Select a profile and ensure a job description is present.", cls=AlertT.error)

    profile = get_profile(profile_name, user)
    if not profile:
        return Alert("Profile not found.", cls=AlertT.error)

    job_details_meta = {"job_url": f"job_id:{job_id}", "job_id": str(job_id), "job_source": "v3_search"}

    try:
        result, _, _ = ats_score_analyzer_gemini(
            job_description=description,
            resume=profile["resume"],
            desired_role_description=profile["desired_role_description"],
            job_details=job_details_meta,
            us_citizen=profile["us_citizen"],
            security_clearance=profile["security_clearance"],
        )
        if "error" in result:
            return Alert(f"Scoring error: {result['error']}", cls=AlertT.error)
        numeric_score = result.get("ats_score_estimate", {}).get("total_overall_score", 0)
        clearance = _clearance_required_from_result(result)
        update_job_score(job_id, numeric_score, json.dumps(result), user, clearance)
        return render_ats_score(result)
    except Exception as e:
        logger.exception("Scoring failed")
        return Alert(f"Scoring failed: {str(e)}", cls=AlertT.error)
