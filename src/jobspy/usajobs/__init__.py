from __future__ import annotations
import os
import logging

import requests

from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Country,
    Location,
    Compensation,
    CompensationInterval,
)
from datetime import datetime

logger = logging.getLogger(__name__)

from bs4 import BeautifulSoup
from html_to_markdown import convert_to_markdown


class USAJobs(Scraper):
    def __init__(
        self,
        proxies: list[str] | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        **kwargs,
    ):
        super().__init__(
            Site.USAJOBS, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs
        )
        self.api_key = os.environ.get("USAJOBS_API_KEY")
        self.base_url = "https://data.usajobs.gov/api"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        # Load parameters from job_finder_config
        job_finder_config = scraper_input.job_finder_config or {}
        usajobs_params = job_finder_config.get("usajobs_params", {})
        l_param = usajobs_params.get("l", [])
        locations = ";".join(l_param if isinstance(l_param, list) else [l_param] if l_param else [])

        logger.info(
            f"*** Scraping USAJobs for search term: {scraper_input.search_term}, location: {locations} ***"
        )
        if not self.api_key:
            logger.error("USAJOBS_API_KEY not found in environment variables.")
            return JobResponse(jobs=[])

        headers = {
            "Authorization-Key": self.api_key,
        }

        params = {
            "Keyword": scraper_input.search_term,
            "LocationName": locations,
            "DatePosted": 30,
            "ResultsPerPage": 500,  # Max allowed
        }

        try:
            response = requests.get(
                f"{self.base_url}/search", headers=headers, params=params
            )
            response.raise_for_status()
            data = response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch data from USAJobs API: {e}")
            return JobResponse(jobs=[])

        job_list = []
        for item in data.get("SearchResult", {}).get("SearchResultItems", []):
            job = self._process_job(item)
            if job:
                job_list.append(job)
        logger.info(
            f"*** Scraping USAJobs Completed. Found {len(job_list)} jobs for search term: {scraper_input.search_term} ***"
        )
        return JobResponse(jobs=job_list)

    def _process_job(self, item: dict) -> JobPost | None:
        job_info = item.get("MatchedObjectDescriptor", {})

        job_id = item.get("MatchedObjectId")
        title = job_info.get("PositionTitle")
        company = job_info.get("OrganizationName")
        job_url = job_info.get("PositionURI")
        security_clearance = (
            job_info.get("UserArea", {}).get("Details", {}).get("SecurityClearance")
        )
        if security_clearance == "Not Required":
            security_clearance = 0
        else:
            security_clearance = 1

        locations = job_info.get("PositionLocationDisplay", "").split("; ")
        # For simplicity, we'll just take the first location
        location_str = locations[0] if locations else ""
        location = self._parse_location(location_str)

        majorDuties = job_info.get("UserArea", {}).get("Details", {}).get("MajorDuties")
        description = self._get_description(job_url)
        if not description:
            description = (
                f"{description}\n{'\n'.join(majorDuties)}"
                if majorDuties
                else description
            )
        qualificationSummary = job_info.get("QualificationSummary", "")
        date_posted_str = job_info.get("PublicationStartDate", "")
        if date_posted_str:
            date_posted = datetime.strptime(date_posted_str, "%Y-%m-%dT%H:%M:%S.%f")
            date_posted = date_posted.date()
        else:
            date_posted = None

        comp: Compensation = self._salary_range(job_info)

        logger.info(
            f"{job_id}: Processing job: {title} at {company}, Description length: {len(description)} chars"
        )
        return JobPost(
            id=job_id,
            title=title,
            company_name=company,
            job_url=job_url,
            location=location,
            description=description,
            security_clearance=security_clearance,
            date_posted=date_posted,
            job_type=None,
            emails=None,
            compensation=comp,
        )

    def _parse_location(self, loc_str: str) -> Location:
        # Example: "Washington, District of Columbia"
        parts = [p.strip() for p in loc_str.split(",")]
        city = None
        state = None

        if len(parts) >= 2:
            city = parts[0]
            state = parts[1]
        elif len(parts) == 1:
            city = parts[0]

        return Location(city=city, state=state, country=Country.USA)

    def _salary_range(self, job_info: dict) -> Compensation:
        position_remuneration = job_info.get("PositionRemuneration", [])
        # PositionRemuneration example:
        # [
        #   {
        #       'MinimumRange': '55486',
        #       'MaximumRange': '99314',
        #       'RateIntervalCode': 'PA',
        #       'Description': 'Per Year'
        #   }
        # ]
        if not position_remuneration:
            return Compensation()

        c = None
        if len(position_remuneration) > 0:
            if len(position_remuneration) > 1:
                logger.warning(
                    f"Multiple salary ranges found for job {job_info.get('PositionTitle')}. Using the first range."
                )
            rate_interval_code = position_remuneration[0].get("RateIntervalCode", None)
            if rate_interval_code != "PA":
                logger.warning(
                    f"Salary range for job {job_info.get('PositionTitle')} is not per year. Using None for salary range."
                )
                return Compensation()

            min_salary = int(position_remuneration[0].get("MinimumRange", None))
            max_salary = int(position_remuneration[0].get("MaximumRange", None))
            interval = CompensationInterval.YEARLY
            c = Compensation(
                min_amount=min_salary, max_amount=max_salary, currency="USD"
            )
        return c

    def _get_description(self, job_url: str) -> str | None:
        try:
            response = requests.get(job_url)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser")
            description_div = soup.find("div", class_="apply-joa-defaults")
            if description_div:
                return convert_to_markdown(str(description_div))
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch job description from {job_url}: {e}")
            return None
