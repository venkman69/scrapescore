from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
from pathlib import Path
from typing import Optional

import undetected_chromedriver as uc
from bs4 import BeautifulSoup
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from html_to_markdown import convert_to_markdown
from jobspy.model import (
    CompensationInterval,
    Compensation,
    Country,
    JobPost,
    JobResponse,
    JobType,
    Location,
    Scraper,
    ScraperInput,
    Site,
)
from scrapescore.lib.config import BROWSER_HEADLESS

logger = logging.getLogger("ZipRecruiter")

CHROME_BINARY = "/usr/bin/google-chrome"
BASE_URL = "https://www.ziprecruiter.com"
DETAIL_PANE = '[data-testid="job-details-scroll-container"]'

# Path to the saved Google Chrome profile (logged in to ZipRecruiter)
_PROFILE_DIR = Path(__file__).parent.parent.parent.parent / "work" / "google_profile"


def _profile_dir() -> Path:
    """Return google_profile path, resolving from the project root."""
    # Relative to this file: src/jobspy/ziprecruiter/__init__.py -> project root is 5 levels up
    candidate = _PROFILE_DIR
    if candidate.exists():
        return candidate
    raise FileNotFoundError(
        f"Google Chrome profile not found at {candidate}. "
        "Log in to ZipRecruiter in Chrome and save the profile at work/google_profile"
    )


def _copy_profile(src_dir: Path) -> str:
    """Copy the Chrome profile to a temp dir to avoid lock conflicts."""
    skip = shutil.ignore_patterns(
        "Cache", "Code Cache", "GPUCache", "ShaderCache",
        "GrShaderCache", "GraphiteDawnCache", "optimization_guide_model_store",
        "segmentation_platform", "HistorySearch",
    )
    tmp = tempfile.mkdtemp(prefix="zr_profile_")
    local_state = src_dir / "Local State"
    if local_state.exists():
        shutil.copy2(str(local_state), tmp)
    default_src = src_dir / "Default"
    if default_src.exists():
        shutil.copytree(
            str(default_src),
            str(Path(tmp) / "Default"),
            symlinks=True,
            ignore=skip,
            ignore_dangling_symlinks=True,
        )
    return tmp


def _make_driver(headless: bool, tmp_profile: str) -> uc.Chrome:
    options = uc.ChromeOptions()
    options.add_argument(f"--user-data-dir={tmp_profile}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-extensions")
    options.add_argument("--disable-background-networking")
    if headless:
        options.add_argument("--headless=new")

    driver = uc.Chrome(
        options=options,
        browser_executable_path=CHROME_BINARY,
        version_main=148,
        headless=headless,
    )
    driver.set_page_load_timeout(60)
    return driver


def _detect_block(driver: uc.Chrome) -> Optional[str]:
    title = driver.title.lower()
    snippet = driver.page_source[:3000].lower()
    if "cloudflare" in snippet or "just a moment" in title:
        return "Cloudflare challenge"
    if "access denied" in title or "403" in title:
        return "Access denied (403)"
    if "captcha" in snippet:
        return "CAPTCHA detected"
    if "are you a robot" in snippet or "are you human" in snippet:
        return "Bot detection page"
    return None


def _wait_for_jobs(driver: uc.Chrome, timeout: int = 20) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR,
                 'article[id^="job-card-"], article[data-job-id], '
                 '.job_result_two_pane_v2, .job_result')
            )
        )
        return True
    except Exception:
        return False


def _parse_jobs(page_source: str, search_query: str = "") -> list[dict]:
    from urllib.parse import urlencode

    soup = BeautifulSoup(page_source, "html.parser")
    cards = (
        soup.select('article[id^="job-card-"]')
        or soup.select("article[data-job-id]")
        or soup.select(".job_result_two_pane_v2")
        or soup.select(".job_result")
    )

    seen: set[str] = set()
    jobs: list[dict] = []

    for card in cards:
        card_id = card.get("id", card.get("data-job-id", ""))
        if card_id in seen:
            continue
        seen.add(card_id)

        token = card_id.replace("job-card-", "") if card_id.startswith("job-card-") else ""

        h2 = card.find("h2")
        if h2:
            title = h2.get_text(strip=True)
        else:
            a_title = card.select_one("a[data-testid='job-title'], .job_link, a.title")
            title = a_title.get_text(strip=True) if a_title else ""

        if not title:
            continue

        job_url = f"{BASE_URL}/jobs-search?{urlencode({'search': search_query, 'lk': token})}" if token else ""

        company_el = (
            card.select_one("a[data-testid='job-card-company']")
            or card.select_one("[data-testid='job-card-company']")
            or card.select_one("a[href*='/co/']")
        )
        location_el = (
            card.select_one("a[data-testid='job-card-location']")
            or card.select_one("[data-testid='job-card-location']")
        )

        salary_text = ""
        for p in card.select("p"):
            txt = p.get_text(strip=True)
            if not salary_text and txt.startswith("$"):
                salary_text = txt
                break

        jobs.append({
            "job_id": token,
            "title": title,
            "job_url": job_url,
            "company": company_el.get_text(strip=True) if company_el else "",
            "location": location_el.get_text(strip=True) if location_el else "",
            "salary_text": salary_text,
            "description": "",
        })

    return jobs


def _extract_description_from_pane(pane_html: str) -> str:
    soup = BeautifulSoup(pane_html, "html.parser")
    for h2 in soup.find_all("h2"):
        if "job description" in h2.get_text(strip=True).lower():
            sib = h2.find_next_sibling("div")
            if sib:
                return convert_to_markdown(str(sib))
    desc_div = soup.select_one('div[class*="whitespace-pre-line"]')
    if desc_div:
        return convert_to_markdown(str(desc_div))
    return ""


def _fetch_descriptions(driver: uc.Chrome, jobs: list[dict]) -> None:
    for i, job in enumerate(jobs):
        token = job.get("job_id", "")
        if not token:
            continue
        card_el_id = f"job-card-{token}"
        try:
            card_el = driver.find_element(By.ID, card_el_id)
        except Exception:
            logger.debug(f"Card element not found: {card_el_id}")
            continue

        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", card_el
            )
            time.sleep(0.4)
            card_el.click()
            WebDriverWait(driver, 12).until(lambda d: token in d.current_url)
            time.sleep(0.6)
            pane_el = driver.find_element(By.CSS_SELECTOR, DETAIL_PANE)
            job["description"] = _extract_description_from_pane(pane_el.get_attribute("innerHTML"))
            logger.debug(f"[{i+1}] {job['title'][:50]}: {len(job['description'])} chars")
        except TimeoutException:
            logger.warning(f"Timeout waiting for right pane: {job['title'][:50]}")
        except Exception as e:
            logger.warning(f"Error fetching description for {job['title'][:50]}: {e}")


def _scroll_and_collect(driver: uc.Chrome, query: str = "") -> list[dict]:
    last_height = driver.execute_script("return document.body.scrollHeight")
    for _ in range(5):
        driver.execute_script("window.scrollBy(0, 800);")
        time.sleep(0.6)
        new_height = driver.execute_script("return document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height
    return _parse_jobs(driver.page_source, search_query=query)


def _parse_salary_text(salary_text: str) -> Compensation:
    """Best-effort parse of ZipRecruiter salary strings like '$120K - $160K/yr'."""
    import re
    if not salary_text:
        return Compensation()
    numbers = re.findall(r"\$([\d,.]+)[Kk]?", salary_text)
    if len(numbers) >= 2:
        try:
            def to_float(s: str) -> float:
                s = s.replace(",", "")
                val = float(s)
                if "K" in salary_text[salary_text.index(s)-1:salary_text.index(s)+len(s)+2].upper() or val < 1000:
                    val *= 1000
                return val
            lo = float(numbers[0].replace(",", ""))
            hi = float(numbers[1].replace(",", ""))
            # Scale up K values
            if lo < 1000:
                lo *= 1000
            if hi < 1000:
                hi *= 1000
            interval = CompensationInterval.YEARLY if "/yr" in salary_text.lower() or "/year" in salary_text.lower() else CompensationInterval.HOURLY
            return Compensation(min_amount=lo, max_amount=hi, interval=interval)
        except Exception:
            pass
    return Compensation()


class ZipRecruiter(Scraper):
    def __init__(self, proxies=None, ca_cert=None, user_agent=None, **kwargs):
        super().__init__(Site.ZIP_RECRUITER, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs)

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        job_list: list[JobPost] = []
        search_term = scraper_input.search_term or ""
        location = scraper_input.location or ""
        results_wanted = scraper_input.results_wanted or 15

        profile_src = _profile_dir()
        tmp_profile = _copy_profile(profile_src)
        # ZipRecruiter's Cloudflare protection detects and blocks headless browsers;
        # always run non-headless and rely on the saved session cookies to pass challenges.
        headless = False

        logger.info(f"*** Scraping ZipRecruiter: '{search_term}' | headless={headless} ***")
        driver = _make_driver(headless=headless, tmp_profile=tmp_profile)

        try:
            # Warm up session using saved cookies; wait for any Cloudflare challenge
            logger.info("Warming up ZipRecruiter session...")
            try:
                driver.get(f"{BASE_URL}/jobseeker/home")
                time.sleep(4)
                if _detect_block(driver):
                    logger.info("Cloudflare on home page — waiting for auto-resolve...")
                    for _ in range(6):
                        time.sleep(5)
                        if not _detect_block(driver):
                            break
            except TimeoutException:
                pass
            logger.info(f"Session page title: {driver.title[:80]}")

            # Build search URL
            from urllib.parse import urlencode
            params: dict = {"search": search_term}
            if location:
                params["location"] = location
            job_finder_config = scraper_input.job_finder_config or {}
            zr_params = job_finder_config.get("ziprecruiter_params", {})
            if zr_params.get("radius"):
                params["radius"] = zr_params["radius"]
            if zr_params.get("days"):
                params["days"] = zr_params["days"]

            url = f"{BASE_URL}/jobs-search?{urlencode(params)}"
            logger.info(f"Navigating to: {url}")

            try:
                driver.get(url)
            except TimeoutException:
                logger.warning("Page load timed out — proceeding with partial load")
            except WebDriverException as e:
                logger.error(f"WebDriver error: {e}")
                return JobResponse(jobs=[])

            time.sleep(3.5)
            logger.info(f"Current URL: {driver.current_url}")
            logger.info(f"Page title: {driver.title[:80]}")

            block = _detect_block(driver)
            if block:
                # Cloudflare challenges often auto-resolve within ~10s when a valid
                # session cookie is present. Wait and retry before giving up.
                logger.warning(f"Detected: {block} — waiting for auto-resolve...")
                for _ in range(6):
                    time.sleep(5)
                    block = _detect_block(driver)
                    if not block:
                        logger.info("Challenge cleared, continuing")
                        break
                else:
                    logger.error(f"Still blocked after wait: {block}")
                    return JobResponse(jobs=[])

            if not _wait_for_jobs(driver):
                logger.error("No job cards found on page")
                return JobResponse(jobs=[])

            jobs_raw = _scroll_and_collect(driver, query=search_term)
            logger.info(f"Parsed {len(jobs_raw)} job cards from HTML")

            if not jobs_raw:
                logger.error("Zero jobs parsed from page")
                return JobResponse(jobs=[])

            # Limit to results_wanted
            jobs_raw = jobs_raw[:results_wanted]

            # Fetch descriptions by clicking each card
            logger.info(f"Fetching descriptions for {len(jobs_raw)} jobs...")
            _fetch_descriptions(driver, jobs_raw)

        finally:
            driver.quit()
            shutil.rmtree(tmp_profile, ignore_errors=True)

        # Convert raw dicts to JobPost
        for raw in jobs_raw:
            if not raw.get("title") or not raw.get("job_url"):
                continue
            compensation = _parse_salary_text(raw.get("salary_text", ""))
            job_post = JobPost(
                id=raw["job_id"],
                title=raw["title"],
                company_name=raw.get("company", ""),
                location=Location(city=raw.get("location", ""), country=Country.USA),
                job_url=raw["job_url"],
                description=raw.get("description", ""),
                compensation=compensation if compensation.min_amount else None,
            )
            job_list.append(job_post)

        logger.info(json.dumps({
            "event": "scrape_complete",
            "scraper": "ziprecruiter",
            "search_term": search_term,
            "jobs_found": len(job_list),
        }))
        return JobResponse(jobs=job_list)
