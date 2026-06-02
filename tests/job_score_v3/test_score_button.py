"""
End-to-end Playwright test: Verify Score button becomes enabled
after selecting a profile and downloading a job description.
Expects the application to be already running.
"""

import sys
import json
from pathlib import Path
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
import os

PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "lib"))

load_dotenv(PROJECT_ROOT / ".env")
V3_PORT = os.getenv("V3_PORT", "5001")
BASE_URL = f"http://localhost:{V3_PORT}"

JOB_URL = "https://www.linkedin.com/jobs/view/4405231826"


def run_score_button_test():
    print("=" * 70)
    print("E2E Test: Score Button Enablement")
    print("=" * 70)
    print(f"\nTarget URL: {BASE_URL}")

    # Authenticate
    import urllib.request
    import urllib.error

    auth_url = f"{BASE_URL}/api/test-auth"
    session_cookie = None

    print(f"\n1. Getting session cookie from {auth_url}...")
    try:
        with urllib.request.urlopen(auth_url, timeout=5) as response:
            data = json.loads(response.read().decode())
            session_cookie = data["session_cookie"]
            print("  ✓ Session cookie obtained")
    except (urllib.error.URLError, ConnectionRefusedError) as e:
        print(f"\n✗ Application not reachable at {BASE_URL}: {e}")
        sys.exit(1)

    try:
        with sync_playwright() as p:
            print("\n2. Launching browser...")
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()
            context.add_cookies(
                [
                    {
                        "name": "session_",
                        "value": session_cookie,
                        "domain": "localhost",
                        "path": "/",
                        "httpOnly": False,
                        "secure": False,
                    }
                ]
            )

            page = context.new_page()

            # Navigate to home
            print(f"\n3. Navigating to {BASE_URL}/...")
            page.goto(f"{BASE_URL}/")
            page.wait_for_selector("text=Welcome, Test User", timeout=10000)
            print("  ✓ Logged in")

            # Click Score in nav
            print("\n4. Clicking Score nav item...")
            page.click('a[href="/score"]')
            page.wait_for_selector("text=Job Fit Score", timeout=10000)
            print("  ✓ Score page loaded")

            # Select a profile from the dropdown (uk-select web component)
            print("\n5. Selecting profile...")
            # Read options from the hidden native <select> inside the uk-select
            all_opts = page.locator("#profile_name select option, #profile_name option")
            count = all_opts.count()
            if count <= 1:
                print("  ✗ No profiles available. Create a profile first.")
                browser.close()
                sys.exit(1)
            profile_text = all_opts.nth(1).inner_text()
            # Click the uk-select to open the dropdown
            page.locator("#profile_name").click()
            page.wait_for_timeout(500)
            # Click the matching option in the visible dropdown
            page.locator(f'li:has-text("{profile_text}")').first.click()
            print(f"  ✓ Selected profile: {profile_text}")

            page.wait_for_timeout(1000)

            # Enter job URL and click download
            print(f"\n6. Entering job URL and downloading...")
            page.fill("#job_url", JOB_URL)
            page.wait_for_timeout(300)

            # Click the download button
            download_btn = page.locator('[hx-post="/score/download-job"]')
            download_btn.click()
            print("  ✓ Download clicked, waiting for job description...")

            # Wait for the textarea to get populated (spinner should disappear)
            # Wait up to 30s for text to appear in the textarea
            for i in range(60):
                text_content = page.locator("#job_text").input_value()
                if len(text_content.strip()) > 100:
                    print(f"  ✓ Job description downloaded ({len(text_content)} chars)")
                    break
                page.wait_for_timeout(500)
            else:
                page.screenshot(path="score_download_timeout.png")
                print("  ✗ Timed out waiting for job description to download")
                browser.close()
                sys.exit(1)

            # Wait a moment for the button OOB swap to complete
            page.wait_for_timeout(2000)

            # Check if Score button is enabled
            print("\n7. Checking Score button state...")
            score_btn = page.locator("#calculate-btn")
            is_disabled = score_btn.is_disabled()
            btn_cls = score_btn.get_attribute("class") or ""

            if is_disabled:
                page.screenshot(path="score_button_disabled.png")
                print(f"  ✗ Score button is DISABLED (class: {btn_cls})")
                print("  Screenshot saved to score_button_disabled.png")
                browser.close()
                sys.exit(1)
            else:
                print(f"  ✓ Score button is ENABLED")

            browser.close()
            print("\n" + "=" * 70)
            print("E2E Test PASSED")
            print("=" * 70)

    except Exception as e:
        print(f"\n✗ E2E Test FAILED: {e}")
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    run_score_button_test()
