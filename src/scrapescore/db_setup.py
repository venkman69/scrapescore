"""
Database Setup Module for Job Finder

This module handles all database table creation and setup.
It is designed to be idempotent - running it multiple times will always
result in the same final database state.

Usage:
    uv run python -m scrapescore.db_setup
"""

import sqlite3
from pathlib import Path
from scrapescore.lib.config import get_storage_dir_config
import typer
import logging

logger = logging.getLogger(__name__)


def get_db_path() -> Path:
    """Get the database path from configuration."""
    db_path = get_storage_dir_config("job_finder_db_path")
    return Path(db_path)


def get_db_connection(db_path: Path = None) -> sqlite3.Connection:
    """
    Get a database connection with foreign key constraints enabled.

    This function should be used throughout the application to ensure
    foreign key constraints (including CASCADE deletes) are enforced.

    Args:
        db_path: Path to the database file. If None, uses default from config.

    Returns:
        Connection to the database with foreign keys enabled
    """
    if db_path is None:
        db_path = get_db_path()

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def add_column_if_not_exists(cursor, table_name: str, col_name: str, col_def: str):
    """
    Add a column to a table if it doesn't already exist.

    Args:
        cursor: Database cursor
        table_name: Name of the table
        col_name: Name of the column to add
        col_def: Column definition (e.g., "TEXT NOT NULL DEFAULT ''")
    """
    try:
        cursor.execute(f"SELECT {col_name} FROM {table_name} LIMIT 1")
        logger.debug(f"Column {col_name} already exists in {table_name}")
    except sqlite3.OperationalError:
        print(f"Migrating {table_name}: Adding {col_name} column...")
        logger.info(f"Adding column {col_name} to {table_name}")
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")


def remove_column_if_exists(cursor, table_name: str, col_name: str):
    """
    Remove a column from a table if it exists.
    Uses 'ALTER TABLE ... DROP COLUMN' which requires SQLite 3.35.0+.
    """
    try:
        cursor.execute(f"SELECT {col_name} FROM {table_name} LIMIT 1")
        print(f"Migrating {table_name}: Removing {col_name} column...")
        logger.info(f"Removing column {col_name} from {table_name}")
        cursor.execute(f"ALTER TABLE {table_name} DROP COLUMN {col_name}")
    except sqlite3.OperationalError as e:
        if "no such column" in str(e).lower():
            logger.debug(f"Column {col_name} does not exist in {table_name}")
        else:
            logger.warning(f"Failed to drop column {col_name} from {table_name}: {e}")
            print(
                f"Warning: Could not drop column {col_name} from {table_name}. "
                "This usually means your SQLite version is older than 3.35.0."
            )


def create_job_details_table(cursor):
    """Create the job_details table with all required columns."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url TEXT NOT NULL,
            site TEXT NOT NULL,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            location TEXT NOT NULL,
            job_type TEXT NOT NULL,
            date_posted TEXT NOT NULL,
            interval TEXT NOT NULL,
            min_amount TEXT NOT NULL,
            max_amount TEXT NOT NULL,
            currency TEXT NOT NULL,
            is_remote TEXT NOT NULL,
            num_urgent_words TEXT NOT NULL,
            benefits TEXT NOT NULL,
            emails TEXT NOT NULL,
            description TEXT NOT NULL,
            job_score INTEGER NOT NULL DEFAULT 0,
            job_score_json TEXT NOT NULL DEFAULT '{}',
            security_clearance_required INTEGER NOT NULL DEFAULT 0,
            usage_metrics TEXT,
            review_status TEXT NOT NULL DEFAULT 'not_reviewed',
            title_compatibility_score TEXT,
            date_created TEXT NOT NULL DEFAULT CURRENT_DATE,
            owning_user TEXT NOT NULL DEFAULT '',
            search_term TEXT DEFAULT '',
            applied_at TEXT,
            job_notes TEXT DEFAULT '',
            resume BLOB
        )
    """)
    logger.info("job_details table created/verified")


def create_job_profiles_table(cursor):
    """Create the job_profiles table with all required columns."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS job_profiles (
            profile_name TEXT PRIMARY KEY,
            resume TEXT NOT NULL DEFAULT '',
            desired_role_description TEXT NOT NULL DEFAULT '',
            additional_skills TEXT NOT NULL DEFAULT '',
            us_citizen INTEGER NOT NULL DEFAULT 0,
            security_clearance TEXT NOT NULL DEFAULT 'None',
            keywords TEXT NOT NULL DEFAULT '[]',
            location TEXT NOT NULL DEFAULT '',
            reject_job_titles TEXT NOT NULL DEFAULT '[]',
            owning_user TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0,
            date_updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    logger.info("job_profiles table created/verified")


def create_applied_job_status_history_table(cursor):
    """Create the applied_job_status_history table for tracking status changes."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS applied_job_status_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            applied_job_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            notes TEXT DEFAULT '',
            changed_at TEXT NOT NULL DEFAULT CURRENT_DATE,
            FOREIGN KEY (applied_job_id) REFERENCES job_details(id) ON DELETE CASCADE
        )
    """)
    logger.info("applied_job_status_history table created/verified")

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_status_history_job_id
        ON applied_job_status_history(applied_job_id)
    """)
    logger.info("idx_status_history_job_id index created/verified")


def create_scraping_logs_table(cursor):
    """Create the scraping_logs table for tracking scraping effectiveness."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scraping_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            scraper TEXT NOT NULL,
            site TEXT,
            search_term TEXT NOT NULL,
            jobs_found INTEGER NOT NULL,
            scrape_duration_seconds REAL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            success BOOLEAN DEFAULT 1,
            error_message TEXT
        )
    """)
    logger.info("scraping_logs table created/verified")

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_scraping_logs_run_id
        ON scraping_logs(run_id)
    """)
    logger.info("idx_scraping_logs_run_id index created/verified")

    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_scraping_logs_timestamp
        ON scraping_logs(timestamp DESC)
    """)
    logger.info("idx_scraping_logs_timestamp index created/verified")


def create_scraper_configs_table(cursor):
    """Create the scraper_configs table for per-user scraper configuration."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS scraper_configs (
            config_key TEXT NOT NULL,
            config_type TEXT NOT NULL,
            company_name TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            owning_user TEXT NOT NULL DEFAULT '',
            date_updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (config_key, owning_user)
        )
    """)
    logger.info("scraper_configs table created/verified")


def setup_database(db_path: Path = None) -> sqlite3.Connection:
    """
    Setup and initialize the database with all tables.

    This function is idempotent - running it multiple times will always
    result in the same final database state.

    Args:
        db_path: Path to the database file. If None, uses default from config.

    Returns:
        Connection to the initialized database
    """
    if db_path is None:
        db_path = get_db_path()

    print(f"Setting up database at: {db_path}")
    logger.info(f"Setting up database at: {db_path}")

    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    cursor = conn.cursor()

    print("Creating tables...")
    create_job_details_table(cursor)
    create_job_profiles_table(cursor)
    create_applied_job_status_history_table(cursor)
    create_scraping_logs_table(cursor)
    create_scraper_configs_table(cursor)

    conn.commit()

    print("Database setup completed successfully!")
    logger.info("Database setup completed successfully!")

    return conn


def verify_database(db_path: Path = None) -> bool:
    """
    Verify that all tables exist in the database.

    Args:
        db_path: Path to the database file. If None, uses default from config.

    Returns:
        True if database is valid, False otherwise
    """
    if db_path is None:
        db_path = get_db_path()

    if not db_path.exists():
        print(f"Database does not exist at: {db_path}")
        return False

    print(f"Verifying database at: {db_path}")
    logger.info(f"Verifying database at: {db_path}")

    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()

        tables_to_check = [
            "job_details",
            "job_profiles",
            "applied_job_status_history",
            "scraping_logs",
            "scraper_configs",
        ]
        for table in tables_to_check:
            cursor.execute(
                f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table}'"
            )
            if cursor.fetchone() is None:
                print(f"ERROR: Table '{table}' does not exist!")
                return False
            print(f"[OK] Table '{table}' exists")

        conn.close()
        print("Database verification passed!")
        return True

    except Exception as e:
        print(f"ERROR: Database verification failed: {e}")
        logger.error(f"Database verification failed: {e}")
        return False


def main(
    verify: bool = typer.Option(
        False, "--verify", "-v", help="Verify database without making changes"
    ),
    db_path: str = typer.Option(
        None, "--db-path", help="Path to database file (overrides config)"
    ),
    verbose: bool = typer.Option(False, "--verbose", help="Enable verbose logging"),
):
    """
    Setup and initialize the Job Finder database.

    This command creates all required tables. It is idempotent and safe to run multiple times.
    """
    log_level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=log_level, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    if verbose:
        logger.setLevel(logging.DEBUG)

    resolved_db_path = Path(db_path) if db_path else get_db_path()

    if verify:
        success = verify_database(resolved_db_path)
        raise typer.Exit(0 if success else 1)

    try:
        conn = setup_database(resolved_db_path)

        cursor = conn.cursor()
        for table in [
            "job_details",
            "job_profiles",
            "applied_job_status_history",
            "scraping_logs",
            "scraper_configs",
        ]:
            cursor.execute(f"SELECT COUNT(*) FROM {table}")
            count = cursor.fetchone()[0]
            print(f"  {table}: {count} records")

        conn.close()
        print("\n[OK] Database is ready to use!")

    except Exception as e:
        print(f"ERROR: Database setup failed: {e}")
        logger.error(f"Database setup failed: {e}")
        raise typer.Exit(1)


if __name__ == "__main__":
    typer.run(main)
