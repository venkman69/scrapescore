"""
Saved Jobs page for job_score.
Two-pane layout identical to search, filtered to review_status='saved'.
Keyword-only filter. Unsaving a job removes it from the list.
"""

import json
import logging

from fasthtml.common import *
from monsterui.all import *

from .common import NavigationLayout, get_auth_user
from .lib.config import BASE_PREFIX
from .db import (
    get_jobs_for_user,
    get_profiles_for_user,
    get_profile,
    update_job_description,
    update_job_review_status,
    update_title_compatibility_score,
)
from .score import render_ats_score
from scrapescore.lib.gemini_ai_runner import ats_score_analyzer_gemini
from scrapescore.lib import utils
from .search import (
    _decision_badge, _score_badge, _status_badge, _compat_badge,
    _format_salary, _bookmark_detail_btn, _job_card,
    _get_job_by_id,
)

logger = logging.getLogger(__name__)

saved_rt = APIRouter(prefix="/saved")

_ROUTE_PREFIX = "saved"

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
        if (cfg && cfg.path && cfg.path.startsWith('/saved/jobs')) {{
            var form = document.getElementById('filter-form');
            if (form) {{
                var params = new URLSearchParams(new FormData(form));
                history.pushState(null, '', {repr(BASE_PREFIX)} + '/saved/?' + params.toString());
            }}
        }}
    }});
}})();
""")


def _filter_bar(keyword=""):
    _keyword_js = "clearTimeout(this._t);this._t=setTimeout(()=>this.form.requestSubmit(),400)"
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
            cls="flex flex-wrap items-end gap-2",
        ),
        id="filter-form",
        hx_get="/saved/jobs",
        hx_target="#job-results",
        hx_trigger="submit",
        hx_indicator="#list-spinner",
    )


@saved_rt("/")
def get(auth, sess, keyword: str = ""):
    from urllib.parse import urlencode
    user_info = sess.get("user_info", {})
    initial_qs = urlencode(dict(keyword=keyword))

    content = Div(
        Div(_filter_bar(keyword), id="filter-bar-container", cls="mb-2"),
        Div(
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
                        hx_get="/saved/jobs",
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
                    hx_get=f"/saved/jobs?{initial_qs}",
                    hx_trigger="load",
                    hx_indicator="#list-spinner",
                    cls="space-y-1.5",
                ),
                id="job-list-pane",
                cls="overflow-y-auto",
                style="height: calc(100vh - 160px)",
            ),
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
    return NavigationLayout(content, title="Saved Jobs", current_path="/saved", user_info=user_info)


_PAGE_SIZE = 100


def _jobs_response(*parts):
    from starlette.responses import HTMLResponse
    html = "".join(to_xml(p) for p in parts if p is not None)
    return HTMLResponse(html, headers={"Cache-Control": "private, max-age=60"})


@saved_rt("/jobs", methods=["GET"])
def get_jobs(auth, keyword: str = "", page: int = 1):
    user = get_auth_user(auth)
    jobs = get_jobs_for_user(user, keyword=keyword, review_status="saved")

    total = len(jobs)
    start = (page - 1) * _PAGE_SIZE
    page_jobs = jobs[start: start + _PAGE_SIZE]
    end = start + len(page_jobs)

    from urllib.parse import urlencode

    def _load_more_btn(page):
        qs = urlencode(dict(keyword=keyword, page=page))
        return Button(
            "Load More",
            hx_get=f"/saved/jobs?{qs}",
            hx_target="this",
            hx_swap="outerHTML",
            hx_indicator="#list-spinner",
            cls=f"{ButtonT.default} text-sm w-full mt-2",
        )

    cards = [_job_card(j, route_prefix=_ROUTE_PREFIX) for j in page_jobs]
    if end < total:
        cards.append(_load_more_btn(page + 1))

    if page == 1:
        if not jobs:
            return _jobs_response(P("0 jobs", cls="text-sm text-muted-foreground font-medium mb-1"), P("No saved jobs found.", cls="text-muted-foreground text-sm p-4"))
        count_label = f"{total} saved job{'s' if total != 1 else ''}"
        if total > _PAGE_SIZE:
            count_label += f" (showing first {_PAGE_SIZE})"
        return _jobs_response(P(count_label, cls="text-sm text-muted-foreground font-medium mb-1"), *cards)

    return _jobs_response(*cards)


@saved_rt("/job/{job_id}", methods=["GET"])
def get_job_detail(job_id: int, auth):
    user = get_auth_user(auth)
    job = _get_job_by_id(job_id, user)
    if not job:
        return P("Job not found.", cls="text-muted-foreground text-sm")

    profiles = get_profiles_for_user(user)
    default_profile = next((p["profile_name"] for p in profiles if p.get("is_default")), "")

    salary = _format_salary(job)
    has_description = bool((job.get("description") or "").strip())

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

    _detail_btn = "flex items-center gap-1.5 text-sm px-3 py-1.5 rounded border hover:bg-accent transition-colors"
    apply_btn = Button(
        UkIcon("send", ratio=0.85), Span("Apply"),
        hx_post=f"/search/apply/{job_id}",
        hx_target=f"#apply-result-{job_id}",
        hx_swap="innerHTML",
        cls=f"{_detail_btn} text-green-700 border-green-300 hover:bg-green-50 dark:text-green-400 dark:border-green-800 dark:hover:bg-green-950",
    )
    bookmark_btn = _bookmark_detail_btn(job_id, job.get("review_status") or "not_reviewed", route_prefix=_ROUTE_PREFIX)
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
        Button(
            UkIcon("arrow-left", ratio=0.85), Span("Back to results"),
            onclick=(
                "document.getElementById('job-detail-pane').classList.remove('mobile-open');"
                "document.getElementById('job-list-pane').classList.remove('mobile-hidden');"
                "document.getElementById('filter-bar-container').classList.remove('mobile-hidden');"
            ),
            cls="md:hidden flex items-center gap-1.5 text-sm mb-3 px-2 py-1 rounded border hover:bg-accent transition-colors",
        ),
        H3(job.get("title", ""), cls="font-bold text-base leading-tight"),
        P(
            Span(job.get("company", ""), cls="font-medium"),
            Span(" · ", cls="text-muted-foreground"),
            Span(job.get("location", ""), cls="text-muted-foreground text-sm"),
            cls="text-sm mt-0.5",
        ),
        A(job.get("job_url", ""), href=job.get("job_url", ""), target="_blank",
          cls="text-xs text-primary hover:underline truncate block mt-0.5"),
        Div(apply_btn, bookmark_btn, reject_btn, cls="flex gap-4 mt-2 flex-wrap"),
        Div(id=f"apply-result-{job_id}", cls="mt-1 text-sm"),
        Div(
            Div(Span("Remote: ", cls="font-medium text-xs"), Span("Yes" if is_remote else "No", cls="text-xs")),
            Div(Span("Travel: ", cls="font-medium text-xs"), Span(travel_detail or "N/A", cls="text-xs")),
            Div(Span("Salary: ", cls="font-medium text-xs"), Span(salary or "N/A", cls="text-xs")),
            Div(Span("Clearance: ", cls="font-medium text-xs"), Span("Required" if job.get("security_clearance_required") else "Not required", cls="text-xs")),
            cls="grid grid-cols-2 gap-x-4 gap-y-0.5 mt-2 border rounded p-2",
        ),
        Div(
            H4("Job Description", cls="text-sm font-semibold"),
            Div(
                TextArea(
                    job.get("description", "") or "",
                    id=f"desc-{job_id}",
                    name="description",
                    rows=10,
                    cls="text-xs w-full",
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
                    hx_post=f"/search/score/{job_id}",
                    hx_target=f"#score-result-{job_id}",
                    hx_indicator=f"#score-spinner-{job_id}",
                    hx_include=f"#desc-{job_id}, #score-profile-{job_id}",
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
        Div(
            stored_score_html,
            id=f"score-result-{job_id}",
            cls="mt-2",
        ),
    )


@saved_rt("/bookmark/{job_id}", methods=["POST"])
def post_bookmark(job_id: int, auth):
    user = get_auth_user(auth)
    job = _get_job_by_id(job_id, user)
    if not job:
        return Div(id=f"job-card-{job_id}")
    current = job.get("review_status") or "not_reviewed"
    new_status = "not_reviewed" if current == "saved" else "saved"
    update_job_review_status(job_id, new_status, user)
    # On the saved page, unsaving removes the card from the list
    if new_status == "not_reviewed":
        return (
            Div(id=f"job-card-{job_id}"),
            _bookmark_detail_btn(job_id, new_status, oob=True, route_prefix=_ROUTE_PREFIX),
        )
    job = _get_job_by_id(job_id, user)
    if not job:
        return Div(id=f"job-card-{job_id}")
    return (
        _job_card(job, route_prefix=_ROUTE_PREFIX),
        _bookmark_detail_btn(job_id, new_status, oob=True, route_prefix=_ROUTE_PREFIX),
    )
