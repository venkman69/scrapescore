"""
Applied Jobs page for job_score.
Two-pane layout identical to Search/Saved, filtered to review_status='applied'.
Scoring uses the resume blob stored at application time, with fallback to profile resume.
"""

import io
import json
import logging

from fasthtml.common import *
from monsterui.all import *

from .common import NavigationLayout, get_auth_user
from .lib.config import BASE_PREFIX
from .profiles import remove_pii
from .db import (
    get_applied_jobs_for_user,
    get_applied_job_by_id,
    withdraw_job,
    get_applied_status_history,
    add_applied_status_event,
    update_applied_job_notes,
    update_applied_job_resume,
    update_applied_job_fields,
    create_applied_job,
    get_profiles_for_user,
    get_default_profile,
    update_job_description,
)
from .score import render_ats_score
from scrapescore.lib.gemini_ai_runner import ats_score_analyzer_gemini
from scrapescore.lib import utils
from .search import _format_salary, _get_job_by_id, _decision_badge, _score_badge, _compat_badge

logger = logging.getLogger(__name__)

applied_rt = APIRouter(prefix="/applied")

_ROUTE_PREFIX = "applied"

_APPLIED_RESUME_JS = """
function appliedResumeDragOver(event, dzId) {
    event.preventDefault();
    var dz = document.getElementById(dzId);
    if (dz) { dz.classList.add('border-blue-400', 'bg-blue-50', 'dark:bg-blue-950/30'); }
}
function appliedResumeDragLeave(event, dzId) {
    var dz = document.getElementById(dzId);
    if (dz) { dz.classList.remove('border-blue-400', 'bg-blue-50', 'dark:bg-blue-950/30'); }
}
function appliedResumeDrop(event, dzId, jobId) {
    event.preventDefault();
    var dz = document.getElementById(dzId);
    if (dz) { dz.classList.remove('border-blue-400', 'bg-blue-50', 'dark:bg-blue-950/30'); }
    var file = event.dataTransfer.files[0];
    if (file) processAppliedResumeFile(file, jobId);
}
function processAppliedResumeFile(file, jobId) {
    var allowed = ['.pdf', '.txt', '.md'];
    var name = file.name.toLowerCase();
    if (!allowed.some(function(ext) { return name.endsWith(ext); })) {
        alert('Please select a PDF, TXT or Markdown file.');
        return;
    }
    var statusEl = document.getElementById('resume-result-' + jobId);
    if (statusEl) statusEl.innerHTML = '<span uk-spinner="ratio: 0.5"></span> Uploading…';
    var fd = new FormData();
    fd.append('resume_file', file);
    fetch(_APPLIED_BASE + '/applied/upload-resume/' + jobId, { method: 'POST', body: fd })
        .then(function(r) {
            if (!r.ok) throw new Error('Upload failed (' + r.status + ')');
            return r.text();
        })
        .then(function(html) {
            var section = document.getElementById('resume-section-' + jobId);
            if (section) section.outerHTML = html;
        })
        .catch(function(err) {
            var statusEl2 = document.getElementById('resume-result-' + jobId);
            if (statusEl2) statusEl2.innerHTML = '<span class="text-red-500">' + err.message + '</span>';
        });
}
"""


def _resume_section(job_id: int, has_resume_blob: bool, sm_btn: str, error_msg: str = "") -> FT:
    dropzone_id = f"resume-dropzone-{job_id}"
    file_input_id = f"resume-file-input-{job_id}"

    indicator = Span(
        UkIcon("file-check", ratio=0.8),
        " Resume on file",
        cls="text-xs text-green-600 dark:text-green-400",
    ) if has_resume_blob else Span(
        UkIcon("file-x", ratio=0.8),
        " No resume uploaded",
        cls="text-xs text-muted-foreground",
    )

    result_content = Span(error_msg, cls="text-xs text-destructive") if error_msg else None

    pdf_viewer = Details(
        Summary("View Resume", cls="text-xs cursor-pointer text-blue-600 hover:text-blue-800 dark:text-blue-400 mt-1"),
        Div(
            Iframe(
                src=f"{BASE_PREFIX}/applied/resume/{job_id}",
                width="100%",
                height="500",
                style="border:1px solid #ccc;border-radius:4px;display:block;",
            ),
            cls="mt-1",
        ),
    ) if has_resume_blob else None

    return Div(
        H4("Resume", cls="text-sm font-semibold"),
        Div(
            indicator,
            Div(result_content, id=f"resume-result-{job_id}", cls="text-xs"),
            cls="flex items-center gap-3 mt-1",
        ),
        Div(
            Div(
                Span(
                    "Drag & drop a PDF, TXT, or MD file here, or click Upload",
                    cls="text-xs text-muted-foreground",
                ),
                cls="flex items-center justify-center py-2",
            ),
            Input(
                type="file",
                name="resume_file",
                accept=".pdf,.txt,.md",
                id=file_input_id,
                style="display:none",
                onchange=f"processAppliedResumeFile(this.files[0], {job_id}); this.value='';",
            ),
            Button(
                "Upload Resume",
                type="button",
                onclick=f"document.getElementById('{file_input_id}').click()",
                cls=f"{sm_btn} mt-1",
            ),
            id=dropzone_id,
            cls="mt-1 border-2 border-dashed border-gray-300 dark:border-gray-600 rounded p-2",
            ondragover=f"appliedResumeDragOver(event, '{dropzone_id}')",
            ondragleave=f"appliedResumeDragLeave(event, '{dropzone_id}')",
            ondrop=f"appliedResumeDrop(event, '{dropzone_id}', {job_id})",
        ),
        pdf_viewer,
        id=f"resume-section-{job_id}",
        cls="mt-3",
    )


_ALL_STATUSES = [
    ("submitted",            "Submitted"),
    ("initial_criteria_met", "Initial Criteria Met"),
    ("screen_scheduled",     "Screen Scheduled"),
    ("screen_completed",     "Screen Completed"),
    ("interview_scheduled",  "Interview Scheduled"),
    ("interview_completed",  "Interview Completed"),
    ("offer",                "Offer"),
    ("offer_accepted",       "Offer Accepted"),
    ("offer_declined",       "Offer Declined"),
    ("not_considered",       "Not Considered"),
    ("withdrawn",            "Withdrawn"),
    ("expired",              "Expired"),
]

_TERMINAL_STATUSES = (
    "offer_accepted",
    "offer_declined",
    "not_considered",
    "withdrawn",
    "expired",
)

_STATUS_BADGE_CLS = {
    "submitted":            "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
    "initial_criteria_met": "bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300",
    "screen_scheduled":     "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300",
    "screen_completed":     "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/40 dark:text-yellow-300",
    "interview_scheduled":  "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300",
    "interview_completed":  "bg-orange-100 text-orange-700 dark:bg-orange-900/40 dark:text-orange-300",
    "offer":                "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300",
    "offer_accepted":       "bg-green-100 text-green-700 dark:bg-green-900/40 dark:text-green-300",
    "offer_declined":       "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300",
    "not_considered":       "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
    "withdrawn":            "bg-slate-100 text-slate-500 dark:bg-slate-600/50 dark:text-slate-100",
    "expired":              "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
}

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
        if (cfg && cfg.path && cfg.path.startsWith('/applied/jobs')) {{
            var form = document.getElementById('filter-form');
            if (form) {{
                var params = new URLSearchParams(new FormData(form));
                history.pushState(null, '', {repr(BASE_PREFIX)} + '/applied/?' + params.toString());
            }}
        }}
    }});
}})();
""")


def _status_badge(status: str) -> FT:
    label = dict(_ALL_STATUSES).get(status, status.replace("_", " ").title())
    cls = _STATUS_BADGE_CLS.get(status, "bg-slate-100 text-slate-500")
    return Span(label, cls=f"text-xs font-semibold px-1.5 py-0.5 rounded {cls}")


def _applied_job_card(job: dict) -> FT:
    jid = job["id"]
    status = job.get("current_status_latest", "submitted") or "submitted"
    changed_at = (job.get("last_status_date") or "")[:10]
    applied_date = (job.get("applied_at") or "")[:10]
    salary = _format_salary(job)
    clearance = "Clearance Req." if job.get("security_clearance_required") else ""
    score = job.get("job_score") or 0
    compat = job.get("title_compatibility_score") or ""
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

    _stop = "event.stopPropagation()"
    withdraw_btn = Button(
        UkIcon("undo-2", ratio=0.85),
        hx_post=f"/applied/withdraw/{jid}",
        hx_target=f"#job-card-{jid}",
        hx_swap="outerHTML",
        hx_confirm="Withdraw this application? The job will return to your search list.",
        onclick=_stop,
        title="Withdraw Application",
        cls="p-0.5 text-muted-foreground hover:text-red-500 transition-colors",
    )

    return Div(
        Div(
            # Left content
            Div(
                # Row 1: title
                Span(job.get("title", ""), cls="font-semibold text-sm leading-tight line-clamp-2"),
                # Row 2: decision + score + compat badges
                Div(
                    _decision_badge(decision) if decision else None,
                    _score_badge(score),
                    _compat_badge(compat) if compat else None,
                    cls="flex flex-wrap gap-1 mt-0.5 items-center",
                ),
                # Row 3: application status + changed_at date
                Div(
                    _status_badge(status),
                    Span(changed_at, cls="text-xs text-muted-foreground whitespace-nowrap") if changed_at else None,
                    cls="flex items-center gap-2 mt-0.5",
                ),
                # Row 4: company + applied_at date
                Div(
                    Span(job.get("company", ""), cls="text-xs text-muted-foreground truncate flex-1"),
                    Span(applied_date, cls="text-xs text-muted-foreground whitespace-nowrap"),
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
            # Right: withdraw button
            Div(withdraw_btn, cls="flex flex-col items-center gap-1 shrink-0 ml-1"),
            cls="flex gap-1 items-start",
        ),
        id=f"job-card-{jid}",
        hx_get=f"/applied/job/{jid}",
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


def _filter_bar(keyword="", sort_by="recent_activity", show_closed=False):
    _NativeSelect = __import__("fasthtml.common", fromlist=["Select"]).Select
    _keyword_js = "clearTimeout(this._t);this._t=setTimeout(()=>this.form.requestSubmit(),400)"
    _submit_js = "this.form.requestSubmit()"

    sort_opts = [
        ("recent_activity", "Recent Activity"),
        ("applied_at",      "Applied Date"),
        ("title",           "Title"),
        ("company",         "Company"),
    ]

    _archive_js = (
        "var i=document.getElementById('show-closed-val');"
        "i.value=i.value?'':'on';"
        "this.dataset.active=String(i.value==='on');"
        "var u=new window.URL(window.location.href);"
        "i.value?u.searchParams.set('show_closed','on'):u.searchParams.delete('show_closed');"
        "history.replaceState({},'',u);"
        "this.closest('form').requestSubmit();"
    )
    _btn_base = "text-sm border rounded px-2 py-0.5 flex items-center gap-1 cursor-pointer transition-colors select-none"

    return Form(
        Style(
            "#archive-toggle[data-active=true]{background:hsl(var(--primary));color:hsl(var(--primary-foreground));border-color:hsl(var(--primary))}"
            " #archive-toggle[data-active=false] #archive-check{display:none}"
        ),
        Div(
            Input(
                id="filter-keyword", name="keyword", value=keyword,
                placeholder="Search title or company...",
                cls="text-sm flex-1 min-w-36", oninput=_keyword_js,
            ),
            _NativeSelect(
                *[Option(label, value=val, selected=(val == sort_by)) for val, label in sort_opts],
                name="sort_by",
                cls="text-sm bg-background text-foreground border border-input rounded px-1",
                onchange=_submit_js,
            ),
            Input(type="hidden", id="show-closed-val", name="show_closed",
                  value="on" if show_closed else ""),
            Button(
                UkIcon("archive", ratio=0.8),
                Span("Archived"),
                UkIcon("check", ratio=0.75, id="archive-check"),
                id="archive-toggle",
                type="button",
                data_active="true" if show_closed else "false",
                onclick=_archive_js,
                cls=f"{_btn_base} bg-background text-foreground border-input",
            ),
            Button(
                UkIcon("plus", ratio=0.8), Span("New Job"),
                type="button",
                hx_get="/applied/new-job-form",
                hx_target="#job-detail-pane",
                hx_swap="innerHTML",
                onclick=(
                    "if(window.innerWidth<768){"
                    "document.getElementById('job-detail-pane').classList.add('mobile-open');"
                    "document.getElementById('job-list-pane').classList.add('mobile-hidden');"
                    "document.getElementById('filter-bar-container').classList.add('mobile-hidden');}"
                ),
                cls=f"{_btn_base} bg-primary text-primary-foreground border-primary",
            ),
            cls="flex flex-wrap items-center gap-2",
        ),
        id="filter-form",
        hx_get="/applied/jobs",
        hx_target="#job-results",
        hx_trigger="submit",
        hx_indicator="#list-spinner",
    )


@applied_rt("/")
def get(auth, sess, keyword: str = "", sort_by: str = "recent_activity", show_closed: str = ""):
    from urllib.parse import urlencode
    user_info = sess.get("user_info", {})
    _show_closed = show_closed == "on"
    initial_qs = urlencode({k: v for k, v in dict(keyword=keyword, sort_by=sort_by, show_closed=show_closed).items() if v})

    content = Div(
        Div(_filter_bar(keyword, sort_by, show_closed=_show_closed), id="filter-bar-container", cls="mb-2"),
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
                        hx_get="/applied/jobs",
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
                    hx_get=f"/applied/jobs?{initial_qs}",
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
    return NavigationLayout(content, title="Applied Jobs", current_path="/applied", user_info=user_info)


_PAGE_SIZE = 100


def _jobs_response(*parts):
    from starlette.responses import HTMLResponse
    html = "".join(to_xml(p) for p in parts if p is not None)
    return HTMLResponse(html, headers={"Cache-Control": "private, max-age=60"})


@applied_rt("/jobs", methods=["GET"])
def get_jobs(auth, keyword: str = "", sort_by: str = "recent_activity",
             show_closed: str = "", page: int = 1):
    user = get_auth_user(auth)
    exclude = () if show_closed == "on" else _TERMINAL_STATUSES
    jobs = get_applied_jobs_for_user(user, keyword=keyword, sort_by=sort_by,
                                     exclude_statuses=exclude)

    total = len(jobs)
    start = (page - 1) * _PAGE_SIZE
    page_jobs = jobs[start: start + _PAGE_SIZE]
    end = start + len(page_jobs)

    from urllib.parse import urlencode

    def _load_more_btn(page):
        qs = urlencode(dict(keyword=keyword, sort_by=sort_by, show_closed=show_closed, page=page))
        return Button(
            "Load More",
            hx_get=f"/applied/jobs?{qs}",
            hx_target="this",
            hx_swap="outerHTML",
            hx_indicator="#list-spinner",
            cls=f"{ButtonT.default} text-sm w-full mt-2",
        )

    cards = [_applied_job_card(j) for j in page_jobs]
    if end < total:
        cards.append(_load_more_btn(page + 1))

    if page == 1:
        if not jobs:
            return _jobs_response(
                P("0 jobs", cls="text-sm text-muted-foreground font-medium mb-1"),
                P("No applied jobs found.", cls="text-muted-foreground text-sm p-4"),
            )
        count_label = f"{total} applied job{'s' if total != 1 else ''}"
        if total > _PAGE_SIZE:
            count_label += f" (showing first {_PAGE_SIZE})"
        return _jobs_response(P(count_label, cls="text-sm text-muted-foreground font-medium mb-1"), *cards)

    return _jobs_response(*cards)


def _history_section(job_id: int) -> FT:
    history = get_applied_status_history(job_id)
    status_opts = [Option("Select status...", value="", selected=True)] + [
        Option(label, value=val) for val, label in _ALL_STATUSES
    ]
    from datetime import date as _date
    today = _date.today().isoformat()

    timeline = []
    for h in history:
        status_cls = _STATUS_BADGE_CLS.get(h["status"], "bg-slate-100 text-slate-500")
        timeline.append(
            Div(
                Div(
                    _status_badge(h["status"]),
                    Span(h.get("changed_at", "")[:10], cls="text-xs text-muted-foreground ml-2"),
                    cls="flex items-center",
                ),
                P(h.get("notes") or "", cls="text-xs text-muted-foreground mt-0.5") if h.get("notes") else None,
                cls="border-l-2 border-slate-200 dark:border-slate-700 pl-2 py-0.5",
            )
        )

    _sm_btn = f"{ButtonT.default} text-xs"
    return Div(
        H4("Application Status History", cls="text-sm font-semibold mb-1"),
        Div(*timeline, cls="space-y-1.5 mb-3") if timeline else P("No events yet.", cls="text-xs text-muted-foreground mb-3"),
        Div(
            H5("Add Event", cls="text-xs font-semibold mb-1"),
            Div(
                Select(
                    *status_opts,
                    id=f"new-status-{job_id}",
                    name="status",
                    cls="text-xs flex-1",
                ),
                Input(
                    id=f"new-date-{job_id}", name="changed_at",
                    type="date", value=today,
                    cls="text-xs w-32",
                ),
                Button(
                    "Add Event",
                    hx_post=f"/applied/add-event/{job_id}",
                    hx_include=f"#new-status-{job_id}, #new-notes-{job_id}, #new-date-{job_id}",
                    hx_target=f"#history-{job_id}",
                    hx_swap="outerHTML",
                    cls=_sm_btn,
                ),
                cls="flex flex-wrap gap-2 items-center",
            ),
            TextArea(
                id=f"new-notes-{job_id}", name="notes",
                placeholder="Notes (optional)",
                rows=3,
                cls="text-xs w-full mt-1",
            ),
        ),
        id=f"history-{job_id}",
        cls="mt-3",
    )




def _fmt_amount(v) -> str:
    if v is None:
        return ""
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else str(f)
    except (ValueError, TypeError):
        return str(v)


def _job_fields_section(job: dict, job_id: int) -> FT:
    is_remote = str(job.get("is_remote", "")).lower() in ("true", "1", "yes")
    applied_date = (job.get("applied_at") or "")[:10]
    clearance = bool(job.get("security_clearance_required"))
    salary = _format_salary(job)

    travel = None
    try:
        score_data = json.loads(job.get("job_score_json") or "{}")
        tv = score_data.get("travel_required")
        if tv is not None:
            try:
                travel = f"{int(float(tv))}%"
            except (ValueError, TypeError):
                travel = str(tv)
    except Exception:
        pass

    return Div(
        # Title + Edit button
        Div(
            Div(
                H3(job.get("title", ""), cls="font-bold text-base leading-tight"),
                P(
                    Span(job.get("company", ""), cls="font-medium"),
                    Span(" · ", cls="text-muted-foreground") if job.get("location") else None,
                    Span(job.get("location", ""), cls="text-muted-foreground text-sm") if job.get("location") else None,
                    cls="text-sm mt-0.5",
                ),
                cls="flex-1 min-w-0",
            ),
            Button(
                UkIcon("pencil", ratio=0.75), Span("Edit"),
                type="button",
                hx_get=f"/applied/edit-job-form/{job_id}",
                hx_target="#job-detail-pane",
                hx_swap="innerHTML",
                cls=f"{ButtonT.default} text-xs flex items-center gap-1 shrink-0",
            ),
            cls="flex items-start justify-between gap-2",
        ),
        # URL
        A(job.get("job_url") or "", href=job.get("job_url") or "#", target="_blank",
          cls="text-xs text-primary hover:underline truncate block mt-0.5") if job.get("job_url") else None,
        # Details grid
        Div(
            Div(Span("Applied: ", cls="font-medium text-xs"), Span(applied_date or "—", cls="text-xs")),
            Div(Span("Travel: ", cls="font-medium text-xs"), Span(travel or "N/A", cls="text-xs")),
            Div(Span("Remote: ", cls="font-medium text-xs"), Span("Yes" if is_remote else "No", cls="text-xs")),
            Div(Span("Clearance: ", cls="font-medium text-xs"), Span("Required" if clearance else "Not required", cls="text-xs")),
            Div(Span("Salary: ", cls="font-medium text-xs"), Span(salary or "N/A", cls="text-xs"), cls="col-span-2"),
            cls="grid grid-cols-2 gap-x-4 gap-y-0.5 mt-1 border rounded p-2",
        ),
    )


def _job_form(job: dict = None, job_id: int = None) -> FT:
    """Shared form for create and edit. job=None → new job; job+job_id → edit."""
    from datetime import date as _date
    is_edit = job_id is not None
    today = _date.today().isoformat()
    j = job or {}

    applied_val = (j.get("applied_at") or today)[:10]
    is_remote = str(j.get("is_remote", "")).lower() in ("true", "1", "yes")
    clearance = bool(j.get("security_clearance_required"))

    _sm_btn = f"{ButtonT.default} text-xs"
    _lbl = "text-xs font-medium shrink-0 w-16"
    _inp = "text-sm flex-1 min-w-0"
    _row_cls = "flex items-center gap-2"

    action = f"/applied/update-job/{job_id}" if is_edit else "/applied/create"

    def _field(label, **kw):
        return Div(Label(label, cls=_lbl), Input(cls=_inp, **kw), cls=_row_cls)

    def _toggle(label, checked=False, **kw):
        extra = {"checked": True} if checked else {}
        return Label(
            Switch(cls="shrink-0", **extra, **kw),
            Span(label, cls="text-xs font-medium ml-2 cursor-pointer select-none"),
            cls="flex items-center gap-2 cursor-pointer",
        )

    cancel_btn = Button(
        "Cancel", type="button",
        hx_get=f"/applied/job/{job_id}",
        hx_target="#job-detail-pane",
        hx_swap="innerHTML",
        cls=f"{ButtonT.default} text-sm mt-3",
    ) if is_edit else None

    return Form(
        H3("Edit Job" if is_edit else "Add Applied Job", cls="font-bold text-base mb-2"),
        Div(
            _field("Title", name="title", placeholder="Job title", value=j.get("title") or ""),
            Div(Label("Applied", cls=_lbl),
                Input(cls=_inp, name="applied_at", type="date", value=applied_val,
                      onclick="try{this.showPicker()}catch(e){}"),
                cls=_row_cls),
            Div(
                _field("Company", name="company", placeholder="Company name", value=j.get("company") or ""),
                _field("Location", name="location", placeholder="City, State", value=j.get("location") or ""),
                cls="grid grid-cols-1 sm:grid-cols-2 gap-2",
            ),
            _field("URL", name="job_url", id="new-job-url", placeholder="https://...", value=j.get("job_url") or ""),
            Div(
                Span("Salary (USD)", cls="text-xs font-semibold text-muted-foreground"),
                Div(
                    _field("Min $", name="min_amount", placeholder="100000", value=_fmt_amount(j.get("min_amount"))),
                    _field("Max $", name="max_amount", placeholder="150000", value=_fmt_amount(j.get("max_amount"))),
                    cls="grid grid-cols-1 sm:grid-cols-2 gap-2",
                ),
                _field("Interval", name="interval", placeholder="yearly / hourly", value=j.get("interval") or ""),
                cls="space-y-1.5 border-l-2 border-muted pl-2",
            ),
            Div(
                _toggle("Remote", checked=is_remote, name="is_remote", value="true"),
                _toggle("Clearance Required", checked=clearance, name="security_clearance_required", value="1"),
                Button("Save Changes", type="submit", cls=f"{ButtonT.primary} text-sm mt-1") if is_edit else None,
                cls="flex flex-col gap-2.5 pt-1",
            ),
            cls="space-y-2 border rounded p-2",
        ),
        Div(
            H4("Description", cls="text-sm font-semibold"),
            Div(
                Button("Retrieve from URL", type="button",
                       hx_post="/applied/retrieve-description",
                       hx_include="#new-job-url",
                       hx_target="#new-job-desc-wrap",
                       hx_swap="innerHTML",
                       hx_indicator="#retrieve-new-spinner",
                       cls=_sm_btn),
                Div(Div(cls="uk-spinner uk-spinner-small", uk_spinner=True),
                    id="retrieve-new-spinner", cls="htmx-indicator"),
                cls="flex items-center gap-2 mt-1 mb-1",
            ),
            Div(
                TextArea(j.get("description") or "", id="new-job-desc", name="description",
                         rows=10, cls="text-xs w-full"),
                id="new-job-desc-wrap",
            ),
            cls="mt-3",
        ),
        Div(
            Button("Save Changes" if is_edit else "Create Job",
                   type="submit", cls=f"{ButtonT.primary} text-sm mt-3"),
            cancel_btn,
            cls="flex items-center gap-3",
        ),
        hx_post=action,
        hx_target="#job-detail-pane",
        hx_swap="innerHTML",
        cls="p-2",
    )


@applied_rt("/job/{job_id}", methods=["GET"])
def get_job_detail(job_id: int, auth):
    user = get_auth_user(auth)
    job = get_applied_job_by_id(job_id, user)
    if not job:
        return P("Job not found.", cls="text-muted-foreground text-sm")

    profiles = get_profiles_for_user(user)
    default_profile = next((p["profile_name"] for p in profiles if p.get("is_default")), None) or (profiles[0]["profile_name"] if profiles else "")

    has_resume_blob = bool(job.get("resume"))
    has_description = bool((job.get("description") or "").strip())

    _sm_btn = f"{ButtonT.default} text-xs"
    _detail_btn = "flex items-center gap-1.5 text-sm px-3 py-1.5 rounded border hover:bg-accent transition-colors"

    withdraw_btn = Button(
        UkIcon("undo-2", ratio=0.85), Span("Withdraw Application"),
        hx_post=f"/applied/withdraw/{job_id}",
        hx_target=f"#job-card-{job_id}",
        hx_swap="outerHTML",
        hx_confirm="Withdraw this application? The job will return to your search list.",
        cls=f"{_detail_btn} text-destructive border-destructive/30 hover:bg-destructive/10 dark:text-red-400 dark:border-red-700/50 dark:hover:bg-red-900/20",
    )

    score_btn_cls = _sm_btn + (" opacity-50 cursor-not-allowed" if not has_description else "")

    stored_score_html = None
    score_json_str = job.get("job_score_json") or "{}"
    try:
        import json as _json
        score_data = _json.loads(score_json_str)
        if score_data and score_data != {}:
            stored_score_html = render_ats_score(score_data)
    except Exception:
        pass

    return Div(
        # Mobile back button
        Button(
            UkIcon("arrow-left", ratio=0.85), Span("Back to results"),
            onclick=(
                "document.getElementById('job-detail-pane').classList.remove('mobile-open');"
                "document.getElementById('job-list-pane').classList.remove('mobile-hidden');"
                "document.getElementById('filter-bar-container').classList.remove('mobile-hidden');"
            ),
            cls="md:hidden flex items-center gap-1.5 text-sm mb-3 px-2 py-1 rounded border hover:bg-accent transition-colors",
        ),
        # Editable job fields
        _job_fields_section(job, job_id),
        Div(withdraw_btn, cls="flex gap-4 mt-2 flex-wrap"),
        # Job Notes
        Div(
            H4("Job Notes", cls="text-sm font-semibold"),
            TextArea(
                job.get("job_notes") or "",
                id=f"notes-{job_id}",
                name="notes",
                rows=3,
                cls="text-xs w-full mt-1",
                placeholder="Add notes about this application...",
            ),
            Div(
                Button(
                    "Save Notes",
                    hx_post=f"/applied/save-notes/{job_id}",
                    hx_include=f"#notes-{job_id}",
                    hx_target=f"#notes-result-{job_id}",
                    hx_swap="innerHTML",
                    cls=_sm_btn,
                ),
                Div(id=f"notes-result-{job_id}", cls="text-xs"),
                cls="flex items-center gap-2 mt-1",
            ),
            cls="mt-3",
        ),
        # Status History
        _history_section(job_id),
        # Resume
        _resume_section(job_id, has_resume_blob, _sm_btn),
        # Job Description
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
        # Score Job
        Div(
            H4("Score Job", cls="text-sm font-semibold"),
            Alert(
                UkIcon("alert-triangle", ratio=0.8, cls="mr-1"),
                f"No resume uploaded. Scoring will use default profile resume: '{default_profile}'.",
                cls=AlertT.warning,
            ) if not has_resume_blob else None,
            Div(
                Button(
                    "Score Job",
                    hx_post=f"/applied/score/{job_id}",
                    hx_target=f"#score-result-{job_id}",
                    hx_indicator=f"#score-spinner-{job_id}",
                    hx_include=f"#desc-{job_id}",
                    hx_swap="innerHTML",
                    disabled=not has_description,
                    cls=score_btn_cls,
                ),
                cls="flex gap-2 items-center mt-1 flex-wrap",
            ),
            Div(
                Div(cls="uk-spinner uk-spinner-small", uk_spinner=True),
                id=f"score-spinner-{job_id}",
                cls="htmx-indicator mt-1",
            ),
            Div(stored_score_html, id=f"score-result-{job_id}", cls="mt-2"),
            cls="mt-3",
        ),
    ), Script(f"var _APPLIED_BASE = {repr(BASE_PREFIX)};", type="text/javascript"), Script(_APPLIED_RESUME_JS, type="text/javascript")


@applied_rt("/new-job-form", methods=["GET"])
def get_new_job_form(auth):
    return _job_form()


@applied_rt("/edit-job-form/{job_id}", methods=["GET"])
def get_edit_job_form(job_id: int, auth):
    user = get_auth_user(auth)
    job = get_applied_job_by_id(job_id, user)
    if not job:
        return P("Job not found.", cls="text-muted-foreground text-sm p-2")
    return _job_form(job, job_id)


@applied_rt("/retrieve-description", methods=["POST"])
def post_retrieve_description(job_url: str = "", auth=None):
    _empty_ta = TextArea(id="new-job-desc", name="description", rows=10, cls="text-xs w-full")
    if not job_url or not job_url.strip():
        return _empty_ta, P("Enter a URL first.", cls="text-xs text-destructive mt-0.5")
    try:
        text = utils.get_markdown_from_url(job_url.strip())
        status_el = (
            Span(f"✓ Retrieved {len(text)} chars.", cls="text-green-600 text-xs mt-0.5 block")
            if text.strip()
            else Span("No content found.", cls="text-destructive text-xs mt-0.5 block")
        )
        return TextArea(text, id="new-job-desc", name="description", rows=10, cls="text-xs w-full"), status_el
    except Exception as e:
        logger.exception("retrieve-description failed")
        return _empty_ta, Span(f"Retrieve failed: {e}", cls="text-destructive text-xs mt-0.5 block")


@applied_rt("/create", methods=["POST"])
def post_create(auth,
                title: str = "", company: str = "", job_url: str = "",
                location: str = "", applied_at: str = "",
                min_amount: str = "", max_amount: str = "",
                interval: str = "",
                is_remote: str = "", security_clearance_required: str = "",
                description: str = ""):
    if not title.strip() or not company.strip():
        return P("Title and Company are required.", cls="text-destructive text-sm p-2")
    user = get_auth_user(auth)
    job_data = dict(
        title=title.strip(), company=company.strip(),
        job_url=job_url.strip(), location=location.strip(),
        applied_at=applied_at, description=description,
        min_amount=min_amount, max_amount=max_amount,
        currency="USD", interval=interval,
        is_remote="true" if is_remote else "false",
        security_clearance_required=bool(security_clearance_required),
    )
    new_id = create_applied_job(job_data, user)
    if not new_id:
        return P("Failed to create job. Please try again.", cls="text-destructive text-sm p-2")
    job = get_applied_job_by_id(new_id, user)
    return (
        get_job_detail(new_id, auth),
        Div(_applied_job_card(job), hx_swap_oob="afterbegin:#job-results"),
    )



@applied_rt("/update-job/{job_id}", methods=["POST"])
def post_update_job(job_id: int, auth,
                    title: str = "", company: str = "", job_url: str = "",
                    location: str = "", applied_at: str = "",
                    min_amount: str = "", max_amount: str = "",
                    interval: str = "",
                    is_remote: str = "", security_clearance_required: str = "",
                    description: str = ""):
    if not title.strip() or not company.strip():
        return P("Title and Company are required.", cls="text-destructive text-sm p-2")
    user = get_auth_user(auth)
    update_applied_job_fields(
        job_id, user,
        title=title.strip(), company=company.strip(),
        job_url=job_url.strip(), location=location.strip(),
        applied_at=applied_at,
        min_amount=min_amount, max_amount=max_amount,
        currency="USD", interval=interval,
        is_remote="true" if is_remote else "false",
        security_clearance_required=bool(security_clearance_required),
    )
    if description is not None:
        update_job_description(job_id, description, user)
    job = get_applied_job_by_id(job_id, user)
    card_oob = Div(_applied_job_card(job), hx_swap_oob=f"outerHTML:#job-card-{job_id}") if job else None
    return get_job_detail(job_id, auth), card_oob


@applied_rt("/withdraw/{job_id}", methods=["POST"])
def post_withdraw(job_id: int, auth):
    user = get_auth_user(auth)
    withdraw_job(job_id, user)
    # Return empty div to remove card + clear detail pane via OOB
    return (
        Div(id=f"job-card-{job_id}"),
        Div(
            P("Job withdrawn. Select another job to view details.", cls="text-muted-foreground text-sm p-4"),
            id="job-detail-pane",
            hx_swap_oob="innerHTML",
        ),
    )


@applied_rt("/save-notes/{job_id}", methods=["POST"])
def post_save_notes(job_id: int, notes: str, auth):
    user = get_auth_user(auth)
    update_applied_job_notes(job_id, notes, user)
    return Span("Saved", cls="text-green-600 text-xs")


@applied_rt("/add-event/{job_id}", methods=["POST"])
def post_add_event(job_id: int, status: str, auth, notes: str = "", changed_at: str = ""):
    if not status:
        return _history_section(job_id)
    from datetime import date as _date
    if not changed_at:
        changed_at = _date.today().isoformat()
    add_applied_status_event(job_id, status, notes, changed_at)
    return _history_section(job_id)


@applied_rt("/upload-resume/{job_id}", methods=["POST"])
async def post_upload_resume(job_id: int, auth, req):
    user = get_auth_user(auth)
    form = await req.form()
    resume_file = form.get("resume_file")
    _sm_btn = f"{ButtonT.default} text-xs"
    upload_ok = False

    if resume_file and hasattr(resume_file, "read"):
        data = await resume_file.read()
        if data:
            update_applied_job_resume(job_id, data, user)
            upload_ok = True

    error_msg = "" if upload_ok else "Upload failed — please try again."
    return _resume_section(job_id, upload_ok, _sm_btn, error_msg=error_msg)


@applied_rt("/resume/{job_id}", methods=["GET"])
def get_resume_file(job_id: int, auth):
    from starlette.responses import Response as StarletteResponse
    user = get_auth_user(auth)
    job = get_applied_job_by_id(job_id, user)
    if not job or not job.get("resume"):
        return StarletteResponse("Not found", status_code=404)
    raw = job["resume"]
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    elif not isinstance(raw, (bytes, bytearray)):
        raw = str(raw).encode("utf-8")
    else:
        raw = bytes(raw)
    content_type = "application/pdf" if raw[:4] == b"%PDF" else "text/plain; charset=utf-8"
    return StarletteResponse(content=raw, media_type=content_type)


@applied_rt("/score/{job_id}", methods=["POST"])
def post_score(job_id: int, description: str, auth):
    user = get_auth_user(auth)
    job = get_applied_job_by_id(job_id, user)
    if not job:
        return Alert("Job not found.", cls=AlertT.error)

    profile = get_default_profile(user)
    if not profile:
        return Alert("No profile found for scoring.", cls=AlertT.error)

    # Use stored resume blob if available, else fall back to profile resume
    resume_blob = job.get("resume")
    if resume_blob:
        try:
            from markitdown import MarkItDown
            import tempfile, os
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
                f.write(resume_blob if isinstance(resume_blob, bytes) else bytes(resume_blob))
                tmp_path = f.name
            md = MarkItDown()
            resume_text = md.convert(tmp_path).markdown
            os.unlink(tmp_path)
        except Exception:
            resume_text = resume_blob.decode("utf-8", errors="ignore") if isinstance(resume_blob, (bytes, bytearray)) else str(resume_blob)
    else:
        resume_text = profile.get("resume", "")

    # Sanitize PII before sending to the scorer — stored blob is source-of-truth and untouched
    resume_text = remove_pii(resume_text)

    job_details_dict = {
        "job_url": job.get("job_url", ""),
        "job_id": str(job_id),
        "job_source": "applied",
    }

    try:
        result, _, _ = ats_score_analyzer_gemini(
            job_description=description or job.get("description", ""),
            resume=resume_text,
            desired_role_description=profile.get("desired_role_description", ""),
            job_details=job_details_dict,
            us_citizen=profile.get("us_citizen", True),
            security_clearance=profile.get("security_clearance", "none"),
            headless=True,
        )
        if "error" in result:
            return Alert(f"Scoring Error: {result['error']}", cls=AlertT.error)
        return render_ats_score(result)
    except Exception as e:
        logger.exception("Applied scoring failed")
        return Alert(f"Scoring Failed: {str(e)}", cls=AlertT.error)
