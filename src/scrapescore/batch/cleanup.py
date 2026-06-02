"""
Job Record Cleanup Module

Provides automatic cleanup of old job records from the database.
Can be run standalone or imported by job_finder.py.
"""
import logging
import sqlite3
import typer
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def cleanup_old_jobs(conn: sqlite3.Connection, config: dict) -> int:
    """
    Remove job records older than the configured retention period.

    Args:
        conn: Database connection
        config: Configuration dict from job_finder_config.yaml

    Returns:
        Number of records removed, or 0 if cleanup was skipped
    """
    if "retention_days" not in config:
        logger.info("retention_days not configured - skipping job cleanup")
        return 0

    retention_days = config.get("retention_days")
    try:
        retention_days = int(retention_days)
        if retention_days <= 0:
            raise ValueError("retention_days must be positive")
    except (ValueError, TypeError):
        logger.warning(f"Invalid retention_days: {retention_days} - skipping job cleanup")
        return 0

    cutoff_date = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")

    logger.info(f"Starting job cleanup (retention: {retention_days} days)")
    logger.info(f"Cutoff date: {cutoff_date}")

    cursor = conn.cursor()

    try:
        count_query = """
            SELECT COUNT(*) FROM job_details
            WHERE review_status NOT IN ('saved', 'applied')
            AND
                CASE
                    WHEN date_posted IS NULL OR date_posted = ''
                    THEN date_created
                    ELSE date_posted
                END < ?
        """
        cursor.execute(count_query, (cutoff_date,))
        count = cursor.fetchone()[0]

        if count == 0:
            logger.info("No expired job records found")
            return 0

        logger.info(f"Found {count} expired job records")

        delete_query = """
            DELETE FROM job_details
            WHERE review_status NOT IN ('saved', 'applied')
            AND
                CASE
                    WHEN date_posted IS NULL OR date_posted = ''
                    THEN date_created
                    ELSE date_posted
                END < ?
        """
        cursor.execute(delete_query, (cutoff_date,))
        conn.commit()

        logger.info(f"Removed {count} old job records")
        return count

    except Exception as e:
        logger.error(f"Error during cleanup: {e}")
        conn.rollback()
        raise


def main(
    retention_days: int = typer.Option(60, "--retention-days", help="Retention period in days"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show what would be deleted without deleting")
):
    """
    Standalone CLI for job cleanup.

    Usage:
        uv run python -m scrapescore.batch.cleanup
        uv run python -m scrapescore.batch.cleanup --retention-days 90
        uv run python -m scrapescore.batch.cleanup --dry-run
    """
    from scrapescore.lib.config import APP_CONFIG, get_storage_dir_config

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    full_config = APP_CONFIG
    db_path = get_storage_dir_config("job_finder_db_path")

    logger.info(f"Database: {db_path}")

    if retention_days:
        full_config["retention_days"] = retention_days

    conn = sqlite3.connect(db_path)

    try:
        if dry_run:
            cutoff_date = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM job_details
                WHERE review_status NOT IN ('saved', 'applied')
                AND
                    CASE
                        WHEN date_posted IS NULL OR date_posted = ''
                        THEN date_created
                        ELSE date_posted
                    END < ?
            """, (cutoff_date,))
            count = cursor.fetchone()[0]
            logger.info(f"[DRY RUN] Would remove {count} records older than {cutoff_date}")
        else:
            cleanup_old_jobs(conn, full_config)
    finally:
        conn.close()


if __name__ == "__main__":
    typer.run(main)
