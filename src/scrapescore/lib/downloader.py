import contextlib
import fcntl
import json
import logging
import os
import re
import threading
import time
import traceback
from typing import Generator
from urllib.parse import urlencode, urlparse, parse_qs

import cloudscraper
import requests
from bs4 import BeautifulSoup
from html_to_markdown import convert_to_markdown
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth

from scrapescore.lib.config import get_storage_dir_config

logger = logging.getLogger(__name__)



def download_url(url: str, headless: bool = True) -> str:
    """
    Download content from a URL using appropriate method based on the source.

    Routes to specialized downloaders for known job sites (workday, linkedin, indeed, dice),
    falls back to requests library, then to playwright if needed.

    Args:
        url: The URL to download content from
        headless: Whether to run playwright in headless mode (default: True)

    Returns:
        HTML content as a string
    """
    if "workday" in url:
        logger.info(f"Downloading job from workday: {url}")
        return download_job_from_workday(url, headless=headless)
    if "linkedin" in url:
        logger.info(f"Downloading job from linkedin: {url}")
        return download_job_from_linkedin(url, headless=headless)
    if "indeed" in url:
        logger.info(f"Downloading job from indeed: {url}")
        return download_job_from_indeed(url, headless=headless)
    if "dice" in url:
        logger.info(f"Downloading job from dice: {url}")
        return download_job_from_dice(url, headless=headless)
    if "greenhouse" in url:
        logger.info(f"Downloading job from greenhouse: {url}")
        return download_job_from_greenhouse(url, headless=headless)

    try:
        logger.info(f"Downloading job using requests: {url}")
        # Add timeout to prevent hanging (15 seconds connection, 30 seconds read)
        response = requests.get(url, timeout=(15, 30))
        response.raise_for_status()
        return response.text
    except Exception as e:
        logger.warning(f"Failed to download job content using requests: {url}")
        logger.info(f"Downloading job using playwright: {url}")
        return download_job_with_playwright(url, headless=headless)



# Global singleton browser instance (with lock for thread safety)
_browser_lock = threading.Lock()
_browser_instance = None
_playwright_instance = None
_browser_ref_count = 0
_lock_file_handle = None


def _get_lock_file_path():
    """Get the path to the browser lock file."""
    user_data_dir = get_storage_dir_config("chrome_user_data_dir")
    lock_dir = os.path.dirname(user_data_dir)
    return os.path.join(lock_dir, ".chrome_browser.lock")


def _acquire_lock():
    """Acquire the browser lock file (blocks until available)."""
    global _lock_file_handle
    lock_path = _get_lock_file_path()

    logger.debug(f"Acquiring browser lock: {lock_path}")
    _lock_file_handle = open(lock_path, "w")
    fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_EX)
    logger.debug("Browser lock acquired")


def _release_lock():
    """Release the browser lock file."""
    global _lock_file_handle
    if _lock_file_handle:
        try:
            fcntl.flock(_lock_file_handle.fileno(), fcntl.LOCK_UN)
            _lock_file_handle.close()
            _lock_file_handle = None
            logger.debug("Browser lock released")
        except Exception as e:
            logger.warning(f"Error releasing browser lock: {e}")


def _cleanup_chrome_locks():
    """Clean up stale Chrome lock files in the user data directory."""
    user_data_dir = get_storage_dir_config("chrome_user_data_dir")

    # Chrome lock files that might prevent new instances
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

    # Also try to remove the lock file in the parent directory
    try:
        git_lock = os.path.join(user_data_dir, "lockfile")
        if os.path.exists(git_lock):
            os.remove(git_lock)
            logger.debug(f"Removed lockfile: {git_lock}")
    except Exception:
        pass


def _get_or_create_browser(headless: bool = True):
    """Get or create the singleton browser instance."""
    global _browser_instance, _playwright_instance, _browser_ref_count

    with _browser_lock:
        if _browser_instance is None:
            user_data_dir = get_storage_dir_config("chrome_user_data_dir")
            logger.info(f"Creating new stealth browser (headless={headless})...")

            # Clean up any stale Chrome lock files before launching
            _cleanup_chrome_locks()

            # Acquire file lock before launching Chrome
            _acquire_lock()

            try:
                _playwright_instance = sync_playwright().start()

                # Use persistent context for authentication state
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

                # Apply stealth to avoid detection
                stealth = Stealth()
                stealth.apply_stealth_sync(_browser_instance)

                logger.info("Browser created successfully")
            except Exception as e:
                # Release lock if browser creation failed
                logger.error(f"Failed to create browser: {e}")
                _release_lock()
                _playwright_instance = None
                raise

        _browser_ref_count += 1
        logger.debug(f"Browser ref count: {_browser_ref_count}")
        return _browser_instance


def _release_browser():
    """Release a reference to the browser. Close when ref count reaches zero."""
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

            # Release file lock when browser is fully closed
            _release_lock()


@contextlib.contextmanager
def get_playwright_stealth_browser(headless: bool = True) -> Generator:
    """
    Context manager that provides a Playwright browser with stealth configuration.

    Uses a singleton pattern with persistent Chrome browser context for
    authentication state and playwright-stealth to avoid detection.
    Uses a file lock to ensure only ONE process across the system can use the browser.

    Args:
        headless: Whether to run the browser in headless mode (default: True)

    Yields:
        Browser context object that can be used to create pages

    Example:
        with get_playwright_stealth_browser(headless=True) as browser:
            page = browser.new_page()
            page.goto("https://example.com")
            # ... use the page
    """
    browser = _get_or_create_browser(headless)
    try:
        yield browser
    finally:
        _release_browser()


def download_job_from_workday(url: str, headless: bool = True) -> str:
    # Use text selector by default
    workday_selector = "Workday, Inc. All rights reserved."
    return download_job_with_playwright(
        url, selector=workday_selector, selector_type="text", headless=headless
    )


def download_job_from_linkedin(url: str, headless: bool = True) -> str:
    # use css selector for "Show more" button
    linkedin_selector = "button.show-more-less-html__button--more"
    html_content = download_job_with_playwright(
        url, selector=linkedin_selector, selector_type="css", headless=headless
    )
    try:
        content_soup = BeautifulSoup(html_content, "html.parser")
        job_desc_tag = content_soup.find("h2", string="About the job").parent.parent
        return job_desc_tag.decode_contents()
    except Exception as e:
        logger.error(
            f"LinkedIn page did not have About the Job section, returning full HTML"
        )
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


def download_job_from_indeed(url: str, headless: bool = True) -> str:
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
        job_description = "".join(str(el) for el in job_description_els)
        return job_description
    else:
        return ""


class TimeoutException(Exception):
    """Exception raised when timeout occurs."""

    pass


def _timeout_thread(seconds: int, result_holder: list) -> None:
    """Thread that sleeps and then sets a flag indicating timeout occurred."""
    time.sleep(seconds)
    result_holder[0] = True


@contextlib.contextmanager
def timeout_context(seconds: int) -> Generator:
    """
    Cross-platform timeout context manager using threading.
    Works on both Windows and Unix systems.
    """
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
            # Timer didn't fire, cancel it (it will become a daemon and die)
            pass
        if timeout_occurred[0]:
            raise TimeoutException(f"Function execution exceeded {seconds} seconds")


def download_job_with_playwright(
    url: str,
    selector: str | None = None,
    selector_type: str = "text",
    timeout: int = 15000,
    headless: bool = True,
) -> str:
    """
    Download job content using Playwright with timeout protection.

    Args:
        url: The URL to download
        selector: Optional selector to wait for
        selector_type: Type of selector ('label', 'css', or 'text')
        timeout: Maximum time to wait for page load in milliseconds (default: 15000ms = 15s)
        headless: Whether to run in headless mode (default: True)

    Returns:
        HTML content of the page, or empty string on error
    """
    # Use the shared stealth browser function
    with get_playwright_stealth_browser(headless=headless) as browser:
        page = None
        try:
            # Convert milliseconds to seconds for timeout context, add 10s buffer
            timeout_seconds = int(timeout / 1000) + 10

            with timeout_context(timeout_seconds):
                page = browser.new_page()

                # Set default timeout for all page operations
                page.set_default_timeout(timeout)

                logger.info(f"Playwright URL: {url}")
                logger.debug(f"Attempting to load page with timeout={timeout}ms")

                # Set a reasonable timeout and use domcontentloaded instead of full load
                # domcontentloaded waits for DOM to be ready but doesn't wait for all resources
                try:
                    page.goto(url, timeout=timeout, wait_until="domcontentloaded")
                    logger.debug(f"Page loaded successfully for {url}")
                except Exception as goto_error:
                    logger.warning(f"page.goto() error for {url}: {goto_error}")
                    # Try to get content anyway, page might be partially loaded
                    try:
                        html_content = page.content()
                        logger.info(f"Got partial content despite goto error for {url}")
                        return html_content
                    except Exception:
                        raise

                time.sleep(2)  # Reduced sleep since we're waiting for domcontentloaded
                pw_locator = None
                if selector:
                    if selector_type == "label":
                        pw_locator = page.get_by_label(selector)
                    elif selector_type == "css":
                        pw_locator = page.locator(selector)
                    else:
                        # Default to text
                        pw_locator = page.get_by_text(selector)

                    try:
                        # Use a shorter timeout for selector visibility check
                        if pw_locator.is_visible(timeout=5000):
                            pass
                        else:
                            logger.warning(f"Locator is not visible: {selector}")
                    except Exception as e:
                        logger.warning(
                            f"Error checking visibility (continuing anyway): {e}"
                        )

                html_content = page.content()
                logger.info(
                    f"Successfully retrieved content for {url} ({len(html_content)} chars)"
                )
                return html_content

        except TimeoutException as te:
            logger.error(f"Timeout error for {url}: {te}")
            return ""
        except Exception as e:
            logger.error(f"Error during playwright execution for {url}: {e}")
            logger.debug(traceback.format_exc())
            return ""
        finally:
            # Explicit cleanup for page
            try:
                if page:
                    page.close()
                    logger.debug(f"Closed page for {url}")
            except Exception as e:
                logger.warning(f"Error closing page: {e}")


def search_workday_jobs_with_playwright(
    url: str, params: dict[str, str] | None = None, timeout: int = 20000
) -> str:
    """
    Search Workday jobs using Playwright with timeout protection.

    Args:
        url: The Workday search URL
        params: Query parameters to add to the URL
        timeout: Maximum time to wait for page load in milliseconds (default: 20000ms = 20s)

    Returns:
        HTML content of the page, or empty string on error
    """
    # Use the shared stealth browser function
    with get_playwright_stealth_browser(headless=True) as browser:
        page = None
        try:
            # Convert milliseconds to seconds for timeout context, add 10s buffer
            timeout_seconds = int(timeout / 1000) + 10

            with timeout_context(timeout_seconds):
                page = browser.new_page()
                page.set_default_timeout(timeout)

                if not params:
                    params = {}
                query_string = urlencode(params, doseq=True)
                if "?" in url:
                    playwright_url = f"{url}&{query_string}" if query_string else url
                else:
                    playwright_url = f"{url}?{query_string}" if query_string else url
                logger.info(f"Playwright URL: {playwright_url}")

                try:
                    page.goto(
                        playwright_url, timeout=timeout, wait_until="domcontentloaded"
                    )
                    logger.debug(f"Page loaded successfully for {playwright_url}")
                except Exception as goto_error:
                    logger.warning(
                        f"page.goto() error for {playwright_url}: {goto_error}"
                    )
                    # Try to get content anyway
                    try:
                        html_content = page.content()
                        logger.info("Got partial content despite goto error")
                        return html_content
                    except Exception:
                        raise

                try:
                    jobs_found_label = page.get_by_text(
                        re.compile(r"JOBS? FOUND", re.IGNORECASE)
                    )
                    # Use a shorter timeout for waiting for the label
                    jobs_found_label.wait_for(state="visible", timeout=10000)
                    logger.info(f"Num jobs found: {jobs_found_label.text_content()}")
                    html_content = page.content(timeout=timeout)
                    return html_content
                except Exception as e:
                    logger.warning(
                        f"Failed to find jobs label on {playwright_url}: {e}"
                    )
                    # Still return the content even if we couldn't find the label
                    html_content = page.content(timeout=timeout)
                    return html_content

        except TimeoutException as te:
            logger.error(f"Timeout error for {url}: {te}")
            return ""
        except Exception as e:
            logger.error(f"Failed to retrieve URL: {url}")
            logger.error(f"Error during playwright execution: {e}")
            logger.debug(traceback.format_exc())
            return ""
        finally:
            # Explicit cleanup for page
            try:
                if page:
                    page.close()
                    logger.debug(f"Closed page for {url}")
            except Exception as e:
                logger.warning(f"Error closing page: {e}")


def get_markdown_from_html(html_str: str) -> str:
    linkedin_selector = (
        "div.show-more-less-html__markup, div[data-testid='job-description']"
    )
    # workspace > div > div > div.af12caab._23524a55._03e556b4 > div > div > div > div._50293d6d.c67a4f8b._528d5339._08542c3e.f7152e2e._58ae5fa6._17943a0d > div:nth-child(3) > div > div > div > div > div
    peraton_selector = (
        "body > div.section-2.white > div > div > div > div.job-desc-content"
    )
    workday_selector = (
        "#mainContent > div > div.css-gk87zv > div.css-e23il0 > div.css-11p01j8"
    )

    jd_selector_list = [linkedin_selector, peraton_selector, workday_selector]
    bs_obj = BeautifulSoup(html_str, "html.parser")
    # check for empty text
    if bs_obj.text.strip() == "":
        return ""
    try:
        for jd_selector in jd_selector_list:
            if bs_obj.select_one(jd_selector):
                job_description = convert_to_markdown(
                    str(bs_obj.select_one(jd_selector))
                )
                break
        else:
            job_description = convert_to_markdown(str(bs_obj))
    except Exception as e:
        try:
            job_description = convert_to_markdown(str(bs_obj))
        except Exception as e:
            logger.warning(f"Failed to extract text from html string: {e}")
            return ""
    return job_description


def get_markdown_from_url(url: str, headless: bool = True) -> str:
    html_str = download_url(url, headless=headless)
    if html_str is None:
        return ""
    if isinstance(html_str, bytes):
        html_str = html_str.decode("utf-9")
    return get_markdown_from_html(html_str)

