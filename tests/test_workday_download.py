"""Test Workday job description retrieval via direct HTTP fetch + JSON-LD extraction.

Usage:
    uv run python tests/test_workday_download.py
    uv run python tests/test_workday_download.py --url "https://<tenant>.myworkdayjobs.com/..."
"""

import sys
from pathlib import Path
from typing import Optional

import typer

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scrapescore.lib.downloader import download_job_from_workday

app = typer.Typer()

DEFAULT_URL = "https://guidehouse.wd1.myworkdayjobs.com/en-US/External/job/Remote-PB-Medical-Coder---Neurology-Clinic_39899-1"
DEFAULT_URL = "https://guidehouse.wd1.myworkdayjobs.com/External/job/US---VA-McLean/CyberArk-Engineer_38918"

def _run_tests(url: str) -> tuple[list[tuple[str, str]], str]:
    results = []

    # Test 1: fetch returns non-empty description
    description = download_job_from_workday(url)
    if description and len(description) > 50:
        results.append((f"Fetched {len(description)} chars for {url}", "PASS"))
        typer.echo(f"\n  Description preview: {description[:100]}")
    else:
        results.append((f"Fetch returned empty/short result for {url}", "FAIL"))

    # Test 2: return type is str
    if isinstance(description, str):
        results.append(("Return type is str", "PASS"))
    else:
        results.append((f"Return type is {type(description).__name__}, expected str", "FAIL"))

    # Test 3: invalid URL returns empty string gracefully
    bad = download_job_from_workday("https://fake.wd1.myworkdayjobs.com/en-US/Jobs/job/no-such-job_00000")
    if isinstance(bad, str) and bad == "":
        results.append(("Invalid URL returns empty string gracefully", "PASS"))
    else:
        results.append((f"Invalid URL returned unexpected value ({len(bad)} chars)", "FAIL"))

    return results, description


@app.command()
def main(
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Full myworkdayjobs.com job URL"),
    output: Optional[str] = typer.Option(None, "--output", "-o", help="Save description to file"),
):
    """Test Workday job description retrieval via direct HTTP fetch + JSON-LD extraction."""
    typer.echo("=" * 60)
    typer.echo("Workday Download Test")
    typer.echo("=" * 60)

    if not url:
        url = DEFAULT_URL
        typer.echo(f"[*] No URL provided — using default:\n    {url}")
    else:
        typer.echo(f"[*] Using URL: {url}")

    results, description = _run_tests(url)

    typer.echo("")
    failed = 0
    for name, status in results:
        typer.echo(f"  [{status}] {name}")
        if status == "FAIL":
            failed += 1

    if output and description:
        Path(output).write_text(description, encoding="utf-8")
        typer.echo(f"\n[+] Saved description → {output}")

    typer.echo("\n" + "=" * 60)
    if failed:
        typer.echo(f"FAILED: {failed}/{len(results)} tests failed")
        raise typer.Exit(1)
    else:
        typer.echo(f"PASSED: all {len(results)} tests passed")


if __name__ == "__main__":
    app()
