import json
import logging
import re
import sqlite3
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from pathlib import Path

import typer
from dotenv import load_dotenv

from scrapescore import db_setup as database_setup
from scrapescore.batch import cleanup
from scrapescore.db import (
    build_job_finder_config,
    get_all_users_with_keywords,
    get_all_users_with_scraper_configs,
    get_default_profile,
)
from scrapescore.lib import gemini_ai_runner, utils
from scrapescore.lib.config import APP_CONFIG, get_storage_dir_config
from scrapescore.lib.models import ClearanceStatus
from scrapescore.lib.systemd_notifier import SystemdNotifier
from jobspy import scrape_jobs

logger = logging.getLogger(__name__)

# Import setup_database from database_setup module
init_db = database_setup.setup_database

_SCRAPER_PARAM_KEYS = ["workday_params", "oraclecloud_params", "eightfold_params", "usajobs_params"]


def _build_user_config(base_config: dict, owning_user: str, profile: dict) -> dict:
    """Build a per-user scrape config from the DB profile and scraper configs only.

    No YAML fallbacks for user-specific fields — all data must come from the profile.
    """
    try:
        scraper_params = build_job_finder_config(owning_user)
        logger.info(
            f"DB scraper config for {owning_user}: "
            + ", ".join(
                f"{k}={len(scraper_params.get(k, {}))}" for k in _SCRAPER_PARAM_KEYS
            )
        )
    except Exception as e:
        logger.warning(f"Failed to load DB scraper config for {owning_user}: {e}.")
        scraper_params = {}

    return {
        **base_config,
        **{k: scraper_params.get(k, {}) for k in _SCRAPER_PARAM_KEYS},
        "keywords": profile.get("keywords", []),
        "location": profile.get("location", ""),
        "desired_role_description": profile.get("desired_role_description", ""),
        "reject_job_titles": profile.get("reject_job_titles", []),
        "us_citizen": profile.get("us_citizen", True),
        "security_clearance": profile.get("security_clearance", "No"),
    }


def check_security_clearance(description: str, title: str) -> int:
    """
    Checks if the job description or titlerequires an active security clearance.
    Looks for 'current' or 'active' and clearance terms in the same sentence.
    Returns 1 if found, 0 otherwise.
    """

    # Split text into sentences (simplistic split on .?! but respecting common abbreviations is harder,
    # but for this purpose simple split is usually enough or we can use regex for the whole logic)
    # The requirement is "typically in the same sentence".
    # We can search for the pattern: (current|active) ... (clearance|TS/SCI|public trust)
    # OR (clearance|TS/SCI|public trust) ... (current|active)
    # ensuring no sentence terminators [.?!] are in between '...'.

    flags = re.IGNORECASE
    keywords = r"(security clearance|TS/SCI|public trust|secret|top.secret)"
    status = r"(current|active|required|requirement|requires)"

    pattern1 = rf"\b{status}\s+(?:\S+\s+){{0,10}}{keywords}\b"

    if description:
        match = re.search(pattern1, description, flags)
        if match:
            logger.info(
                f"Found security clearance requirement in description of this title: {title}: {match.group()}"
            )
            return 1

    if title:
        status = r"(current|active|with)"
        pattern1 = rf"\b{status}.*{keywords}\b"
        match = re.search(pattern1, title, flags)
        if match:
            logger.info(
                f"Found security clearance requirement in the title: {title}: {match.group()}"
            )
            return 1

    return 0


def reject_job_titles(title: str, config: dict) -> bool:
    reject_list = config.get("reject_job_titles", [])
    for word in reject_list:
        if word in title.lower():
            logger.warning(f"Rejecting job with title: {title} matching {word}")
            return True
    return False


def save_jobs(conn, jobs_df, owning_user: str = "", search_term: str = "") -> tuple[int, int, int]:
    cursor = conn.cursor()
    # Prepare data for insertion
    # Ensure all columns exist in the dataframe, if not, fill with valid empty values
    expected_cols = [
        "job_url",
        "site",
        "title",
        "company",
        "location",
        "job_type",
        "security_clearance_required",
        "date_posted",
        "interval",
        "min_amount",
        "max_amount",
        "currency",
        "is_remote",
        "num_urgent_words",
        "benefits",
        "emails",
        "description",
        "title_compatibility_score",
    ]

    for col in expected_cols:
        if col not in jobs_df.columns:
            jobs_df[col] = ""

    # Convert dataframe to list of tuples for insertion
    # We need to be careful with column order matching the insert statement

    saved_count = 0
    no_jd_skipped_count = 0
    job_exists_count = 0
    for _, row in jobs_df.iterrows():
        # Handle potential None/NaN values
        row = row.fillna("")
        title = str(row["title"])
        job_url = str(row["job_url"])

        # Check if job_url already exists for this user
        cursor.execute(
            "SELECT 1 FROM job_details WHERE job_url = ? AND owning_user = ?",
            (job_url, owning_user),
        )
        if cursor.fetchone():
            job_exists_count += 1
            logger.info(f"Skipping duplicate job: {job_url}")
            continue

        description = str(row["description"])
        if not description or description.strip() == "":
            no_jd_skipped_count += 1
            logger.warning(f"job with empty description: {job_url}")

        security_clearance = check_security_clearance(description, title)

        # Handle date_posted being None or empty
        today_str = datetime.now().strftime("%Y-%m-%d")
        if row["date_posted"] is None or str(row["date_posted"]).strip() == "":
            date_posted_value = today_str
        else:
            date_posted_value = str(row["date_posted"])

        cursor.execute(
            """
            INSERT INTO job_details (
                job_url, site, title, company, location, job_type,
                date_posted, interval, min_amount, max_amount, currency,
                is_remote, num_urgent_words, benefits, emails, description,
                job_score, job_score_json, security_clearance_required,
                usage_metrics, review_status, title_compatibility_score, date_created,
                owning_user, search_term
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                job_url,
                str(row["site"]),
                str(row["title"]),
                str(row["company"]),
                str(row["location"]),
                str(row["job_type"]),
                date_posted_value,
                str(row["interval"]),
                str(row["min_amount"]),
                str(row["max_amount"]),
                str(row["currency"]),
                str(row["is_remote"]),
                str(row["num_urgent_words"]),
                str(row["benefits"]),
                str(row["emails"]),
                description,
                0,
                "{}",
                security_clearance,
                "",
                "not_reviewed",
                str(row["title_compatibility_score"]),
                today_str,
                owning_user,
                search_term,
            ),
        )
        saved_count += 1

    conn.commit()
    logger.info(
        f"""SAVE INFO *** :
        Saved {saved_count} new jobs to database. 
        Jobs with no jd {no_jd_skipped_count}. 
        Jobs already in db {job_exists_count}."""
    )
    return saved_count, no_jd_skipped_count, job_exists_count


def update_security_clearance_adhoc(conn):
    """
    Ad-hoc function to update security_clearance_required for existing records.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT id, description, title FROM job_details")
    rows = cursor.fetchall()
    logger.info("*** BEGIN ADHOC CLEARANCE update ***")
    updated_count = 0
    for row in rows:
        job_id, description, title = row
        if description or title:
            req_clearance = check_security_clearance(description, title)
            if req_clearance:
                cursor.execute(
                    "UPDATE job_details SET security_clearance_required = ? WHERE id = ?",
                    (req_clearance, job_id),
                )
                updated_count += 1

    conn.commit()
    if updated_count > 0:
        logger.info(
            f"Updated {updated_count} existing jobs with security clearance requirement."
        )

    logger.info("*** END ADHOC CLEARANCE update ***")


def update_compatibility_scores_adhoc(conn, config):
    """
    Update missing title_compatibility_score for existing records.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, title FROM job_details WHERE title_compatibility_score IS NULL OR title_compatibility_score = ''"
    )
    rows = cursor.fetchall()

    if not rows:
        return

    logger.info(f"Found {len(rows)} jobs missing title compatibility score.")
    desired_role_description = config.get("desired_role_description", "")

    # Extract all titles for analysis
    titles_to_analyze = [row[1] for row in rows]
    results = job_title_compatibility(titles_to_analyze, desired_role_description)

    # Map results for batch update
    # results is a list[dict] returned by job_title_compatibility, or empty list on error
    score_map = {res["job_title"]: res["score"] for res in results}

    updated_count = 0
    for job_id, title in rows:
        score = score_map.get(title, "low")
        cursor.execute(
            "UPDATE job_details SET title_compatibility_score = ? WHERE id = ?",
            (score, job_id),
        )
        updated_count += 1

    conn.commit()
    if updated_count > 0:
        logger.info(f"Updated {updated_count} existing jobs with compatibility score.")


def get_latest_resume(resume_dir: Path) -> str | None:
    if not resume_dir.exists():
        return None

    # Find all pdf/md/txt files
    files = (
        list(resume_dir.glob("*.pdf"))
        + list(resume_dir.glob("*.md"))
        + list(resume_dir.glob("*.txt"))
    )
    if not files:
        return None

    # Sort by modification time
    latest_file = max(files, key=lambda p: p.stat().st_mtime)
    return str(latest_file)



def score_jobs_batch(conn, config):
    logger.info("\n--- Starting Batch Job Scoring ---")
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, job_url, description, title, company, site, owning_user "
        "FROM job_details WHERE security_clearance_required = 0 AND job_score = 0"
    )
    rows = cursor.fetchall()
    logger.info(f"Found {len(rows)} jobs eligible for scoring.")

    jobs_by_user: dict[str, list] = {}
    for row in rows:
        jobs_by_user.setdefault(row[6], []).append(row)

    for owning_user, user_rows in jobs_by_user.items():
        profile = get_default_profile(owning_user)
        if not profile:
            logger.warning(f"No default profile for {owning_user}, skipping {len(user_rows)} jobs.")
            continue

        resume_text = profile.get("resume", "")
        if not resume_text:
            logger.warning(f"No resume in profile for {owning_user}, skipping.")
            continue
        resume_text = utils.remove_pii(resume_text)

        desired_role_description = profile.get("desired_role_description", "")
        if not desired_role_description:
            logger.warning(f"No desired_role_description in profile for {owning_user}, skipping.")
            continue

        us_citizen = bool(profile.get("us_citizen", True))
        security_clearance = profile.get("security_clearance", "No")

        for row in user_rows:
            job_id, job_url, description, title, company, site, _ = row
            logger.info(f"Scoring Job ID: {job_id} ({title} at {company})")

            job_details_dict = {
                "job_url": job_url,
                "job_id": str(job_id),
                "job_source": site,
            }

            try:
                result, _, usage_metrics = gemini_ai_runner.ats_score_analyzer_gemini(
                    job_description=description,
                    resume=resume_text,
                    desired_role_description=desired_role_description,
                    job_details=job_details_dict,
                    us_citizen=us_citizen,
                    security_clearance=security_clearance,
                )

                result_json = result
                final_job_score = 0
                if isinstance(result_json, dict):
                    if result_json.get("schema_version") == "1.0":
                        final_job_score = result_json.get("ats_score_estimate", {}).get("total_overall_score", 0)
                        logger.info(f"ATS Score (v1.0): {final_job_score}, Decision: {result_json.get('decision', 'N/A')}")
                    else:
                        score_data = result_json.get("score", {})
                        if isinstance(score_data, dict):
                            final_score = score_data.get("final_score", 0)
                            final_job_score = int(final_score * 100)
                            confirm_final_score = (
                                score_data.get("desired_job_score", 0)
                                + score_data.get("required_skill_match_score", 0)
                                + score_data.get("preferred_skill_match_score", 0)
                            )
                            if confirm_final_score != final_score:
                                logger.error(f"Final score mismatch. Expected: {confirm_final_score}, Actual: {final_score}")
                                score_data["final_score"] = confirm_final_score
                                result_json["score"] = score_data

                security_clearance_required = 0
                if isinstance(result_json, dict):
                    if result_json.get("schema_version") == "1.0":
                        clearance_status = result_json.get("clearance_assessment", {}).get("status", "")
                        if clearance_status == ClearanceStatus.ACTIVE_REQUIRED.value:
                            security_clearance_required = 1
                    else:
                        security_clearances = result_json.get("security_clearances", {})
                        if isinstance(security_clearances, dict):
                            if security_clearances.get("matching_security_clearances_count", 0) > 0 or \
                               security_clearances.get("missing_security_clearances_count", 0) > 0:
                                security_clearance_required = 1

                cursor.execute(
                    "UPDATE job_details SET job_score = ?, job_score_json = ?, usage_metrics = ?, security_clearance_required = ? WHERE id = ?",
                    (final_job_score, json.dumps(result_json), json.dumps(usage_metrics), security_clearance_required, job_id),
                )
                conn.commit()
                logger.info(f"  -> Scored: {final_job_score}")

            except Exception as e:
                logger.error(f"  -> Failed to score job {job_id}: {e}")


def job_title_compatibility(
    job_titles: list[str], desired_role_description: str
) -> list[dict]:
    """
    Evaluate job titles compatibility with the desired role description.
    """
    if not job_titles:
        return []

    # Filter out empty or None titles
    valid_titles = [t for t in job_titles if t and isinstance(t, str)]
    if not valid_titles:
        return []

    unique_titles = list(set(valid_titles))
    logger.info(f"Analyzing compatibility for {len(unique_titles)} unique titles...")

    results = gemini_ai_runner.hr_title_analyzer_gemini(
        unique_titles, desired_role_description
    )

    return results


def score_high_jobs(conn, config):
    logger.info("--- Starting Auto-Scoring for High Compatibility Jobs (JIRA-033) ---")

    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, job_url, description, title, company, site, date_posted, owning_user "
        "FROM job_details WHERE security_clearance_required = 0 AND job_score = 0 "
        "AND job_score_json = '{}' AND title_compatibility_score = 'high'"
    )
    rows = cursor.fetchall()

    # Keep only jobs posted within the last 7 days
    cutoff_date = datetime.now().date() - timedelta(days=7)
    jobs_to_score = []
    for row in rows:
        date_posted = row[6]
        if not date_posted:
            continue
        try:
            job_date = datetime.strptime(str(date_posted).strip().split(" ")[0], "%Y-%m-%d").date()
            if job_date >= cutoff_date:
                jobs_to_score.append(row)
        except Exception as e:
            logger.warning(f"Could not parse date '{date_posted}' for job {row[0]}: {e}")

    if not jobs_to_score:
        logger.info("No high compatibility jobs found within the last 7 days to score.")
        return

    logger.info(f"Found {len(jobs_to_score)} high compatibility jobs from last 7 days to score.")

    jobs_by_user: dict[str, list] = {}
    for row in jobs_to_score:
        jobs_by_user.setdefault(row[7], []).append(row)

    for owning_user, user_rows in jobs_by_user.items():
        profile = get_default_profile(owning_user)
        if not profile:
            logger.warning(f"No default profile for {owning_user}, skipping {len(user_rows)} jobs.")
            continue

        resume_text = profile.get("resume", "")
        if not resume_text:
            logger.warning(f"No resume in profile for {owning_user}, skipping.")
            continue

        desired_role_description = profile.get("desired_role_description", "")
        if not desired_role_description:
            logger.warning(f"No desired_role_description in profile for {owning_user}, skipping.")
            continue

        us_citizen = bool(profile.get("us_citizen", True))
        security_clearance = profile.get("security_clearance", "No")

        for row in user_rows:
            job_id, job_url, description, title, company, site, date_posted, _ = row
            logger.info(f"Auto-Scoring Job ID: {job_id} ({title} at {company})")
            if not description or description.strip() == "":
                logger.warning(f"Job ID {job_id} has empty description, skipping.")
                continue

            job_details_dict = {
                "job_url": job_url,
                "job_id": str(job_id),
                "job_source": site,
            }

            try:
                result, _, usage_metrics = gemini_ai_runner.ats_score_analyzer_gemini(
                    job_description=description,
                    resume=resume_text,
                    desired_role_description=desired_role_description,
                    job_details=job_details_dict,
                    us_citizen=us_citizen,
                    security_clearance=security_clearance,
                )

                result_json = result
                final_job_score = 0
                if isinstance(result_json, dict):
                    if result_json.get("schema_version") == "1.0":
                        final_job_score = result_json.get("ats_score_estimate", {}).get("total_overall_score", 0)
                    else:
                        score_data = result_json.get("score", {})
                        if isinstance(score_data, dict):
                            final_job_score = int(score_data.get("final_score", 0) * 100)

                security_clearance_required = 0
                if isinstance(result_json, dict):
                    if result_json.get("schema_version") == "1.0":
                        clearance_status = result_json.get("clearance_assessment", {}).get("status", "")
                        if clearance_status == ClearanceStatus.ACTIVE_REQUIRED.value:
                            security_clearance_required = 1
                    else:
                        security_clearances = result_json.get("security_clearances", {})
                        if isinstance(security_clearances, dict):
                            if security_clearances.get("matching_security_clearances_count", 0) > 0 or \
                               security_clearances.get("missing_security_clearances_count", 0) > 0:
                                security_clearance_required = 1

                cursor.execute(
                    "UPDATE job_details SET job_score = ?, job_score_json = ?, usage_metrics = ?, security_clearance_required = ? WHERE id = ?",
                    (final_job_score, json.dumps(result_json), json.dumps(usage_metrics), security_clearance_required, job_id),
                )
                conn.commit()
                logger.info(f"  -> Scored: {final_job_score}")

            except Exception as e:
                logger.error(f"  -> Failed to score job {job_id}: {e}")


def main(
    enable_scoring: bool = typer.Option(
        False, "--enable-scoring", help="Enable batch scoring of jobs"
    ),
    skip_scrape: bool = typer.Option(
        False, "--skip-scrape", help="Skip scraping and only process existing results"
    ),
):
    base_dir = Path(__file__).parent
    # print command line options
    logger.info(f"enable_scoring: {enable_scoring}")
    logger.info(f"skip_scrape: {skip_scrape}")
    config = APP_CONFIG

    db_path = get_storage_dir_config("job_finder_db_path")
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    logger.info(f"Initializing database at {db_path}")
    conn = init_db(db_path)

    notifier = SystemdNotifier()
    notifier.notify_ready()
    notifier.notify_status("Initialized. Starting ad-hoc updates.")

    # Run ad-hoc update on every run to ensure data consistency or consistency after code update
    update_security_clearance_adhoc(conn)
    update_compatibility_scores_adhoc(conn, config)

    # Scrape if requested
    total_jobs_found = 0
    jobs_with_no_jd = 0
    if not skip_scrape:
        run_id = str(uuid.uuid4())
        logger.info(f"Starting job finder run with ID: {run_id}")

        users = get_all_users_with_keywords()
        if not users:
            logger.warning(
                "*** NO USERS WITH A DEFAULT PROFILE AND KEYWORDS FOUND ***"
            )
        else:
            logger.info(f"*** SCRAPING for {len(users)} user(s): {users} ***")

        for owning_user in users:
            profile = get_default_profile(owning_user)
            if not profile:
                logger.warning(f"No default profile for {owning_user}, skipping.")
                continue

            user_config = _build_user_config(config, owning_user, profile)

            keywords_config = user_config.get("keywords", [])
            if isinstance(keywords_config, str):
                keywords_list = [keywords_config]
            else:
                keywords_list = keywords_config

            if not keywords_list:
                logger.warning(f"No keywords for {owning_user}, skipping.")
                continue

            logger.info(
                f"\n=== Scraping for user: {owning_user} ({len(keywords_list)} keywords) ==="
            )
            notifier.notify_status(
                f"Scraping {owning_user}: {len(keywords_list)} keywords..."
            )

            for keyword in keywords_list:
                try:
                    logger.info(
                        f"\n--- Scraping for keyword: {keyword} ({owning_user}) ---"
                    )
                    google_search_term = (
                        f"{keyword} {user_config['location']} since yesterday"
                    )
                    results_wanted = config.get("scraper", {}).get("results_wanted", 20)
                    scrape_timeout = config.get("scraper", {}).get("scrape_timeout", 30)
                    scrape_kwargs = dict(
                        site_name=user_config["site_names"],
                        search_term=keyword,
                        google_search_term=google_search_term,
                        location=user_config["location"],
                        hours_old=user_config["hours_old"],
                        results_wanted=results_wanted,
                        country_indeed="USA",
                        run_id=run_id,
                        job_finder_config=user_config,
                    )
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(scrape_jobs, **scrape_kwargs)
                        try:
                            jobs = future.result(timeout=scrape_timeout)
                        except FuturesTimeoutError:
                            logger.error(
                                f"scrape_jobs timed out after {scrape_timeout}s for keyword '{keyword}' — skipping."
                            )
                            future.cancel()
                            continue

                    logger.info(f"Found {len(jobs)} jobs for {keyword}")

                    if not jobs.empty:
                        logger.info("Evaluating job title compatibility...")
                        desired_role_description = user_config.get(
                            "desired_role_description", ""
                        )

                        jobs = jobs[
                            ~jobs["title"].apply(
                                lambda x: reject_job_titles(x, user_config)
                            )
                        ]

                        job_titles = jobs["title"].tolist()
                        try:
                            compatibility_results = job_title_compatibility(
                                job_titles, desired_role_description
                            )
                            score_map = {
                                res["job_title"]: res["score"]
                                for res in compatibility_results
                            }
                        except Exception as compat_e:
                            logger.warning(
                                f"job_title_compatibility failed for keyword '{keyword}': {compat_e}. "
                                "Defaulting all title scores to 'low'."
                            )
                            score_map = {}

                        jobs["title_compatibility_score"] = jobs["title"].map(
                            lambda x: score_map.get(x, "low")
                        )

                        saved_jobs_count, no_jd_count, job_exists_count = save_jobs(
                            conn, jobs, owning_user, search_term=keyword
                        )
                        total_jobs_found += saved_jobs_count
                        jobs_with_no_jd += no_jd_count
                    else:
                        logger.warning(f"No jobs found for {keyword}.")

                    notifier.notify_status(
                        f"{owning_user} / '{keyword}'. Total new jobs so far: {total_jobs_found}"
                    )
                    notifier.notify_watchdog()

                except Exception as e:
                    logger.error(
                        f"Error scraping jobs for keyword '{keyword}' (user: {owning_user}): {e}"
                    )
                    logger.error(traceback.format_exc())

    # Auto-score high compatibility jobs (JIRA-033)
    notifier.notify_status("Scraping complete. Auto-scoring high compatibility jobs...")
    score_high_jobs(conn, config)

    # Batch Score if requested
    if enable_scoring:
        logger.info("Scoring jobs...")
        notifier.notify_status("Batch scoring jobs...")
        score_jobs_batch(conn, config)

    # Cleanup old job records (if configured)
    cleanup.cleanup_old_jobs(conn, config)

    notifier.notify_status(
        f"Done. New jobs: {total_jobs_found}, No JD: {jobs_with_no_jd}"
    )
    notifier.notify_stopping()
    conn.close()
    if not skip_scrape:
        logger.info(
            f"** JOB FINDER STATUS **: new_jobs_found={total_jobs_found}, jobs_with_no_jd={jobs_with_no_jd}"
        )


if __name__ == "__main__":
    load_dotenv()
    logs_dir = Path(get_storage_dir_config("logs_dir"))
    utils.config_logger("job_finder.log", logs_dir)
    typer.run(main)
