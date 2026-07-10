import json
import logging
import multiprocessing as mp
import os
import queue
import re
import signal
import sqlite3
import traceback
import uuid
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


def _scrape_jobs_worker(result_queue, scrape_kwargs, logs_dir):
    """Run scrape_jobs in a child process and return its DataFrame via a Queue.

    Runs in a killable subprocess because scrape_jobs can hang indefinitely inside a
    thread (e.g. Playwright's sync teardown in the Google/ZipRecruiter scrapers), and a
    thread cannot be cancelled — only a real OS process can be killed. The parent kills
    this whole process tree on timeout.
    """
    # Own process group so the parent can kill the whole tree (incl. Playwright
    # node/chromium descendants) on timeout without touching the parent's group.
    try:
        os.setsid()
    except Exception:
        pass
    # Re-establish logging in the child: config_logger only runs under __main__, which
    # the spawn start method does not enter. Without this the per-site scrape log lines
    # would be lost.
    try:
        utils.config_logger("job_finder.log", logs_dir)
    except Exception:
        pass
    try:
        df = scrape_jobs(**scrape_kwargs)
        result_queue.put(("ok", df))
    except Exception as e:
        result_queue.put(("error", repr(e)))


def _kill_proc_tree(proc):
    """SIGKILL the subprocess and its descendants, tolerating the setsid race."""
    pid = proc.pid
    if pid is None:
        return
    try:
        # Only killpg if the child became its own group leader (setsid succeeded), so we
        # never signal the parent's own process group.
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    proc.join(timeout=5)


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


def save_jobs(conn, jobs_df, owning_user: str = "", search_term: str = "") -> tuple[int, int, int, dict[str, int]]:
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
    # Net-new jobs actually inserted, keyed by scraper enum value (the df "site" column,
    # e.g. "linkedin"/"workday" — same value stored in scraping_logs.scraper).
    saved_by_scraper: dict[str, int] = {}
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
        scraper_key = str(row["site"])
        saved_by_scraper[scraper_key] = saved_by_scraper.get(scraper_key, 0) + 1

    conn.commit()
    logger.info(
        f"""SAVE INFO *** :
        Saved {saved_count} new jobs to database. 
        Jobs with no jd {no_jd_skipped_count}. 
        Jobs already in db {job_exists_count}."""
    )
    return saved_count, no_jd_skipped_count, job_exists_count, saved_by_scraper


def _update_scraping_logs_net_new(
    conn, run_id: str, search_term: str, saved_by_scraper: dict[str, int]
) -> None:
    """Overwrite scraping_logs.jobs_found with net-new jobs saved (JIRA-095 follow-up).

    log_scraping_activity() (inside the scrape subprocess) initially writes jobs_found as
    the raw scraped count, before the per-user save/dedup runs. This reconciles it to the
    number of jobs actually inserted into job_details for this user, per scraper. The
    job_finder loop is sequential (per user, per keyword), so the only success=1 rows
    matching (run_id, search_term) are the current scrape's rows.

    Precision is per-scraper: for multi-site boards (Workday/Eightfold/Oracle) there are
    several sub-site rows for one scraper; the aggregate is placed on the lowest-id row and
    the rest set to 0 so the per-scraper sum stays exact (per-company is not tracked).

    A telemetry update must never break a scrape run, so failures are logged and swallowed.
    """
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, scraper FROM scraping_logs "
            "WHERE run_id = ? AND search_term = ? AND success = 1 ORDER BY id",
            (run_id, search_term),
        )
        rows = cursor.fetchall()
        if not rows:
            return

        # Group row ids by scraper (rows already ordered by id).
        ids_by_scraper: dict[str, list[int]] = {}
        for row_id, scraper in rows:
            ids_by_scraper.setdefault(scraper, []).append(row_id)

        updates: list[tuple[int, int]] = []
        for scraper, ids in ids_by_scraper.items():
            count = saved_by_scraper.get(scraper, 0)
            # Aggregate on the lowest-id row, 0 on the rest → per-scraper sum stays exact.
            for i, row_id in enumerate(ids):
                updates.append((count if i == 0 else 0, row_id))

        cursor.executemany(
            "UPDATE scraping_logs SET jobs_found = ? WHERE id = ?", updates
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to update scraping_logs net-new jobs_found: {e}")


def _mark_scrape_incomplete(
    conn, run_id: str, search_term: str, since_id, reason: str
) -> None:
    """Mark this keyword's per-site scraping_logs rows failed when nothing was delivered.

    The per-site rows are written *inside* the scrape subprocess as success=1 with a raw
    jobs_found count, before the parent ever saves anything. If the subprocess is then
    killed on timeout (or scrape_jobs raises), those jobs never reach save_jobs — yet the
    rows still read success=1 with a positive count, so a total outage looks healthy on the
    dashboard (exactly how the 30s-timeout regression hid itself). Rewrite the current
    scrape's rows to success=0, jobs_found=0 with an error so telemetry reflects that no
    jobs were actually delivered.

    Scoped to rows inserted after `since_id` for this (run_id, search_term) so a prior
    user's successful scrape of the same keyword in the same run is left untouched.
    Best-effort: telemetry must never break a scrape run, so failures are swallowed.
    """
    if since_id is None:
        return
    try:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE scraping_logs SET success = 0, jobs_found = 0, error_message = ? "
            "WHERE id > ? AND run_id = ? AND search_term = ? AND success = 1",
            (reason, since_id, run_id, search_term),
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Failed to mark scrape incomplete in scraping_logs: {e}")


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

    # Logs dir, passed to scrape subprocesses so their per-site logs reach job_finder.log.
    logs_dir = Path(get_storage_dir_config("logs_dir"))

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
                    # Run scrape_jobs in a killable subprocess. A thread-based timeout
                    # cannot stop a hung scrape (threads can't be cancelled), so on
                    # timeout we SIGKILL the whole process tree — including any stuck
                    # Playwright node/chromium drivers — and move on.
                    # Highest scraping_logs id before this scrape, so a timeout/error can
                    # scope its cleanup to only the rows this subprocess is about to write.
                    try:
                        _c = conn.cursor()
                        _c.execute("SELECT COALESCE(MAX(id), 0) FROM scraping_logs")
                        pre_scrape_max_id = _c.fetchone()[0]
                    except Exception:
                        pre_scrape_max_id = None
                    ctx = mp.get_context("spawn")
                    result_queue = ctx.Queue()
                    proc = ctx.Process(
                        target=_scrape_jobs_worker,
                        args=(result_queue, scrape_kwargs, logs_dir),
                        daemon=True,
                    )
                    proc.start()
                    logger.info(
                        f"Started scrape subprocess pid={proc.pid} for '{keyword}' "
                        f"(timeout={scrape_timeout}s)"
                    )
                    try:
                        status, payload = result_queue.get(timeout=scrape_timeout)
                    except queue.Empty:
                        logger.error(
                            f"scrape_jobs timed out after {scrape_timeout}s for keyword "
                            f"'{keyword}' (subprocess pid={proc.pid}); killing subprocess "
                            f"tree and skipping."
                        )
                        _kill_proc_tree(proc)
                        # The subprocess wrote per-site success rows before being killed,
                        # but nothing was saved — mark them failed so telemetry is honest.
                        _mark_scrape_incomplete(
                            conn,
                            run_id,
                            keyword,
                            pre_scrape_max_id,
                            f"scrape subprocess killed after {scrape_timeout}s timeout",
                        )
                        continue
                    finally:
                        # Never leave a child behind, even on the success path.
                        proc.join(timeout=5)
                        if proc.is_alive():
                            _kill_proc_tree(proc)

                    if status == "error":
                        logger.error(
                            f"scrape_jobs failed for '{keyword}': {payload} — skipping."
                        )
                        # Any per-site success rows written before the failure delivered
                        # nothing to save — mark them failed so telemetry is honest.
                        _mark_scrape_incomplete(
                            conn,
                            run_id,
                            keyword,
                            pre_scrape_max_id,
                            f"scrape_jobs failed: {payload}",
                        )
                        continue
                    jobs = payload

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

                        saved_jobs_count, no_jd_count, job_exists_count, saved_by_scraper = save_jobs(
                            conn, jobs, owning_user, search_term=keyword
                        )
                        # Reconcile scraping_logs.jobs_found from raw scraped count to the
                        # net-new jobs actually saved for this user.
                        _update_scraping_logs_net_new(
                            conn, run_id, keyword, saved_by_scraper
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
