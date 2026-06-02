from __future__ import annotations
import logging
from bs4 import BeautifulSoup
from urllib.parse import quote, urljoin
from html_to_markdown import convert_to_markdown
import requests
import re
import json
from datetime import datetime, date

from jobspy.model import (
    JobType,
    Scraper,
    ScraperInput,
    Site,
    JobPost,
    JobResponse,
    Location,
    Country,
)
from jobspy.model import Compensation, CompensationInterval
from jobspy.util import get_enum_from_job_type

logger = logging.getLogger("Marriott")


class Marriott(Scraper):
    def __init__(
        self,
        proxies: list[str] | None = None,
        ca_cert: str | None = None,
        user_agent: str | None = None,
        **kwargs,
    ):
        super().__init__(
            Site.MARRIOTT, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs
        )
        self.base_url = "https://careers.marriott.com"

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        job_list: list[JobPost] = []
        search_term = scraper_input.search_term or ""

        url = f"{self.base_url}/jobs"
        params = {}
        if search_term:
            params["keyword"] = quote(search_term)

        logger.info(f"*** Scraping Marriott with criteria: {scraper_input.search_term} ***")
        try:
            # Check if we should use the local file for testing/dev as per instructions
            # In a real scenario, we'd use requests.
            # If the user specifically said "use the file marriott.html",
            # I'll implement a fallback or check if the file exists and use it?
            # Or just use requests. I'll use requests.

            logger.info(f"Fetching jobs from {url} with params {params}")
            response = requests.get(
                url, params=params, timeout=scraper_input.request_timeout
            )
            response.raise_for_status()
            html_content = response.text

            soup = BeautifulSoup(html_content, "html.parser")

            # Select job items
            # Based on inspection: <li class="results-list__item" ...>
            items = soup.select("li.results-list__item")

            for item in items:
                job = self._process_job_item(item)
                if job:
                    job_list.append(job)
            logger.info(f"Num jobs found: {len(job_list)}")

        except Exception as e:
            logger.error(f"Error scraping Marriott: {e}")

        logger.info(f"*** Scraping Marriott Completed. Found {len(job_list)} jobs for criteria: {scraper_input.search_term} ***")
        return JobResponse(jobs=job_list)

    def _process_job_item(self, item) -> JobPost | None:
        try:
            # Title & URL
            # <h3 class="results-list__item-title"><a ...>Title</a> ...
            title_tag = item.select_one(
                "h3.results-list__item-title a.results-list__item-title--link"
            )

            if not title_tag:
                return None

            title = title_tag.get_text(strip=True)
            href = title_tag.get("href")
            job_url = urljoin(self.base_url, href)

            # Job ID
            # <span class="reference">25203057</span>
            ref_tag = item.select_one(".reference")
            job_id = ref_tag.get_text(strip=True) if ref_tag else None

            if not job_id:
                # Fallback to extracting from URL if needed
                # URL: /.../job/HASH
                parts = href.split("/job/")
                if len(parts) > 1:
                    job_id = parts[1]

            # Location
            # <div class="results-list__item-street--label__wrapper"><span class="results-list__item-street--label">...</span></div>
            location = None
            loc_tag = item.select_one(".results-list__item-street--label")
            if loc_tag:
                loc_str = loc_tag.get_text(strip=True)
                location = self._parse_location(loc_str)

            # Company
            # <div class="results-list__item-ownership"><span class="results-list__item-ownership--label">Marriott</span></div>
            company_name = "Marriott"
            comp_tag = item.select_one(".results-list__item-ownership--label")
            if comp_tag:
                company_name = comp_tag.get_text(strip=True)

            # Fetch job detail page to get description and other metadata
            details = self._get_job_details(job_url)
            logger.info(f"{job_id}: Fetched details for job: {title} at {company_name}, Description length: {len(details.get('description', ''))} chars")
            return JobPost(
                id=job_id,
                title=title,
                job_url=job_url,
                company_name=company_name,
                location=location,
                date_posted=details.get("date_posted"),
                description=details.get("description"),
                compensation=details.get("compensation"),
                emails=details.get("emails"),
                job_type=details.get("job_type", []),
                is_remote=details.get("is_remote"),
            )
        except Exception as e:
            logger.error(f"Error processing item: {e}")
            return None

    def _parse_location(self, loc_str: str) -> Location:
        # Example: "7750 Wisconsin Ave, Bethesda MD, United States"
        # Example: "Remote" -> handled?

        if "remote" in loc_str.lower():
            return Location(country=Country.USA)  # Assumption

        parts = [p.strip() for p in loc_str.split(",")]
        country = Country.USA  # Default
        city = None
        state = None

        if not parts:
            return Location(country=country)

        # Check last part for country
        possible_country = parts[-1]
        try:
            # Try to map country
            country = Country.from_string(possible_country)
            parts.pop()  # Remove country from parts
        except ValueError:
            pass  # Keep as is, maybe it's state

        if parts:
            # Assume last remaining part is "City State" or just "City"
            city_state = parts[-1]
            # Simple heuristic: Split by space. Last word is state code if len=2, else city.
            # "Bethesda MD"
            cs_parts = city_state.split()
            if len(cs_parts) > 1 and len(cs_parts[-1]) == 2:
                state = cs_parts[-1]
                city = " ".join(cs_parts[:-1])
            else:
                city = city_state

        return Location(city=city, state=state, country=country)

    def _get_job_details(self, job_url: str) -> dict:
        """Fetch a job detail page and extract description, date_posted, job_type, emails."""
        details: dict = {
            "description": None,
            "date_posted": None,
            "job_type": [],
            "emails": None,
            "is_remote": None,
        }
        try:
            logger.info(f"Fetching job detail page: {job_url}")
            resp = requests.get(job_url, timeout=10)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            # First try to parse JSON-LD JobPosting which Marriott includes
            jsonld = None
            ld_script = soup.select_one('script[type="application/ld+json"]')
            if ld_script and ld_script.string:
                try:
                    jsonld = json.loads(ld_script.string)
                except Exception:
                    # sometimes the page contains multiple JSON blobs or extra text
                    try:
                        # attempt to extract first JSON object
                        txt = ld_script.string.strip()
                        start = txt.find("{")
                        end = txt.rfind("}")
                        if start != -1 and end != -1:
                            jsonld = json.loads(txt[start : end + 1])
                    except Exception:
                        jsonld = None

            desc_text = None
            if isinstance(jsonld, dict):
                # description may be HTML string inside JSON-LD
                desc_html = jsonld.get("description")
                if desc_html:
                    # desc_text = BeautifulSoup(desc_html, 'html.parser').get_text(separator='\n', strip=True)
                    desc_text = convert_to_markdown(desc_html)
                    details["description"] = desc_text

                # datePosted -> normalize to date object (YYYY-MM-DD)
                dp = jsonld.get("datePosted")
                if dp:
                    parsed = None
                    try:
                        parsed = datetime.fromisoformat(str(dp))
                    except Exception:
                        parsed = None
                    if parsed:
                        details["date_posted"] = parsed.date()

                # employmentType can be list or string
                et = jsonld.get("employmentType") or jsonld.get("employment_type")
                jt = []
                if isinstance(et, list):
                    for v in et:
                        if isinstance(v, str) and "FULL" in v.upper():
                            jt.append("full-time")
                        elif isinstance(v, str) and "PART" in v.upper():
                            jt.append("part-time")
                        elif isinstance(v, str) and "CONTRACT" in v.upper():
                            jt.append("contract")
                elif isinstance(et, str):
                    if "FULL" in et.upper():
                        jt.append("full-time")
                    elif "PART" in et.upper():
                        jt.append("part-time")
                    elif "CONTRACT" in et.upper():
                        jt.append("contract")
                if jt:
                    details["job_type"] = self._get_job_type(jt)

                # jobLocationType TELECOMMUTE indicates remote/telecommute
                jlt = jsonld.get("jobLocationType")
                if isinstance(jlt, str) and "telecommute" in jlt.lower():
                    details["is_remote"] = True

            # If JSON-LD didn't provide description, fall back to page selectors
            if not details.get("description"):
                desc_selectors = [
                    "section.job-description div.description",
                    "div.job-description-grid div.description",
                    "div.description",
                    "section.job-description",
                    "div.job-content",
                ]
                for sel in desc_selectors:
                    node = soup.select_one(sel)
                    if node and node.get_text(strip=True):
                        desc_text = node.get_text(separator="\n", strip=True)
                        details["description"] = desc_text
                        break

                if desc_text:
                    # extract emails if present
                    emails = re.findall(
                        r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", desc_text
                    )
                    if emails:
                        details["emails"] = list(dict.fromkeys(emails))

                    # heuristics for job type and remote
                    lower = desc_text.lower()
                    jt = []
                    if "full time" in lower or "full-time" in lower:
                        jt.append("full-time")
                    if "part time" in lower or "part-time" in lower:
                        jt.append("part-time")
                    if "contract" in lower:
                        jt.append("contract")
                    if "remote" in lower or "telecommute" in lower:
                        details["is_remote"] = True
                    if jt:
                        details["job_type"] = jt

            # date posted fallback: <time> or meta[itemprop=datePosted]
            if not details.get("date_posted"):

                def _parse_to_date(s: str) -> date | None:
                    if not s:
                        return None
                    s = s.strip()
                    # try ISO first
                    try:
                        return datetime.fromisoformat(s).date()
                    except Exception:
                        pass
                    # try common formats
                    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            return datetime.strptime(s, fmt).date()
                        except Exception:
                            continue
                    return None

                time_tag = soup.select_one("time")
                if time_tag and time_tag.get("datetime"):
                    parsed = _parse_to_date(str(time_tag.get("datetime")))
                    if parsed:
                        details["date_posted"] = parsed
                else:
                    meta_date = soup.select_one(
                        'meta[itemprop="datePosted"]'
                    ) or soup.select_one('meta[name="date"]')
                    if meta_date and meta_date.get("content"):
                        parsed = _parse_to_date(str(meta_date.get("content")))
                        if parsed:
                            details["date_posted"] = parsed

                # Compensation: try JSON-LD `baseSalary` or page `data-pay-range`
            comp_obj = None
            try:
                # JSON-LD baseSalary
                currency = None
                minv = None
                maxv = None
                unit = None
                if isinstance(jsonld, dict) and jsonld.get("baseSalary"):
                    bs = jsonld.get("baseSalary")
                    # baseSalary may be dict with value object
                    if isinstance(bs, dict):
                        currency = bs.get("currency") or bs.get("currencyCode")
                        val = bs.get("value") or bs.get("baseSalary") or None
                        if isinstance(val, dict):
                            minv = val.get("minValue") or val.get("value")
                            maxv = val.get("maxValue")
                            unit = val.get("unitText") or val.get("unit")
                        elif isinstance(val, (int, float, str)):
                            try:
                                num = float(str(val).replace(",", "").replace("$", ""))
                                minv = maxv = num
                            except Exception:
                                pass
                    elif isinstance(bs, str):
                        # try parse like "$76,000 - $103,000 annually"
                        m = re.search(
                            r"\$?([0-9,]+)\s*-\s*\$?([0-9,]+)\s*([A-Za-z]+)?", bs
                        )
                        if m:
                            try:
                                minv = float(m.group(1).replace(",", ""))
                                maxv = float(m.group(2).replace(",", ""))
                                unit = m.group(3)
                            except Exception:
                                pass

                # If JSON-LD contained a max value of zero, prefer extracting from raw HTML
                if maxv is not None:
                    try:
                        if float(maxv) == 0:
                            pay_div = soup.select_one(
                                "div.summary-list-item[data-pay-range]"
                            )
                            if pay_div:
                                pr = pay_div.get("data-pay-range")
                                if pr:
                                    m = re.search(
                                        r"\$?([0-9,]+)\s*-\s*\$?([0-9,]+)\s*([A-Za-z]+)?",
                                        str(pr),
                                    )
                                    if m:
                                        try:
                                            minv = float(m.group(1).replace(",", ""))
                                            maxv = float(m.group(2).replace(",", ""))
                                            unit = m.group(3)
                                        except Exception:
                                            pass
                    except Exception:
                        pass

                # fallback: if no JSON-LD present or no values found, try page `data-pay-range`
                if minv is None and maxv is None:
                    pay_div = soup.select_one("div.summary-list-item[data-pay-range]")
                    if pay_div:
                        pr = pay_div.get("data-pay-range")
                        if pr:
                            m = re.search(
                                r"\$?([0-9,]+)\s*-\s*\$?([0-9,]+)\s*([A-Za-z]+)?",
                                str(pr),
                            )
                            if m:
                                try:
                                    minv = float(m.group(1).replace(",", ""))
                                    maxv = float(m.group(2).replace(",", ""))
                                    unit = m.group(3)
                                except Exception:
                                    pass

                # normalize unit to CompensationInterval
                interval_enum = None
                if unit:
                    u = str(unit).upper()
                    if "YEAR" in u:
                        interval_enum = CompensationInterval.YEARLY
                    elif "MONTH" in u:
                        interval_enum = CompensationInterval.MONTHLY
                    elif "WEEK" in u:
                        interval_enum = CompensationInterval.WEEKLY
                    elif "DAY" in u:
                        interval_enum = CompensationInterval.DAILY
                    elif "HOUR" in u:
                        interval_enum = CompensationInterval.HOURLY

                if minv is not None or maxv is not None:
                    # ensure floats
                    try:
                        min_amt = float(minv) if minv is not None else None
                    except Exception:
                        min_amt = None
                    try:
                        max_amt = float(maxv) if maxv is not None else None
                    except Exception:
                        max_amt = None
                    comp_obj = Compensation(
                        interval=interval_enum,
                        min_amount=min_amt,
                        max_amount=max_amt,
                        currency=(currency if currency else "USD"),
                    )
            except Exception:
                comp_obj = None

            if comp_obj:
                details["compensation"] = comp_obj

        except Exception as e:
            logger.debug(f"Failed to fetch details for {job_url}: {e}")

        return details

    def _get_job_type(self, job_type: list[str]) -> list[JobType]:
        if not job_type:
            return [JobType.OTHER]
        job_type_result = []
        for job_type_str in job_type:
            job_type_str = job_type_str.lower()
            if job_type_str == "full_time" or job_type_str == "full-time":
                job_type_str = "fulltime"

            job_type_enum = get_enum_from_job_type(job_type_str)
            if job_type_enum is None:
                job_type_result.append(JobType.OTHER)
            else:
                job_type_result.append(job_type_enum)
        if job_type_result:
            return list(set(job_type_result))
        return [JobType.OTHER]
