"""
Profile CRUD routes for job_score.
"""

import json
import logging
import re
import os
import tempfile
from fasthtml.common import *
from monsterui.all import *
from .db import get_profiles_for_user, get_profile, save_profile, update_profile_by_rowid, delete_profile, save_resume_blob, save_ats_score
from .lib.gemini_ai_runner import analyze_resume_ats
from .lib.config import BASE_PREFIX

logger = logging.getLogger(__name__)

ar = APIRouter(prefix="/profiles")


def remove_pii(text: str) -> str:
    """Remove PII from the text."""
    # Remove candidate names from the first line
    lines = text.splitlines()
    if len(lines) > 0:
        text = (
            re.sub(
                r"^(\*\*)?[A-Za-z]+ [A-Za-z]+(\*\*)?", "firstname lastname", lines[0]
            )
            + "\n"
            + "\n".join(lines[1:])
        )

    # Remove email addresses
    text = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        "email@example.com",
        text,
    )
    # Remove phone numbers
    text = re.sub(
        r"(?:\(\d{3}\)[\s.-]*|\b\d{3}[\s.-]+)\d{3}[\s.-]+\d{4}\b", "123-456-7890", text
    )
    # Remove hyperlinks but leave text behind
    text = re.sub(r"<a\s[^>]*>(.*?)</a>", r"\1", text)
    # Remove URLs
    text = re.sub(r"http\S+|www.\S+", "http://pii_replaced_example.com", text)
    return text


def convert_file_to_markdown(file_path: str) -> str:
    """Convert a PDF, TXT or Markdown file to Markdown."""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".pdf":
        from markitdown import MarkItDown

        md = MarkItDown()
        result = md.convert(file_path)
        markdown_text = result.markdown
        return remove_pii(markdown_text)
    elif ext in (".txt", ".md"):
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return remove_pii(f.read())
    return ""


def _parse_json_list(value: str) -> list:
    """Parse JSON list string to Python list."""
    if not value:
        return []
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return []


def _serialize_json_list(items: list) -> str:
    """Serialize Python list to JSON string."""
    return json.dumps(items, ensure_ascii=False)


def _profile_card(p: dict) -> FT:
    """A single profile displayed as a styled card with elegant top-right actions."""
    # Default indicator
    is_default = p.get("is_default", 0)
    star_cls = (
        "text-yellow-500" if is_default else "text-slate-300 hover:text-yellow-400"
    )
    star_title = "Default Profile" if is_default else "Set as Default"

    return Card(
        # Top-right action buttons
        Div(
            # Default Toggle (Star)
            Button(
                UkIcon("star", ratio=0.8),
                cls=f"{star_cls} p-1 transition-colors",
                hx_post=f"/profiles/set-default/{p['profile_name']}",
                hx_target="#profiles-container",
                title=star_title,
            )
            if not is_default
            else Div(
                UkIcon("star", ratio=0.8),
                cls=f"{star_cls} p-1",
                title=star_title,
            ),
            Button(
                UkIcon("pencil", ratio=0.8),
                cls="text-slate-400 hover:text-blue-500 p-1 transition-colors",
                hx_get=f"/profiles/edit/{p['profile_name']}",
                hx_target="#profiles-container",
                hx_swap="innerHTML",
                title="Edit Profile",
            ),
            Button(
                UkIcon("copy", ratio=0.8),
                cls="text-slate-400 hover:text-green-500 p-1 transition-colors",
                hx_post=f"/profiles/duplicate/{p['profile_name']}",
                hx_target="#profiles-container",
                hx_swap="innerHTML",
                title="Duplicate Profile",
            ),
            Button(
                UkIcon("trash", ratio=0.8),
                cls="text-slate-400 hover:text-red-500 p-1 transition-colors",
                hx_delete=f"/profiles/delete/{p['profile_name']}",
                hx_confirm=f"Are you sure you want to delete profile '{p['profile_name']}'?",
                hx_target="closest .uk-card",
                hx_swap="delete",
                title="Delete Profile",
            ),
            cls="absolute top-2 right-2 flex gap-1 items-center",
        ),
        P(f"Location: {p.get('location', '')}", cls=TextPresets.muted_sm),
        P(
            f"Clearance: {p.get('security_clearance', 'None')}",
            cls=TextPresets.muted_sm,
        ),
        header=CardTitle(
            DivLAligned(
                p["profile_name"],
                Span("Default", cls="uk-badge ml-2 bg-yellow-500")
                if is_default
                else "",
            )
        ),
        cls=f"{CardT.hover} relative",
    )


def _create_list_field(
    field_id: str, label: str, items: list[str], max_items: int = 100
) -> FT:
    """Create a list field UI with add/edit buttons."""
    div_id = f"{field_id}_list"
    json_val = _serialize_json_list(items)

    return Div(
        Input(type="hidden", name=field_id, id=f"{field_id}_hidden", value=json_val),
        Div(
            Label(label, cls="text-sm font-medium w-36 shrink-0", style="border:none;background:none;padding:0;box-shadow:none;border-radius:0"),
            Input(
                id=f"{field_id}_input",
                placeholder=f"Add {label.lower()}...",
                cls="flex-1",
                onkeydown=f"if(event.key === 'Enter') {{ event.preventDefault(); addListItem('{field_id}', {max_items}); }}",
            ),
            Button(
                UkIcon("plus"),
                type="button",
                cls="text-blue-500 hover:text-blue-700 p-2 transition-colors [&_svg]:w-[20px] [&_svg]:h-[20px]",
                onclick=f"addListItem('{field_id}', {max_items})",
                title=f"Add {label.lower()}",
            ),
            cls="flex items-center gap-2",
        ),
        Div(_render_list_items(field_id, items), id=div_id),
    )


def _render_list_items(field_id: str, items: list[str]) -> FT:
    """Render list items as tags with edit/delete buttons."""
    if not items:
        return P("No items added yet", cls="text-sm text-gray-400 italic")

    def _render_item(item: str, index: int) -> FT:
        return Span(
            item,
            NotStr(
                f'<span onclick="removeListItem(\'{field_id}\', {index})" title="Delete" style="cursor:pointer;display:inline-flex;align-items:center;color:#94a3b8;margin-left:3px;" onmouseover="this.style.color=\'#ef4444\'" onmouseout="this.style.color=\'#94a3b8\'">'
                '<svg width="8" height="8" viewBox="0 0 20 20" xmlns="http://www.w3.org/2000/svg"><path fill="none" stroke="currentColor" stroke-width="2.5" d="M16,16 L4,4 M16,4 L4,16"></path></svg>'
                '</span>'
            ),
            cls="inline-flex items-center bg-slate-100 dark:bg-slate-700 border border-slate-200 dark:border-slate-600 px-1.5 py-px rounded text-xs text-slate-600 dark:text-slate-300 leading-none",
        )

    return Div(
        *[_render_item(item, i) for i, item in enumerate(items)],
        cls="flex flex-wrap gap-1 mt-1",
        id=f"{field_id}-list",
    )


_ATS_SECTIONS = [
    ("Resume Basics", [
        ("resume_clarity", "Resume Clarity"),
        ("contact_information", "Contact Information"),
        ("chronological_order", "Chronological Order"),
        ("formatting", "Formatting"),
        ("resume_length", "Resume Length"),
    ]),
    ("Summary Strength", [
        ("headline", "Headline"),
        ("summary", "Summary"),
    ]),
    ("Experience Audit", [
        ("experience_details", "Experience Details"),
        ("recent_experience", "Recent Experience"),
        ("role_separation", "Role Separation"),
    ]),
    ("Achievements", [
        ("quantified_achievements", "Quantified Achievements"),
        ("technologies", "Technologies"),
        ("numbers_placement", "Numbers Placement"),
    ]),
    ("Language & Tone", [
        ("verb_usage", "Verb Usage"),
        ("grammar", "Grammar"),
        ("punctuation", "Punctuation"),
        ("voice_and_terse", "Voice & Terse"),
    ]),
    ("Visual Impact", [
        ("text_format", "Text Format"),
        ("layout", "Layout"),
        ("font_styles", "Font Styles"),
        ("file_size", "File Size"),
    ]),
]


def _score_badge(score: int) -> FT:
    color = (
        "bg-green-100 text-green-800" if score >= 8
        else "bg-yellow-100 text-yellow-800" if score >= 5
        else "bg-red-100 text-red-800"
    )
    return Span(f"{score}/10", cls=f"inline-block px-2 py-0.5 rounded text-xs font-bold {color}")


def _render_ats_results(ats_json_str: str) -> FT | str:
    """Render stored ATS analysis JSON as grouped score cards."""
    try:
        data = json.loads(ats_json_str) if ats_json_str else {}
    except (json.JSONDecodeError, TypeError):
        data = {}
    if not data:
        return ""

    section_cards = []
    for section_title, fields in _ATS_SECTIONS:
        rows = []
        for key, label in fields:
            cat = data.get(key, {})
            if not cat:
                continue
            rows.append(
                Div(
                    Div(
                        Span(label, cls="text-xs font-medium text-slate-700 dark:text-slate-300"),
                        _score_badge(cat.get("score", 0)),
                        cls="flex items-center justify-between",
                    ),
                    P(cat.get("analysis", ""), cls="text-xs text-slate-500 mt-0.5"),
                    cls="py-1 border-b border-slate-100 dark:border-slate-700 last:border-0",
                )
            )
        if rows:
            section_cards.append(
                Div(
                    H4(section_title, cls="text-sm font-semibold text-slate-800 dark:text-slate-200 mb-2"),
                    *rows,
                    cls="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-3 mb-3",
                )
            )

    # ATS Summary
    interp = data.get("ats_interpretation", {})
    titles = data.get("top_matching_job_titles", [])
    skills = data.get("key_skills_recognized", [])

    def _chips(items: list, color: str) -> FT:
        return Div(
            *[Span(item, cls=f"inline-block px-2 py-0.5 rounded-full text-xs {color} mr-1 mb-1") for item in items],
            cls="flex flex-wrap mt-1",
        )

    summary_card = Div(
        H4("ATS Summary", cls="text-sm font-semibold text-slate-800 dark:text-slate-200 mb-2"),
        Div(
            Div(
                Span("ATS Interpretation", cls="text-xs font-medium text-slate-700 dark:text-slate-300"),
                _score_badge(interp.get("score", 0)),
                cls="flex items-center justify-between",
            ),
            P(interp.get("analysis", ""), cls="text-xs text-slate-500 mt-0.5"),
            cls="py-1 border-b border-slate-100 dark:border-slate-700",
        ) if interp else "",
        Div(
            P("Top Matching Job Titles", cls="text-xs font-medium text-slate-700 dark:text-slate-300 mt-2"),
            _chips(titles, "bg-blue-100 text-blue-800"),
        ) if titles else "",
        Div(
            P("Key Skills Recognized", cls="text-xs font-medium text-slate-700 dark:text-slate-300 mt-2"),
            _chips(skills, "bg-green-100 text-green-800"),
        ) if skills else "",
        cls="bg-white dark:bg-slate-800 border border-slate-200 dark:border-slate-700 rounded-lg p-3 mb-3",
    )

    return Details(
        Summary("ATS Resume Analysis", cls="cursor-pointer text-sm font-semibold text-slate-800 dark:text-slate-200 py-1"),
        Div(
            *section_cards,
            summary_card,
            cls="mt-2",
        ),
        open=True,
        cls="mt-2 border border-slate-200 dark:border-slate-700 rounded-lg px-3 py-2",
    )


def _profile_form(profile: dict | None = None) -> FT:
    """Editable form for creating or editing a profile."""
    creating = profile is None
    name_val = "" if creating else profile.get("profile_name", "")

    # Detect what kind of blob is stored for this profile
    _blob = profile.get("resume_blob") if profile else None
    if _blob and isinstance(_blob, memoryview):
        _blob = bytes(_blob)
    has_pdf_blob = bool(_blob and _blob[:4] == b"%PDF")
    has_text_blob = bool(_blob and not has_pdf_blob)
    blob_text = _blob.decode("utf-8", errors="replace") if has_text_blob else ""
    existing_ats_json = (profile.get("ats_score") or "{}") if profile else "{}"

    # Get current values with defaults
    resume_val = profile.get("resume", "") if profile else ""
    desired_role_val = profile.get("desired_role_description", "") if profile else ""
    additional_skills_val = _parse_json_list(
        profile.get("additional_skills", "[]") if profile else "[]"
    )
    us_citizen_val = 1 if (profile and profile.get("us_citizen")) else 0
    security_clearance_val = (
        profile.get("security_clearance", "None") if profile else "None"
    )
    keywords_val = _parse_json_list(profile.get("keywords", "[]") if profile else "[]")
    location_val = profile.get("location", "") if profile else ""
    reject_job_titles_val = _parse_json_list(
        profile.get("reject_job_titles", "[]") if profile else "[]"
    )

    _card = Card(
        Form(
            Div(
                Button(
                    UkIcon("arrow-left", ratio=0.8),
                    "Back to Profiles",
                    cls="uk-button uk-button-default flex items-center gap-1",
                    hx_get="/profiles/",
                    hx_target="#profiles-container",
                    hx_swap="innerHTML",
                    type="button",
                ),
                cls="mb-6",
            ),
            Div(
                Label("Profile Name", cls="text-sm font-medium w-36 shrink-0", style="border:none;background:none;padding:0;box-shadow:none;border-radius:0"),
                Input(id="profile_name", name="profile_name", value=name_val, cls="flex-1"),
                cls="flex items-center gap-3",
            ),
            Hidden(
                name="profile_rowid",
                id="profile_rowid_field",
                value=str(profile.get("rowid", "")) if not creating else "",
            ) if not creating else None,
            Hidden(
                name="profile_name_hidden", id="profile_name_hidden", value=name_val
            ),
            # Resume field with file upload - collapsible
            Details(
                Summary(
                    Span(
                        "Resume (Markdown and PII Redacted, paste below or upload)",
                        cls="text-sm font-medium",
                        style="color:inherit",
                    ),
                    Span("✓", id="resume_tick", cls="text-green-500 ml-2 text-sm font-bold",
                         style=("" if resume_val else "display:none")),
                    Div(id="pii_spinner", cls="text-blue-500 ml-2 inline-flex items-center", style="display:none")(
                        UkIcon("spinner", cls="uk-margin-small-right"),
                        Span("Sanitizing PII...", cls="text-xs italic"),
                    ),
                ),
                Div(
                    Button(
                        "Upload Resume",
                        type="button",
                        cls="uk-button uk-button-default uk-button-small",
                        onclick="document.getElementById('resume_file_input').click()",
                    ),
                    Span("or drag & drop a file below", cls="text-xs text-gray-400 self-center"),
                    Button(
                        "ATS Resume Analysis",
                        type="button",
                        id="ats_analyze_btn",
                        cls="uk-button uk-button-primary uk-button-small ml-4",
                        hx_post=f"{BASE_PREFIX}/profiles/analyze-ats",
                        hx_include="#profile_name_hidden",
                        hx_target="#ats_results_section",
                        hx_swap="innerHTML",
                        hx_indicator="#ats_spinner",
                    ),
                    Span("see bottom of page for results", cls="text-xs text-gray-400 self-center"),
                    cls="flex gap-2 mt-2 items-center flex-wrap",
                ),
                Input(
                    type="file",
                    name="resume_file",
                    accept=".pdf,.txt,.md",
                    id="resume_file_input",
                    onchange="uploadAndConvertResume(this, 'resume_textarea')",
                    style="display:none",
                ),
                P("Sanitized Text For AI", cls="text-xs text-gray-500 mt-2 mb-1"),
                Div(
                    TextArea(
                        resume_val,
                        rows=5,
                        placeholder="Paste your resume in Markdown format, or drag & drop / upload a PDF, TXT or MD file...",
                        id="resume_textarea",
                        name="resume",
                        onblur="sanitizeResumePII(this)",
                        oninput="document.getElementById('resume_tick').style.display = this.value.trim() ? 'inline' : 'none'",
                        cls="w-full",
                    ),
                    id="resume_dropzone",
                    cls="mt-1 border-2 border-dashed border-gray-300 rounded p-2",
                    ondragover="resumeDragOver(event)",
                    ondragleave="resumeDragLeave(event)",
                    ondrop="resumeDrop(event, 'resume_textarea')",
                ),
                Div(id="resume_preview", cls="mt-1 text-sm text-gray-600"),
                Div(
                    *(
                        [
                            P("Uploaded Original Resume", cls="text-xs text-gray-500 mt-3 mb-1"),
                            Details(
                                Summary("View Resume", cls="text-xs cursor-pointer text-blue-600 hover:text-blue-800 dark:text-blue-400 mt-1"),
                                Div(
                                    Iframe(
                                        src=f"{BASE_PREFIX}/profiles/resume-pdf?profile_name={name_val}",
                                        width="100%",
                                        height="500",
                                        style="border:1px solid #ccc;border-radius:4px;display:block;",
                                    ),
                                    cls="mt-1",
                                ),
                            ),
                        ] if has_pdf_blob else [
                            P("Uploaded Original Resume", cls="text-xs text-gray-500 mt-3 mb-1"),
                            TextArea(
                                blob_text,
                                readonly=True,
                                rows=10,
                                cls="w-full mt-1",
                                style="font-family:monospace;font-size:0.8em;",
                            ),
                        ] if has_text_blob else []
                    ),
                    id="original_resume_section",
                ),
                Div(
                    Div(cls="uk-spinner", uk_spinner=True),
                    Span("AI is analyzing your resume... this may take 30-60 seconds.", cls="ml-2 text-sm text-slate-500"),
                    id="ats_spinner",
                    cls="htmx-indicator flex items-center my-2",
                ),
                Div(
                    _render_ats_results(existing_ats_json),
                    id="ats_results_section",
                    cls="mt-4",
                ),
                open=bool(resume_val),
            ),
            Div(
                Label(
                    "Desired Role Description",
                    fr="desired_role_description",
                    cls="block mb-1",
                ),
                TextArea(
                    desired_role_val,
                    id="desired_role_description",
                    name="desired_role_description",
                    rows=6,
                    placeholder="""This helps check whether a job title is relevant before scoring.
1. Describe the desired role, For example: Senior Technical Director, or Senior Architect or Senior Project Manager
2. Describe the type of work you are looking for, e.g. "Cloud modernization" or "Solution Delivery"
3. Titles/Roles to avoid - false positives. E.g.:
    - "Security Guard" or "Operations Manager" if you are looking for cybersecurity role
    - "Scrum Master" or "Account Manager" if you are looking for a project management role.
    """,
                ),
                cls="mt-4",
            ),
            # Additional Skills (list UI)
            _create_list_field(
                "additional_skills", "Additional Skills", additional_skills_val
            ),
            # US Citizen
            Div(
                Label("US Citizen", cls="text-sm font-medium w-36 shrink-0", style="border:none;background:none;padding:0;box-shadow:none;border-radius:0"),
                CheckboxX(
                    id="us_citizen",
                    name="us_citizen",
                    checked=bool(us_citizen_val),
                    value="1",
                ),
                cls="flex items-center gap-3",
            ),
            # Security clearance dropdown
            Div(
                Label("Security Clearance", cls="text-sm font-medium w-36 shrink-0", style="border:none;background:none;padding:0;box-shadow:none;border-radius:0"),
                Select(
                    Option(
                        "None", value="None", selected=security_clearance_val == "None"
                    ),
                    Option(
                        "Secret",
                        value="Secret",
                        selected=security_clearance_val == "Secret",
                    ),
                    Option(
                        "Top Secret",
                        value="Top Secret",
                        selected=security_clearance_val == "Top Secret",
                    ),
                    Option(
                        "TS/SCI",
                        value="TS/SCI",
                        selected=security_clearance_val == "TS/SCI",
                    ),
                    id="security_clearance",
                    name="security_clearance",
                    cls="flex-1",
                ),
                cls="flex items-center gap-3",
            ),
            # Keywords (list UI)
            _create_list_field(
                "keywords", "Job Search Keywords (max 3)", keywords_val, max_items=3
            ),
            # Location (single text input)
            Div(
                Label("Job Search Location", cls="text-sm font-medium w-36 shrink-0", style="border:none;background:none;padding:0;box-shadow:none;border-radius:0"),
                Input(id="location", name="location", value=location_val, placeholder="e.g. McLean, VA", cls="flex-1"),
                cls="flex items-center gap-3",
            ),
            # Reject Job Titles (list UI)
            _create_list_field(
                "reject_job_titles", "Reject Job Titles", reject_job_titles_val
            ),
            # Removed Finish button as requested since auto-save is active
            Div(
                cls="mt-8 border-t pt-4 text-xs text-gray-400 italic",
                children="All changes are saved automatically.",
            ),
            cls="space-y-4",
            id="profile-form",
            hx_post="/profiles/autosave",
            hx_trigger="change",
            hx_swap="none",
        ),
        header=CardTitle("Edit Profile" if not creating else "Create Profile"),
    )
    return _card, Script(f"var _PROFILES_PREFIX = {repr(BASE_PREFIX)};"), Script("""
        // Sync profile name field with hidden input
        const profileNameInput = document.getElementById('profile_name');
        const profileNameHidden = document.getElementById('profile_name_hidden');
        if (profileNameInput && profileNameHidden) {
            profileNameInput.addEventListener('input', function() {
                profileNameHidden.value = this.value;
            });
        }

        const profileForm = document.getElementById('profile-form');
        if (profileForm) {
            profileForm.addEventListener('htmx:afterRequest', function(evt) {
                if (evt.detail.successful && evt.detail.xhr.responseText === 'Saved') {
                    UIkit.notification('✓ Saved', { status: 'success', timeout: 1500, pos: 'bottom-right' });
                }
            });
        }

        function addListItem(fieldId, limit = 100) {
            const input = document.getElementById(fieldId + '_input');
            const hidden = document.getElementById(fieldId + '_hidden');
            if (!input || !hidden) return;

            const val = input.value.trim();
            if (!val) return;

            let list = JSON.parse(hidden.value || '[]');
            if (list.length >= limit) {
                alert(`Maximum of ${limit} items allowed.`);
                return;
            }
            if (!list.includes(val)) {
                list.push(val);
                hidden.value = JSON.stringify(list);
                input.value = '';
                htmx.ajax('POST', _PROFILES_PREFIX + '/profiles/render-list-items/' + fieldId, {
                    target: '#' + fieldId + '_list',
                    values: { items: JSON.stringify(list) }
                });
                triggerAutosave();
            }
        }

        function removeListItem(fieldId, index) {
            const hidden = document.getElementById(fieldId + '_hidden');
            if (!hidden) return;

            let list = JSON.parse(hidden.value || '[]');
            list.splice(index, 1);
            hidden.value = JSON.stringify(list);
            htmx.ajax('POST', _PROFILES_PREFIX + '/profiles/render-list-items/' + fieldId, {
                target: '#' + fieldId + '_list',
                values: { items: JSON.stringify(list) }
            });
            triggerAutosave();
        }

        function triggerAutosave() {
            const form = document.getElementById('profile-form');
            if (window.htmx && form) {
                htmx.trigger(form, 'change');
            }
        }

        function sanitizeResumePII(textarea) {
            const text = textarea.value;
            if (!text || text.trim().length === 0) return;

            const spinner = document.getElementById('pii_spinner');
            if (spinner) spinner.style.display = 'flex';

            fetch(_PROFILES_PREFIX + '/profiles/sanitize-pii', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'text=' + encodeURIComponent(text)
            })
            .then(r => { if (!r.ok) throw new Error('sanitize-pii ' + r.status); return r.text(); })
            .then(sanitized => {
                if (sanitized !== text) {
                    textarea.value = sanitized;
                    triggerAutosave();
                }
            })
            .catch(() => {})
            .finally(() => {
                if (spinner) spinner.style.display = 'none';
            });
        }

        function processResumeFile(file, textareaId) {
            const allowedExtensions = ['.pdf', '.txt', '.md'];
            const fileName = file.name.toLowerCase();
            if (!allowedExtensions.some(ext => fileName.endsWith(ext))) {
                alert('Please select a PDF, TXT or Markdown file.');
                return;
            }

            const preview = document.getElementById('resume_preview');
            if (preview) {
                preview.innerHTML = '<span uk-spinner="ratio: 1.5"></span> Processing Resume...';
                preview.className = 'mt-2 text-sm text-gray-600';
            }

            const formData = new FormData();
            formData.append('file', file);
            const profileNameHidden = document.getElementById('profile_name_hidden');
            if (profileNameHidden && profileNameHidden.value) {
                formData.append('profile_name', profileNameHidden.value);
            }

            fetch(_PROFILES_PREFIX + '/profiles/upload-resume', {
                method: 'POST',
                body: formData
            })
            .then(response => {
                if (!response.ok) throw new Error('Failed to process resume');
                return response.text();
            })
            .then(markdown => {
                const textarea = document.getElementById(textareaId);
                if (textarea) {
                    textarea.value = markdown;
                    sanitizeResumePII(textarea);
                }
                const tick = document.getElementById('resume_tick');
                if (tick) tick.style.display = markdown.trim() ? 'inline' : 'none';
                if (preview) {
                    preview.innerHTML = 'Successfully uploaded ' + file.name;
                    preview.className = 'mt-2 text-sm text-blue-600 font-semibold';
                }
                const profileNameEl = document.getElementById('profile_name_hidden');
                const pname = profileNameEl ? encodeURIComponent(profileNameEl.value) : '';
                const origSection = document.getElementById('original_resume_section');
                if (fileName.endsWith('.pdf')) {
                    if (origSection) {
                        origSection.innerHTML = '<p class="text-xs text-gray-500 mt-3 mb-1">Uploaded Original Resume</p><details><summary class="text-xs cursor-pointer text-blue-600 hover:text-blue-800 dark:text-blue-400 mt-1">View Resume</summary><div class="mt-1"><iframe src="' + _PROFILES_PREFIX + '/profiles/resume-pdf?profile_name=' + pname + '" width="100%" height="500" style="border:1px solid #ccc;border-radius:4px;display:block;"></iframe></div></details>';
                    }
                } else {
                    if (origSection) {
                        fetch(_PROFILES_PREFIX + '/profiles/resume-pdf?profile_name=' + pname)
                            .then(function(r) { return r.ok ? r.text() : ''; })
                            .then(function(text) {
                                const label = document.createElement('p');
                                label.className = 'text-xs text-gray-500 mt-3 mb-1';
                                label.textContent = 'Uploaded Original Resume';
                                const ta = document.createElement('textarea');
                                ta.readOnly = true;
                                ta.rows = 10;
                                ta.className = 'w-full mt-1';
                                ta.style.fontFamily = 'monospace';
                                ta.style.fontSize = '0.8em';
                                ta.value = text;
                                origSection.innerHTML = '';
                                origSection.appendChild(label);
                                origSection.appendChild(ta);
                            });
                    }
                }
                triggerAutosave();
            })
            .catch(error => {
                if (preview) {
                    preview.innerHTML = 'Error processing resume: ' + error.message;
                    preview.className = 'mt-2 text-sm text-red-600 font-semibold';
                }
                alert('Error processing resume: ' + error.message);
            });
        }

        function uploadAndConvertResume(inputElement, textareaId) {
            const file = inputElement.files[0];
            if (!file) return;
            inputElement.value = '';
            processResumeFile(file, textareaId);
        }

        function resumeDragOver(event) {
            event.preventDefault();
            const dz = document.getElementById('resume_dropzone');
            if (dz) dz.classList.add('border-blue-400', 'bg-blue-50');
        }

        function resumeDragLeave(event) {
            const dz = document.getElementById('resume_dropzone');
            if (dz) dz.classList.remove('border-blue-400', 'bg-blue-50');
        }

        function resumeDrop(event, textareaId) {
            event.preventDefault();
            const dz = document.getElementById('resume_dropzone');
            if (dz) dz.classList.remove('border-blue-400', 'bg-blue-50');
            const file = event.dataTransfer.files[0];
            if (file) processResumeFile(file, textareaId);
        }
    """, type="text/javascript")


def _render_profiles_ui(profiles):
    """Render the profiles list UI (header + cards)."""
    cards = [_profile_card(p) for p in profiles]
    if not cards:
        cards = [P("No profiles yet.", cls=TextPresets.muted_sm)]

    return Div(
        DivFullySpaced(
            H2("Profiles"),
            Button(
                UkIcon("plus-circle"),
                cls="text-blue-500 hover:text-blue-700 p-2 transition-colors [&_svg]:w-[32px] [&_svg]:h-[32px]",
                hx_get="/profiles/create",
                hx_target="#profiles-container",
                hx_swap="innerHTML",
                title="Create New Profile",
            ),
        ),
        Div(*cards, cls="space-y-4"),
    )


@ar("/")
def get(auth, sess, hx_request: bool = False):
    """List all profiles for the current user."""
    profiles = get_profiles_for_user(auth)

    # Auto-set default if none exists or only one profile exists
    if profiles:
        has_default = any(p.get("is_default") for p in profiles)
        if not has_default or len(profiles) == 1:
            from .db import set_default_profile

            set_default_profile(profiles[0]["profile_name"], auth)
            profiles = get_profiles_for_user(auth)

    # If HTMX request to #profiles-container, return narrowed UI
    if hx_request:
        return _render_profiles_ui(profiles)

    from .common import NavigationLayout

    user_info = sess.get("user_info", {})
    name = user_info.get("name", "User")

    return NavigationLayout(
        Div(id="profiles-container", cls="space-y-4 mt-4")(
            _render_profiles_ui(profiles)
        ),
        current_path="/profiles",
        user_info=user_info,
    )


@ar("/edit/{profile_name}")
def get_edit(auth, profile_name: str):
    """Return edit form for a specific profile."""
    profile = get_profile(profile_name, auth)
    if not profile:
        return P(f"Profile '{profile_name}' not found.")
    return _profile_form(profile)


@ar("/create")
def get_create(auth):
    """Return empty form for creating a new profile."""
    return _profile_form()


@ar("/save")
def post(
    auth,
    sess,
    profile_name: str,
    original_name: str = "",
    resume: str = "",
    desired_role_description: str = "",
    additional_skills: str = "",
    us_citizen: str = "",
    security_clearance: str = "None",
    additional_skills_input: str = "",
    keywords_input: str = "",
    location: str = "",
    reject_job_titles_input: str = "",
):
    """Save (insert or update) a profile."""
    name_to_save = original_name if original_name else profile_name

    # Parse list fields
    additional_skills_list = _parse_json_list(additional_skills_input)
    keywords_list = _parse_json_list(keywords_input)
    reject_job_titles_list = _parse_json_list(reject_job_titles_input)

    profile_data = {
        "profile_name": name_to_save,
        "resume": resume,
        "desired_role_description": desired_role_description,
        "additional_skills": _serialize_json_list(additional_skills_list),
        "us_citizen": 1 if us_citizen else 0,
        "security_clearance": security_clearance or "None",
        "keywords": _serialize_json_list(keywords_list),
        "location": location.strip(),
        "reject_job_titles": _serialize_json_list(reject_job_titles_list),
        "owning_user": auth,
    }
    save_profile(profile_data)
    return Redirect("/profiles")


@ar("/autosave")
def post_autosave(
    auth,
    profile_rowid: str = "",
    profile_name: str = "",
    resume: str = "",
    desired_role_description: str = "",
    additional_skills: str = "[]",
    us_citizen: str = "0",
    security_clearance: str = "None",
    keywords: str = "[]",
    location: str = "",
    reject_job_titles: str = "[]",
):
    """Auto-save profile data without redirecting."""
    if not profile_name:
        return "No profile name"

    profile_data = {
        "profile_name": profile_name,
        "resume": resume,
        "desired_role_description": desired_role_description,
        "additional_skills": additional_skills,
        "us_citizen": 1 if us_citizen == "1" else 0,
        "security_clearance": security_clearance or "None",
        "keywords": keywords,
        "location": location.strip(),
        "reject_job_titles": reject_job_titles,
        "owning_user": auth,
    }
    if profile_rowid:
        update_profile_by_rowid(int(profile_rowid), profile_data)
    else:
        save_profile(profile_data)
    return "Saved"


@ar("/render-list-items/{field_id}")
async def post_render_list(field_id: str, items: str = "[]"):
    """Return rendered HTML for list items."""
    try:
        item_list = json.loads(items)
    except (json.JSONDecodeError, TypeError):
        item_list = []
    return _render_list_items(field_id, item_list)


@ar("/analyze-ats")
def post_analyze_ats(auth, profile_name: str = ""):
    """Run ATS resume quality analysis via Gemini and persist the result."""
    if not profile_name:
        return Alert("Please enter a profile name first.", cls=AlertT.error)
    profile = get_profile(profile_name, auth)
    if not profile or not profile.get("resume"):
        return Alert("No resume text found. Please upload a resume first.", cls=AlertT.error)
    try:
        result = analyze_resume_ats(profile["resume"])
        if "error" in result:
            return Alert(f"Analysis error: {result['error']}", cls=AlertT.error)
        ats_json = json.dumps(result)
        save_ats_score(profile_name, auth, ats_json)
        return _render_ats_results(ats_json)
    except Exception as e:
        logger.exception("ATS analysis failed")
        return Alert(f"Analysis failed: {str(e)}", cls=AlertT.error)


@ar("/upload-resume")
def post_resume_upload(auth, file: UploadFile, profile_name: str = ""):
    """Convert uploaded PDF/TXT/MD to Markdown and immediately save to database."""
    try:
        if not profile_name:
            return "Please enter a profile name first."

        content = file.file.read()
        filename = file.filename or "resume.pdf"
        ext = os.path.splitext(filename)[1]
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=ext, delete=False
        ) as tmp_file:
            tmp_file.write(content)
            tmp_path = tmp_file.name

        try:
            markdown_content = convert_file_to_markdown(tmp_path)
            existing_profile = get_profile(profile_name, auth)
            if existing_profile:
                existing_profile["resume"] = markdown_content
                save_profile(existing_profile)
                save_resume_blob(profile_name, auth, content)
            return markdown_content
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except Exception as e:
        return str(e)


@ar("/resume-pdf")
def get_resume_pdf(auth, profile_name: str = ""):
    """Serve the stored resume blob for a profile."""
    from starlette.responses import Response as StarletteResponse
    if not profile_name:
        return StarletteResponse("Not found", status_code=404)
    profile = get_profile(profile_name, auth)
    if not profile or not profile.get("resume_blob"):
        return StarletteResponse("Not found", status_code=404)
    raw = profile["resume_blob"]
    if isinstance(raw, memoryview):
        raw = bytes(raw)
    elif not isinstance(raw, (bytes, bytearray)):
        raw = str(raw).encode("utf-8")
    else:
        raw = bytes(raw)
    content_type = "application/pdf" if raw[:4] == b"%PDF" else "text/plain; charset=utf-8"
    return StarletteResponse(content=raw, media_type=content_type)


@ar("/sanitize-pii")
async def post_sanitize_pii(text: str = ""):
    """Sanitize PII from text and return it."""
    return remove_pii(text)


@ar("/duplicate/{profile_name}")
def post_duplicate(auth, profile_name: str):
    """Duplicate a profile and open the copy for editing."""
    source = get_profile(profile_name, auth)
    if not source:
        return P(f"Profile '{profile_name}' not found.")

    existing_names = {p["profile_name"] for p in get_profiles_for_user(auth)}
    base = profile_name
    candidate = f"{base} (copy)"
    if candidate in existing_names:
        i = 2
        while f"{base} (copy {i})" in existing_names:
            i += 1
        candidate = f"{base} (copy {i})"

    new_profile = {
        "profile_name": candidate,
        "resume": source.get("resume", ""),
        "desired_role_description": source.get("desired_role_description", ""),
        "additional_skills": source.get("additional_skills", "[]"),
        "us_citizen": source.get("us_citizen", 0),
        "security_clearance": source.get("security_clearance", "None"),
        "keywords": source.get("keywords", "[]"),
        "location": source.get("location", ""),
        "reject_job_titles": source.get("reject_job_titles", "[]"),
        "owning_user": auth,
        "is_default": 0,
    }
    save_profile(new_profile)
    saved = get_profile(candidate, auth)
    return _profile_form(saved)


@ar("/set-default/{profile_name}")
def post_set_default(auth, profile_name: str):
    """Set a profile as the default and refresh the list."""
    from .db import set_default_profile, get_profiles_for_user

    set_default_profile(profile_name, auth)
    profiles = get_profiles_for_user(auth)
    return _render_profiles_ui(profiles)


@ar("/delete/{profile_name}")
def delete(auth, profile_name: str):
    """Delete a profile."""
    delete_profile(profile_name, auth)
    return ""
