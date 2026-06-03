import json
import os
import sys
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth
import time
import re
import traceback
from urllib.parse import urlparse, parse_qs
import cloudscraper
import threading
import fcntl
from typing import Generator
import contextlib
import requests
import logging

logger = logging.getLogger(__name__)

DEFAULT_CHROME_USER_DATA_DIR = "./work/google_profile"


def download_url(url: str, headless: bool = True, chrome_user_data_dir: str | None = None) -> str:
    """
    Download content from a URL using appropriate method based on the source.

    Routes to specialized downloaders for known job sites (workday, linkedin, indeed, dice),
    falls back to requests library, then to playwright if needed.
    """
    if "workday" in url:
        logger.info(f"Downloading job from workday: {url}")
        return download_job_from_workday(url, headless=headless, chrome_user_data_dir=chrome_user_data_dir)
    if "linkedin" in url:
        logger.info(f"Downloading job from linkedin: {url}")
        return download_job_from_linkedin(url, headless=headless, chrome_user_data_dir=chrome_user_data_dir)
    if "indeed" in url:
        logger.info(f"Downloading job from indeed: {url}")
        return download_job_from_indeed(url, headless=headless, chrome_user_data_dir=chrome_user_data_dir)
    if "dice" in url:
        logger.info(f"Downloading job from dice: {url}")
        return download_job_from_dice(url, headless=headless)
    if "greenhouse" in url:
        logger.info(f"Downloading job from greenhouse: {url}")
        return download_job_from_greenhouse(url, headless=headless)

    try:
        logger.info(f"Downloading job using requests: {url}")
        response = requests.get(url, timeout=(15, 30))
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.warning(f"Failed to download job content using requests: {url}")
        logger.info(f"Downloading job using playwright: {url}")
        return download_job_with_playwright(url, headless=headless, chrome_user_data_dir=chrome_user_data_dir)


# Global singleton browser instance (with lock for thread safety)
_browser_lock = threading.Lock()
_browser_instance = None
_playwright_instance = None
_browser_ref_count = 0
_lock_file_handle = None


def _get_lock_file_path(chrome_user_data_dir: str | None = None) -> str:
    user_data_dir = chrome_user_data_dir or DEFAULT_CHROME_USER_DATA_DIR
    lock_dir = os.path.dirname(user_data_dir)
    return os.path.join(lock_dir, ".chrome_browser.lock")


def _acquire_lock(chrome_user_data_dir: str | None = None):
    global _lock_file_handle
    lock_path = _get_lock_file_path(chrome_user_data_dir)
    logger.debug(f"Acquiring browser lock: {lock_path}")
    _lock_file_handle = open(lock_path, "w")
    fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_EX)
    logger.debug("Browser lock acquired")


def _release_lock():
    global _lock_file_handle
    if _lock_file_handle:
        try:
            fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_UN)
            _lock_file_handle.close()
            _lock_file_handle = None
            logger.debug("Browser lock released")
        except Exception as e:
            logger.warning(f"Error releasing browser lock: {e}")


def _cleanup_chrome_locks(chrome_user_data_dir: str | None = None):
    user_data_dir = chrome_user_data_dir or DEFAULT_CHROME_USER_DATA_DIR
    lock_files = [
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "SingletonCookie.old",
        "chrome_debug.log",
    ]
    for lock_file in lock_files:
        lock_path = os.path.join(user_data_dir, lock_file)
        try:
            if os.path.exists(lock_path):
                os.remove(lock_path)
                logger.debug(f"Removed stale Chrome lock: {lock_path}")
        except Exception as e:
            logger.debug(f"Could not remove {lock_path}: {e}")

    try:
        git_lock = os.path.join(user_data_dir, "lockfile")
        if os.path.exists(git_lock):
            os.remove(git_lock)
            logger.debug(f"Removed lockfile: {git_lock}")
    except Exception:
        pass


def _get_or_create_browser(headless: bool = True, chrome_user_data_dir: str | None = None):
    global _browser_instance, _playwright_instance, _browser_ref_count

    with _browser_lock:
        if _browser_instance is None:
            user_data_dir = chrome_user_data_dir or DEFAULT_CHROME_USER_DATA_DIR
            logger.info(f"Creating new stealth browser (headless={headless})...")

            _cleanup_chrome_locks(user_data_dir)
            _acquire_lock(user_data_dir)

            try:
                _playwright_instance = sync_playwright().start()
                _browser_instance = (
                    _playwright_instance.chromium.launch_persistent_context(
                        user_data_dir,
                        channel="chrome",
                        headless=headless,
                        no_viewport=True,
                        args=[
                            "--disable-blink-features=AutomationControlled",
                            "--no-sandbox",
                            "--disable-infobars",
                            "--window-size=1020,980",
                        ],
                    )
                )
                stealth = Stealth()
                stealth.apply_stealth_sync(_browser_instance)
                logger.info("Browser created successfully")
            except Exception as e:
                logger.error(f"Failed to create browser: {e}")
                _release_lock()
                _playwright_instance = None
                raise

        _browser_ref_count += 1
        logger.debug(f"Browser ref count: {_browser_ref_count}")
        return _browser_instance


def _release_browser():
    global _browser_instance, _playwright_instance, _browser_ref_count

    with _browser_lock:
        _browser_ref_count -= 1
        logger.debug(f"Browser ref count: {_browser_ref_count}")

        if _browser_ref_count <= 0 and _browser_instance is not None:
            logger.info("Closing browser (ref count reached zero)")
            try:
                _browser_instance.close()
            except Exception as e:
                logger.warning(f"Error closing browser: {e}")
            try:
                if _playwright_instance:
                    _playwright_instance.stop()
            except Exception as e:
                logger.warning(f"Error stopping playwright: {e}")

            _browser_instance = None
            _playwright_instance = None
            _browser_ref_count = 0
            _release_lock()


@contextlib.contextmanager
def get_playwright_stealth_browser(headless: bool = True, chrome_user_data_dir: str | None = None) -> Generator:
    """
    Context manager that provides a Playwright browser with stealth configuration.

    Uses a singleton pattern with persistent Chrome browser context for
    authentication state and playwright-stealth to avoid detection.
    """
    browser = _get_or_create_browser(headless, chrome_user_data_dir)
    try:
        yield browser
    finally:
        _release_browser()


_WORKDAY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def download_job_from_workday(url: str, headless: bool = True, chrome_user_data_dir: str | None = None) -> str:
    try:
        resp = requests.get(url, headers=_WORKDAY_HEADERS, timeout=20)
        if not resp.ok:
            logger.warning(f"Workday fetch returned {resp.status_code} for {url}")
            return ""
        resp.encoding = resp.apparent_encoding or "utf-8"
        page = resp.text
    except Exception as e:
        logger.error(f"Workday fetch failed for {url}: {e}")
        return ""
    soup = BeautifulSoup(page, "html.parser")
    ld_script = soup.find("script", type="application/ld+json")
    if ld_script:
        try:
            data = json.loads(ld_script.string)
            desc = data.get("description", "")
            if desc:
                return desc
        except Exception as e:
            logger.warning(f"Workday JSON-LD parse failed for {url}: {e}")
    # Fallback: Workday sometimes embeds JSON without the type attribute
    m = re.search(r'<script[^>]*>\s*(\{[^<]*"jobLocation"[^<]*)\s*</script>', page, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            desc = data.get("description", "")
            if desc:
                return desc
        except Exception:
            pass
    logger.warning(f"Workday: could not extract description for {url}")
    return ""


_LINKEDIN_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def download_job_from_linkedin(url: str, headless: bool = True, chrome_user_data_dir: str | None = None) -> str:
    try:
        resp = requests.get(url, headers=_LINKEDIN_HEADERS, timeout=20)
        if not resp.ok:
            logger.warning(f"LinkedIn fetch returned {resp.status_code} for {url}")
            return ""
        html_content = resp.text
    except Exception as e:
        logger.error(f"LinkedIn fetch failed for {url}: {e}")
        return ""
    soup = BeautifulSoup(html_content, "html.parser")
    desc = soup.find("div", class_="description__text--rich")
    if desc:
        return desc.decode_contents()
    logger.warning(f"LinkedIn page did not have description__text--rich section for {url}")
    return html_content


def _extract_indeed_job_key(url: str) -> str | None:
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    return (params.get("jk") or params.get("vjk") or [None])[0]


def _fetch_indeed_description(job_key: str) -> str | None:
    """Fetch job description from Indeed using the mobile app user-agent to get LD+JSON."""
    from jobspy.indeed.constant import api_headers
    import urllib3
    urllib3.disable_warnings()

    url = f"https://www.indeed.com/viewjob?jk={job_key}"
    headers = {
        "user-agent": api_headers["user-agent"],
        "accept": "text/html,application/xhtml+xml",
        "accept-language": "en-US,en;q=0.9",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15, verify=False, allow_redirects=True)
        if not resp.ok:
            logger.warning(f"Indeed viewjob returned {resp.status_code} for key: {job_key}")
            return None
        soup = BeautifulSoup(resp.text, "html.parser")
        ld_script = soup.find("script", type="application/ld+json")
        if ld_script:
            ld_data = json.loads(ld_script.string)
            return ld_data.get("description")
    except Exception as e:
        logger.warning(f"Indeed mobile fetch failed for {job_key}: {e}")
    return None


def download_job_from_indeed(url: str, headless: bool = True, chrome_user_data_dir: str | None = None) -> str:
    job_key = _extract_indeed_job_key(url)
    if not job_key:
        logger.error(f"Could not extract job key from Indeed URL: {url}")
        return ""
    description = _fetch_indeed_description(job_key)
    if description:
        logger.info(f"Indeed mobile fetch succeeded for key: {job_key}")
        return description
    logger.error(f"Indeed mobile fetch returned no description for key: {job_key}")
    return ""


def download_job_from_dice(url: str, headless: bool = True) -> str:
    scraper = cloudscraper.create_scraper()
    response = scraper.get(url)
    response.raise_for_status()
    bs_obj = BeautifulSoup(response.text, "html.parser")
    ld_script = bs_obj.find("script", type="application/ld+json")
    if ld_script:
        try:
            ld_json = ld_script.string
            if ld_json:
                ld_data = json.loads(ld_json)
                if isinstance(ld_data, dict) and "description" in ld_data:
                    return ld_data["description"]
            else:
                logger.warning(f"No LD+JSON content found in script tag for {url}")
                return ""
        except Exception as e:
            logger.warning(f"Error parsing LD+JSON: {e}")
            return ""
    logger.warning(f"No LD+JSON script tag found for {url}")
    return ""


def download_job_from_greenhouse(url: str, headless: bool = True) -> str:
    greenhouse_selector = 'div[class^="job__description"]'
    scraper = cloudscraper.create_scraper()
    response = scraper.get(url)
    response.raise_for_status()
    bs_obj = BeautifulSoup(response.text, "html.parser")
    job_description_els = bs_obj.select(greenhouse_selector)
    if job_description_els:
        return "".join(str(el) for el in job_description_els)
    return ""


class TimeoutException(Exception):
    pass


def _timeout_thread(seconds: int, result_holder: list) -> None:
    time.sleep(seconds)
    result_holder[0] = True


@contextlib.contextmanager
def timeout_context(seconds: int) -> Generator:
    """Cross-platform timeout context manager using threading."""
    timeout_occurred = [False]
    timer_thread = None
    try:
        timer_thread = threading.Thread(
            target=_timeout_thread, args=(seconds, timeout_occurred), daemon=True
        )
        timer_thread.start()
        yield
    finally:
        if timer_thread and timer_thread.is_alive():
            pass
        if timeout_occurred[0]:
            raise TimeoutException(f"Function execution exceeded {seconds} seconds")


def download_job_with_playwright(
    url: str,
    selector: str | None = None,
    selector_type: str = "text",
    timeout: int = 15000,
    headless: bool = True,
    chrome_user_data_dir: str | None = None,
) -> str:
    """Download job content using Playwright with timeout protection."""
    with get_playwright_stealth_browser(headless=headless, chrome_user_data_dir=chrome_user_data_dir) as browser:
        page = None
        try:
            timeout_seconds = int(timeout / 1000) + 10

            with timeout_context(timeout_seconds):
                page = browser.new_page()
                page.set_default_timeout(timeout)

                logger.info(f"Playwright URL: {url}")
                logger.debug(f"Attempting to load page with timeout={timeout}ms")

                try:
                    page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                    logger.debug(f"Page loaded successfully for {url}")
                except Exception as goto_error:
                    logger.warning(f"page.goto() error for {url}: {goto_error}")
                    try:
                        html_content = page.content()
                        logger.info(f"Got partial content despite goto error for {url}")
                        return html_content
                    except Exception:
                        raise

                time.sleep(2)
                pw_locator = None
                if selector:
                    if selector_type == "label":
                        pw_locator = page.get_by_label(selector)
                    elif selector_type == "css":
                        pw_locator = page.locator(selector)
                    else:
                        pw_locator = page.get_by_text(selector)

                    try:
                        if pw_locator.is_visible(timeout=5000):
                            pass
                        else:
                            logger.warning(f"Locator is not visible: {selector}")
                    except Exception as e:
                        logger.warning(f"Error checking visibility (continuing anyway): {e}")

                html_content = page.content()
                logger.info(f"Successfully retrieved content for {url} ({len(html_content)} chars)")
                return html_content

        except TimeoutException as te:
            logger.error(f"Timeout error for {url}: {te}")
            return ""
        except Exception as e:
            logger.error(f"Error during playwright execution for {url}: {e}")
            logger.debug(traceback.format_exc())
            return ""
        finally:
            try:
                if page:
                    page.close()
                    logger.debug(f"Closed page for {url}")
            except Exception as e:
                logger.warning(f"Error closing page: {e}")

