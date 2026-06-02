"""Test LinkedIn job description retrieval via direct HTTP fetch.

Usage:
    uv run python tests/test_linkedin_download.py
    uv run python tests/test_linkedin_download.py --job-id <id>
    uv run python tests/test_linkedin_download.py --url "https://www.linkedin.com/jobs/view/<id>/"
"""

import re
import sys
from pathlib import Path
from typing import Optional

import typer

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scrapescore.lib.downloader import download_job_from_linkedin

app = typer.Typer()

DEFAULT_JOB_ID = "4405231826"
DEFAULT_JOB_ID = "4400672101"


def _extract_job_id(url: str) -> Optional[str]:
    m = re.search(r"/jobs/view/(\d+)", url)
    return m.group(1) if m else None


def _run_tests(job_id: str) -> list[tuple[str, str]]:
    results = []
    url = f"https://www.linkedin.com/jobs/view/{job_id}/"

    # Test 1: end-to-end fetch returns non-empty content
    description = download_job_from_linkedin(url)
    if description and len(description) > 50:
        results.append((f"Fetched {len(description)} chars for job {job_id}", "PASS"))
        typer.echo(f"\n  Description preview: {description[:100]}")
    else:
        results.append((f"Fetch returned empty/short result for job {job_id}", "FAIL"))

    # Test 2: result is a string (not an exception)
    if isinstance(description, str):
        results.append(("Return type is str", "PASS"))
    else:
        results.append((f"Return type is {type(description).__name__}, expected str", "FAIL"))

    # Test 3: invalid job ID returns empty string gracefully
    bad = download_job_from_linkedin("https://www.linkedin.com/jobs/view/000000000000/")
    if isinstance(bad, str) and bad == "":
        results.append(("Invalid job ID returns empty string gracefully", "PASS"))
    else:
        results.append((f"Invalid job ID returned unexpected value ({len(bad)} chars)", "FAIL"))

    return results


@app.command()
def main(
    job_id: Optional[str] = typer.Option(None, "--job-id", "-i", help="LinkedIn job ID from URL /jobs/view/<id>/"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Full LinkedIn job URL"),
):
    """Test LinkedIn job description retrieval via direct HTTP fetch."""
    typer.echo("=" * 60)
    typer.echo("LinkedIn Download Test")
    typer.echo("=" * 60)

    if url:
        job_id = _extract_job_id(url)
        if not job_id:
            typer.echo(f"[ERROR] Could not extract job ID from URL: {url}", err=True)
            raise typer.Exit(1)

    if not job_id:
        job_id = DEFAULT_JOB_ID
        typer.echo(f"[*] No job ID provided — using default: {job_id}")
    else:
        typer.echo(f"[*] Using job ID: {job_id}")

    results = _run_tests(job_id)

    typer.echo("")
    failed = 0
    for name, status in results:
        typer.echo(f"  [{status}] {name}")
        if status == "FAIL":
            failed += 1

    typer.echo("\n" + "=" * 60)
    if failed:
        typer.echo(f"FAILED: {failed}/{len(results)} tests failed")
        raise typer.Exit(1)
    else:
        typer.echo(f"PASSED: all {len(results)} tests passed")


if __name__ == "__main__":
    app()
