#!/usr/bin/env python3
"""
Manual Scraper CLI - Run job scrapers with custom search terms.

Examples:
    # Search Indeed for security engineer roles
    PYTHONPATH="./src:./src/lib" uv run src/manual_scraper.py scrape indeed "security engineer"

    # Search LinkedIn with location override
    PYTHONPATH="./src:./src/lib" uv run src/manual_scraper.py scrape linkedin "iam architect" --location "Austin, TX"

    # List all available sites
    PYTHONPATH="./src:./src/lib" uv run src/manual_scraper.py --list-sites

    # Via shell wrapper
    ./bin/run_manual_scraper.sh scrape indeed "security engineer" --hours-old 72 --results 20
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from dotenv import load_dotenv

from jobspy import SCRAPER_MAPPING
from jobspy.model import JobPost, ScraperInput, Site
from scrapescore.lib import utils

# Configure logging
utils.config_logger("manual_scraper.log", Path("logs"))
logger = logging.getLogger(__name__)


def format_job(job: JobPost) -> Optional[dict]:
    """
    Format a JobPost into the output JSON structure.

    Args:
        job: JobPost to format

    Returns:
        Dict with formatted job data, or None if title/url missing
    """
    # Skip jobs missing required fields
    if not job.title or not job.job_url:
        if not job.title:
            logger.error(f"Skipping job missing title: {job.job_url}")
        else:
            logger.error(f"Skipping job missing URL: {job.title}")
        return None

    # Format compensation
    compensation = None
    if job.compensation:
        compensation = {
            "min": job.compensation.min_amount,
            "max": job.compensation.max_amount,
        }

    # Format location
    location_str = None
    if job.location:
        location_str = job.location.display_location()

    # Format date
    date_str = None
    if job.date_posted:
        date_str = job.date_posted.isoformat()

    return {
        "title": job.title,
        "company": job.company_name,
        "url": job.job_url,
        "date_posted": date_str,
        "compensation": compensation,
        "location": location_str,
    }

app = typer.Typer(help="Manual job scraper CLI - test search strings against job sites")


def display_sites() -> None:
    """Display all available job sites and exit."""
    sites = sorted([site.value for site in Site])
    typer.echo("Available job sites:")
    for site in sites:
        typer.echo(f"  {site}")
    raise typer.Exit()

def load_config_and_create_input(
    site: Site,
    search_term: str,
    location: Optional[str],
    distance: int,
    hours_old: Optional[int],
    results_wanted: int,
) -> ScraperInput:
    """
    Load config and create ScraperInput with defaults and overrides.

    Args:
        site: Job site to scrape
        search_term: Search query
        location: Override default location (None uses config)
        distance: Search radius in miles
        hours_old: Override default hours_old (None uses config)
        results_wanted: Number of results to return

    Returns:
        Configured ScraperInput
    """
    load_dotenv()
    config = utils.read_resource_as_yaml("job_finder_config.yaml")

    # Use CLI override or config default
    final_location = location if location else config.get("location", "McLean, VA")
    final_hours_old = hours_old if hours_old is not None else config.get("hours_old", 168)

    return ScraperInput(
        site_type=[site],
        search_term=search_term,
        location=final_location,
        distance=distance,
        hours_old=final_hours_old,
        results_wanted=results_wanted,
        job_finder_config=config,
    )


def scrape(
    site: str = typer.Argument(..., help="Job site to scrape (lowercase, e.g., 'indeed', 'linkedin')"),
    search: str = typer.Argument(..., help="Search term(s)"),
    location: Optional[str] = typer.Option(None, "--location", "-l", help="Override default location"),
    distance: int = typer.Option(25, "--distance", "-d", help="Search radius in miles"),
    hours_old: Optional[int] = typer.Option(None, "--hours-old", "-h", help="Maximum job age in hours"),
    results: int = typer.Option(15, "--results", "-r", help="Number of results to return"),
) -> None:
    """
    Run a job scraper with the specified site and search term.

    Outputs JSON with job results including title, url, date_posted, compensation, and location.
    """
    # Resolve site
    try:
        site_enum = Site[site.upper()]
    except KeyError:
        available = ", ".join(sorted(list([s.name for s in Site])))
        typer.echo(f"Error: Unknown site '{site}'", err=True)
        typer.echo(f"Available sites: {available}", err=True)
        raise typer.Exit(code=1)

    # Create scraper input
    scraper_input = load_config_and_create_input(
        site=site_enum,
        search_term=search,
        location=location,
        distance=distance,
        hours_old=hours_old,
        results_wanted=results,
    )

    logger.info(f"Scraping {site_enum.value} for '{search}'")

    # Get scraper and scrape
    scraper_class = SCRAPER_MAPPING[site_enum]
    scraper = scraper_class()
    response = scraper.scrape(scraper_input)

    # Format results
    formatted_jobs = []
    for job in response.jobs:
        formatted = format_job(job)
        if formatted:
            formatted_jobs.append(formatted)

    # Build output
    output = {
        "metadata": {
            "site": site_enum.value,
            "search_term": search,
            "location": scraper_input.location,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "jobs_found": len(formatted_jobs),
        },
        "jobs": formatted_jobs,
    }

    # Pretty-print JSON to stdout
    typer.echo(json.dumps(output, indent=2))

    logger.info(f"Found {len(formatted_jobs)} jobs")


# Register the scrape command
app.command()(scrape)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    list_sites: bool = typer.Option(False, "--list-sites", help="List all available sites and exit"),
):
    """Manual job scraper CLI - test search strings against job sites."""
    if list_sites:
        display_sites()
        raise typer.Exit()

    # Show help if no command is provided
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
