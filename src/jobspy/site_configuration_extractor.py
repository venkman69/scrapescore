"""Site configuration extractor for job board URLs."""

import logging
import re
from urllib.parse import urlparse, parse_qs, unquote, urlencode

from playwright.sync_api import sync_playwright
from scrapescore.lib.config import BROWSER_HEADLESS

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)

# Workday facet values are 32-char hex strings
_WORKDAY_HEX_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_WORKDAY_FACET_WHITELIST = {"locations", "jobFamilyGroup", "jobFamily", "workerSubType", "timeType"}
_ORACLE_URL_PARAM_WHITELIST = {
    "location", "locationId", "locationLevel",
    "selectedCategoriesFacet", "selectedTitlesFacet", "selectedWorkplaceTypesFacet",
    "radius", "radiusUnit",
}
_WORKDAY_CXS_PATTERN = re.compile(r"/wday/cxs/([^/]+)/([^/]+)")
_CXS_WAIT_TIMEOUT = 15_000  # ms
_ORACLE_REST_PATH = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
_USAJOBS_HOST = "usajobs.gov"
_USAJOBS_SEARCH_PATH = "/search/results/"


def extract_site_config(url: str, company_name: str) -> dict:
    """Extract site configuration from a job board URL.

    Supports Workday, OracleCloud, and Eightfold URLs. Returns empty dict
    for unsupported sites or on failure.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    raw_params = parse_qs(parsed.query)

    if "myworkdayjobs.com" in host:
        return extract_workday_config(url, company_name)

    if "/hcmUI/CandidateExperience/" in parsed.path:
        return extract_oraclecloud_config(url, company_name)

    if "/careers" in parsed.path and "pid" in raw_params:
        return extract_eightfold_config(url, company_name)

    if _USAJOBS_HOST in host:
        return extract_usajobs_config(url, company_name)

    logger.warning("Unsupported site: %s", host)
    return {}


def extract_workday_config(url: str, company_name: str) -> dict:
    """Extract Workday configuration by parsing the URL and intercepting
    network requests to discover the tenant site."""
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.hostname}"

    # Extract search params — only keep Workday hex-hash facet values
    raw_params = parse_qs(parsed.query)
    search_params = filter_workday_facets(raw_params)

    # Intercept wday/cxs request to discover tenant_site
    tenant_site = _discover_tenant_site(url)

    if not tenant_site:
        if not search_params:
            logger.error(
                "Could not discover tenant_site and no search params found for %s", url
            )
            return {}
        logger.error("Could not discover tenant_site for %s", url)
        return {}

    # Derive config key from company name
    config_key = re.sub(r"[^a-z0-9]", "", company_name.lower())

    config = {
        config_key: {
            "company_name": company_name,
            "tenant_site": tenant_site,
            "base_url": base_url,
        }
    }
    if search_params:
        config[config_key]["search_params"] = search_params

    return config


def filter_workday_facets(raw_params: dict[str, list[str]]) -> dict:
    """Keep only whitelisted Workday facet params whose values are 32-char hex strings."""
    facets = {}
    for key, values in raw_params.items():
        if key not in _WORKDAY_FACET_WHITELIST:
            continue
        hex_values = [v for v in values if _WORKDAY_HEX_PATTERN.match(v)]
        if hex_values:
            facets[key] = hex_values if len(hex_values) > 1 else hex_values[0]
    return facets


def filter_oracle_url_params(raw_params: dict[str, list[str]]) -> dict:
    """Keep only whitelisted URL params from an OracleCloud job search URL."""
    result = {}
    for key, values in raw_params.items():
        if key not in _ORACLE_URL_PARAM_WHITELIST:
            continue
        result[key] = values if len(values) > 1 else values[0]
    return result


def extract_oraclecloud_config(url: str, company_name: str) -> dict:
    """Extract OracleCloud configuration by parsing the URL and intercepting
    the recruitingCEJobRequisitions REST call to discover API details."""
    parsed = urlparse(url)
    raw_params = parse_qs(parsed.query)
    url_params = filter_oracle_url_params(raw_params)

    # Intercept the REST API request to discover base_url and finder params
    finder_params = _discover_oracle_finder(url)

    if not finder_params:
        logger.error("Could not discover OracleCloud API params for %s", url)
        return {}

    config_key = re.sub(r"[^a-z0-9]", "", company_name.lower())

    config = {
        config_key: {
            "company_name": company_name,
            "siteNumber": finder_params.get("siteNumber", ""),
            "facetsList": finder_params.get("facetsList", ""),
            "sortBy": finder_params.get("sortBy", "RELEVANCY"),
            "base_url": finder_params["_base_url"],
            **url_params,
        }
    }

    return config


def filter_eightfold_url_params(raw_params: dict[str, list[str]]) -> dict:
    """Keep pid, location, sort_by, and all filter_* params; drop keyword (query),
    pagination (start), and other non-filter params."""
    _KEEP = {"pid", "location", "sort_by"}
    result = {}
    for key, values in raw_params.items():
        if key in _KEEP or key.startswith("filter_"):
            result[key] = values if len(values) > 1 else values[0]
    return result


def extract_eightfold_config(url: str, company_name: str) -> dict:
    """Extract Eightfold configuration from URL query parameters.
    No Playwright needed — all config is in the URL itself."""
    parsed = urlparse(url)
    raw_params = parse_qs(parsed.query)
    base_url = f"{parsed.scheme}://{parsed.hostname}"

    config_key = re.sub(r"[^a-z0-9]", "", company_name.lower())

    config = {
        config_key: {
            "company_name": company_name,
            "base_url": base_url,
            **filter_eightfold_url_params(raw_params),
        }
    }

    return config


def filter_usajobs_url_params(raw_params: dict[str, list[str]]) -> dict:
    """Return all USAJobs query params except 'k' (keyword/search term)."""
    result = {}
    for key, values in raw_params.items():
        if key == "k":
            continue
        result[key] = values if len(values) > 1 else values[0]
    return result


def extract_usajobs_config(url: str, company_name: str) -> dict:
    """Extract USAJobs configuration from URL query parameters.
    No Playwright needed — all config is in the URL itself.
    The 'k' param (keyword) is excluded because it is supplied at search time."""
    parsed = urlparse(url)
    raw_params = parse_qs(parsed.query)
    base_url = f"{parsed.scheme}://{parsed.hostname}"

    config_key = re.sub(r"[^a-z0-9]", "", company_name.lower())

    config = {
        config_key: {
            "company_name": company_name,
            "base_url": base_url,
            **filter_usajobs_url_params(raw_params),
        }
    }

    return config


def _collect_oracle_location_ids(raw_params: dict[str, list[str]]) -> list[str]:
    """Collect all location IDs from locationId and selectedLocationsFacet params."""
    ids = []

    for val in raw_params.get("locationId", []):
        if val not in ids:
            ids.append(val)

    for val in raw_params.get("selectedLocationsFacet", []):
        # Semicolon-separated (may be URL-encoded as %3B)
        for part in unquote(val).split(";"):
            part = part.strip()
            if part and part not in ids:
                ids.append(part)

    return ids


def _discover_oracle_finder(url: str) -> dict:
    """Launch the URL in Playwright and intercept the recruitingCEJobRequisitions
    request to extract finder parameters and base_url."""
    pw = None
    logger.info("Discovering OracleCloud API params for %s", url)
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=BROWSER_HEADLESS)
        page = browser.new_page()

        with page.expect_request(
            lambda req: _ORACLE_REST_PATH in req.url,
            timeout=_CXS_WAIT_TIMEOUT,
        ) as req_info:
            logger.info("Navigating to %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)

        intercepted = urlparse(req_info.value.url)
        base_url = f"{intercepted.scheme}://{intercepted.hostname}"

        # Parse finder param: "findReqs;key=val,key=val,..."
        query = parse_qs(intercepted.query)
        finder_raw = query.get("finder", [""])[0]
        if not finder_raw:
            logger.error("No finder param in intercepted URL")
            return {}

        params = _parse_finder(finder_raw)
        params["_base_url"] = base_url

        logger.info(
            "Discovered OracleCloud params: siteNumber=%s, base_url=%s",
            params.get("siteNumber"),
            base_url,
        )
        return params

    except Exception as e:
        logger.error("Failed to discover OracleCloud params: %s", e)
        return {}
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass


def _parse_finder(finder_raw: str) -> dict:
    """Parse an Oracle finder param like 'findReqs;siteNumber=CX_1001,facetsList=...,...'
    into a dict of key-value pairs."""
    # Split on first semicolon to skip the finder name
    parts = finder_raw.split(";", 1)
    if len(parts) < 2:
        return {}

    result = {}
    for pair in parts[1].split(","):
        if "=" in pair:
            key, value = pair.split("=", 1)
            result[key.strip()] = unquote(value.strip())
    return result


def _first_of(raw_params: dict, finder_params: dict, key: str):
    """Return the first non-empty value for key from raw_params then finder_params."""
    vals = raw_params.get(key, [])
    if vals:
        return vals[0]
    return finder_params.get(key)


def build_search_url(config_type: str, config: dict) -> str:
    """Reconstruct a browsable job search URL from a stored scraper config.

    Reverses extract_site_config: given a config_type and the config dict
    (as stored in config_json), returns the canonical search page URL.
    """
    if config_type == "workday":
        return _build_workday_url(config)
    if config_type == "oraclecloud":
        return _build_oraclecloud_url(config)
    if config_type == "eightfold":
        return _build_eightfold_url(config)
    if config_type == "usajobs":
        return _build_usajobs_url(config)
    return ""


def _build_workday_url(config: dict) -> str:
    base_url = config.get("base_url", "").rstrip("/")
    tenant_site = config.get("tenant_site", "").strip("/")
    search_params = config.get("search_params", {})

    path = f"{base_url}/{tenant_site}/jobs" if tenant_site else f"{base_url}/jobs"

    if not search_params:
        return path

    # search_params values may be a single string or a list
    pairs = []
    for key, val in search_params.items():
        if isinstance(val, list):
            for v in val:
                pairs.append((key, v))
        else:
            pairs.append((key, val))

    return f"{path}?{urlencode(pairs)}"


def _build_oraclecloud_url(config: dict) -> str:
    base_url = config.get("base_url", "").rstrip("/")
    site_number = config.get("siteNumber", "")

    path = f"{base_url}/hcmUI/CandidateExperience/en/sites/{site_number}/job-search"

    params = []
    location_id = config.get("locationId")
    if location_id:
        ids = location_id if isinstance(location_id, list) else [location_id]
        for loc in ids:
            params.append(("locationId", loc))

    for key in ("radius", "radiusUnit", "sortBy"):
        val = config.get(key)
        if val is not None and val != "":
            params.append((key, val))

    return f"{path}?{urlencode(params)}" if params else path


def _build_eightfold_url(config: dict) -> str:
    base_url = config.get("base_url", "").rstrip("/")
    skip_keys = {"base_url", "company_name", "domain"}

    params = []
    for key, val in config.items():
        if key in skip_keys:
            continue
        if isinstance(val, list):
            for v in val:
                params.append((key, v))
        else:
            params.append((key, val))

    path = f"{base_url}/careers"
    return f"{path}?{urlencode(params)}" if params else path


def _build_usajobs_url(config: dict) -> str:
    base_url = config.get("base_url", "https://www.usajobs.gov").rstrip("/")
    skip_keys = {"base_url", "company_name"}

    params = []
    for key, val in config.items():
        if key in skip_keys:
            continue
        if isinstance(val, list):
            for v in val:
                params.append((key, v))
        else:
            params.append((key, val))

    path = f"{base_url}{_USAJOBS_SEARCH_PATH}"
    return f"{path}?{urlencode(params)}" if params else path


def _discover_tenant_site(url: str) -> str:
    """Launch the URL in Playwright and intercept the wday/cxs AJAX request
    to extract tenant/portal from the URL path."""
    pw = None
    logger.info("Discovering tenant_site for %s", url)
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=BROWSER_HEADLESS)
        page = browser.new_page()

        with page.expect_request(
            lambda req: "/wday/cxs/" in req.url,
            timeout=_CXS_WAIT_TIMEOUT,
        ) as req_info:
            logger.info("Navigating to %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)

        cxs_url = req_info.value.url
        match = _WORKDAY_CXS_PATTERN.search(cxs_url)
        if match:
            tenant_site = f"{match.group(1)}/{match.group(2)}"
            logger.info("Discovered tenant_site: %s", tenant_site)
            return tenant_site

        logger.error("CXS request URL did not match expected pattern: %s", cxs_url)
        return ""

    except Exception as e:
        logger.error("Failed to discover tenant_site: %s", e)
        return ""
    finally:
        if pw:
            try:
                pw.stop()
            except Exception:
                pass
