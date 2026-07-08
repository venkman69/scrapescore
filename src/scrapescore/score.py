"""
Scoring router for job_score.
Handles profile comparison against job postings using AI.
"""

import json
import logging
from fasthtml.common import *
from monsterui.all import *

from .common import NavigationLayout, get_auth_user
from .db import get_profiles_for_user, get_profile, create_applied_job, create_saved_job, update_job_score, _clearance_required_from_result
from .lib.config import BASE_PREFIX
from scrapescore.lib import utils
from scrapescore.lib.gemini_ai_runner import ats_score_analyzer_gemini

logger = logging.getLogger(__name__)

score_rt = APIRouter(prefix="/score")


def render_action_button(enabled=False, oob=False):
    """Renders the Score button."""
    btn_cls = f"{ButtonT.primary} font-bold shadow-lg"
    if not enabled:
        btn_cls += " opacity-50 cursor-not-allowed"

    btn = Button(
        DivLAligned(
            UkIcon("target", ratio=1.2, cls="mr-1"),
            Span("Score"),
        ),
        id="calculate-btn",
        name="calculate",
        cls=btn_cls,
        disabled=not enabled,
        hx_post="/score/calculate",
        hx_target="#scoring-results",
        hx_indicator="#progress-spinner",
        hx_include="#profile_name, #job_text, #job_url",
        hx_on__before_request="document.getElementById('scoring-results').innerHTML=''",
    )

    if oob:
        return Div(btn, id="action_btn_container", hx_swap_oob="innerHTML")
    return btn


def _add_to_applied_accordion():
    from datetime import date as _date
    today = _date.today().isoformat()
    _lbl = "text-xs font-medium shrink-0 w-16"
    _inp = "text-sm flex-1 min-w-0"
    _row_cls = "flex items-center gap-2"

    def _field(label, **kw):
        return Div(Label(label, cls=_lbl), Input(cls=_inp, **kw), cls=_row_cls)

    def _toggle(label, **kw):
        return Label(
            Switch(cls="shrink-0", **kw),
            Span(label, cls="text-xs font-medium ml-2 cursor-pointer select-none"),
            cls="flex items-center gap-2 cursor-pointer",
        )

    return Div(
        Ul(
            Li(
                Div(
                    DivFullySpaced(
                        DivLAligned(UkIcon("plus-circle", ratio=0.9, cls="mr-1"), Span("Add to Applied Jobs", cls="text-sm font-medium")),
                        UkIcon("chevron-down", cls="transition-transform duration-200"),
                    ),
                    cls="uk-accordion-title flex items-center justify-between cursor-pointer rounded border border-slate-200 dark:border-slate-700 px-3 py-2 hover:bg-slate-50 dark:hover:bg-slate-800",
                ),
                Div(
                    Form(
                        # Populated via OOB swap after scoring; empty until then
                        Input(type="hidden", id="score_json_result", name="score_json_result", value=""),
                        Div(
                            _field("Title", name="title", placeholder="Job title"),
                            Div(
                                Label("Applied", cls=_lbl),
                                Input(cls=_inp, name="applied_at", type="date", value=today,
                                      onclick="try{this.showPicker()}catch(e){}"),
                                cls=_row_cls,
                            ),
                            Div(
                                _field("Company", name="company", placeholder="Company name"),
                                _field("Location", name="location", placeholder="City, State"),
                                cls="grid grid-cols-1 sm:grid-cols-2 gap-2",
                            ),
                            Div(
                                Span("Salary (USD)", cls="text-xs font-semibold text-muted-foreground"),
                                Div(
                                    _field("Min $", name="min_amount", placeholder="100000"),
                                    _field("Max $", name="max_amount", placeholder="150000"),
                                    cls="grid grid-cols-1 sm:grid-cols-2 gap-2",
                                ),
                                _field("Interval", name="interval", placeholder="yearly / hourly"),
                                cls="space-y-1.5 border-l-2 border-muted pl-2",
                            ),
                            Div(
                                _toggle("Remote", name="is_remote", value="true"),
                                _toggle("Clearance Required", name="security_clearance_required", value="1"),
                                cls="flex flex-col gap-2.5 pt-1",
                            ),
                            cls="space-y-2 border rounded p-2",
                        ),
                        Div(
                            Button(
                                "Add to Applied Jobs",
                                id="add-applied-btn",
                                type="submit",
                                disabled=True,
                                cls=f"{ButtonT.primary} text-sm mt-3",
                            ),
                        ),
                        Div(id="add-applied-result", cls="mt-2"),
                        hx_post="/score/add-to-db",
                        hx_include="#job_url, #job_text",
                        hx_target="#add-applied-result",
                        hx_swap="innerHTML",
                        cls="p-2",
                    ),
                    cls="uk-accordion-content mt-2",
                ),
            ),
            uk_accordion="collapsible: true",
            cls="uk-accordion",
        ),
        Script("""
(function() {
  function syncAddBtn() {
    var url = document.getElementById('job_url');
    var btn = document.getElementById('add-applied-btn');
    if (url && btn) btn.disabled = !url.value.trim();
  }
  var url = document.getElementById('job_url');
  if (url) {
    url.addEventListener('input', syncAddBtn);
    url.addEventListener('change', syncAddBtn);
    syncAddBtn();
  }
})();
"""),
        id="add-applied-accordion",
        cls="mt-2",
    )


def _add_to_saved_accordion():
    _lbl = "text-xs font-medium shrink-0 w-16"
    _inp = "text-sm flex-1 min-w-0"
    _row_cls = "flex items-center gap-2"

    def _field(label, **kw):
        return Div(Label(label, cls=_lbl), Input(cls=_inp, **kw), cls=_row_cls)

    def _toggle(label, **kw):
        return Label(
            Switch(cls="shrink-0", **kw),
            Span(label, cls="text-xs font-medium ml-2 cursor-pointer select-none"),
            cls="flex items-center gap-2 cursor-pointer",
        )

    return Div(
        Ul(
            Li(
                Div(
                    DivFullySpaced(
                        DivLAligned(UkIcon("bookmark", ratio=0.9, cls="mr-1"), Span("Add to Saved Jobs", cls="text-sm font-medium")),
                        UkIcon("chevron-down", cls="transition-transform duration-200"),
                    ),
                    cls="uk-accordion-title flex items-center justify-between cursor-pointer rounded border border-slate-200 dark:border-slate-700 px-3 py-2 hover:bg-slate-50 dark:hover:bg-slate-800",
                ),
                Div(
                    Form(
                        Input(type="hidden", id="score_json_result_saved", name="score_json_result", value=""),
                        Div(
                            _field("Title", name="title", placeholder="Job title"),
                            Div(
                                _field("Company", name="company", placeholder="Company name"),
                                _field("Location", name="location", placeholder="City, State"),
                                cls="grid grid-cols-1 sm:grid-cols-2 gap-2",
                            ),
                            Div(
                                Span("Salary (USD)", cls="text-xs font-semibold text-muted-foreground"),
                                Div(
                                    _field("Min $", name="min_amount", placeholder="100000"),
                                    _field("Max $", name="max_amount", placeholder="150000"),
                                    cls="grid grid-cols-1 sm:grid-cols-2 gap-2",
                                ),
                                _field("Interval", name="interval", placeholder="yearly / hourly"),
                                cls="space-y-1.5 border-l-2 border-muted pl-2",
                            ),
                            Div(
                                _toggle("Remote", name="is_remote", value="true"),
                                _toggle("Clearance Required", name="security_clearance_required", value="1"),
                                cls="flex flex-col gap-2.5 pt-1",
                            ),
                            cls="space-y-2 border rounded p-2",
                        ),
                        Div(
                            Button(
                                "Add to Saved Jobs",
                                id="add-saved-btn",
                                type="submit",
                                disabled=True,
                                cls=f"{ButtonT.primary} text-sm mt-3",
                            ),
                        ),
                        Div(id="add-saved-result", cls="mt-2"),
                        hx_post="/score/add-to-saved",
                        hx_include="#job_url, #job_text",
                        hx_target="#add-saved-result",
                        hx_swap="innerHTML",
                        cls="p-2",
                    ),
                    cls="uk-accordion-content mt-2",
                ),
            ),
            uk_accordion="collapsible: true",
            cls="uk-accordion",
        ),
        Script("""
(function() {
  function syncSavedBtn() {
    var url = document.getElementById('job_url');
    var btn = document.getElementById('add-saved-btn');
    if (url && btn) btn.disabled = !url.value.trim();
  }
  var url = document.getElementById('job_url');
  if (url) {
    url.addEventListener('input', syncSavedBtn);
    url.addEventListener('change', syncSavedBtn);
    syncSavedBtn();
  }
})();
"""),
        id="add-saved-accordion",
        cls="mt-2",
    )


@score_rt("/")
def get(auth, sess):
    user = get_auth_user(auth)
    profiles = get_profiles_for_user(user)

    # Header
    header = DivCentered(
        H2("Job Fit Score"),
        P(
            "Compare your profile against a job description to get a detailed compatibility score.",
            cls="text-slate-500",
        ),
        cls="mb-8",
    )

    # Profile Selection Row
    profile_options = [
        Option(p["profile_name"], value=p["profile_name"]) for p in profiles
    ]

    # Sub-header for inputs
    inputs_section = Card(
        Div(
            # Profile Selection
            Div(
                Div(
                    Label("Select Profile", fr="profile_name", cls="text-sm font-medium w-36 shrink-0", style="border:none;background:none;padding:0;box-shadow:none;border-radius:0"),
                    Select(
                        Option("-- Select a Profile --", value="", selected=True),
                        *profile_options,
                        id="profile_name",
                        name="profile_name",
                        hx_get="/score/preview-profile",
                        hx_target="#profile-preview-container",
                        hx_trigger="change",
                        hx_on__after_request="htmx.trigger(this, 'validate')",
                        cls="flex-1",
                    ),
                    cls="flex items-center gap-3",
                ),
                Div(
                    hx_post="/score/validate",
                    hx_target="#action_btn_container",
                    hx_trigger="validate from:#profile_name",
                    hx_include="#profile_name, #job_text",
                    cls="hidden",
                ),
            ),
            # Profile Resume Expander (uk-accordion)
            Div(id="profile-preview-container", cls="mt-2"),
            # Job Selection Row
            Div(
                Label("Job URL (Optional)", fr="job_url", cls="text-sm font-medium w-36 shrink-0", style="border:none;background:none;padding:0;box-shadow:none;border-radius:0"),
                Input(
                    id="job_url",
                    name="job_url",
                    placeholder="https://www.linkedin.com/jobs/view/...",
                    cls="flex-1",
                ),
                Button(
                    UkIcon("download", ratio=0.9),
                    cls=f"{ButtonT.primary} whitespace-nowrap",
                    hx_post="/score/download-job",
                    hx_target="#job_text_container",
                    hx_include="#job_url, #profile_name",
                    hx_indicator="#job_description_card",
                    title="Download Job Description",
                ),
                cls="flex items-center gap-2 mt-2",
            ),
            cls="space-y-4",
        )
    )

    # Job Description Section
    job_description_section = Card(
        H3("Job Description"),
        Div(
            Div(
                Div(cls="uk-spinner uk-spinner-medium", uk_spinner=True),
                P("Extracting job description...", cls="text-sm mt-2"),
                cls="htmx-indicator absolute inset-0 bg-white/80 dark:bg-slate-900/80 z-10 flex flex-col items-center justify-center",
            ),
            TextArea(
                id="job_text",
                name="job_text",
                placeholder="Paste job description here...",
                rows=15,
                cls="bg-white dark:bg-slate-900",
                # Trigger validation on input
                hx_post="/score/validate",
                hx_target="#action_btn_container",
                hx_trigger="input changed delay:500ms",
                hx_include="#profile_name",
            ),
            id="job_text_container",
            cls="relative space-y-2",
        ),
        id="job_description_card",
        cls="mt-6",
    )

    # Spinner (outside results so it survives result swaps)
    progress_spinner = DivCentered(
        Div(cls="uk-spinner uk-spinner-large", uk_spinner=True),
        P("AI is analyzing the fit... this may take 30-60 seconds.", cls="mt-4 text-slate-500"),
        id="progress-spinner",
        cls="htmx-indicator my-4",
    )

    # Result Section
    result_section = Div(id="scoring-results")

    action_btn = DivCentered(
        render_action_button(enabled=False),
        id="action_btn_container",
        cls="my-6",
    )

    content = Container(
        header,
        inputs_section,
        job_description_section,
        action_btn,
        _add_to_applied_accordion(),
        _add_to_saved_accordion(),
        progress_spinner,
        result_section,
        cls="py-8 max-w-4xl",
    )

    return NavigationLayout(content, title="Score Calculator", current_path="/score", user_info=sess.get("user_info", {}))


@score_rt("/preview-profile")
def get_preview(profile_name: str, auth):
    if not profile_name:
        return ""

    user = get_auth_user(auth)
    profile = get_profile(profile_name, user)

    if not profile:
        return Div("Profile not found", cls="text-red-500")

    return Div(
        Ul(
            Li(
                Div(
                    DivFullySpaced(
                        Span("View Resume Content", cls="text-sm font-medium"),
                        UkIcon("chevron-down", cls="transition-transform duration-200"),
                    ),
                    cls="uk-accordion-title flex items-center justify-between cursor-pointer rounded border border-slate-200 dark:border-slate-700 px-3 py-2 hover:bg-slate-50 dark:hover:bg-slate-800",
                ),
                Div(
                    TextArea(
                        profile["resume"],
                        readOnly=True,
                        rows=15,
                        cls="bg-slate-50 text-slate-800 dark:bg-slate-800 dark:text-slate-200 border-0",
                    ),
                    cls="uk-accordion-content mt-2",
                ),
            ),
            uk_accordion="collapsible: true",
            cls="uk-accordion",
        ),
        cls="mt-1",
    )


def _is_ready(profile_name, job_text, user):
    """Check if both profile and job text are sufficient to enable the Score button."""
    if not profile_name or not job_text or len(job_text.strip()) < 100:
        return False
    profile = get_profile(profile_name, user)
    return bool(profile and profile.get("resume") and len(profile["resume"].strip()) >= 100)


@score_rt("/download-job")
def post_download(job_url: str, profile_name: str = "", auth=None):
    user = get_auth_user(auth)

    if not job_url:
        return TextArea(
            id="job_text",
            name="job_text",
            placeholder="Please provide a job URL first",
            rows=15,
            cls="border-red-500 bg-white dark:bg-slate-900",
        ), render_action_button(enabled=False, oob=True)

    try:
        job_text = utils.extract_text_from_various_sources(job_url)
        enabled = _is_ready(profile_name, job_text, user)
        return TextArea(
            job_text,
            id="job_text",
            name="job_text",
            rows=15,
            cls="bg-white dark:bg-slate-900",
            hx_post="/score/validate",
            hx_target="#action_btn_container",
            hx_trigger="keyup changed delay:500ms",
            hx_include="#profile_name",
        ), render_action_button(enabled=enabled, oob=True)
    except Exception as e:
        return Div(
            TextArea(
                id="job_text",
                name="job_text",
                placeholder="Paste job description here...",
                rows=15,
                cls="bg-white dark:bg-slate-900",
                hx_post="/score/validate",
                hx_target="#action_btn_container",
                hx_trigger="input changed delay:500ms",
                hx_include="#profile_name",
            ),
            P(f"Error downloading: {str(e)}", cls="text-red-500 text-sm"),
        ), render_action_button(enabled=False, oob=True)


@score_rt("/validate")
def post_validate(profile_name: str, job_text: str, auth):
    """Checks if the Score button should be enabled."""
    user = get_auth_user(auth)
    enabled = _is_ready(profile_name, job_text, user)
    return render_action_button(enabled=enabled)


@score_rt("/calculate")
def post_calculate(profile_name: str, job_text: str, job_url: str = "", auth=None):
    if not profile_name or not job_text:
        return Alert("Please select a profile and provide job text.", cls=AlertT.error)

    user = get_auth_user(auth)
    profile = get_profile(profile_name, user)

    if not profile:
        return Alert("Selected profile not found.", cls=AlertT.error)

    job_details = {
        "job_url": job_url or "pasted_text",
        "job_id": "manual_v3",
        "job_source": "manual",
    }

    try:
        # Perform scoring using Gemini
        result, save_path, _ = ats_score_analyzer_gemini(
            job_description=job_text,
            resume=profile["resume"],
            desired_role_description=profile["desired_role_description"],
            job_details=job_details,
            us_citizen=profile["us_citizen"],
            security_clearance=profile["security_clearance"],
        )

        if "error" in result:
            return Alert(f"Scoring Error: {result['error']}", cls=AlertT.error)

        score_json_str = json.dumps(result)
        return Div(
            render_ats_score(result),
            # OOB-swap populates the hidden fields inside both accordion forms
            Input(
                type="hidden", id="score_json_result", name="score_json_result",
                value=score_json_str, hx_swap_oob="outerHTML:#score_json_result",
            ),
            Input(
                type="hidden", id="score_json_result_saved", name="score_json_result",
                value=score_json_str, hx_swap_oob="outerHTML:#score_json_result_saved",
            ),
        )
    except Exception as e:
        logger.exception("Scoring failed")
        return Alert(f"Scoring Failed: {str(e)}", cls=AlertT.error)


@score_rt("/add-to-db", methods=["POST"])
def post_add_to_db(
    job_text: str = "", job_url: str = "", score_json_result: str = "",
    title: str = "", company: str = "", location: str = "", applied_at: str = "",
    min_amount: str = "", max_amount: str = "", interval: str = "",
    is_remote: str = "", security_clearance_required: str = "",
    auth=None,
):
    user = get_auth_user(auth)
    if not job_url or not job_url.strip():
        return Alert("A job URL is required.", cls=AlertT.error)

    from datetime import date as _date
    job_data = {
        "job_url": job_url.strip(),
        "description": job_text,
        "title": title,
        "company": company,
        "location": location,
        "applied_at": applied_at or _date.today().isoformat(),
        "min_amount": min_amount,
        "max_amount": max_amount,
        "interval": interval,
        "is_remote": "true" if is_remote else "false",
        "security_clearance_required": bool(security_clearance_required),
    }
    new_id = create_applied_job(job_data, user)
    if not new_id:
        return Alert("Failed to add job to database.", cls=AlertT.error)

    if score_json_result:
        try:
            result = json.loads(score_json_result)
            numeric_score = result.get("ats_score_estimate", {}).get("total_overall_score", 0)
            clearance = _clearance_required_from_result(result)
            update_job_score(new_id, numeric_score, score_json_result, user, clearance)
        except Exception:
            logger.warning("Score JSON could not be saved for new job %s", new_id)

    return Alert(
        "Job added. ",
        A("View in Applied tab", href=f"{BASE_PREFIX}/applied/", cls="underline font-medium"),
        cls=AlertT.success,
    )


@score_rt("/add-to-saved", methods=["POST"])
def post_add_to_saved(
    job_text: str = "", job_url: str = "", score_json_result: str = "",
    title: str = "", company: str = "", location: str = "",
    min_amount: str = "", max_amount: str = "", interval: str = "",
    is_remote: str = "", security_clearance_required: str = "",
    auth=None,
):
    user = get_auth_user(auth)
    if not job_url or not job_url.strip():
        return Alert("A job URL is required.", cls=AlertT.error)

    job_data = {
        "job_url": job_url.strip(),
        "description": job_text,
        "title": title,
        "company": company,
        "location": location,
        "min_amount": min_amount,
        "max_amount": max_amount,
        "interval": interval,
        "is_remote": "true" if is_remote else "false",
        "security_clearance_required": bool(security_clearance_required),
    }
    new_id = create_saved_job(job_data, user)
    if not new_id:
        return Alert("Failed to add job to database.", cls=AlertT.error)

    if score_json_result:
        try:
            result = json.loads(score_json_result)
            numeric_score = result.get("ats_score_estimate", {}).get("total_overall_score", 0)
            clearance = _clearance_required_from_result(result)
            update_job_score(new_id, numeric_score, score_json_result, user, clearance)
        except Exception:
            logger.warning("Score JSON could not be saved for new saved job %s", new_id)

    return Alert(
        "Job saved. ",
        A("View in Saved tab", href=f"{BASE_PREFIX}/saved/", cls="underline font-medium"),
        cls=AlertT.success,
    )


def _score_border_cls(score):
    if score >= 80:
        return "border-l-4 border-green-500 bg-green-50 dark:bg-green-900/20"
    if score >= 60:
        return "border-l-4 border-yellow-500 bg-yellow-50 dark:bg-yellow-900/20"
    return "border-l-4 border-red-500 bg-red-50 dark:bg-red-900/20"


def _score_text_cls(score):
    if score >= 80:
        return "text-green-600 dark:text-green-400"
    if score >= 60:
        return "text-yellow-600 dark:text-yellow-400"
    return "text-red-600 dark:text-red-400"


def _decision_text_cls(decision):
    d = decision.upper()
    if d == "PASS":
        return "text-green-600 dark:text-green-400"
    if d == "CONDITIONAL":
        return "text-yellow-600 dark:text-yellow-400"
    return "text-red-600 dark:text-red-400"


def render_ats_score(result):
    """Render ATS scoring results using MonsterUI — mirrors v2 layout."""
    decision = result.get("decision", "Unknown")
    score_est = result.get("ats_score_estimate", {})
    total_score = score_est.get("total_overall_score", 0)
    tier = score_est.get("tier", "N/A")
    confidence = score_est.get("confidence_score", 0)
    reason = result.get("reason", "")
    breakdown = result.get("scoring_breakdown", {})

    # Decision Panel — compact, single row
    if decision.upper() == "PASS":
        decision_border = "border-green-500"
    elif decision.upper() == "CONDITIONAL":
        decision_border = "border-yellow-500"
    elif decision.upper() == "FAIL":
        decision_border = "border-red-500"
    else:
        decision_border = "border-blue-500"

    decision_panel = Card(
        Div(
            Div(
                H3("Decision: ", Span(decision, cls=_decision_text_cls(decision)), cls="m-0"),
                P(reason, cls="text-sm text-slate-600 dark:text-slate-400 mt-1"),
                P(
                    Span(B("Tier: "), tier, cls="mr-4"),
                    Span(B("Confidence: "), f"{confidence:.0%}"),
                    cls="text-xs text-slate-500 mt-1",
                ),
            ),
            Div(
                f"{total_score}",
                cls=f"text-4xl font-bold {_decision_text_cls(decision)}",
            ),
            cls="flex items-start justify-between",
        ),
        cls=f"mb-4 border-l-4 {decision_border} bg-slate-50 dark:bg-slate-800",
    )

    # Scoring Breakdown — 2×2 grid matching v2 layout
    keyword = breakdown.get("keyword_alignment", {})
    seniority = breakdown.get("seniority_alignment", {})
    impact = breakdown.get("impact_metrics", {})
    structure = breakdown.get("structural_parsability", {})

    breakdown_grid = Grid(
        _render_scoring_panel(
            "Keyword Alignment", keyword, "tag",
            _keyword_detail(keyword),
        ),
        _render_scoring_panel(
            "Seniority Alignment", seniority, "user",
            _seniority_detail(seniority),
        ),
        _render_scoring_panel(
            "Impact Metrics", impact, "trending-up",
            _impact_detail(impact),
        ),
        _render_scoring_panel(
            "Structural Parsability", structure, "layout",
            _structure_detail(structure),
        ),
        cols=1,
        cols_md=2,
        cls="gap-2 mb-4",
    )

    # Strategic Pivots
    pivots = result.get("strategic_pivots", [])
    pivot_card = None
    if pivots:
        pivot_card = Card(
            DivLAligned(UkIcon("lightbulb", cls="mr-2"), H4("Strategic Recommendations", cls="m-0")),
            Ul(
                *[Li(p, cls="text-sm text-slate-700 dark:text-slate-300") for p in pivots],
                cls="list-disc ml-6 mt-3",
            ),
            cls="mb-4 border-l-4 border-teal-500 bg-teal-50 dark:bg-teal-900/20",
        )

    # Eligibility Checks — 3-column grid matching v2 layout
    checks_grid = Grid(
        _render_clearance_card(result.get("clearance_assessment", {})),
        _render_citizenship_card(result.get("citizenship_assessment", {})),
        _render_job_reqs_card(result.get("job_requirements", {})),
        cols=1,
        cols_md=3,
        cls="gap-2 mb-4",
    )

    return Div(
        H2("Analysis Results", cls="border-b pb-2 mb-4"),
        decision_panel,
        pivot_card,
        breakdown_grid,
        H3("Eligibility Checks", cls="mb-4"),
        checks_grid,
    )


# --- Scoring breakdown panels (Job vs Resume + Resume Analysis) ---


def _render_scoring_panel(title, data, icon, detail):
    score = data.get("score", 0)
    weight = data.get("weight", 0)

    return Card(
        DivFullySpaced(
            DivLAligned(UkIcon(icon, cls="mr-2"), H4(title, cls="m-0")),
            Div(
                Div(f"{score}%", cls=f"text-2xl font-bold {_score_text_cls(score)}"),
                P(f"{weight:.0%} weight", cls="text-xs text-slate-400"),
                cls="text-right",
            ),
        ),
        Div(detail, cls="mt-3"),
        cls=f"h-full {_score_border_cls(score)}",
    )


def _keyword_detail(data):
    if not data:
        return None
    parts = []
    matches = data.get("top_matches", [])[:5]
    if matches:
        parts.append(P(
            Strong("Matches: "),
            Span(", ".join(matches), cls="text-green-700 dark:text-green-400"),
            cls="text-sm mb-1",
        ))
    missing = data.get("missing_critical_terms", [])[:5]
    if missing:
        parts.append(P(
            Strong("Missing: "),
            Span(", ".join(missing), cls="text-red-700 dark:text-red-400"),
            cls="text-sm",
        ))
    return Div(*parts, cls="space-y-1") if parts else None


def _seniority_detail(data):
    if not data:
        return None
    years = data.get("years_of_experience_detected")
    grade = data.get("title_match_grade", "N/A")
    parts = []
    if years is not None:
        parts.append(P(f"Experience: {years} years", cls="text-sm"))
    if grade:
        parts.append(P(f"Title Grade: {grade}", cls="text-sm"))
    return Div(*parts) if parts else None


def _impact_detail(data):
    if not data:
        return None
    anchors = data.get("detected_anchors", [])[:5]
    if anchors:
        return P(Strong("Impact words: "), Span(", ".join(anchors)), cls="text-sm")
    return None


def _structure_detail(data):
    if not data:
        return None
    risk = data.get("format_risk", "Unknown")
    return P(f"Format Risk: {risk}", cls="text-sm")


# --- Eligibility check cards ---


def _clean_enum(status):
    if "." in str(status):
        return str(status).split(".")[-1].replace("_", " ").title()
    return str(status)


def _render_clearance_card(data):
    if not data:
        return Card(H4("Security Clearance"), P("N/A", cls="text-sm"), cls="h-full")

    status = _clean_enum(data.get("status", "Unknown"))
    req_types = data.get("required_clearance_types", [])
    elig = data.get("eligibility_score", 0)
    phrasing = data.get("detected_phrasing", "")

    if "Active" in status or "Required" in status:
        border = "border-l-4 border-yellow-500 bg-yellow-50 dark:bg-yellow-900/20"
        status_cls = "text-yellow-700 dark:text-yellow-400"
    elif "Obtain" in status:
        border = "border-l-4 border-orange-500 bg-orange-50 dark:bg-orange-900/20"
        status_cls = "text-orange-700 dark:text-orange-400"
    else:
        border = "border-l-4 border-green-500 bg-green-50 dark:bg-green-900/20"
        status_cls = "text-green-700 dark:text-green-400"

    details = [P(
        Strong("Required: "),
        ", ".join(req_types) if req_types else "None specified",
        cls="text-sm",
    )]
    if elig > 0:
        details.append(P(f"Eligibility: {elig}/100", cls="text-sm"))

    analysis = phrasing

    return Card(
        DivFullySpaced(
            DivLAligned(UkIcon("shield", cls="mr-2"), H4("Security Clearance", cls="m-0")),
            Div(status, cls=f"text-lg font-bold {status_cls}"),
        ),
        Div(
            *details,
            P(analysis, cls="text-xs italic text-slate-500 dark:text-slate-400 mt-2 pt-2 border-t border-slate-200 dark:border-slate-700") if analysis else None,
            cls="mt-3 space-y-1",
        ),
        cls=f"h-full {border}",
    )


def _render_citizenship_card(data):
    if not data:
        return Card(H4("Citizenship"), P("N/A", cls="text-sm"), cls="h-full")

    status = _clean_enum(data.get("status", "Unknown"))
    meets = data.get("meets_requirement", False)
    reason = data.get("reason", "")

    if meets:
        border = "border-l-4 border-green-500 bg-green-50 dark:bg-green-900/20"
        status_cls = "text-green-700 dark:text-green-400"
    else:
        border = "border-l-4 border-red-500 bg-red-50 dark:bg-red-900/20"
        status_cls = "text-red-700 dark:text-red-400"

    details = [
        P(Strong("Requirement: "), status, cls="text-sm"),
        P(Strong("Status: "), "Meets" if meets else "Does Not Meet", cls="text-sm"),
    ]

    analysis = reason

    return Card(
        DivFullySpaced(
            DivLAligned(UkIcon("flag", cls="mr-2"), H4("Citizenship", cls="m-0")),
            Div("Meets" if meets else "No", cls=f"text-lg font-bold {status_cls}"),
        ),
        Div(
            *details,
            P(analysis, cls="text-xs italic text-slate-500 dark:text-slate-400 mt-2 pt-2 border-t border-slate-200 dark:border-slate-700") if analysis else None,
            cls="mt-3 space-y-1",
        ),
        cls=f"h-full {border}",
    )


def _render_job_reqs_card(data):
    if not data:
        return Card(H4("Job Requirements"), P("N/A", cls="text-sm"), cls="h-full")

    years = data.get("years_of_experience_required", 0)
    travel = data.get("travel_percentage", "0%")
    phrasing = data.get("detected_requirements_phrasing", "")

    details = [
        P(Strong("Experience: "), f"{years} years", cls="text-sm"),
        P(Strong("Travel: "), travel, cls="text-sm"),
    ]

    return Card(
        DivFullySpaced(
            DivLAligned(UkIcon("clipboard-list", cls="mr-2"), H4("Job Requirements", cls="m-0")),
            Div(f"{years}yr", cls="text-lg font-bold text-blue-700 dark:text-blue-400"),
        ),
        Div(
            *details,
            P(
                f'Detected: "{phrasing}"',
                cls="text-xs italic text-slate-500 dark:text-slate-400 mt-2 pt-2 border-t border-slate-200 dark:border-slate-700",
            ) if phrasing else None,
            cls="mt-3 space-y-1",
        ),
        cls="h-full border-l-4 border-blue-500 bg-blue-50 dark:bg-blue-900/20",
    )
