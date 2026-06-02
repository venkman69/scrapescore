"""
Verification test for JIRA-100: FastHTML application with Google OAuth.

Tests:
1. Root path redirects to login for unauthenticated users
2. Login page shows "Sign in with Google" link
3. Authenticated profiles page shows profile list with Create link
4. Create form loads with all expected fields
5. Edit form loads with existing profile data
6. Profile save creates a new profile

Usage:
    PYTHONPATH="./src:./src/lib" uv run python tests/verify_jira_100.py
"""
import subprocess
import sys
import time
import sqlite3
from pathlib import Path

# Add project src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent / "src" / "lib"))

from job_scorev2.database_setup import get_db_connection


def setup_test_secrets():
    """Create dummy OAuth secrets file if not present."""
    secrets_path = Path("./work/job_finder/client_secrets.json")
    if not secrets_path.exists():
        secrets_path.parent.mkdir(parents=True, exist_ok=True)
        secrets_path.write_text(
            '{"web":{"client_id":"dummy.apps.googleusercontent.com",'
            '"project_id":"test-project","auth_uri":"https://accounts.google.com/o/oauth2/auth",'
            '"token_uri":"https://oauth2.googleapis.com/token",'
            '"client_secret":"dummy-secret"}}'
        )
    return secrets_path


def cleanup_test_data():
    """Remove test profiles from the database."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM job_profiles WHERE owning_user = ?",
        ("test-auth-user",)
    )
    conn.commit()
    conn.close()


def insert_test_profile():
    """Insert a test profile for edit testing."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT OR IGNORE INTO job_profiles
        (profile_name, resume, desired_role_description, location,
         security_clearance, owning_user, keywords, reject_job_titles,
         additional_skills, us_citizen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        "Test Profile Edit", "Test resume content",
        "Security Architect", '["McLean, VA"]', "Secret",
        "test-auth-user", '["IAM"]', '["analyst"]',
        '["Python", "AWS"]', 1
    ))
    conn.commit()
    conn.close()


def start_app():
    """Start the FastHTML app in background."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "job_score.app"],
        env={
            **dict(__import__("os").environ),
            "PYTHONPATH": "./src:./src/lib",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(3)
    return proc


def stop_app(proc):
    """Stop the FastHTML app."""
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def test_with_playwright():
    """Run Playwright-based UI tests."""
    from playwright.sync_api import sync_playwright

    test_results = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context()

        # Test 1: Login page
        page = context.new_page()
        page.goto("http://localhost:5001/login")
        assert page.title() == "Login", f"Expected 'Login', got '{page.title()}'"

        # Wait for page to fully load with MonsterUI Card
        time.sleep(2)
        # Sign in button has onclick with Google URL
        sign_in_button = page.locator("button[onclick*='accounts.google.com']")
        assert sign_in_button.count() == 1, "Sign in with Google button not found"
        test_results.append(("Login page: shows Google sign-in button", "PASS"))

        # Test 2: Root redirects to login
        page.goto("http://localhost:5001/")
        # Should end up on login page
        time.sleep(1)
        assert "/login" in page.url, f"Expected redirect to /login, got {page.url}"
        test_results.append(("Root path: redirects to /login", "PASS"))

        # Test 3: Test authenticated pages via session injection
        # We use the Starlette test client approach by setting session data directly
        # Playwright can't easily set Starlette session cookies (they're signed)
        # Instead, we'll test the app's internal routes via the test client

        browser.close()

    return test_results


def _make_auth_cookie() -> str:
    """Create a signed session cookie for test-auth-user."""
    import itsdangerous, json, base64
    from fasthtml.core import get_key

    secret_key = get_key()
    signer = itsdangerous.TimestampSigner(secret_key)
    session_data = {"auth": "test-auth-user", "user_info": {
        "email": "test@example.com", "name": "Test User"
    }}
    serialized = base64.b64encode(json.dumps(session_data).encode()).decode()
    return signer.sign(serialized).decode()


def test_with_test_client():
    """Test authenticated routes using Starlette test client."""
    from starlette.testclient import TestClient
    from job_score.app import app

    client = TestClient(app)
    test_results = []
    auth_cookie = _make_auth_cookie()

    # Test 1: Unauthenticated root redirects to login
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303, f"Expected 303, got {response.status_code}"
    assert "/login" in response.headers.get("location", ""), "Root should redirect to login"
    test_results.append(("Unauthenticated / redirects to /login", "PASS"))

    # Test 2: Login page content
    response = client.get("/login")
    assert response.status_code == 200
    assert "Sign in with Google" in response.text, "Google sign-in link missing"
    test_results.append(("Login page: Google sign-in link present", "PASS"))

    # Test 3: Authenticated profiles page
    response = client.get("/profiles/", cookies={"session_": auth_cookie})
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    assert "Test Profile Edit" in response.text, f"Test profile not shown in listing"
    assert "Create Profile" in response.text, "Create Profile link missing"
    test_results.append(("Profiles page: shows profile listing and Create link", "PASS"))

    # Test 4: Edit form loads
    response = client.get("/profiles/edit/Test Profile Edit", cookies={"session_": auth_cookie})
    assert response.status_code == 200
    assert "Security Architect" in response.text, "Desired role not populated in edit form"
    # Note: Location is stored as JSON list, so we can't search for "McLean, VA" in the HTML
    test_results.append(("Edit form: loads with existing data", "PASS"))

    # Test 5: Create form loads
    response = client.get("/profiles/create", cookies={"session_": auth_cookie})
    assert response.status_code == 200
    assert "Profile Name" in response.text, "Profile Name field missing"
    assert "Resume" in response.text, "Resume field missing"
    assert "Location" in response.text, "Location field missing"
    test_results.append(("Create form: has all expected fields", "PASS"))

    # Test 6: Save a new profile
    response = client.post("/profiles/save", data={
        "profile_name": "New Test Profile",
        "resume": "My resume",
        "desired_role_description": "Security Engineer",
        "additional_skills": "Python",
        "us_citizen": "1",
        "security_clearance": "None",
        "keywords": '["python"]',
        "location": "Reston, VA",
        "reject_job_titles": '["analyst"]',
    }, follow_redirects=False, cookies={"session_": auth_cookie})
    assert response.status_code in (303, 302), f"Expected redirect, got {response.status_code}"

    # Verify the profile was saved
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM job_profiles WHERE profile_name = ? AND owning_user = ?",
        ("New Test Profile", "test-auth-user")
    )
    row = cursor.fetchone()
    conn.close()
    assert row is not None, "New profile was not saved to database"
    test_results.append(("Save profile: creates new profile in database", "PASS"))

    return test_results


def main():
    print("=" * 60)
    print("JIRA-100 Verification Tests")
    print("=" * 60)

    # Setup
    setup_test_secrets()
    cleanup_test_data()
    insert_test_profile()

    # Run test client tests (doesn't need running server)
    print("\n--- Test Client Tests ---")
    try:
        results = test_with_test_client()
        for name, status in results:
            print(f"  [{status}] {name}")
    except Exception as e:
        print(f"  [FAIL] Test client tests failed: {e}")
        import traceback
        traceback.print_exc()

    # Run Playwright tests (needs running server)
    print("\n--- Playwright Tests ---")
    proc = None
    try:
        proc = start_app()
        results = test_with_playwright()
        for name, status in results:
            print(f"  [{status}] {name}")
    except Exception as e:
        print(f"  [FAIL] Playwright tests failed: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if proc:
            stop_app(proc)

    # Cleanup
    cleanup_test_data()

    print("\n" + "=" * 60)
    print("Verification complete.")
    print("=" * 60)


if __name__ == "__main__":
    main()
