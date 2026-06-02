"""
End-to-end Playwright test for job_score login and navigation items.
Note: This test expects the application to be already running.
"""

import sys
import os
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv

# Add project src to path
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "src" / "lib"))

# Load environment variables
load_dotenv(PROJECT_ROOT / ".env")
V3_PORT = os.getenv("V3_PORT", "5001")
BASE_URL = f"http://localhost:{V3_PORT}"


def run_nav_test():
    """Run the navigation test."""
    print("=" * 70)
    print("E2E Test: Login and Navigation Items")
    print("=" * 70)
    print(f"\nTarget URL: {BASE_URL}")

    # Check if app is up
    import urllib.request
    import json
    import urllib.error

    auth_url = f"{BASE_URL}/api/test-auth"
    session_cookie = None

    print(f"\n1. Verifying application is running at {auth_url}...")
    try:
        with urllib.request.urlopen(auth_url, timeout=5) as response:
            content = response.read().decode()
            data = json.loads(content)
            session_cookie = data["session_cookie"]
            print("  ✓ Application is up and session cookie obtained")
    except (urllib.error.URLError, ConnectionRefusedError) as e:
        print(f"\n✗ ERROR: Application is not reachable at {BASE_URL}")
        print("  Please ensure the app is running before executing this test.")
        print(f"  Technical error: {e}")
        sys.exit(1)
    except json.JSONDecodeError as je:
        print(f"\n✗ ERROR: Failed to parse authentication response from {auth_url}")
        print(f"  Technical error: {je}")
        sys.exit(1)

    try:
        with sync_playwright() as p:
            # Maintain headless=False as requested by user
            print("\n2. Launching browser...")
            browser = p.chromium.launch(headless=False)
            context = browser.new_context()

            if session_cookie:
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
            print(f"3. Navigating to {BASE_URL}/...")
            page.goto(f"{BASE_URL}/")

            # Wait for the welcome message to appear
            try:
                page.wait_for_selector("text=Welcome, Test User", timeout=10000)
            except Exception as e:
                print(f"  ✗ Timeout waiting for welcome message: {e}")
                page.screenshot(path="auth_failure.png")
                with open("auth_failure_content.html", "w") as f:
                    f.write(page.content())
                raise

            print("  ✓ Logged in successfully")

            # 4. Verify Navigation Items
            print("\n4. Verifying navigation items...")

            expected_nav = [
                ("Search", "/search"),
                ("Saved", "/saved"),
                ("Applied", "/applied"),
                ("Analytics", "/analytics"),
                ("Score", "/score"),
                ("Config", "/config"),
                ("Profiles", "/profiles"),
            ]

            for name, path in expected_nav:
                # Check for link with correct text and href
                selector = f'a[href="{path}"]'
                element = page.wait_for_selector(selector, timeout=5000)
                assert element is not None, (
                    f"Navigation item '{name}' with path '{path}' not found"
                )

                inner_text = element.inner_text()
                assert name in inner_text, (
                    f"Navigation item '{name}' text mismatch, got: '{inner_text}'"
                )
                print(f"  ✓ Navigation item '{name}' is available")

            # Check Logout link
            logout_selector = 'a[href="/logout"]'
            assert page.wait_for_selector(logout_selector, timeout=5000) is not None, (
                "Logout link not found"
            )
            print("  ✓ Navigation item 'Logout' is available")

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
    run_nav_test()
