from __future__ import annotations

import re
import time
import json
import asyncio
from datetime import datetime, timedelta
from typing import Optional

import html_to_markdown
from playwright.sync_api import sync_playwright
import logging
from scrapescore.lib.config import BROWSER_HEADLESS
from jobspy.model import (
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Location,
    JobType,
    Compensation,
    CompensationInterval,
)
from jobspy.util import extract_emails_from_text, extract_job_type
from jobspy.google.util import log
logger = logging.getLogger("Google")
class Google(Scraper):
    def __init__(
        self,
        proxies: list[str] | str | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        **kwargs,
    ):
        """
        Initializes Google Scraper with the Google Careers search url
        """
        site = Site(Site.GOOGLE)
        super().__init__(site, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs)

        self.country = None
        self.scraper_input = None
        self.seen_urls = set()

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        Scrapes Google Careers for jobs with scraper_input criteria using Playwright.
        :param scraper_input: Information about job search criteria.
        :return: JobResponse containing a list of jobs.
        """
        self.scraper_input = scraper_input
        job_list: list[JobPost] = []
        
        # Limit results to a reasonable number if not specified
        results_wanted = scraper_input.results_wanted if scraper_input.results_wanted else 20
        seen_ids = set()
        logger.info(f"*** Scraping Google Careers with criteria: {scraper_input.search_term} ***")

        # Workaround: Clear any existing asyncio event loop to avoid conflict with sync_playwright
        # This error can occur when sync_playwright runs in threads where an asyncio loop exists
        try:
            asyncio.set_event_loop(None)
        except:
            pass

        with sync_playwright() as p:
            # Launch browser
            browser = p.chromium.launch(
                headless=BROWSER_HEADLESS,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                ]
            )
            
            # Context with User Agent to look like a real browser
            user_agent = self.user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
            context = browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1920, "height": 1080},
                locale="en-US",
                timezone_id="America/New_York"
            )
            page = context.new_page()

            # Construct the search query
            # Task 2: Update to use google careers URL
            base_url = "https://www.google.com/about/careers/applications/jobs/results"
            params = []
            if scraper_input.search_term:
                params.append(f"q={scraper_input.search_term}")
            if scraper_input.location:
                params.append(f"location={scraper_input.location}")
            
            # Join params with &
            query_string = "&".join(params)
            url = f"{base_url}?{query_string}"
            
            log.info(f"Navigating to Google Careers: {url}")
            # Use longer timeout and domcontentloaded to avoid timeout errors
            # domcontentloaded waits for HTML to be parsed, not all resources
            try:
                page.goto(url, timeout=60000, wait_until="domcontentloaded")
            except Exception as e:
                log.error(f"Failed to navigate to Google Careers: {e}")
                browser.close()
                return JobResponse(jobs=[])

            # Check for Consent Popup
            try:
                # Look for "Accept all" or "I agree" buttons
                consent_button = page.get_by_role("button", name=re.compile(r"Accept all|I agree|Agree", re.IGNORECASE))
                if consent_button.is_visible(timeout=3000):
                    log.info("Clicking consent button...")
                    consent_button.click()
                    time.sleep(2)
            except Exception:
                pass

            # Wait for results to load
            try:
                page.wait_for_selector("ul.spHGqe", timeout=10000)
            except Exception:
                log.error("Job list container 'ul.spHGqe' not found. Maybe no results?")
                browser.close()
                return JobResponse(jobs=[])

            # Task 36: Extract detailed jobs from script tag if available
            try:
                # The script tag with class "ds:1" contains the initial job list with full details
                script_content = None
                script_handle = page.query_selector("script.ds\\:1")
                if script_handle:
                    script_content = script_handle.text_content()
                
                if script_content:
                    logger.info("Found ds:1 script tag, extracting detailed job data...")
                    # Extract data array: data:[ ... ]});
                    # We look for "data:[" and the last matching "]"
                    start_marker = "data:["
                    start_idx = script_content.find(start_marker)
                    
                    if start_idx != -1:
                        json_start = start_idx + len("data:")
                        json_str = script_content[json_start:]
                        
                        # Remove trailing characters to find the end of the array
                        # Usually ends with "});" or similar
                        last_bracket = json_str.rfind("]")
                        if last_bracket != -1:
                            json_str = json_str[:last_bracket+1]
                            
                            try:
                                jobs_data = json.loads(json_str)
                                
                                target_list = jobs_data
                                if len(jobs_data) > 0 and isinstance(jobs_data[0], list):
                                    first_element = jobs_data[0]
                                    if len(first_element) > 0 and isinstance(first_element[0], list):
                                         # Case: [ [job1, job2], metadata... ]
                                         target_list = jobs_data[0]
                                    elif len(first_element) > 0 and isinstance(first_element[0], str):
                                         # Case: [ job1, job2 ]
                                         target_list = jobs_data
                                
                                logger.info(f"Extracted {len(target_list)} jobs from script tag")
                                
                                for i, job_item in enumerate(target_list):
                                    if len(job_list) >= results_wanted:
                                        break
                                        
                                    try:
                                        # Parse job_item
                                        if not isinstance(job_item, list):
                                            # log.warning(f"Job item {i} is not a list: {type(job_item)}")
                                            continue
                                            
                                        # Index mapping based on analysis
                                        j_id = job_item[0]
                                        if isinstance(j_id, list):
                                            # unexpected nesting?
                                            j_id = j_id[0]
                                            
                                        j_title = job_item[1]
                                        j_url = f"https://www.google.com/about/careers/applications/jobs/results/{j_id}"
                                        
                                        # Description parts
                                        desc_parts = []
                                        
                                        # About/Summary (Index 10)
                                        if len(job_item) > 10 and job_item[10] and isinstance(job_item[10], list) and len(job_item[10]) > 1:
                                            if job_item[10][1]:
                                                desc_parts.append(f"<h3>About the job</h3>{job_item[10][1]}")
                                        
                                        # Responsibilities (Index 3)
                                        if len(job_item) > 3 and job_item[3] and isinstance(job_item[3], list) and len(job_item[3]) > 1:
                                            if job_item[3][1]:
                                                desc_parts.append(f"<h3>Responsibilities</h3>{job_item[3][1]}")
                                                
                                        # Qualifications (Index 4)
                                        if len(job_item) > 4 and job_item[4] and isinstance(job_item[4], list) and len(job_item[4]) > 1:
                                            if job_item[4][1]:
                                                desc_parts.append(f"<h3>Qualifications</h3>{job_item[4][1]}")
                                        
                                        full_description = "<br>".join(desc_parts)
                                        full_description = html_to_markdown.convert(full_description)
                                        # Company (Index 7)
                                        j_company = job_item[7] if len(job_item) > 7 else "Google"
                                        
                                        # Location (Index 9)
                                        j_loc_obj = None
                                        if len(job_item) > 9 and job_item[9] and isinstance(job_item[9], list) and len(job_item[9]) > 0:
                                            # item[9][0] is the first location group?
                                            # item[9][0][0] is the string "City, State, Country"
                                            if job_item[9][0] and len(job_item[9][0]) > 0:
                                                loc_str = job_item[9][0][0]
                                                parts = [p.strip() for p in loc_str.split(",")]
                                                city = parts[0] if len(parts) > 0 else None
                                                state = parts[1] if len(parts) > 1 else None
                                                country = parts[2] if len(parts) > 2 else None
                                                j_loc_obj = Location(city=city, state=state, country=country)
                                        
                                        # Date Posted (Index 13)
                                        j_date_posted = datetime.now().date()
                                        if len(job_item) > 13 and job_item[13] and isinstance(job_item[13], list) and len(job_item[13]) > 0:
                                            ts = job_item[13][0]
                                            if ts:
                                                try:
                                                    j_date_posted = datetime.fromtimestamp(ts).date()
                                                except:
                                                    pass

                                        if j_id in seen_ids:
                                             continue

                                        seen_ids.add(j_id)
                                        self.seen_urls.add(j_url)
                                        
                                        is_remote = False
                                        if j_loc_obj:
                                            loc_str_check = f"{j_loc_obj.city or ''} {j_loc_obj.state or ''} {j_loc_obj.country or ''}".lower()
                                            if "remote" in loc_str_check:
                                                is_remote = True
                                        logger.info(f"Fetched details for job: {j_id}, Title: {j_title},  Description length: {len(full_description)} chars")
                                        job_post = JobPost(
                                            id=str(j_id),
                                            title=j_title,
                                            company_name=j_company,
                                            location=j_loc_obj,
                                            job_url=j_url,
                                            description=full_description,
                                            date_posted=j_date_posted,
                                            is_remote=is_remote
                                        )
                                        job_list.append(job_post)
                                        
                                    except Exception as e:
                                        logger.error(f"Error parsing JSON job item {i}: {e}")
                                        
                            except json.JSONDecodeError as e:
                                logger.error(f"Failed to parse ds:1 JSON: {e}")
            except Exception as e:
                logger.error(f"Error extracting data from script tag: {e}")

            browser.close()

        logger.info(f"*** Scraping Google Complete. Found {len(job_list)} jobs ***")
        return JobResponse(jobs=job_list)
