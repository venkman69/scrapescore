"""Test Indeed job description retrieval via mobile user-agent + LD+JSON.

Usage:
    uv run python tests/test_indeed_download.py
    uv run python tests/test_indeed_download.py --job-key <jk>
    uv run python tests/test_indeed_download.py --url "https://www.indeed.com/viewjob?jk=<jk>"
"""

import sys
from pathlib import Path
from typing import Optional

import typer

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scrapescore.lib.downloader import (
    _extract_indeed_job_key,
    _fetch_indeed_description,
    download_job_from_indeed,
)

app = typer.Typer()


def _get_fresh_job_key() -> str:
    """Fetch a currently live job key from Indeed via GraphQL."""
    import requests
    import urllib3

    urllib3.disable_warnings()
    from jobspy.indeed.constant import api_headers

    query = """
    query {
        jobSearch(what: "software engineer" limit: 1 sort: RELEVANCE) {
            results { job { key } }
        }
    }
    """
    resp = requests.post(
        "https://apis.indeed.com/graphql",
        headers=api_headers,
        json={"query": query},
        timeout=15,
        verify=False,
    )
    resp.raise_for_status()
    results = resp.json()["data"]["jobSearch"]["results"]
    if not results:
        raise RuntimeError("GraphQL search returned no results — cannot obtain a fresh job key")
    return results[0]["job"]["key"]


def _run_tests(job_key: str) -> list[tuple[str, str]]:
    results = []

    # Test 1: key extraction from viewjob URL
    url_viewjob = f"https://www.indeed.com/viewjob?jk={job_key}"
    extracted = _extract_indeed_job_key(url_viewjob)
    if extracted == job_key:
        results.append(("Key extraction from viewjob URL", "PASS"))
    else:
        results.append((f"Key extraction from viewjob URL (got {extracted!r})", "FAIL"))

    # Test 2: key extraction from vjk-style URL
    url_vjk = f"https://www.indeed.com/?vjk={job_key}&advn=9999"
    extracted_vjk = _extract_indeed_job_key(url_vjk)
    if extracted_vjk == job_key:
        results.append(("Key extraction from vjk-style URL", "PASS"))
    else:
        results.append((f"Key extraction from vjk-style URL (got {extracted_vjk!r})", "FAIL"))

    # Test 3: key extraction returns None for bare domain
    if _extract_indeed_job_key("https://www.indeed.com/") is None:
        results.append(("Key extraction returns None for bare URL", "PASS"))
    else:
        results.append(("Key extraction returns None for bare URL", "FAIL"))

    # Test 4: mobile fetch returns non-empty HTML description
    description = _fetch_indeed_description(job_key)
    if description and len(description) > 50:
        results.append((f"Mobile fetch returned {len(description)} chars of HTML", "PASS"))
        typer.echo(f"\n  Description preview: {description[:100]}")
    else:
        results.append((f"Mobile fetch returned empty/short result", "FAIL"))

    # Test 5: end-to-end download_job_from_indeed via URL
    desc_via_url = download_job_from_indeed(url_viewjob)
    if desc_via_url and len(desc_via_url) > 50:
        results.append((f"download_job_from_indeed returned {len(desc_via_url)} chars", "PASS"))
    else:
        results.append(("download_job_from_indeed returned empty result", "FAIL"))

    # Test 6: invalid key returns empty string (not an exception)
    desc_bad = download_job_from_indeed("https://www.indeed.com/viewjob?jk=000000invalid000")
    if isinstance(desc_bad, str) and desc_bad == "":
        results.append(("Invalid job key returns empty string gracefully", "PASS"))
    else:
        results.append((f"Invalid job key returned unexpected value: {desc_bad!r:.60}", "FAIL"))

    # Test 7: URL with no key returns empty string
    desc_no_key = download_job_from_indeed("https://www.indeed.com/jobs?q=engineer")
    if isinstance(desc_no_key, str) and desc_no_key == "":
        results.append(("URL without job key returns empty string gracefully", "PASS"))
    else:
        results.append((f"URL without key returned unexpected value: {desc_no_key!r:.60}", "FAIL"))

    return results


@app.command()
def main(
    job_key: Optional[str] = typer.Option(None, "--job-key", "-k", help="Indeed job key (jk param)"),
    url: Optional[str] = typer.Option(None, "--url", "-u", help="Full Indeed job URL"),
):
    """Test Indeed job description retrieval via mobile user-agent + LD+JSON."""
    typer.echo("=" * 60)
    typer.echo("Indeed Download Test")
    typer.echo("=" * 60)

    if url:
        job_key = _extract_indeed_job_key(url)
        if not job_key:
            typer.echo(f"[ERROR] Could not extract job key from URL: {url}", err=True)
            raise typer.Exit(1)

    if not job_key:
        typer.echo("[*] No job key provided — fetching a live key from Indeed GraphQL...")
        try:
            job_key = _get_fresh_job_key()
            typer.echo(f"[*] Using live job key: {job_key}")
        except Exception as e:
            typer.echo(f"[ERROR] Could not obtain a live job key: {e}", err=True)
            raise typer.Exit(1)
    else:
        typer.echo(f"[*] Using job key: {job_key}")

    results = _run_tests(job_key)

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
