from __future__ import annotations
from typing import Optional, Any, Dict, List
import urllib.parse
import json
import re
import asyncio
from playwright.sync_api import sync_playwright, Page, Response, Locator
from playwright_stealth import Stealth
import logging
from jobspy.model import CompensationInterval, Scraper, ScraperInput, Site, JobPost, JobResponse, Location, Compensation, Country, JobType
from html_to_markdown import convert_to_markdown

logger = logging.getLogger("ZipRecruiter")


class ZipRecruiter(Scraper):
    def __init__(self, proxies: list[str] | None = None, ca_cert: str | None = None, user_agent: str | None = None, **kwargs):
        super().__init__(Site.ZIP_RECRUITER, proxies=proxies, ca_cert=ca_cert, user_agent=user_agent, **kwargs)
        # Workaround: Clear any existing asyncio event loop to avoid conflict with sync_playwright
        # This error can occur when sync_playwright runs in threads where an asyncio loop exists
        try:
            asyncio.set_event_loop(None)
        except:
            pass
        # Create one shared Playwright instance for this scraper
        self._pw_manager = sync_playwright()
        self._playwright = self._pw_manager.start()

    def _bot_evasion(self, page: Page):
        # Additional stealth scripts to complement playwright-stealth
        page.add_init_script("""
            // Pass the Webdriver Test.
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined,
            });

            // Pass the Chrome Test.
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {},
            };

            // Pass the Permissions Test.
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
        """)
        page.add_init_script("""
            // Overwrite the `plugins` property to use a custom getter.
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            // Overwrite the `languages` property to use a custom getter.
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });

            // Hide automation indicators
            delete navigator.__proto__.webdriver;

            // Mock navigator connection
            Object.defineProperty(navigator, 'connection', {
                get: () => ({
                    effectiveType: '4g',
                    rtt: 50,
                    downlink: 10,
                }),
            });

            // Mock getBoundingClientRect to avoid non-zero offsets
            const originalGetBoundingClientRect = Element.prototype.getBoundingClientRect;
            Element.prototype.getBoundingClientRect = function() {
                const rect = originalGetBoundingClientRect.call(this);
                // Add slight randomness to appear more natural
                return rect;
            };
        """)
        page.add_init_script("""
            // Canvas fingerprint randomization
            const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
            HTMLCanvasElement.prototype.toDataURL = function(type) {
                const context = this.getContext('2d');
                if (context) {
                    const imageData = context.getImageData(0, 0, this.width, this.height);
                    for (let i = 0; i < imageData.data.length; i += 4) {
                        imageData.data[i] += Math.random() * 0.1;
                    }
                    context.putImageData(imageData, 0, 0);
                }
                return originalToDataURL.apply(this, arguments);
            };

            // WebGL fingerprint randomization
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) {
                    return 'Intel Inc.';
                }
                if (parameter === 37446) {
                    return 'Intel Iris OpenGL Engine';
                }
                return getParameter.call(this, parameter);
            };
        """)

    def _create_browser_session(self):
        """Create a new Playwright browser session with stealth."""
        browser = self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-extensions",
                "--disable-sync",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
                "--disable-infobars",
                "--disable-notifications",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1320, "height": 921},
            locale="en-US",
            timezone_id="America/New_York",
            device_scale_factor=1,
        )
        page = context.new_page()
        stealth_obj = Stealth()
        stealth_obj.apply_stealth_sync(page)
        # Apply additional custom bot evasion
        self._bot_evasion(page)
        return browser, context, page

    def _extract_js_vars_json(self, page: Page) -> Optional[Dict[str, Any]]:
        """
        JIRA-054 Step 1: Extract JavaScript variables from the page.
        Extracts the JSON object that contains job data.
        Update 2: Uses Playwright locator to find the script tag.
        """
        try:
            # Look for script tag with id="js_variables"
            js_vars_loc = page.locator("script#js_variables")
            if js_vars_loc.count() > 0:
                js_vars_content = js_vars_loc.inner_text()
                logger.debug(f"Found js_variables script tag")
                js_vars_json = json.loads(js_vars_content)

                logger.info(f"Successfully extracted JavaScript variables JSON")
                return js_vars_json
            else:
                logger.error("Could not find script#js_variables element")
                return None
        except Exception as e:
            logger.error(f"Error extracting JavaScript variables: {e}")
            return None

    def _parse_job_card(self, job_card: Dict[str, Any], js_vars: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        JIRA-054 Step 2: Parse job card from JSON structure.
        Extract all attributes except description.
        """
        try:
            listing_key = job_card.get('listingKey') or job_card.get('applyButtonConfig', {}).get('listingKey')
            if not listing_key:
                logger.warning(f"No listingKey found in job card")
                return None

            title = job_card.get('title')
            if not title:
                logger.warning(f"No title found for job card")
                return None

            # Company info
            company = job_card.get('company', {})
            company_name = company.get('name', 'Unknown Company') if isinstance(company, dict) else str(company)

            # Location
            location_data = job_card.get('location', {})
            location_str = location_data.get('displayName', '') if isinstance(location_data, dict) else str(location_data)

            # Pay/Compensation
            pay = job_card.get('pay', {})
            compensation = self._parse_compensation(pay)

            # Employment type
            employment_types = job_card.get('employmentTypes', [])
            job_type = self._parse_employment_type(employment_types)

            # JIRA-054 Step 3a: Construct job URL
            job_url = f"https://www.ziprecruiter.com/jobs/a/b?lk={listing_key}"
            logger.info(f"{listing_key}: Parsed job card: {title} at {company_name}, Job Description length: {len(job_card.get('description', ''))} chars")
            return {
                'listing_key': listing_key,
                'title': title,
                'company_name': company_name,
                'location': location_str,
                'job_url': job_url,
                'compensation': compensation,
                'job_type': job_type,
                'job_card': job_card,  # Store full job card for description extraction
            }
        except Exception as e:
            logger.error(f"Error parsing job card: {e}")
            return None

    def _parse_compensation(self, pay: Dict[str, Any]) -> Compensation:
        """Parse compensation from pay data."""
        try:
            if not pay:
                return Compensation()

            min_amount = pay.get('min') or pay.get('minAnnual')
            max_amount = pay.get('max') or pay.get('maxAnnual')

            if min_amount and max_amount:
                return Compensation(
                    min_amount=float(min_amount),
                    max_amount=float(max_amount),
                    interval=CompensationInterval.YEARLY
                )
            return Compensation()
        except Exception:
            return Compensation()

    def _parse_employment_type(self, employment_types: List[Any]) -> Optional[JobType]:
        """Parse employment type from list."""
        if not employment_types:
            return None

        try:
            # employment_types is a list of dicts with 'name' field
            for et in employment_types:
                if isinstance(et, dict):
                    name = et.get('name', '').lower()
                    if 'full-time' in name or 'fulltime' in name:
                        return JobType.FULL_TIME
                    elif 'part-time' in name or 'parttime' in name:
                        return JobType.PART_TIME
                    elif 'contract' in name or 'contractor' in name:
                        return JobType.CONTRACT
                    elif 'intern' in name:
                        return JobType.INTERNSHIP
        except Exception:
            pass

        return None

    def _get_first_job_description(self, js_vars: Dict[str, Any]) -> Optional[str]:
        """
        JIRA-054 Step 4a: Get description for the first job (default loaded).
        Extract from getJobDetailsResponse.jobDetails.htmlFullDescription
        """
        try:
            job_details = js_vars.get('getJobDetailsResponse', {}).get('jobDetails', {})
            html_description = job_details.get('htmlFullDescription')

            if html_description:
                return convert_to_markdown(html_description)
            else:
                logger.warning("No htmlFullDescription found for first job")
                return None
        except Exception as e:
            logger.error(f"Error extracting first job description: {e}")
            return None

    def _get_job_description_by_clicking(self, page: Page, job_data: Dict[str, Any]) -> Optional[str]:
        """
        JIRA-054 Step 4b: Get description by clicking on job.
        Intercepts the protobuf API call and decodes it.
        """
        try:
            listing_key = job_data['listing_key']
            title = job_data['title']

            # Find and click on the job card
            # Look for the job card by title or listing key
            job_card_locator = page.locator("article").filter(has_text=title).first

            if job_card_locator.count() == 0:
                # Try alternative selector
                job_card_locator = page.locator(f"[data-listing-key='{listing_key}']").first

            if job_card_locator.count() == 0:
                logger.warning(f"Could not find job card for {title}")
                return None

            # Set up response interception for the API call
            api_response = []

            def handle_response(response: Response):
                if "GetBreakroomJobDetails" in response.url:
                    try:
                        api_response.append(response)
                    except Exception as e:
                        logger.debug(f"Error handling response: {e}")

            page.on("response", handle_response)

            # Click on the job card
            job_card_locator.click()

            # Wait for the API call
            page.wait_for_timeout(2000)

            # Remove the listener
            page.remove_listener("response", handle_response)

            # Process the API response
            if api_response:
                return self._decode_protobuf_description(api_response[0])
            else:
                logger.warning(f"No API response received for {title}")
                # Fallback: try to get description from page content
                return self._extract_description_from_page(page)

        except Exception as e:
            logger.error(f"Error getting job description by clicking: {e}")
            return None

    def _decode_protobuf_description(self, response: Response) -> Optional[str]:
        """
        JIRA-054 Step 4b: Decode protobuf response to extract description.
        """
        try:
            from protobuf_decoder.protobuf_decoder import Parser

            # Get the raw response body (Playwright automatically decompresses gzip)
            raw_data = response.body()

            # Debug: log response info
            logger.debug(f"Response URL: {response.url}")
            logger.debug(f"Response headers: {response.headers}")
            logger.debug(f"Response body type: {type(raw_data)}, length: {len(raw_data) if raw_data else 0}")

            # Check content type
            content_type = response.headers.get('content-type', '')
            logger.debug(f"Content-Type: {content_type}")

            # If it's JSON, parse as JSON instead of protobuf
            if 'application/json' in content_type:
                logger.debug("Response is JSON, parsing as JSON")
                if isinstance(raw_data, bytes):
                    raw_data = raw_data.decode('utf-8')
                json_data = json.loads(raw_data)
                if 'jobDetails' in json_data and 'htmlFullDescription' in json_data['jobDetails']:
                    return convert_to_markdown(json_data['jobDetails']['htmlFullDescription'])
                elif 'htmlFullDescription' in json_data:
                    return convert_to_markdown(json_data['htmlFullDescription'])
                return None

            # Convert bytes to hex string for protobuf_decoder
            # The library expects hex-encoded strings
            hex_string = raw_data.hex()

            # Parse the protobuf data
            parsed_data = Parser().parse(hex_string)
            data_dict = parsed_data.to_dict()

            # The description is at: results[0].data.results[0].data.results[13].data
            # Based on test_proto.py analysis
            logger.debug(f"Protobuf data keys: {list(data_dict.keys())}")

            # Navigate the protobuf structure to find the description
            # The structure is: results[0].data.results[0].data.results[N].data
            # where N is a variable field number (10, 13, etc.)
            try:
                if 'results' in data_dict and len(data_dict['results']) > 0:
                    level_1 = data_dict['results'][0]
                    if 'data' in level_1 and 'results' in level_1['data']:
                        level_2 = level_1['data']['results'][0]
                        if 'data' in level_2 and 'results' in level_2['data']:
                            results_array = level_2['data']['results']
                            # Find the item with HTML description (long string with HTML tags)
                            for item in results_array:
                                if isinstance(item, dict) and 'data' in item:
                                    description_html = item['data']
                                    if isinstance(description_html, str) and len(description_html) > 100:
                                        # Verify it's actually HTML
                                        if '<' in description_html and '>' in description_html:
                                            if '<p>' in description_html or '<div>' in description_html or '<h3>' in description_html:
                                                logger.debug(f"Found HTML description in field {item.get('field', 'unknown')}, length: {len(description_html)}")
                                                return convert_to_markdown(description_html)
            except (KeyError, IndexError, TypeError) as e:
                logger.debug(f"Error navigating protobuf structure: {e}")

            # Fallback: recursively search for any HTML description
            def find_html_recursive(obj, depth=0):
                if depth > 15:  # Limit recursion depth
                    return None

                if isinstance(obj, dict):
                    # Check for known HTML field names
                    if 'htmlFullDescription' in obj:
                        return obj['htmlFullDescription']
                    if 'htmlDescription' in obj:
                        return obj['htmlDescription']

                    # Check all values
                    for key, value in obj.items():
                        if isinstance(value, str) and len(value) > 500:
                            # Check if it's HTML
                            if '<div>' in value or '<p>' in value or '<h3>' in value:
                                if '</div>' in value or '</p>' in value or '</h3>' in value:
                                    logger.debug(f"Found HTML at key: {key}, length: {len(value)}")
                                    return value
                        elif isinstance(value, (dict, list)):
                            result = find_html_recursive(value, depth + 1)
                            if result:
                                return result

                elif isinstance(obj, list):
                    for item in obj:
                        result = find_html_recursive(item, depth + 1)
                        if result:
                            return result

                return None

            description = find_html_recursive(data_dict)
            if description:
                return convert_to_markdown(description)

            logger.warning(f"Could not find description in protobuf response")
            return None

        except Exception as e:
            logger.error(f"Error decoding protobuf description: {e}")
            import traceback
            logger.debug(f"Traceback: {traceback.format_exc()}")
            # Try to parse as JSON as fallback
            try:
                raw_data = response.body()
                import gzip
                if 'gzip' in response.headers.get('content-encoding', ''):
                    raw_data = gzip.decompress(raw_data)
                if isinstance(raw_data, bytes):
                    raw_data = raw_data.decode('utf-8')
                json_data = json.loads(raw_data)
                if 'jobDetails' in json_data and 'htmlFullDescription' in json_data['jobDetails']:
                    return convert_to_markdown(json_data['jobDetails']['htmlFullDescription'])
                elif 'htmlFullDescription' in json_data:
                    return convert_to_markdown(json_data['htmlFullDescription'])
            except Exception as json_e:
                logger.debug(f"Also failed to parse as JSON: {json_e}")
            return None

    def _extract_description_from_page(self, page: Page) -> Optional[str]:
        """Fallback: Extract description from page content after clicking."""
        try:
            # Look for the description in the right-hand panel
            desc_locator = page.locator("div.mb-24.lg\\:mb-auto.hidden.sm\\:block").first
            if desc_locator.count() > 0:
                desc_html = desc_locator.inner_html()
                return convert_to_markdown(desc_html)
            return None
        except Exception as e:
            logger.error(f"Error extracting description from page: {e}")
            return None

    def scrape(self, scraper_input: ScraperInput) -> JobResponse:
        """
        JIRA-054: Two-pass approach for stealth:
        1. Extract JavaScript variables and parse jobs from JSON
        2. Get descriptions by clicking and intercepting API calls
        """
        job_list: list[JobPost] = []
        results_wanted = scraper_input.results_wanted

        # Load parameters from job_finder_config
        job_finder_config = scraper_input.job_finder_config or {}
        zr_params = job_finder_config.get("ziprecruiter_params", {})

        browser, context, page = self._create_browser_session()
        base_url = "https://www.ziprecruiter.com/jobs-search"
        logger.info(f"*** Scraping ZipRecruiter with criteria: {scraper_input.search_term} ***")
        try:
            # Build search URL
            params = {
                "search": scraper_input.search_term,
                "location": scraper_input.location,
                "radius": zr_params.get("radius", 25),
                "days": zr_params.get("days", 30),
                "refine_by_experience_level":zr_params.get("refine_by_experience_level", "Senior"),
                "refine_by_salary": zr_params.get("refine_by_salary", "180000"),
            }
            params = {k: v for k, v in params.items() if v}
            url = f"{base_url}?{urllib.parse.urlencode(params)}"

            logger.info(f"Navigating to: {url}")
            page.goto(url)

            # Wait for page to load
            page.wait_for_load_state("networkidle", timeout=15000)
            page.wait_for_timeout(2000)

            # Close login popup by clicking near top left corner
            try:
                page.mouse.click(50, 100)
                logger.debug("Clicked to close login popup")
                page.wait_for_timeout(500)
            except Exception as e:
                logger.debug(f"Could not close login popup (may not be present): {e}")

            # JIRA-054 Step 1: Extract JavaScript variables
            js_vars = self._extract_js_vars_json(page)
            if not js_vars:
                logger.error("Failed to extract JavaScript variables")
                return JobResponse(jobs=[])

            # JIRA-054 Step 2: Get job cards from hydrateJobCardsResponse
            job_cards = []
            hydrate_response = js_vars.get('hydrateJobCardsResponse', {})
            if 'jobCards' in hydrate_response:
                job_cards = hydrate_response['jobCards']
                logger.info(f"Found {len(job_cards)} job cards in JSON")

            if not job_cards:
                logger.error("No job cards found in JSON")
                return JobResponse(jobs=[])

            # Limit to results_wanted
            job_cards = job_cards[:results_wanted]

            # Parse all job cards
            parsed_jobs = []
            for idx, job_card in enumerate(job_cards):
                parsed = self._parse_job_card(job_card, js_vars)
                if parsed:
                    # JIRA-054 Step 4a: Get description for first job from JSON
                    if idx == 0:
                        description = self._get_first_job_description(js_vars)
                        parsed['description'] = description
                    parsed_jobs.append(parsed)

            logger.info(f"Parsed {len(parsed_jobs)} jobs")

            # JIRA-054 Step 4b: Get descriptions for remaining jobs
            for idx, job_data in enumerate(parsed_jobs):
                if idx == 0:
                    continue  # First job already has description

                logger.info(f"Getting description for job {idx + 1}/{len(parsed_jobs)}: {job_data['title']}")
                description = self._get_job_description_by_clicking(page, job_data)
                job_data['description'] = description

                # Small delay between clicks
                page.wait_for_timeout(500)

            # Convert to JobPost objects
            for job_data in parsed_jobs:
                job_post = JobPost(
                    id=job_data['listing_key'],
                    title=job_data['title'],
                    company_name=job_data['company_name'],
                    location=Location(city=job_data['location'], country=Country.USA),
                    job_url=job_data['job_url'],
                    description=job_data.get('description', ''),
                    compensation=job_data['compensation'],
                    job_type=job_data.get('job_type'),
                )
                job_list.append(job_post)

            logger.info(f"Successfully scraped {len(job_list)} jobs")

        except Exception as e:
            logger.error(f"Error during scraping: {e}")
        finally:
            if browser:
                browser.close()
        logger.info(f"*** Scraping ZipRecruiter Completed. Found {len(job_list)} jobs for criteria: {scraper_input.search_term} ***")
        return JobResponse(jobs=job_list)
