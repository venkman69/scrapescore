from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Tuple

import pandas as pd

from jobspy.glassdoor import Glassdoor
from jobspy.google import Google
from jobspy.indeed import Indeed
from jobspy.linkedin import LinkedIn
from jobspy.workday import WorkDay
from jobspy.marriott import Marriott
from jobspy.oraclecloud import OracleCloud
from jobspy.eightfold import EightFold
from jobspy.usajobs import USAJobs
from jobspy.model import JobType, Location, JobResponse, Country
from jobspy.model import SalarySource, ScraperInput, Site
from jobspy.util import (
    set_logger_level,
    extract_salary,
    create_logger,
    get_enum_from_value,
    map_str_to_site,
    convert_to_annual,
    desired_order,
)
from jobspy.ziprecruiter import ZipRecruiter

SCRAPER_MAPPING = {
    Site.LINKEDIN: LinkedIn,
    Site.INDEED: Indeed,
    Site.ZIP_RECRUITER: ZipRecruiter,
    Site.GLASSDOOR: Glassdoor,
    Site.GOOGLE: Google,
    Site.WORKDAY: WorkDay,
    Site.MARRIOTT: Marriott,
    Site.ORACLECLOUD: OracleCloud,
    Site.EIGHTFOLD: EightFold,
    Site.USAJOBS: USAJobs,
}


def scrape_site(
    site: Site,
    scraper_input: ScraperInput,
    run_id: str,
    proxies: list[str] | str | None = None,
    ca_cert: str | None = None,
    user_agent: str | None = None,
) -> Tuple[str, JobResponse]:
    """
    Scrape a single job site and log the activity.

    For multi-site scrapers (OracleCloud, WorkDay, EightFold), this function
    expands the scrape into individual site-specific scrapes with proper logging.

    Args:
        site: The job site to scrape
        scraper_input: ScraperInput with search parameters
        run_id: Unique identifier for this job_finder run (groups all scrapes from one session)
        proxies: Optional proxy list
        ca_cert: Optional CA certificate
        user_agent: Optional user agent string

    Returns:
        Tuple of (site_name, JobResponse)
    """
    scraper_class = SCRAPER_MAPPING[site]
    if isinstance(proxies, str):
        proxies = [proxies]

    # Check if this is a multi-site scraper (OracleCloud, WorkDay, EightFold)
    multi_site_scrapers = [Site.ORACLECLOUD, Site.WORKDAY, Site.EIGHTFOLD]
    is_multi_site = site in multi_site_scrapers

    if is_multi_site:
        # Multi-site scrapers: expand into individual site scrapes
        all_scraped_data = JobResponse(jobs=[])
        config = scraper_input.job_finder_config or {}
        params_key = f"{site.value}_params"
        site_params = config.get(params_key, {})

        if not site_params:
            create_logger(site.value).warning(f"No {params_key} found in config")
            return site.value, all_scraped_data

        _db_path = config.get("storage_dirs", {}).get("job_finder_db_path")

        # Iterate through each configured site
        for site_key in site_params.keys():
            # Create new ScraperInput with site_config set
            site_input = scraper_input.model_copy(update={"site_config": site_key})

            scraper = scraper_class(
                proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, db_path=_db_path
            )
            start_time = time.time()

            try:
                scraped_data = scraper.scrape(site_input)
                duration = time.time() - start_time

                # Log with specific site name
                scraper.log_scraping_activity(
                    run_id=run_id,
                    search_term=scraper_input.search_term or "N/A",
                    jobs_found=len(scraped_data.jobs),
                    site=site_key,
                    scrape_duration_seconds=duration,
                    success=True,
                )

                all_scraped_data.jobs.extend(scraped_data.jobs)
                create_logger(site.value).info(
                    f"finished scraping {site_key}: found {len(scraped_data.jobs)} jobs in {duration:.2f}s"
                )

            except Exception as e:
                duration = time.time() - start_time
                scraper.log_scraping_activity(
                    run_id=run_id,
                    search_term=scraper_input.search_term or "N/A",
                    jobs_found=0,
                    site=site_key,
                    scrape_duration_seconds=duration,
                    success=False,
                    error_message=str(e),
                )
                create_logger(site.value).error(
                    f"scraping {site_key} failed after {duration:.2f}s: {e}"
                )
                # Continue with other sites even if one fails

        return site.value, all_scraped_data
    else:
        # Single-site scrapers (LinkedIn, Indeed, Glassdoor, etc.)
        _jfc = scraper_input.job_finder_config or {}
        _db_path = _jfc.get("storage_dirs", {}).get("job_finder_db_path")
        scraper = scraper_class(proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, db_path=_db_path)

        # Time the scrape and log activity (JIRA-095)
        start_time = time.time()
        try:
            scraped_data: JobResponse = scraper.scrape(scraper_input)
            duration = time.time() - start_time

            # Log successful scraping (site=None for single-site scrapers)
            scraper.log_scraping_activity(
                run_id=run_id,
                search_term=scraper_input.search_term or "N/A",
                jobs_found=len(scraped_data.jobs),
                scrape_duration_seconds=duration,
                success=True,
            )

            cap_name = site.value.capitalize()
            site_name = "ZipRecruiter" if cap_name == "Zip_recruiter" else cap_name
            site_name = "LinkedIn" if cap_name == "Linkedin" else cap_name
            create_logger(site.value).info(
                f"finished scraping: found {len(scraped_data.jobs)} jobs in {duration:.2f}s"
            )
            return site.value, scraped_data
        except Exception as e:
            duration = time.time() - start_time

            # Log failed scraping
            scraper.log_scraping_activity(
                run_id=run_id,
                search_term=scraper_input.search_term or "N/A",
                jobs_found=0,
                scrape_duration_seconds=duration,
                success=False,
                error_message=str(e),
            )

            create_logger(site.value).error(
                f"scraping failed after {duration:.2f}s: {e}"
            )
            raise


# Update the SCRAPER_MAPPING dictionary in the scrape_jobs function


def scrape_jobs(
    site_name: str | list[str] | Site | list[Site] | None = None,
    search_term: str | None = None,
    google_search_term: str | None = None,
    location: str | None = None,
    distance: int | None = 50,
    is_remote: bool = False,
    job_type: str | None = None,
    easy_apply: bool | None = None,
    results_wanted: int = 15,
    country_indeed: str = "usa",
    proxies: list[str] | str | None = None,
    ca_cert: str | None = None,
    description_format: str = "markdown",
    linkedin_fetch_description: bool | None = False,
    linkedin_company_ids: list[int] | None = None,
    offset: int | None = 0,
    hours_old: int | None = None,
    enforce_annual_salary: bool = False,
    verbose: int = 0,
    user_agent: str | None = None,
    run_id: str | None = None,
    job_finder_config: dict | None = None,
    **kwargs,
) -> pd.DataFrame:
    """
    Scrapes job data from job boards concurrently (JIRA-095: Added run_id for logging)

    Args:
        run_id: Unique identifier for the job_finder run (groups all scrapes from one session).
                If None, a new UUID will be generated.

    Returns:
        Pandas DataFrame containing job data
    """
    set_logger_level(verbose)

    # Generate run_id if not provided (for backward compatibility)
    if run_id is None:
        run_id = str(uuid.uuid4())

    job_type_enum = get_enum_from_value(job_type) if job_type else None

    def get_site_type():
        site_types = list(Site)
        if isinstance(site_name, str):
            site_types = [map_str_to_site(site_name)]
        elif isinstance(site_name, Site):
            site_types = [site_name]
        elif isinstance(site_name, list):
            site_types = [
                map_str_to_site(site) if isinstance(site, str) else site
                for site in site_name
            ]
        return site_types

    country_enum = Country.from_string(country_indeed)

    scraper_input = ScraperInput(
        site_type=get_site_type(),
        country=country_enum,
        search_term=search_term,
        google_search_term=google_search_term,
        location=location,
        distance=distance,
        is_remote=is_remote,
        job_type=job_type_enum,
        easy_apply=easy_apply,
        description_format=description_format,
        linkedin_fetch_description=linkedin_fetch_description,
        results_wanted=results_wanted,
        linkedin_company_ids=linkedin_company_ids,
        offset=offset,
        hours_old=hours_old,
        job_finder_config=job_finder_config,
    )

    site_to_jobs_dict = {}

    if len(scraper_input.site_type) == 1:
        site = scraper_input.site_type[0]
        site_value, scraped_data = scrape_site(
            site, scraper_input, run_id, proxies, ca_cert, user_agent
        )
        site_to_jobs_dict[site_value] = scraped_data
    else:

        def worker(site):
            return scrape_site(
                site, scraper_input, run_id, proxies, ca_cert, user_agent
            )

        with ThreadPoolExecutor() as executor:
            future_to_site = {
                executor.submit(worker, site): site for site in scraper_input.site_type
            }

            for future in as_completed(future_to_site):
                site_value, scraped_data = future.result()
                site_to_jobs_dict[site_value] = scraped_data

    jobs_dfs: list[pd.DataFrame] = []

    for site, job_response in site_to_jobs_dict.items():
        for job in job_response.jobs:
            job_data = job.dict()
            job_url = job_data["job_url"]
            job_data["site"] = site
            job_data["company"] = job_data["company_name"]
            job_data["job_type"] = (
                ", ".join(job_type.value[0] for job_type in job_data["job_type"])
                if job_data["job_type"]
                else None
            )
            job_data["emails"] = (
                ", ".join(job_data["emails"]) if job_data["emails"] else None
            )
            if job_data["location"]:
                job_data["location"] = Location(
                    **job_data["location"]
                ).display_location()

            # Handle compensation
            compensation_obj = job_data.get("compensation")
            if compensation_obj and isinstance(compensation_obj, dict):
                job_data["interval"] = (
                    compensation_obj.get("interval").value
                    if compensation_obj.get("interval")
                    else None
                )
                job_data["min_amount"] = compensation_obj.get("min_amount")
                job_data["max_amount"] = compensation_obj.get("max_amount")
                job_data["currency"] = compensation_obj.get("currency", "USD")
                job_data["salary_source"] = SalarySource.DIRECT_DATA.value
                if enforce_annual_salary and (
                    job_data["interval"]
                    and job_data["interval"] != "yearly"
                    and job_data["min_amount"]
                    and job_data["max_amount"]
                ):
                    convert_to_annual(job_data)
            else:
                if country_enum == Country.USA:
                    (
                        job_data["interval"],
                        job_data["min_amount"],
                        job_data["max_amount"],
                        job_data["currency"],
                    ) = extract_salary(
                        job_data["description"],
                        enforce_annual_salary=enforce_annual_salary,
                    )
                    job_data["salary_source"] = SalarySource.DESCRIPTION.value

            job_data["salary_source"] = (
                job_data["salary_source"]
                if "min_amount" in job_data and job_data["min_amount"]
                else None
            )

            # naukri-specific fields
            job_data["skills"] = (
                ", ".join(job_data["skills"]) if job_data["skills"] else None
            )
            job_data["experience_range"] = job_data.get("experience_range")
            job_data["company_rating"] = job_data.get("company_rating")
            job_data["company_reviews_count"] = job_data.get("company_reviews_count")
            job_data["vacancy_count"] = job_data.get("vacancy_count")
            job_data["work_from_home_type"] = job_data.get("work_from_home_type")

            job_df = pd.DataFrame([job_data])
            jobs_dfs.append(job_df)

    if jobs_dfs:
        # Step 1: Filter out all-NA columns from each DataFrame before concatenation
        filtered_dfs = [df.dropna(axis=1, how="all") for df in jobs_dfs]

        # Step 2: Concatenate the filtered DataFrames
        jobs_df = pd.concat(filtered_dfs, ignore_index=True)

        # Step 3: Ensure all desired columns are present, adding missing ones as empty
        for column in desired_order:
            if column not in jobs_df.columns:
                jobs_df[column] = None  # Add missing columns as empty

        # Reorder the DataFrame according to the desired order
        jobs_df = jobs_df[desired_order]

        # Step 4: Sort the DataFrame as required
        return jobs_df.sort_values(
            by=["site", "date_posted"], ascending=[True, False]
        ).reset_index(drop=True)
    else:
        return pd.DataFrame()


# Add BDJobs to __all__
__all__ = [
    "BDJobs",
]
