"""
Database operations for job_score.

Database operations for job_score.
"""

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from scrapescore.db_setup import get_db_connection


def get_profiles_for_user(owning_user: str) -> list[dict]:
    """Get all profiles belonging to a user."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM job_profiles WHERE owning_user = ? ORDER BY date_updated DESC",
        (owning_user,),
    )
    rows = cursor.fetchall()
    conn.close()
    profiles = [dict(r) for r in rows]

    # Parse JSON fields
    for p in profiles:
        for field in ["additional_skills", "keywords", "reject_job_titles"]:
            if field in p and isinstance(p[field], str):
                try:
                    p[field] = json.loads(p[field]) if p[field] else []
                except json.JSONDecodeError:
                    p[field] = []
    return profiles


def get_profile(profile_name: str, owning_user: str) -> dict | None:
    """Get a single profile by name and owner."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT rowid, * FROM job_profiles WHERE profile_name = ? AND owning_user = ?",
        (profile_name, owning_user),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def save_profile(profile_data: dict):
    """Insert or update a profile. Uses UPSERT on profile_name."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO job_profiles (profile_name, resume, desired_role_description,
            additional_skills, us_citizen, security_clearance, keywords,
            location, reject_job_titles, owning_user, is_default, date_updated)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(profile_name) DO UPDATE SET
            resume=excluded.resume,
            desired_role_description=excluded.desired_role_description,
            additional_skills=excluded.additional_skills,
            us_citizen=excluded.us_citizen,
            security_clearance=excluded.security_clearance,
            keywords=excluded.keywords,
            location=excluded.location,
            reject_job_titles=excluded.reject_job_titles,
            is_default=excluded.is_default,
            date_updated=CURRENT_TIMESTAMP
    """,
        (
            profile_data["profile_name"],
            profile_data["resume"],
            profile_data["desired_role_description"],
            profile_data["additional_skills"],
            profile_data["us_citizen"],
            profile_data["security_clearance"],
            profile_data["keywords"],
            profile_data["location"],
            profile_data["reject_job_titles"],
            profile_data["owning_user"],
            profile_data.get("is_default", 0),
        ),
    )
    conn.commit()
    conn.close()


def update_profile_by_rowid(rowid: int, profile_data: dict):
    """Update all fields of an existing profile identified by rowid (allows renaming)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE job_profiles SET
            profile_name=?, resume=?, desired_role_description=?,
            additional_skills=?, us_citizen=?, security_clearance=?,
            keywords=?, location=?, reject_job_titles=?,
            date_updated=CURRENT_TIMESTAMP
           WHERE rowid=?""",
        (
            profile_data["profile_name"],
            profile_data["resume"],
            profile_data["desired_role_description"],
            profile_data["additional_skills"],
            profile_data["us_citizen"],
            profile_data["security_clearance"],
            profile_data["keywords"],
            profile_data["location"],
            profile_data["reject_job_titles"],
            rowid,
        ),
    )
    conn.commit()
    conn.close()


def delete_profile(profile_name: str, owning_user: str):
    """Delete a profile by name and owner."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM job_profiles WHERE profile_name = ? AND owning_user = ?",
        (profile_name, owning_user),
    )
    conn.commit()
    conn.close()


def set_default_profile(profile_name: str, owning_user: str):
    """Exclusively set a profile as default by unsetting all others for that user."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        # Unset all
        cursor.execute(
            "UPDATE job_profiles SET is_default = 0 WHERE owning_user = ?",
            (owning_user,),
        )
        # Set target
        cursor.execute(
            "UPDATE job_profiles SET is_default = 1 WHERE profile_name = ? AND owning_user = ?",
            (profile_name, owning_user),
        )
        conn.commit()
    finally:
        conn.close()


# --- Scraper Config CRUD ---

def save_scraper_config(config_data: dict):
    """Insert or update a scraper config. Uses UPSERT on (config_key, owning_user)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO scraper_configs (config_key, config_type, company_name, config_json, owning_user, date_updated)
        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(config_key, owning_user) DO UPDATE SET
            config_type=excluded.config_type,
            company_name=excluded.company_name,
            config_json=excluded.config_json,
            date_updated=CURRENT_TIMESTAMP
    """,
        (
            config_data["config_key"],
            config_data["config_type"],
            config_data["company_name"],
            config_data["config_json"],
            config_data["owning_user"],
        ),
    )
    conn.commit()
    conn.close()


def get_scraper_configs_for_user(owning_user: str) -> list[dict]:
    """Get all scraper configs for a user, parsed with config_json as dict."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM scraper_configs WHERE owning_user = ? ORDER BY config_type, company_name",
        (owning_user,),
    )
    rows = cursor.fetchall()
    conn.close()
    results = []
    for r in rows:
        d = dict(r)
        try:
            d["config_json"] = json.loads(d.get("config_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            d["config_json"] = {}
        results.append(d)
    return results


def get_scraper_config(config_key: str, owning_user: str) -> dict | None:
    """Get a single scraper config by key and owner."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM scraper_configs WHERE config_key = ? AND owning_user = ?",
        (config_key, owning_user),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    try:
        d["config_json"] = json.loads(d.get("config_json", "{}"))
    except (json.JSONDecodeError, TypeError):
        d["config_json"] = {}
    return d


def delete_scraper_config(config_key: str, owning_user: str):
    """Delete a scraper config by key and owner."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM scraper_configs WHERE config_key = ? AND owning_user = ?",
        (config_key, owning_user),
    )
    conn.commit()
    conn.close()


def get_all_users_with_scraper_configs() -> list[str]:
    """Return distinct owning_user values that have at least one scraper config."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT owning_user FROM scraper_configs WHERE owning_user != ''")
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_all_users_with_keywords() -> list[str]:
    """Return distinct owning_user values that have a default profile with non-empty keywords."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT DISTINCT owning_user FROM job_profiles "
        "WHERE is_default = 1 AND owning_user != '' AND keywords IS NOT NULL AND keywords != '' AND keywords != '[]'"
    )
    rows = cursor.fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_default_profile(owning_user: str) -> dict | None:
    """Return the is_default=1 profile for a user, with JSON fields parsed."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM job_profiles WHERE owning_user = ? AND is_default = 1 LIMIT 1",
        (owning_user,),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    profile = dict(row)
    for field in ["keywords", "reject_job_titles", "additional_skills"]:
        if field in profile and isinstance(profile[field], str):
            try:
                profile[field] = json.loads(profile[field]) if profile[field] else []
            except json.JSONDecodeError:
                profile[field] = []
    return profile


def build_job_finder_config(owning_user: str) -> dict:
    """Build the job_finder_config dict from all user scraper configs.
    Returns the same structure as job_finder_config.yaml:
    {"workday_params": {...}, "oraclecloud_params": {...}, "eightfold_params": {...}, "usajobs_params": {...}}
    """
    configs = get_scraper_configs_for_user(owning_user)
    result = {}
    for cfg in configs:
        if cfg["config_type"] == "usajobs":
            result["usajobs_params"] = cfg["config_json"]
        else:
            params_key = f"{cfg['config_type']}_params"
            result.setdefault(params_key, {})
            result[params_key][cfg["config_key"]] = cfg["config_json"]
    return result


_logger = logging.getLogger(__name__)

_DATE_RANGE_DAYS = {
    "today": 0,
    "yesterday": 1,
    "1week": 7,
    "2weeks": 14,
    "1month": 30,
}


def get_jobs_for_user(
    owning_user: str,
    keyword: str = "",
    date_range: str = "all",
    sort_by: str = "date_posted",
    compatibility: str = "",
    clearance: str = "",
    review_status: str = "",
) -> list[dict]:
    """Return job_details rows for a user with optional filters applied."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    conditions = ["owning_user = ?"]
    params: list = [owning_user]

    if keyword:
        conditions.append("(title LIKE ? OR company LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])

    if date_range and date_range != "all":
        days = _DATE_RANGE_DAYS.get(date_range)
        if days is not None:
            cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            conditions.append("date_posted >= ?")
            params.append(cutoff)

    if clearance == "required":
        conditions.append("security_clearance_required = 1")
    elif clearance == "not_required":
        conditions.append("security_clearance_required = 0")

    if review_status and review_status != "all":
        conditions.append("review_status = ?")
        params.append(review_status)

    if compatibility and compatibility != "all":
        conditions.append("title_compatibility_score = ?")
        params.append(compatibility)

    sort_map = {
        "date_posted": "date_posted DESC",
        "score": "job_score DESC",
        "title": "title ASC",
        "company": "company ASC",
    }
    order = sort_map.get(sort_by, "date_posted DESC")

    where = " AND ".join(conditions)
    cursor.execute(f"SELECT * FROM job_details WHERE {where} ORDER BY {order}", params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_job_review_status(job_id: int, status: str, owning_user: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE job_details SET review_status = ? WHERE id = ? AND owning_user = ?",
        (status, job_id, owning_user),
    )
    conn.commit()
    conn.close()


def update_job_description(job_id: int, description: str, owning_user: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE job_details SET description = ? WHERE id = ? AND owning_user = ?",
        (description, job_id, owning_user),
    )
    conn.commit()
    conn.close()


def update_title_compatibility_score(job_id: int, score: str, owning_user: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE job_details SET title_compatibility_score = ? WHERE id = ? AND owning_user = ?",
        (score, job_id, owning_user),
    )
    conn.commit()
    conn.close()


def apply_job(job_id: int, owning_user: str) -> bool:
    """Mark a job as applied in job_details and record initial status history entry."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        now = datetime.now().strftime("%Y-%m-%d")
        cursor.execute(
            "UPDATE job_details SET review_status = 'applied', applied_at = ? WHERE id = ? AND owning_user = ?",
            (now, job_id, owning_user),
        )
        if cursor.rowcount == 0:
            return False
        conn.commit()

        cursor.execute(
            "INSERT INTO applied_job_status_history (applied_job_id, status, notes, changed_at) VALUES (?, ?, ?, ?)",
            (job_id, "submitted", "Applied via Job Finder", now),
        )
        conn.commit()
        return True
    except Exception as e:
        _logger.error(f"apply_job error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


_APPLIED_STATUS_HISTORY_JOIN = """
    LEFT JOIN (
        SELECT applied_job_id, status, changed_at,
               ROW_NUMBER() OVER (PARTITION BY applied_job_id ORDER BY changed_at DESC, id DESC) AS rn
        FROM applied_job_status_history
    ) h ON jd.id = h.applied_job_id AND h.rn = 1
"""

_APPLIED_SORT_MAP = {
    "recent_activity": "h.changed_at DESC NULLS LAST, jd.applied_at DESC NULLS LAST",
    "applied_at":      "jd.applied_at DESC NULLS LAST",
    "title":           "jd.title ASC",
    "company":         "jd.company ASC",
}


def get_applied_jobs_for_user(
    owning_user: str,
    keyword: str = "",
    sort_by: str = "applied_at",
    status_filter: str = "",
    exclude_statuses: tuple = (),
) -> list[dict]:
    """Query job_details WHERE review_status='applied', with latest status from history."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    conditions = ["jd.owning_user = ?", "jd.review_status = 'applied'"]
    params: list = [owning_user]

    if keyword:
        conditions.append("(jd.title LIKE ? OR jd.company LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])

    if status_filter and status_filter != "all":
        conditions.append("COALESCE(h.status, 'submitted') = ?")
        params.append(status_filter)

    if exclude_statuses:
        placeholders = ",".join("?" * len(exclude_statuses))
        conditions.append(f"COALESCE(h.status, 'submitted') NOT IN ({placeholders})")
        params.extend(exclude_statuses)

    where = " AND ".join(conditions)
    order = _APPLIED_SORT_MAP.get(sort_by, _APPLIED_SORT_MAP["applied_at"])

    cursor.execute(
        f"""
        SELECT jd.*, COALESCE(h.status, 'submitted') AS current_status_latest,
               h.changed_at AS last_status_date
        FROM job_details jd
        {_APPLIED_STATUS_HISTORY_JOIN}
        WHERE {where}
        ORDER BY {order}
        """,
        params,
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_applied_job_by_id(job_id: int, owning_user: str) -> dict | None:
    """Single job_details row (review_status='applied') with latest history status."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        f"""
        SELECT jd.*, COALESCE(h.status, 'submitted') AS current_status_latest,
               h.changed_at AS last_status_date
        FROM job_details jd
        {_APPLIED_STATUS_HISTORY_JOIN}
        WHERE jd.id = ? AND jd.owning_user = ? AND jd.review_status = 'applied'
        """,
        (job_id, owning_user),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def withdraw_job(job_id: int, owning_user: str) -> bool:
    """Withdraw: clear applied state from job_details and delete all history entries."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "DELETE FROM applied_job_status_history WHERE applied_job_id = ?",
            (job_id,),
        )
        cursor.execute(
            """UPDATE job_details
               SET review_status = 'not_reviewed', applied_at = NULL, job_notes = '', resume = NULL
               WHERE id = ? AND owning_user = ?""",
            (job_id, owning_user),
        )
        conn.commit()
        return True
    except Exception as e:
        _logger.error(f"withdraw_job error: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_applied_status_history(job_id: int) -> list[dict]:
    """All history rows for a job, newest first."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM applied_job_status_history WHERE applied_job_id = ? ORDER BY changed_at DESC, id DESC",
        (job_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_applied_status_event(job_id: int, status: str, notes: str, changed_at: str) -> None:
    """Insert a new status history entry."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO applied_job_status_history (applied_job_id, status, notes, changed_at) VALUES (?, ?, ?, ?)",
        (job_id, status, notes, changed_at),
    )
    conn.commit()
    conn.close()


def update_applied_job_notes(job_id: int, notes: str, owning_user: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE job_details SET job_notes = ? WHERE id = ? AND owning_user = ?",
        (notes, job_id, owning_user),
    )
    conn.commit()
    conn.close()


def update_applied_job_resume(job_id: int, resume_bytes: bytes, owning_user: str) -> None:
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE job_details SET resume = ? WHERE id = ? AND owning_user = ?",
        (resume_bytes, job_id, owning_user),
    )
    conn.commit()
    conn.close()


def create_applied_job(job_data: dict, owning_user: str) -> int:
    """Insert a manually-created applied job and its initial status history entry."""
    from datetime import datetime as _dt
    conn = get_db_connection()
    cursor = conn.cursor()
    today = _dt.now().strftime("%Y-%m-%d")
    try:
        cursor.execute(
            """
            INSERT INTO job_details (
                job_url, site, title, company, location, job_type,
                date_posted, interval, min_amount, max_amount, currency,
                is_remote, num_urgent_words, benefits, emails, description,
                job_score, job_score_json, security_clearance_required,
                review_status, date_created, owning_user, applied_at, job_notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                job_data.get("job_url", ""),
                "manual",
                job_data.get("title", ""),
                job_data.get("company", ""),
                job_data.get("location", ""),
                "",
                today,
                job_data.get("interval", ""),
                job_data.get("min_amount", ""),
                job_data.get("max_amount", ""),
                job_data.get("currency", ""),
                job_data.get("is_remote", "false"),
                "0",
                "",
                "",
                job_data.get("description", ""),
                0,
                "{}",
                1 if job_data.get("security_clearance_required") else 0,
                "applied",
                today,
                owning_user,
                job_data.get("applied_at", "") or today,
                "",
            ),
        )
        new_id = cursor.lastrowid
        conn.commit()
        cursor.execute(
            "INSERT INTO applied_job_status_history (applied_job_id, status, notes, changed_at) VALUES (?,?,?,?)",
            (new_id, "submitted", "Manually added", today),
        )
        conn.commit()
        return new_id
    except Exception as e:
        _logger.error(f"create_applied_job error: {e}")
        conn.rollback()
        return 0
    finally:
        conn.close()


def update_applied_job_fields(
    job_id: int, owning_user: str,
    title: str = "", job_url: str = "", company: str = "",
    location: str = "",
    min_amount: str = "", max_amount: str = "",
    currency: str = "", interval: str = "", applied_at: str = "",
    is_remote: str = "", security_clearance_required: bool = False,
) -> None:
    def _num(v):
        try:
            f = float(v)
            return int(f) if f == int(f) else f
        except (ValueError, TypeError):
            return ""

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        """UPDATE job_details SET title=?, job_url=?, company=?, location=?,
           min_amount=?, max_amount=?, currency=?, interval=?, applied_at=?,
           is_remote=?, security_clearance_required=?
           WHERE id=? AND owning_user=?""",
        (
            title or "", job_url or "", company or "", location or "",
            _num(min_amount), _num(max_amount),
            currency or "", interval or "", applied_at or "",
            is_remote or "false", 1 if security_clearance_required else 0,
            job_id, owning_user,
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Analytics queries
# ---------------------------------------------------------------------------

def get_search_terms_from_jobs(owning_user: str) -> list[str]:
    """Distinct search_term values present in job_details for this user (excludes empty)."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT DISTINCT search_term
        FROM job_details
        WHERE owning_user = ? AND search_term IS NOT NULL AND search_term != ''
        ORDER BY search_term
        """,
        (owning_user,),
    )
    result = [r["search_term"] for r in cursor.fetchall()]
    conn.close()
    return result


def get_keyword_quality(owning_user: str) -> list[dict]:
    """Per-keyword quality summary, derived entirely from job_details."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            search_term AS keyword,
            COUNT(*) AS total,
            SUM(CASE WHEN title_compatibility_score = 'high'   THEN 1 ELSE 0 END) AS high_compat,
            SUM(CASE WHEN title_compatibility_score = 'medium' THEN 1 ELSE 0 END) AS medium_compat,
            SUM(CASE WHEN title_compatibility_score = 'low'    THEN 1 ELSE 0 END) AS low_compat
        FROM job_details
        WHERE owning_user = ? AND search_term IS NOT NULL AND search_term != ''
        GROUP BY search_term
        ORDER BY high_compat DESC, total DESC
        """,
        (owning_user,),
    )
    result = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return result


def get_keyword_site_breakdown(owning_user: str, keyword: str, compat: str = "all") -> list[dict]:
    """Per-site breakdown for a keyword, filtered by compat tier. Sourced from job_details."""
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            LOWER(site) AS site,
            COUNT(*) AS total,
            SUM(CASE WHEN title_compatibility_score = 'high'   THEN 1 ELSE 0 END) AS high_compat,
            SUM(CASE WHEN title_compatibility_score = 'medium' THEN 1 ELSE 0 END) AS medium_compat,
            SUM(CASE WHEN title_compatibility_score = 'low'    THEN 1 ELSE 0 END) AS low_compat
        FROM job_details
        WHERE owning_user = ?
          AND LOWER(search_term) = LOWER(?)
          AND (? = 'all' OR LOWER(title_compatibility_score) = LOWER(?))
        GROUP BY LOWER(site)
        ORDER BY high_compat DESC, total DESC
        """,
        (owning_user, keyword, compat, compat),
    )
    result = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return result


def get_job_funnel_stats(owning_user: str) -> dict:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN title_compatibility_score = 'high' AND review_status = 'not_reviewed' THEN 1 ELSE 0 END) AS high_unreviewed,
            SUM(CASE WHEN review_status = 'saved'    THEN 1 ELSE 0 END) AS saved,
            SUM(CASE WHEN review_status = 'applied'  THEN 1 ELSE 0 END) AS applied,
            SUM(CASE WHEN review_status = 'rejected' THEN 1 ELSE 0 END) AS rejected
        FROM job_details
        WHERE owning_user = ?
        """,
        (owning_user,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else {}


def get_source_effectiveness() -> list[dict]:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            LOWER(site) AS site,
            COUNT(*) AS total,
            SUM(CASE WHEN title_compatibility_score = 'high' THEN 1 ELSE 0 END) AS high_compat
        FROM job_details
        WHERE site IS NOT NULL AND site != ''
        GROUP BY LOWER(site)
        ORDER BY high_compat DESC, total DESC
        """
    )
    result = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return result


def get_applications_timeline(owning_user: str) -> list[dict]:
    conn = get_db_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT
            strftime('%Y-%m', applied_at) AS month,
            COUNT(*) AS applied
        FROM job_details
        WHERE review_status = 'applied' AND applied_at IS NOT NULL AND owning_user = ?
        GROUP BY month
        ORDER BY month DESC
        """,
        (owning_user,),
    )
    result = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return result
