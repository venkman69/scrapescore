from hmac import new
from urllib.parse import quote
import json

from html_to_markdown import convert_to_markdown

from datetime import datetime
from typing import Optional

from jobspy.exception import WorkdayException
from jobspy.model import (
    JobPost,
    Location,
    JobResponse,
    Country,
    Scraper,
    ScraperInput,
    Site,
)
from jobspy.util import (
    get_enum_from_job_type,
    JobType,
    create_session,
)

import logging

logger = logging.getLogger("workday")


class WorkDay(Scraper):
    delay = 3
    band_delay = 4
    jobs_per_page = 25

    def __init__(
        self,
        proxies: list[str] | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        **kwargs,
    ):
        """
        Initializes WorkdayScraper
        """
        self.site_configs = {}
        super().__init__(
            Site.WORKDAY, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs
        )

        self.session = create_session(
            proxies=proxies[0] if isinstance(proxies, list) and proxies else proxies,  # type: ignore
            ca_cert=ca_cert,
            is_tls=False,
        )

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrape Workday jobs - site_config is REQUIRED.

        Args:
            scraper_input: Must contain site_config (e.g., "capone", "crowdstrike")

        Returns:
            JobResponse with jobs from the specified site only
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []
        if not scraper_input.search_term:
            raise WorkdayException("Search term is required")

        # Load parameters from job_finder_config
        job_finder_config = scraper_input.job_finder_config or {}
        site_configs = job_finder_config.get("workday_params", {})

        if not site_configs:
            raise ValueError("No workday_params found in job_finder_config")

        # site_config is REQUIRED for WorkDay scraper
        if not scraper_input.site_config:
            raise ValueError(
                f"WorkDay requires site_config. Valid options: {list(site_configs.keys())}"
            )

        site_name = scraper_input.site_config
        if site_name not in site_configs:
            raise ValueError(
                f"Unknown site_config '{site_name}'. Valid options: {list(site_configs.keys())}"
            )

        site_config = site_configs[site_name]
        company_name = site_config.get("company_name", site_name.capitalize())

        logger.info(f"*** Scraping Workday site: {site_name} ***")

        base_url = site_config.get("base_url", "")
        if base_url.endswith("/"):
            logger.warning(
                f"Warning: base_url should not end with a slash '/': {base_url}"
            )
            base_url = base_url[:-1]
        jobs_url_relative = site_config.get("jobs_url", "")
        if jobs_url_relative.startswith("/"):
            logger.warning(
                f"Warning: jobs_url should not start with a slash '/': {jobs_url_relative}"
            )
            jobs_url_relative = jobs_url_relative[1:]
        if jobs_url_relative.endswith("/"):
            logger.warning(
                f"Warning: jobs_url should not end with a slash '/': {jobs_url_relative}"
            )
            jobs_url_relative = jobs_url_relative[:-1]

        search_params = site_config.get("search_params", {}).copy()

        tenant_site = site_config.get("tenant_site", "")
        if not tenant_site:
            raise ValueError(f"Missing tenant_site for {site_name}")

        job_cards = self._search_workday_jobs(
            scraper_input.search_term,
            base_url,
            tenant_site,
            search_params,
        )

        for job_card in job_cards:
            job_post = self._process_job(
                job_card, base_url, tenant_site, company_name
            )
            if job_post:
                job_list.append(job_post)

        logger.info(f"*** Scraped Workday site: {site_name}. Found {len(job_list)} jobs ***")
        return JobResponse(jobs=job_list)

    def _process_job(
        self,
        job_card: dict,
        base_url: str,
        tenant_site: str,
        company_name: str,
    ) -> Optional[JobPost]:
        external_path = job_card.get("externalPath", "")
        if not external_path:
            return None

        # Job detail URL for the REST API
        detail_url = f"{base_url}/wday/cxs/{tenant_site}{external_path}"

        try:
            response = self.session.get(detail_url)
            if response.status_code != 200:
                logger.error(
                    f"Failed to get job details: {response.status_code} - {detail_url}"
                )
                return None

            job_details = response.json().get("jobPostingInfo", {})

            # Use externalUrl if available, otherwise fallback to manual construction
            job_url = job_details.get("externalUrl")
            if not job_url:
                job_url = f"{base_url}{external_path}"
                logger.warning(
                    f"{external_path}: externalUrl not found in job details, constructed job URL: {job_url}")
            if self._check_if_job_exists(job_url):
                logger.info(f"{external_path}: Job already exists: {job_url}")
                return None

            job_id = job_details.get("jobReqId")
            title = job_details.get("title", "Unknown")
            description_html = job_details.get("jobDescription", "")
            description_md = convert_to_markdown(description_html)
            logger.info(f"{external_path}: Processing job: {title} (ID: {job_id}), Description length: {len(description_md)} chars")

            location = self._get_location(job_details)
            job_type = self._get_job_type(job_details)

            date_posted_str = job_details.get("startDate")
            date_posted = None
            if date_posted_str:
                date_posted = datetime.strptime(date_posted_str, "%Y-%m-%d")

            return JobPost(
                id=job_id,
                title=title,
                company_name=company_name,
                location=location,
                date_posted=date_posted,
                job_url=job_url,
                compensation=None,
                job_type=job_type,
                description=description_md,
            )
        except Exception as e:
            logger.error(f"Error processing job details: {e}")
            return None

    def _get_job_type(self, job_details: dict) -> list[JobType]:
        job_type_raw = (
            job_details.get("timeType") or job_details.get("employmentType") or ""
        )
        job_type_str = job_type_raw.lower()
        if not job_type_str:
            return [JobType.OTHER]

        job_type_enum = get_enum_from_job_type(job_type_str.replace(" ", ""))
        if job_type_enum is None:
            return [JobType.OTHER]
        return [job_type_enum]

    def _get_location(self, job_details: dict) -> Location:
        # Prioritize detailed location object if available
        req_location = job_details.get("jobRequisitionLocation", {})
        location_str = req_location.get("descriptor") or job_details.get("location", "")

        if not location_str:
            return Location()

        # Often format is "City, State" or "Country, State, City..."
        parts = [p.strip() for p in location_str.split(",")]

        city = None
        state = None
        country = None

        # Simple heuristic for US locations: "City, State" or "USA, State, City..."
        if len(parts) >= 2:
            if parts[0] in ["USA", "US"]:
                country = Country.USA
                state = parts[1]
                city = parts[2] if len(parts) > 2 else None
            else:
                city = parts[0]
                state = parts[1]
        else:
            city = parts[0]

        # Try to get country from nested object
        country_data = req_location.get("country") or job_details.get("country")
        if country_data:
            country_code = country_data.get("alpha2Code") or country_data.get(
                "descriptor"
            )
            if country_code:
                country = Country.from_string(country_code)

        return Location(city=city, state=state, country=country)

    def _search_workday_jobs(
        self,
        query: str,
        base_url: str,
        tenant_site: str,
        search_params: dict,
    ) -> list[dict]:
        search_url = f"{base_url}/wday/cxs/{tenant_site}/jobs"

        # Ensure all search_params values are lists as expected by CXS API
        applied_facets = {}
        for k, v in search_params.items():
            if isinstance(v, list):
                applied_facets[k] = v
            else:
                applied_facets[k] = [v]
        payload = {
            "appliedFacets": applied_facets,
            "limit": 20,
            "offset": 0,
            "searchText": query,
        }

        headers = {
            "accept": "application/json",
            "accept-language": "en-US",
            "content-type": "application/json",
            "pragma": "no-cache",
        }

        try:
            logger.info(f"Searching Workday with payload: {json.dumps(payload)}")
            response = self.session.post(search_url, json=payload, headers=headers)
            if response.status_code != 200:
                logger.error(
                    f"Failed to search Workday jobs: {response.status_code} - {search_url}"
                )
                logger.error(f"Response: {response.text[:500]}")
                return []

            data = response.json()
            job_postings = data.get("jobPostings", [])
            logger.info(f"Number of jobs found: {data.get('total', len(job_postings))}")

            return job_postings
        except Exception as e:
            logger.error(f"Error searching Workday jobs: {e}")
            return []
