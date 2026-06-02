from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional
from urllib.parse import quote



from jobspy.model import (
    JobPost,
    Location,
    JobResponse,
    Scraper,
    ScraperInput,
    Site,
    JobType,
    Country,
)
from jobspy.util import (
    get_enum_from_job_type,
    create_session,
)
from html_to_markdown import convert_to_markdown

logger = logging.getLogger("eightfold")


class EightFold(Scraper):
    def __init__(
        self,
        proxies: list[str] | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        **kwargs,
    ):
        self.site_configs = {}
        super().__init__(
            Site.EIGHTFOLD, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs
        )

        self.session = create_session(proxies=self.proxies, ca_cert=ca_cert)

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrape EightFold jobs - site_config is REQUIRED.

        Args:
            scraper_input: Must contain site_config (e.g., "microsoft")

        Returns:
            JobResponse with jobs from the specified site only
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []

        # Load parameters from job_finder_config
        job_finder_config = scraper_input.job_finder_config or {}
        site_configs = job_finder_config.get("eightfold_params", {})

        if not site_configs:
            raise ValueError("No eightfold_params found in job_finder_config")

        # site_config is REQUIRED for EightFold scraper
        if not scraper_input.site_config:
            raise ValueError(
                f"EightFold requires site_config. Valid options: {list(site_configs.keys())}"
            )

        site_name = scraper_input.site_config
        if site_name not in site_configs:
            raise ValueError(
                f"Unknown site_config '{site_name}'. Valid options: {list(site_configs.keys())}"
            )

        site_config = site_configs[site_name]
        company_name = site_config.get("company_name", site_name.capitalize())
        logger.info(f"*** Scraping EightFold site: {site_name} ***")

        domain = site_config.get("domain")
        base_url = site_config.get("base_url")

        if not domain or not base_url:
            raise ValueError(f"Missing domain or base_url for {site_name}")

        start = 0
        while True:
            location = site_config.get("location", scraper_input.location or "")
            search_term = quote(scraper_input.search_term or "")
            params = {
                "domain": domain,
                "query": search_term,
                "location": location,
                "start": start,
                "sort_by": "match",
                "hl": "en",
            }
            try:
                search_url = f"{base_url}/api/pcsx/search"
                response = self.session.get(search_url, params=params)
                if response.status_code != 200:
                    logger.error(
                        f"Failed to search {site_name}: {response.status_code} - {response.text[:100]}"
                    )
                    break
                data = response.json()
                positions = data.get("data", {}).get("positions", [])

                if not positions:
                    break

                for pos in positions:
                    job_post = self._process_job(pos, domain, base_url, company_name)
                    if job_post:
                        job_list.append(job_post)

                if len(positions) < 10:  # Assuming 10 is the default page size
                    break
                start += len(positions)

                if scraper_input.results_wanted and len(job_list) >= scraper_input.results_wanted:
                    break

            except Exception as e:
                logger.error(f"Error searching {company_name}: {e}")
                raise

        logger.info(f"*** Scraped EightFold site: {site_name}. Found {len(job_list)} jobs ***")

        return JobResponse(
            jobs=job_list[: scraper_input.results_wanted]
            if scraper_input.results_wanted
            else job_list
        )

    def _process_job(
        self, pos: dict, domain: str, base_url: str, company_name: str
    ) -> Optional[JobPost]:
        job_id = str(pos.get("id"))
        params = {
            "position_id": job_id,
            "domain": domain,
            "hl": "en",
        }
        try:
            # note that this is the api that returns JSON
            # the job_url is a human readable url
            details_url = f"{base_url}/api/pcsx/position_details"
            response = self.session.get(details_url, params=params)
            if response.status_code != 200:
                logger.error(
                    f"Failed to get details for {job_id}: {response.status_code}"
                )
                return None
            details_data = response.json()

            job_data = details_data.get("data", {})
            description_html = job_data.get("jobDescription", "")
            description_md = (
                convert_to_markdown(description_html) if description_html else ""
            )

            # # Extract locations
            # loc_list = job_data.get("standardizedLocations", [])
            # location = None
            # if loc_list:
            #     # Standardized: "Redmond, WA, US"
            #     parts = loc_list[0].split(", ")
            #     city = parts[0] if len(parts) > 0 else None
            #     state = parts[1] if len(parts) > 1 else None
            #     country = parts[2] if len(parts) > 2 else None
            #     location = Location(
            #         city=city,
            #         state=state,
            #         country=Country.from_string(country) if country else None,
            #     )

            posted_ts = job_data.get("postedTs")
            date_posted = None
            if posted_ts:
                date_posted = datetime.fromtimestamp(posted_ts).date()

            job_url = f"{base_url}{job_data.get('positionUrl')}"
            if self._check_if_job_exists(job_url):
                logger.info(f"Job {job_url} already exists")
                return None
            logger.info(f"Fetched details for job: {job_id}, Title: {job_data.get('name', 'Unknown')}, Description length: {len(description_md)} chars")
            return JobPost(
                id=job_id,
                title=job_data.get("name", "Unknown"),
                company_name=company_name,
                location=None,
                date_posted=date_posted,
                job_url=job_url,
                description=description_md,
                job_type=self._get_job_type(job_data),
            )
        except Exception as e:
            logger.error(f"Error processing job {job_id}: {e}")
            return None

    def _get_job_type(self, job_data: dict) -> list[JobType]:
        # EightFold might have custom fields for employment type
        # In microsoft example: "efcustomTextEmploymentType": ["Full-Time"]
        emp_types = job_data.get("efcustomTextEmploymentType", [])
        if not emp_types:
            return [JobType.OTHER]

        res = []
        for et in emp_types:
            enum_val = get_enum_from_job_type(et.lower().replace("-", ""))
            if enum_val:
                res.append(enum_val)
        return res if res else [JobType.OTHER]
