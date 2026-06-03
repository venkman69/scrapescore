"""Test OracleCloud scraper against configured companies in the DB.

Usage:
    # List configured OracleCloud companies
    uv run tests/test_oraclecloud_scraper.py list

    # Scrape a specific company (uses first user with configs if --user not given)
    uv run tests/test_oraclecloud_scraper.py scrape jpmc --search "software engineer"
    uv run tests/test_oraclecloud_scraper.py scrape nfcu --search "data engineer" --user me@example.com
"""

import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

load_dotenv()

from scrapescore.lib import utils
from scrapescore.db import (
    build_job_finder_config,
    get_all_users_with_scraper_configs,
    get_scraper_configs_for_user,
)
from jobspy.oraclecloud import OracleCloud
from jobspy.model import ScraperInput, Site

utils.config_logger("test_oraclecloud_scraper.log", Path("logs"))
logger = logging.getLogger(__name__)

app = typer.Typer(
    help="Test OracleCloud scraper against DB-configured companies",
    epilog=(
        "Examples:\n\n"
        "  uv run tests/test_oraclecloud_scraper.py list\n\n"
        "  uv run tests/test_oraclecloud_scraper.py list --user me@example.com\n\n"
        "  uv run tests/test_oraclecloud_scraper.py scrape jpmc\n\n"
        '  uv run tests/test_oraclecloud_scraper.py scrape jpmc --search "data engineer" --results 10\n\n'
        "  uv run tests/test_oraclecloud_scraper.py scrape nfcu --output jobs.json"
    ),
)


def _resolve_user(user: Optional[str]) -> str:
    if user:
        return user
    users = get_all_users_with_scraper_configs()
    if not users:
        typer.echo("ERROR: No users with scraper configs found in the DB.", err=True)
        raise typer.Exit(1)
    return users[0]


def _get_oraclecloud_configs(owning_user: str) -> list[dict]:
    return [
        c for c in get_scraper_configs_for_user(owning_user)
        if c["config_type"] == "oraclecloud"
    ]


@app.command(
    epilog=(
        "Examples:\n\n"
        "  uv run tests/test_oraclecloud_scraper.py list\n\n"
        "  uv run tests/test_oraclecloud_scraper.py list --user me@example.com"
    ),
)
def list(
    user: Optional[str] = typer.Option(None, "--user", "-u", help="Owning user (email). Defaults to first user in DB."),
):
    """List all OracleCloud companies configured in the DB."""
    owning_user = _resolve_user(user)
    typer.echo(f"User: {owning_user}\n")

    configs = _get_oraclecloud_configs(owning_user)
    if not configs:
        typer.echo("No OracleCloud companies configured.")
        raise typer.Exit(0)

    typer.echo(f"{'Key':<20} {'Company':<30} {'Base URL'}")
    typer.echo("-" * 80)
    for cfg in configs:
        key = cfg["config_key"]
        company = cfg.get("company_name", "")
        base_url = cfg.get("config_json", {}).get("base_url", "")
        typer.echo(f"{key:<20} {company:<30} {base_url}")

    typer.echo(f"\n{len(configs)} company/companies configured.")


@app.command(
    epilog=(
        "Examples:\n\n"
        "  uv run tests/test_oraclecloud_scraper.py scrape jpmc\n\n"
        '  uv run tests/test_oraclecloud_scraper.py scrape jpmc --search "data engineer" --results 10\n\n'
        "  uv run tests/test_oraclecloud_scraper.py scrape nfcu --output jobs.json\n\n"
        "  uv run tests/test_oraclecloud_scraper.py scrape nfcu --user me@example.com --search \"cloud architect\""
    ),
)
def scrape(
    company: str = typer.Argument(..., help="Company config key (e.g. 'jpmc', 'nfcu')"),
    search: str = typer.Option("software engineer", "--search", "-s", help="Search term"),
    user: Optional[str] = typer.Option(None, "--user", "-u", help="Owning user (email). Defaults to first user in DB."),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Write jobs JSON to file"),
    results: int = typer.Option(5, "--results", "-r", help="Max jobs to return"),
):
    """Scrape a single OracleCloud company and print the results."""
    owning_user = _resolve_user(user)
    typer.echo("=" * 60)
    typer.echo(f"OracleCloud Scraper Test")
    typer.echo("=" * 60)
    typer.echo(f"User:    {owning_user}")
    typer.echo(f"Company: {company}")
    typer.echo(f"Search:  {search}")
    typer.echo("")

    # Validate the company key exists
    configs = _get_oraclecloud_configs(owning_user)
    config_keys = [c["config_key"] for c in configs]
    if company not in config_keys:
        typer.echo(f"ERROR: '{company}' not found. Configured companies: {config_keys}", err=True)
        raise typer.Exit(1)

    # Build job_finder_config the same way the batch runner does
    job_finder_config = build_job_finder_config(owning_user)

    scraper_input = ScraperInput(
        site_type=[Site.ORACLECLOUD],
        search_term=search,
        results_wanted=results,
        site_config=company,
        job_finder_config=job_finder_config,
    )

    typer.echo(f"[*] Scraping '{company}'...")
    scraper = OracleCloud()
    response = scraper.scrape(scraper_input)

    jobs = response.jobs
    typer.echo(f"\n[+] Found {len(jobs)} job(s)\n")

    job_dicts = []
    for job in jobs:
        d = {
            "id": job.id,
            "title": job.title,
            "company": job.company_name,
            "url": job.job_url,
            "date_posted": job.date_posted.isoformat() if job.date_posted else None,
            "location": job.location.display_location() if job.location else None,
            "description_chars": len(job.description) if job.description else 0,
        }
        job_dicts.append(d)
        typer.echo(f"  {d['title']}")
        typer.echo(f"    URL:         {d['url']}")
        typer.echo(f"    Location:    {d['location']}")
        typer.echo(f"    Posted:      {d['date_posted']}")
        typer.echo(f"    Desc chars:  {d['description_chars']}")
        typer.echo("")

    if output:
        Path(output).write_text(json.dumps(job_dicts, indent=2), encoding="utf-8")
        typer.echo(f"[+] Saved {len(job_dicts)} jobs → {output}")

    if not jobs:
        typer.echo("WARNING: No jobs returned — check logs for errors.", err=True)
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
