"""Scraper configuration CRUD routes for job_score."""

import json
import logging
import re
from urllib.parse import urlparse

from fasthtml.common import *
from monsterui.all import *
from starlette.responses import JSONResponse

from .lib.config import BASE_PREFIX
from .db import (
    get_scraper_configs_for_user,
    get_scraper_config,
    save_scraper_config,
    delete_scraper_config,
)
from jobspy.site_configuration_extractor import extract_site_config

logger = logging.getLogger(__name__)

ar = APIRouter(prefix="/config")

CONFIG_TYPES = ["workday", "oraclecloud", "eightfold", "usajobs"]


def _label_row(label: str, id: str = "", **kwargs) -> FT:
    """Horizontal label-left / input-right field row."""
    return Div(
        Span(label, cls="w-36 text-sm font-medium shrink-0"),
        Input(id=id, name=id, cls="flex-1 uk-input", **kwargs),
        cls="flex items-center gap-3",
    )


def _config_type_badge(config_type: str) -> FT:
    colors = {
        "workday": "bg-blue-100 text-blue-700",
        "oraclecloud": "bg-green-100 text-green-700",
        "eightfold": "bg-purple-100 text-purple-700",
        "usajobs": "bg-orange-100 text-orange-700",
    }
    return Span(
        config_type,
        cls=f"text-xs font-semibold px-2 py-0.5 rounded {colors.get(config_type, 'bg-slate-100 text-slate-700')}",
    )


def _config_card(cfg: dict) -> FT:
    """A single config displayed as a card."""
    cj = cfg.get("config_json", {})
    base_url = cj.get("base_url", "")
    summary_parts = []
    if "tenant_site" in cj:
        summary_parts.append(f"Tenant: {cj['tenant_site']}")
    if "siteNumber" in cj:
        summary_parts.append(f"Site: {cj['siteNumber']}")
    if "domain" in cj:
        summary_parts.append(f"Domain: {cj['domain']}")
    if "l" in cj:
        locs = cj["l"]
        count = len(locs) if isinstance(locs, list) else 1
        summary_parts.append(f"Locations: {count}")

    return Card(
        Div(
            Button(
                UkIcon("pencil", ratio=0.8),
                cls="text-slate-400 hover:text-blue-500 p-1 transition-colors",
                hx_get=f"/config/edit/{cfg['config_key']}",
                hx_target="#config-container",
                hx_swap="innerHTML",
                title="Edit Config",
            ),
            Button(
                UkIcon("trash", ratio=0.8),
                cls="text-slate-400 hover:text-red-500 p-1 transition-colors",
                hx_delete=f"/config/delete/{cfg['config_key']}",
                hx_confirm=f"Delete config for '{cfg['company_name']}'?",
                hx_target="closest .uk-card",
                hx_swap="delete",
                title="Delete Config",
            ),
            cls="absolute top-2 right-2 flex gap-1 items-center",
        ),
        P(base_url, cls=TextPresets.muted_sm),
        P(" | ".join(summary_parts), cls=TextPresets.muted_sm) if summary_parts else "",
        header=CardTitle(
            DivLAligned(
                cfg["company_name"],
                _config_type_badge(cfg["config_type"]),
                cls="gap-2",
            )
        ),
        body_cls="space-y-1",
        cls=f"{CardT.hover} relative",
    )


def _type_fields(config_type: str, config_json: dict | None = None) -> FT:
    """Render type-specific form fields."""
    cj = config_json or {}

    if config_type == "workday":
        return Div(
            _label_row("Base URL", id="base_url", value=cj.get("base_url", "")),
            _label_row("Tenant Site", id="tenant_site", value=cj.get("tenant_site", "")),
            Div(
                Label("Search Params (JSON)", fr="search_params", cls="block mb-1 text-sm font-medium"),
                TextArea(
                    json.dumps(cj.get("search_params", {}), indent=2),
                    id="search_params",
                    name="search_params",
                    rows=4,
                    placeholder='{"locations": ["hex_id"], "timeType": "hex_id"}',
                ),
            ),
            cls="space-y-2",
        )

    if config_type == "oraclecloud":
        return Div(
            _label_row("Base URL", id="base_url", value=cj.get("base_url", "")),
            _label_row("Site Number", id="siteNumber", value=cj.get("siteNumber", "")),
            _label_row("Location ID", id="locationId", value=cj.get("locationId", "")),
            _label_row("Facets List", id="facetsList", value=cj.get("facetsList", "")),
            Div(
                Span("Radius / Unit / Sort", cls="w-36 text-sm font-medium shrink-0"),
                Div(
                    Input(id="radius", name="radius", value=str(cj.get("radius", "")), placeholder="Radius", cls="uk-input w-24"),
                    Input(id="radiusUnit", name="radiusUnit", value=cj.get("radiusUnit", "MI"), placeholder="MI", cls="uk-input w-20"),
                    Input(id="sortBy", name="sortBy", value=cj.get("sortBy", "RELEVANCY"), placeholder="RELEVANCY", cls="uk-input flex-1"),
                    cls="flex gap-2 flex-1",
                ),
                cls="flex items-center gap-3",
            ),
            _label_row("Limit", id="limit", value=str(cj.get("limit", ""))),
            cls="space-y-2",
        )

    if config_type == "eightfold":
        return Div(
            _label_row("Base URL", id="base_url", value=cj.get("base_url", "")),
            _label_row("Domain", id="domain", value=cj.get("domain", "")),
            _label_row("Location", id="location", value=cj.get("location", "")),
            Div(
                Label("Extra Params (JSON)", fr="extra_params", cls="block mb-1 text-sm font-medium"),
                TextArea(
                    json.dumps(
                        {k: v for k, v in cj.items() if k not in ("base_url", "domain", "location", "company_name")},
                        indent=2,
                    ),
                    id="extra_params",
                    name="extra_params",
                    rows=4,
                ),
            ),
            cls="space-y-3",
        )

    if config_type == "usajobs":
        l_val = cj.get("l", [])
        locations_text = "\n".join(l_val if isinstance(l_val, list) else [l_val] if l_val else [])
        extra = {k: v for k, v in cj.items() if k not in ("base_url", "company_name", "l")}
        return Div(
            _label_row("Base URL", id="base_url", value=cj.get("base_url", "https://www.usajobs.gov")),
            Div(
                Label("Locations (one per line)", fr="usajobs_l", cls="block mb-1 text-sm font-medium"),
                TextArea(
                    locations_text,
                    id="usajobs_l",
                    name="usajobs_l",
                    rows=4,
                    placeholder="McLean, Virginia\nReston, Virginia\nArlington, Virginia",
                ),
            ),
            Div(
                Label("Extra Params (JSON)", fr="extra_params", cls="block mb-1 text-sm font-medium"),
                TextArea(
                    json.dumps(extra, indent=2) if extra else "",
                    id="extra_params",
                    name="extra_params",
                    rows=3,
                    placeholder='{"ws": "1", "hp": ["public", "ses"]}',
                ),
            ),
            cls="space-y-3",
        )

    return Div()


def _config_form(config: dict | None = None) -> FT:
    """Form for creating or editing a scraper config."""
    editing = config is not None
    config_key = config["config_key"] if editing else ""
    company_name = config.get("company_name", "") if editing else ""
    config_type = config.get("config_type", "workday") if editing else "workday"
    config_json = config.get("config_json", {}) if editing else {}

    return Card(
        Script(f"var _CFG_PREFIX = {repr(BASE_PREFIX)};"),
        Script(
            """
            function extractConfigFromUrl() {
                const url = document.getElementById('extract_url').value.trim();
                const company = document.getElementById('extract_company').value.trim();
                const statusEl = document.getElementById('extract_status');

                if (!url) { statusEl.textContent = 'Please enter a URL'; return; }
                if (!company) { statusEl.textContent = 'Please enter a company name'; return; }

                statusEl.innerHTML = '<span uk-spinner="ratio: 0.6"></span> Extracting...';

                fetch(_CFG_PREFIX + '/config/extract', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'url=' + encodeURIComponent(url) + '&company_name=' + encodeURIComponent(company)
                })
                .then(r => {
                    if (!r.ok) throw new Error('Extraction failed');
                    return r.json();
                })
                .then(data => {
                    const key = Object.keys(data)[0];
                    if (!key) throw new Error('No config extracted');

                    const config = data[key];
                    const urlObj = new URL(url);

                    // Determine type
                    let configType = 'workday';
                    if (urlObj.hostname.includes('myworkdayjobs.com')) configType = 'workday';
                    else if (urlObj.pathname.includes('/hcmUI/CandidateExperience/')) configType = 'oraclecloud';
                    else if (urlObj.pathname.includes('/careers') && urlObj.searchParams.has('pid')) configType = 'eightfold';
                    else if (urlObj.hostname.includes('usajobs.gov')) configType = 'usajobs';

                    // Fill common fields
                    document.getElementById('config_key').value = key;
                    document.getElementById('company_name').value = config.company_name || '';
                    document.getElementById('config_json_hidden').value = JSON.stringify(config);

                    // Set type dropdown and reload fields via HTMX
                    const typeSelect = document.getElementById('config_type_select');
                    const typeHidden = document.getElementById('config_type_hidden');
                    typeSelect.value = configType;
                    typeHidden.value = configType;

                    // Check if type changed from current — if so, swap fields first then fill
                    const currentType = typeSelect.getAttribute('data-current') || 'workday';
                    if (configType !== currentType) {
                        htmx.ajax('GET', _CFG_PREFIX + '/config/fields/' + configType, {
                            target: '#type-fields',
                            swap: 'innerHTML'
                        });
                        typeSelect.setAttribute('data-current', configType);
                        document.getElementById('type-fields').addEventListener('htmx:afterSwap', function fillOnce() {
                            fillTypeFields(configType, config);
                            document.getElementById('type-fields').removeEventListener('htmx:afterSwap', fillOnce);
                        });
                    } else {
                        fillTypeFields(configType, config);
                    }

                    statusEl.textContent = 'Extracted ' + configType + ' config for ' + (config.company_name || key);
                })
                .catch(e => {
                    statusEl.textContent = 'Error: ' + e.message;
                });
            }

            function fillTypeFields(configType, config) {
                if (configType === 'workday') {
                    const baseUrl = document.getElementById('base_url');
                    const tenantSite = document.getElementById('tenant_site');
                    const searchParams = document.getElementById('search_params');
                    if (baseUrl) baseUrl.value = config.base_url || '';
                    if (tenantSite) tenantSite.value = config.tenant_site || '';
                    if (searchParams) searchParams.value = config.search_params ? JSON.stringify(config.search_params, null, 2) : '{}';
                } else if (configType === 'oraclecloud') {
                    const el = (id) => document.getElementById(id);
                    if (el('base_url')) el('base_url').value = config.base_url || '';
                    if (el('siteNumber')) el('siteNumber').value = config.siteNumber || '';
                    if (el('locationId')) el('locationId').value = config.locationId || '';
                    if (el('facetsList')) el('facetsList').value = config.facetsList || '';
                    if (el('radius')) el('radius').value = config.radius || '';
                    if (el('radiusUnit')) el('radiusUnit').value = config.radiusUnit || 'MI';
                    if (el('sortBy')) el('sortBy').value = config.sortBy || 'RELEVANCY';
                    if (el('limit')) el('limit').value = config.limit || '';
                } else if (configType === 'eightfold') {
                    const el = (id) => document.getElementById(id);
                    if (el('base_url')) el('base_url').value = config.base_url || '';
                    if (el('domain')) el('domain').value = config.domain || '';
                    if (el('location')) el('location').value = config.location || '';
                    const extras = Object.fromEntries(
                        Object.entries(config).filter(([k]) => !['company_name','base_url','domain','location'].includes(k))
                    );
                    if (el('extra_params') && Object.keys(extras).length > 0) {
                        el('extra_params').value = JSON.stringify(extras, null, 2);
                    }
                } else if (configType === 'usajobs') {
                    const el = (id) => document.getElementById(id);
                    if (el('base_url')) el('base_url').value = config.base_url || 'https://www.usajobs.gov';
                    if (el('usajobs_l')) {
                        const locs = Array.isArray(config.l) ? config.l : (config.l ? [config.l] : []);
                        el('usajobs_l').value = locs.join('\\n');
                    }
                    const extras = Object.fromEntries(
                        Object.entries(config).filter(([k]) => !['company_name','base_url','l'].includes(k))
                    );
                    if (el('extra_params') && Object.keys(extras).length > 0) {
                        el('extra_params').value = JSON.stringify(extras, null, 2);
                    }
                }
            }
            """,
            type="text/javascript",
        ),
        Form(
            Div(
                Button(
                    UkIcon("arrow-left", ratio=0.8),
                    "Back to Configs",
                    cls=f"{ButtonT.default} flex items-center gap-1",
                    hx_get="/config/",
                    hx_target="#config-container",
                    hx_swap="innerHTML",
                    type="button",
                ),
                cls="mb-4",
            ),
            # URL auto-populate section
            Div(
                H4("Auto-populate from URL", cls="text-sm font-semibold mb-2"),
                Div(
                    Input(
                        id="extract_url",
                        placeholder="Paste Workday, OracleCloud, Eightfold, or USAJobs URL...",
                        cls="uk-input min-w-0 w-full sm:flex-1",
                    ),
                    Input(
                        id="extract_company",
                        placeholder="Company Name",
                        cls="uk-input min-w-0 w-full sm:w-48",
                    ),
                    Button(
                        "Extract",
                        type="button",
                        cls=f"{ButtonT.default} w-full sm:w-auto shrink-0",
                        onclick="extractConfigFromUrl()",
                    ),
                    cls="flex flex-col sm:flex-row gap-2",
                ),
                Div(id="extract_status", cls="text-xs text-gray-400 mt-1"),
                cls="mb-4 p-3 bg-base-200 rounded-lg border border-base-300",
            ),
            # Common fields
            _label_row("Config Key", id="config_key", value=config_key, disabled=editing),
            Hidden(name="original_key", value=config_key) if editing else None,
            _label_row("Company Name", id="company_name", value=company_name),
            Div(
                Span("Config Type", cls="w-36 text-sm font-medium shrink-0"),
                NotStr(
                    f'<select id="config_type_select" name="config_type" class="uk-select flex-1"'
                    f' onchange="document.getElementById(\'config_type_hidden\').value=this.value; htmx.ajax(\'GET\', _CFG_PREFIX + \'/config/fields/\' + this.value, {{target: \'#type-fields\', swap: \'innerHTML\'}}); this.setAttribute(\'data-current\', this.value);">'
                    + "".join(
                        f'<option value="{ct}" {"selected" if ct == config_type else ""}>{ct.capitalize()}</option>'
                        for ct in CONFIG_TYPES
                    )
                    + '</select>'
                ),
                Hidden(name="config_type", id="config_type_hidden", value=config_type),
                cls="flex items-center gap-3",
            ),
            Hidden(name="config_json", id="config_json_hidden", value=json.dumps(config_json)),
            # Type-specific fields
            Div(
                _type_fields(config_type, config_json),
                id="type-fields",
                cls="mt-4",
            ),
            # Save button
            Div(
                Button("Save", type="submit", cls=ButtonT.primary),
                cls="mt-6",
            ),
            cls="space-y-3",
            id="config-form",
            hx_post="/config/save",
            hx_target="#config-container",
            hx_swap="innerHTML",
        ),
        header=CardTitle("Edit Config" if editing else "Create Config"),
    )


def _render_configs_ui(configs: list[dict]) -> FT:
    """Render configs grouped by type."""
    if not configs:
        return Div(
            DivFullySpaced(
                H2("Scraper Configs"),
                Button(
                    UkIcon("plus-circle"),
                    cls="text-blue-500 hover:text-blue-700 p-2 transition-colors [&_svg]:w-[32px] [&_svg]:h-[32px]",
                    hx_get="/config/create",
                    hx_target="#config-container",
                    hx_swap="innerHTML",
                    title="Add Config",
                ),
            ),
            P("No scraper configs yet. Click + to add one.", cls=TextPresets.muted_sm + " mt-4"),
        )

    by_type = {}
    for c in configs:
        by_type.setdefault(c["config_type"], []).append(c)

    groups = []
    for ct in CONFIG_TYPES:
        if ct in by_type:
            cards = [_config_card(c) for c in by_type[ct]]
            groups.append(
                Div(
                    H4(f"{ct.capitalize()} Configs", cls="text-sm font-semibold text-gray-600 mb-2"),
                    Div(*cards, cls="space-y-3"),
                    cls="mb-4",
                )
            )

    return Div(
        DivFullySpaced(
            H2("Scraper Configs"),
            Button(
                UkIcon("plus-circle"),
                cls="text-blue-500 hover:text-blue-700 p-2 transition-colors [&_svg]:w-[32px] [&_svg]:h-[32px]",
                hx_get="/config/create",
                hx_target="#config-container",
                hx_swap="innerHTML",
                title="Add Config",
            ),
        ),
        Div(*groups, cls="mt-4 space-y-2"),
    )


@ar("/")
def get(auth, sess, hx_request: bool = False):
    """List all scraper configs for the current user."""
    configs = get_scraper_configs_for_user(auth)

    if hx_request:
        return _render_configs_ui(configs)

    from .common import NavigationLayout

    user_info = sess.get("user_info", {})
    name = user_info.get("name", "User")

    return NavigationLayout(
        Div(
            Div(id="config-container", cls="space-y-4 mt-4")(
                _render_configs_ui(configs)
            ),
        ),
        title="Scraper Configuration",
        current_path="/config",
        user_info=user_info,
    )


@ar("/create")
def get_create(auth):
    """Return empty form for creating a new config."""
    return _config_form()


@ar("/edit/{config_key}")
def get_edit(auth, config_key: str):
    """Return edit form for a specific config."""
    config = get_scraper_config(config_key, auth)
    if not config:
        return P(f"Config '{config_key}' not found.")
    return _config_form(config)


@ar("/fields/{config_type}")
def get_fields(config_type: str):
    """Return type-specific form fields (HTMX partial)."""
    return _type_fields(config_type)


@ar("/fields/{config_type}", methods=["POST"])
def post_fields(config_type: str, config_json: str = ""):
    """Return type-specific form fields pre-filled with config data."""
    try:
        body = json.loads(config_json) if config_json else {}
    except json.JSONDecodeError:
        body = {}
    return _type_fields(config_type, body)


@ar("/save", methods=["POST"])
def post_save(
    auth,
    config_key: str = "",
    original_key: str = "",
    company_name: str = "",
    config_type: str = "workday",
    base_url: str = "",
    tenant_site: str = "",
    search_params: str = "",
    siteNumber: str = "",
    locationId: str = "",
    facetsList: str = "",
    radius: str = "",
    radiusUnit: str = "MI",
    sortBy: str = "RELEVANCY",
    limit: str = "",
    domain: str = "",
    location: str = "",
    extra_params: str = "",
    usajobs_l: str = "",
    config_json: str = "",
):
    """Save a scraper config."""
    key_to_save = original_key if original_key else config_key

    # USAJobs is single-item: block creating a second one
    if config_type == "usajobs" and not original_key:
        existing = [c for c in get_scraper_configs_for_user(auth) if c["config_type"] == "usajobs"]
        if existing:
            return Div(
                P("Only one USAJobs configuration is allowed per user. Edit or delete the existing one first.",
                  cls="text-red-600 text-sm font-medium mb-3"),
                Button(
                    "Back to Configs",
                    cls=ButtonT.default,
                    hx_get="/config/",
                    hx_target="#config-container",
                    hx_swap="innerHTML",
                ),
            )

    # For multi-site types, enforce base_url uniqueness per user per type
    if config_type in ("workday", "oraclecloud", "eightfold") and base_url:
        for c in get_scraper_configs_for_user(auth):
            if c["config_type"] != config_type:
                continue
            if c["config_key"] == key_to_save:
                continue  # same record being edited
            existing_base = c.get("config_json", {}).get("base_url", "")
            if existing_base.rstrip("/") == base_url.rstrip("/"):
                existing_company = c.get("company_name", "")
                label = f"{existing_company} " if existing_company else ""
                return Div(
                    P(f"A {label}{config_type} configuration for '{base_url}' already exists. Edit or delete it first.",
                      cls="text-red-600 text-sm font-medium mb-3"),
                    Button(
                        "Back to Configs",
                        cls=ButtonT.default,
                        hx_get="/config/",
                        hx_target="#config-container",
                        hx_swap="innerHTML",
                    ),
                )

    # Build config_json from type-specific fields
    if config_type == "workday":
        cj = {"company_name": company_name, "base_url": base_url}
        if tenant_site:
            cj["tenant_site"] = tenant_site
        if search_params:
            try:
                cj["search_params"] = json.loads(search_params)
            except json.JSONDecodeError:
                pass
    elif config_type == "oraclecloud":
        cj = {"company_name": company_name, "base_url": base_url}
        if siteNumber:
            cj["siteNumber"] = siteNumber
        if locationId:
            cj["locationId"] = locationId
        if facetsList:
            cj["facetsList"] = facetsList
        if radius:
            cj["radius"] = int(radius)
        if radiusUnit:
            cj["radiusUnit"] = radiusUnit
        if sortBy:
            cj["sortBy"] = sortBy
        if limit:
            cj["limit"] = int(limit)
    elif config_type == "eightfold":
        cj = {"company_name": company_name, "base_url": base_url}
        if domain:
            cj["domain"] = domain
        if location:
            cj["location"] = location
        if extra_params:
            try:
                cj.update(json.loads(extra_params))
            except json.JSONDecodeError:
                pass
    elif config_type == "usajobs":
        cj = {"company_name": company_name, "base_url": base_url}
        if usajobs_l:
            locs = [x.strip() for x in usajobs_l.splitlines() if x.strip()]
            if locs:
                cj["l"] = locs
        if extra_params:
            try:
                cj.update(json.loads(extra_params))
            except json.JSONDecodeError:
                pass
    else:
        cj = json.loads(config_json) if config_json else {}

    save_scraper_config({
        "config_key": key_to_save,
        "config_type": config_type,
        "company_name": company_name,
        "config_json": json.dumps(cj),
        "owning_user": auth,
    })

    configs = get_scraper_configs_for_user(auth)
    return _render_configs_ui(configs)


@ar("/delete/{config_key}", methods=["DELETE"])
def delete(auth, config_key: str):
    """Delete a scraper config."""
    delete_scraper_config(config_key, auth)
    return ""


@ar("/extract", methods=["POST"])
def post_extract(url: str = "", company_name: str = ""):
    """Extract config from a URL and return as JSON."""
    if not url or not company_name:
        return Response("Missing url or company_name", status_code=400)

    result = extract_site_config(url, company_name)
    if not result:
        return Response("Could not extract config from URL", status_code=422)

    return JSONResponse(result)
