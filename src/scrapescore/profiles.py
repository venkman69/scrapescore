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
from .db import get_profiles_for_user, get_profile, save_profile, update_profile_by_rowid, delete_profile
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


def _profile_form(profile: dict | None = None) -> FT:
    """Editable form for creating or editing a profile."""
    creating = profile is None
    name_val = "" if creating else profile.get("profile_name", "")

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
                    cls="mt-2 border-2 border-dashed border-gray-300 rounded p-2",
                    ondragover="resumeDragOver(event)",
                    ondragleave="resumeDragLeave(event)",
                    ondrop="resumeDrop(event, 'resume_textarea')",
                ),
                Div(
                    Button(
                        "Upload Resume",
                        type="button",
                        cls="uk-button uk-button-default uk-button-small",
                        onclick="document.getElementById('resume_file_input').click()",
                    ),
                    Span("or drag & drop a file above", cls="text-xs text-gray-400 self-center"),
                    cls="flex gap-2 mt-1 items-center",
                ),
                Input(
                    type="file",
                    name="resume_file",
                    accept=".pdf,.txt,.md",
                    id="resume_file_input",
                    onchange="uploadAndConvertResume(this, 'resume_textarea')",
                    style="display:none",
                ),
                Div(id="resume_preview", cls="mt-1 text-sm text-gray-600"),
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
            return markdown_content
        finally:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
    except Exception as e:
        return str(e)


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
