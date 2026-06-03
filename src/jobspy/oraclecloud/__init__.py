from __future__ import annotations
import logging
from datetime import datetime
from urllib.parse import quote

from html_to_markdown import convert_to_markdown
from bs4 import BeautifulSoup
import json as _json

from jobspy.lib.downloader import download_url
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Country,
    Location,
)
from jobspy.util import create_session

logger = logging.getLogger("OracleCloud")

_HCM_JOBS_ENDPOINT = "/hcmRestApi/resources/latest/recruitingCEJobRequisitions"


class OracleCloud(Scraper):
    def __init__(
        self,
        proxies: list[str] | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        **kwargs,
    ):
        super().__init__(
            Site.ORACLECLOUD, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs
        )
        # companies will hold per-company params (e.g., nfcu, jpmc)
        self.companies: dict[str, dict] = {}
        self.session = create_session(
            proxies=proxies[0] if isinstance(proxies, list) and proxies else proxies,
            ca_cert=ca_cert,
            is_tls=False,
        )
        self.session.headers.update({"Accept": "application/json"})

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrape OracleCloud jobs - site_config is REQUIRED.

        Args:
            scraper_input: Must contain site_config (e.g., "nfcu", "jpmc", "oracle")

        Returns:
            JobResponse with jobs from the specified site only
        """
        # Load parameters from job_finder_config
        job_finder_config = scraper_input.job_finder_config or {}
        oracle_params = job_finder_config.get("oraclecloud_params", {})
        self.chrome_user_data_dir = job_finder_config.get("storage_dirs", {}).get("chrome_user_data_dir")

        # site_config is REQUIRED for OracleCloud scraper
        if not scraper_input.site_config:
            raise ValueError(
                f"OracleCloud requires site_config. Valid options: {list(oracle_params.keys())}"
            )

        site_key = scraper_input.site_config
        if site_key not in oracle_params:
            raise ValueError(
                f"Unknown site_config '{site_key}'. Valid options: {list(oracle_params.keys())}"
            )

        params = oracle_params[site_key]
        company_name = params.get("company_name", site_key.capitalize())

        logger.info(f"*** Scraping OracleCloud company: {company_name} (site_key={site_key}) ***")

        try:
            jobs = self._scrape_company(company_name, params, scraper_input)
            return JobResponse(jobs=jobs)
        except Exception as e:
            logger.error(f"Error scraping company {company_name}: {e}")
            raise

    def _scrape_company(
        self, company_name: str, params: dict, scraper_input: ScraperInput
    ) -> list[JobPost]:
        # Adapted from legacy NFCU implementation but parameterized per company
        job_list: list[JobPost] = []
        base_url = params.get("base_url", "")
        jobs_endpoint = params.get("jobs_endpoint", _HCM_JOBS_ENDPOINT)
        job_details_endpoint = params.get("job_details_endpoint", _HCM_JOBS_ENDPOINT)
        limit = params.get("limit", 10)
        offset = 0
        total_jobs = None

        search_term = scraper_input.search_term or ""
        keyword = search_term if search_term else ""
        if keyword:
             keyword = quote('"'+keyword+'"')

        query_params = {
            "onlyData": "true",
            "expand": "requisitionList.workLocation,requisitionList.otherWorkLocations,requisitionList.secondaryLocations,flexFieldsFacet.values,requisitionList.requisitionFlexFields",
        }

        while True:
            # build finder parameters for the Oracle Cloud jobs endpoint

            encoded_facets = (params.get("facetsList") or "").replace(";", "%3B")
            params_list = [
                f"siteNumber={params.get('siteNumber')}",
                f"facetsList={encoded_facets}",
                f"limit={limit}",
                f"keyword={keyword}",
                f"locationId={params.get('locationId')}",
                f"radius={params.get('radius')}",
                f"radiusUnit={params.get('radiusUnit')}",
                f"sortBy={params.get('sortBy')}",
                f"offset={offset}",
            ]
            finder_val = f"findReqs;{','.join(params_list)}"

            url = f"{base_url}{jobs_endpoint}"
            full_url = f"{url}?onlyData=true&expand={query_params['expand']}&finder={finder_val}"

            try:
                logger.info(f"Fetching jobs for {company_name} from: {full_url}")
                resp = self.session.get(full_url, timeout=30)
                resp.raise_for_status()
                data = resp.json()

                items = data.get("items", [])
                if not items:
                    break

                item = items[0]
                total_jobs = item.get("TotalJobsCount")
                req_list = item.get("requisitionList", [])

                if not req_list:
                    break

                for req in req_list:
                    job = self._process_job(
                        req, base_url, job_details_endpoint, params, company_name
                    )
                    if job:
                        job_list.append(job)

                offset += len(req_list)
                if total_jobs is not None and offset >= total_jobs:
                    break

                if len(req_list) == 0:
                    break

            except Exception as e:
                logger.error(f"Error scraping {company_name} page: {e}")
                break
        
        logger.info(f"*** Scraping OracleCloud Completed. Found {len(job_list)} jobs for {company_name} for criteria: {scraper_input.search_term} ***")

        return job_list

    def _process_job(
        self,
        req: dict,
        base_url: str,
        job_details_endpoint: str,
        params: dict,
        company_name: str,
    ) -> JobPost | None:
        try:
            job_id = req.get("Id")
            title = req.get("Title") or ""

            details_data = self._fetch_job_details(
                job_id, base_url, job_details_endpoint, params
            )
            job_url = f"{base_url}/hcmUI/CandidateExperience/en/sites/{params.get('siteNumber')}/job/{job_id}"

            # Collect description fields from the detail response (if available) or the search result
            if details_data:
                # Path-based returns flat resource; finder-based wraps in {"items": [...]}
                item = details_data.get("items", [None])[0] if "items" in details_data else details_data
            else:
                item = {}

            _desc_sources = [
                item.get("ExternalDescriptionStr"),
                item.get("InternalResponsibilitiesStr"),
                item.get("ExternalQualificationsStr"),
                item.get("InternalQualificationsStr"),
                item.get("CorporateDescriptionStr"),
                # also try the same fields from the search result directly
                req.get("ExternalResponsibilitiesStr"),
                req.get("ExternalQualificationsStr"),
                req.get("ShortDescriptionStr"),
            ]
            description_html = "\n".join([f for f in _desc_sources if f])

            # Last resort: render the job HTML page via Playwright and extract JSON-LD
            if not description_html:
                description_html = self._fetch_description_from_page(job_url) or ""
                if description_html:
                    logger.info(f"{job_id}: Got description from rendered HTML page ({len(description_html)} chars)")
                else:
                    logger.warning(f"{job_id}: No description available from REST API or HTML page")

            description = convert_to_markdown(description_html) if description_html else ""

            location = self._parse_location(req.get("PrimaryLocation") or "")

            posted_date_str = req.get("PostedDate")
            date_posted = None
            if posted_date_str:
                try:
                    date_posted = datetime.strptime(posted_date_str, "%Y-%m-%d").date()
                except ValueError:
                    pass
            logger.info(f"{job_id}: Processing job: {title} at {company_name}, Description length: {len(description)} chars")

            return JobPost(
                id=job_id,
                title=title,
                company_name=company_name,
                job_url=job_url,
                location=location,
                date_posted=date_posted,
                description=description,
                job_type=[],
                emails=[],
            )

        except Exception as e:
            logger.error(
                f"Error processing job {req.get('Id')} for {company_name}: {e}"
            )
            return None

    def _fetch_job_details(self, job_id, base_url, job_details_endpoint, params):
        url = f"{base_url}{job_details_endpoint}/{job_id}"
        try:
            resp = self.session.get(url, params={"onlyData": "true"}, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"Failed to fetch details for job {job_id}: {e}")
            return None

    def _fetch_description_from_page(self, job_url: str) -> str:
        """Fetch the job HTML page (via Playwright if needed) and extract description from JSON-LD."""
        try:
            chrome_user_data_dir = getattr(self, "chrome_user_data_dir", None)
            html = download_url(job_url, chrome_user_data_dir=chrome_user_data_dir)
            if not html:
                return ""
            soup = BeautifulSoup(html, "html.parser")
            ld = soup.find("script", type="application/ld+json")
            if ld and ld.string:
                data = _json.loads(ld.string)
                return data.get("description", "")
        except Exception as e:
            logger.debug(f"HTML page description fetch failed for {job_url}: {e}")
        return ""

    def _parse_location(self, loc_str):
        # "Vienna, VA, United States"
        if not loc_str:
            return Location(country=Country.USA)

        parts = [p.strip() for p in loc_str.split(",")]
        country = Country.USA
        city = None
        state = None

        if len(parts) >= 3:
            city = parts[0]
            state = parts[1]
            try:
                country = Country.from_string(parts[2])
            except Exception:
                pass
        elif len(parts) == 2:
            city = parts[0]
            state = parts[1]

        return Location(city=city, state=state, country=country)
