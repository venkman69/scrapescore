"""
End-to-end Playwright test: Create a profile via the Profiles page.
Expects the application to be already running.
"""

import sys
import os
import json
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

SAMPLE_RESUME = """firstname lastname, CISSP
Vienna, VA | 123-456-7890 | email@example.com | http://pii_replaced_example.com

Cybersecurity Executive | IAM & AppSec Transformation
A technically-grounded Cybersecurity Executive with 20+ years of progressive leadership experience specializing in the strategic
execution of large-scale, complex security transformations. A recognized leader who thrives in ambiguity, expertly translating
business objectives into clear, accelerated technical delivery roadmaps. Proven expertise driving critical uplifts in security posture
across global organizations, with deep domain focus in Identity and Access Management (IAM) transformation, enterprise Risk and
Fraud Services, Edge security (including Credential Validation Attack mitigation), and full-lifecycle Application Security (AppSec).
Adept at managing multi-faceted projects and achieving impactful results by partnering effectively with both business and technology
owners.

Technical Profile

Identity & Access Management (IAM): MFA, Zero Trust, OAuth/OIDC, SAML, FIDO (U2F/UAF), Federation, SSO
Modernization & Migration (ForgeRock, PingFederate, SiteMinder), Kerberos, Agent/Agentless Architecture, RBAC/ABAC
entitlement and authorization.

Application Security (AppSec): SDLC integration, SAST/DAST/SCA tooling, threat modeling, secure code review, DevSecOps
pipelines, container security, API security testing.

Leadership & Strategy: Executive briefings, board-level reporting, budget management ($5M+), vendor management,
cross-functional team leadership (15+ direct/indirect reports), talent development, M&A security due diligence.
"""

TEST_PROFILE_NAME = "Test Profile E2E"


def run_create_profile_test():
    print("=" * 70)
    print("E2E Test: Create Profile")
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
            context.add_cookies([
                {
                    "name": "session_",
                    "value": session_cookie,
                    "domain": "localhost",
                    "path": "/",
                    "httpOnly": False,
                    "secure": False,
                }
            ])

            page = context.new_page()

            # Navigate to home first
            print(f"\n3. Navigating to {BASE_URL}/...")
            page.goto(f"{BASE_URL}/")
            page.wait_for_selector("text=Welcome, Test User", timeout=10000)
            print("  ✓ Logged in")

            # Click Profiles in nav
            print("\n4. Clicking Profiles nav item...")
            page.click('a[href="/profiles"]')
            page.wait_for_selector("text=Profiles", timeout=10000)
            print("  ✓ Profiles page loaded")

            # Click the + create button
            print("\n5. Clicking Create Profile button...")
            create_btn = page.locator('[title="Create New Profile"]')
            create_btn.click()
            page.wait_for_selector("text=Create Profile", timeout=10000)
            print("  ✓ Create Profile form loaded")

            # Fill in profile name
            print("\n6. Filling profile form...")
            page.fill('#profile_name', TEST_PROFILE_NAME)
            print("  ✓ Profile name filled")

            # Fill resume textarea
            page.fill('#resume_textarea', SAMPLE_RESUME)
            print("  ✓ Resume filled")

            # Fill desired role description
            page.fill('#desired_role_description', 'Senior Principal Cybersecurity Engineer')
            print("  ✓ Desired role description filled")

            # Check US Citizen checkbox
            us_citizen = page.locator('#us_citizen')
            if not us_citizen.is_checked():
                us_citizen.check()
            print("  ✓ US Citizen checked")

            # Select Security Clearance (uk-select web component wraps a hidden <select>)
            # Set value on the hidden native select and dispatch uk-select:input event
            page.evaluate("""() => {
                const wrapper = document.querySelector('#security_clearance');
                const nativeSelect = wrapper.querySelector('select');
                if (nativeSelect) {
                    nativeSelect.value = 'Top Secret';
                    nativeSelect.dispatchEvent(new Event('change', { bubbles: true }));
                }
                // Also dispatch the uk-select:input event that MonsterUI listens for
                wrapper.dispatchEvent(new CustomEvent('uk-select:input', {
                    detail: { value: 'Top Secret' },
                    bubbles: true,
                }));
            }""")
            page.wait_for_timeout(500)
            print("  ✓ Security clearance set to Top Secret")

            # Wait for autosave to fire (triggered by change events)
            print("\n7. Waiting for autosave...")
            page.wait_for_timeout(2000)

            # Click Back to Profiles to verify the profile was saved
            print("\n8. Navigating back to profiles list...")
            page.click('text=Back to Profiles')
            page.wait_for_selector("text=Profiles", timeout=10000)
            page.wait_for_timeout(1000)

            # Verify the new profile appears in the list
            print("\n9. Verifying profile was created...")
            profile_card = page.locator(f'text="{TEST_PROFILE_NAME}"')
            if profile_card.count() > 0:
                print(f"  ✓ Profile '{TEST_PROFILE_NAME}' found in list")
            else:
                page.screenshot(path="profile_create_failure.png")
                print(f"\n✗ Profile '{TEST_PROFILE_NAME}' NOT found in list")
                print("  Screenshot saved to profile_create_failure.png")
                browser.close()
                sys.exit(1)

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
    run_create_profile_test()
