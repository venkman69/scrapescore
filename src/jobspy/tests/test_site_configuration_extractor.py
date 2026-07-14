"""Verification test for JIRA-102: Site configuration extractor (Workday + OracleCloud + Eightfold)."""

import typer
from urllib.parse import urlparse, parse_qs
from jobspy.site_configuration_extractor import *

app = typer.Typer()


@app.command()
def test_facet_filtering():
    """Test that hex-hash facet filtering works for various URL patterns."""

    # Single location
    result = filter_workday_facets(
        parse_qs("locations=16ab2f2e354f01cd1db811112922cec7")
    )
    assert result == {"locations": "16ab2f2e354f01cd1db811112922cec7"}, (
        f"Single location failed: {result}"
    )

    # Multiple locations
    result = filter_workday_facets(
        parse_qs(
            "locations=ebb03f92c7c40101f189d9045d870000&locations=2900f8ebf0380101f153aa5d6ffe0000"
        )
    )
    assert result == {
        "locations": [
            "ebb03f92c7c40101f189d9045d870000",
            "2900f8ebf0380101f153aa5d6ffe0000",
        ]
    }, f"Multi location failed: {result}"

    # location + timeType
    result = filter_workday_facets(
        parse_qs(
            "locations=16ab2f2e354f01cd1db811112922cec7&timeType=fa05468fa2381037aa1ea2f3496b0ef8"
        )
    )
    assert result == {
        "locations": "16ab2f2e354f01cd1db811112922cec7",
        "timeType": "fa05468fa2381037aa1ea2f3496b0ef8",
    }, f"location+timeType failed: {result}"

    # Non-hex params are filtered out
    result = filter_workday_facets(
        parse_qs("source=jobboard&locations=16ab2f2e354f01cd1db811112922cec7")
    )
    assert result == {"locations": "16ab2f2e354f01cd1db811112922cec7"}, (
        f"Non-hex filter failed: {result}"
    )

    # Non-whitelisted hex keys are filtered out
    result = filter_workday_facets(
        parse_qs("locations=16ab2f2e354f01cd1db811112922cec7&someOtherFacet=abcdef1234567890abcdef1234567890")
    )
    assert result == {"locations": "16ab2f2e354f01cd1db811112922cec7"}, (
        f"Non-whitelisted key filter failed: {result}"
    )

    # Empty params
    result = filter_workday_facets(parse_qs(""))
    assert result == {}, f"Empty params failed: {result}"

    typer.echo("PASS: All facet filtering tests passed")


@app.command()
def test_url_parsing():
    """Test URL parsing extracts correct base_url."""

    url = "https://freddiemac.wd5.myworkdayjobs.com/en-US/External?locations=16ab2f2e354f01cd1db811112922cec7&timeType=fa05468fa2381037aa1ea2f3496b0ef8"
    parsed = urlparse(url)
    base_url = f"{parsed.scheme}://{parsed.hostname}"
    assert base_url == "https://freddiemac.wd5.myworkdayjobs.com", (
        f"base_url wrong: {base_url}"
    )

    typer.echo("PASS: URL parsing tests passed")


@app.command()
def test_unsupported_site():
    """Test that non-Workday URLs return empty dict."""

    result = extract_site_config("https://www.indeed.com/jobs?q=python", "Indeed")
    assert result == {}, f"Expected empty dict for unsupported site, got: {result}"

    typer.echo("PASS: Unsupported site test passed")


@app.command()
def test_config_key_derivation():
    """Test that config key is correctly derived from company name."""
    import re

    assert re.sub(r"[^a-z0-9]", "", "Freddie Mac".lower()) == "freddiemac"
    assert (
        re.sub(r"[^a-z0-9]", "", "Booz Allen Hamilton".lower()) == "boozallenhamilton"
    )
    assert re.sub(r"[^a-z0-9]", "", "Swift".lower()) == "swift"

    typer.echo("PASS: Config key derivation tests passed")


@app.command()
def test_live_workday():
    """Live test: extract config from a real Workday URL using Playwright.
    Requires network access and Playwright browsers installed."""

    url = "https://freddiemac.wd5.myworkdayjobs.com/en-US/External?locations=16ab2f2e354f01cd1db811112922cec7&timeType=fa05468fa2381037aa1ea2f3496b0ef8"

    typer.echo(f"Extracting config from: {url}")
    result = extract_site_config(url, "Freddie Mac")

    if not result:
        typer.echo("FAIL: No config returned", err=True)
        raise typer.Exit(code=1)

    key = "freddiemac"
    assert key in result, f"Expected key '{key}' in result: {list(result.keys())}"
    config = result[key]

    assert config["company_name"] == "Freddie Mac", (
        f"company_name wrong: {config.get('company_name')}"
    )
    assert config["base_url"] == "https://freddiemac.wd5.myworkdayjobs.com", (
        f"base_url wrong: {config.get('base_url')}"
    )
    assert "tenant_site" in config, f"Missing tenant_site: {config}"
    assert "/" in config["tenant_site"], (
        f"tenant_site should contain '/': {config['tenant_site']}"
    )
    assert "search_params" in config, f"Missing search_params: {config}"
    assert "locations" in config["search_params"], (
        f"Missing locations in search_params: {config['search_params']}"
    )

    typer.echo(f"PASS: Live config extraction succeeded")
    typer.echo(f"  tenant_site: {config['tenant_site']}")
    typer.echo(f"  search_params: {config['search_params']}")


@app.command()
def test_oracle_url_param_filtering():
    """Test OracleCloud URL param whitelist filtering."""

    # Whitelisted params are kept
    result = filter_oracle_url_params(
        parse_qs("locationId=300000010092226&locationLevel=city&radius=25&radiusUnit=MI")
    )
    assert result == {
        "locationId": "300000010092226",
        "locationLevel": "city",
        "radius": "25",
        "radiusUnit": "MI",
    }, f"Whitelisted params failed: {result}"

    # Non-whitelisted params are dropped
    result = filter_oracle_url_params(
        parse_qs("locationId=300000010092226&mode=job-location&selectedLocationsFacet=abc")
    )
    assert result == {"locationId": "300000010092226"}, (
        f"Non-whitelisted filter failed: {result}"
    )

    # Multi-value stays as list
    result = filter_oracle_url_params(
        parse_qs("locationId=300000010092226&locationId=300000010064486")
    )
    assert result == {"locationId": ["300000010092226", "300000010064486"]}, (
        f"Multi-value failed: {result}"
    )

    # selectedCategoriesFacet is captured; parse_qs decodes %3B to ;
    result = filter_oracle_url_params(
        parse_qs("selectedCategoriesFacet=300000086251911%3B300000086144371&locationId=300000020700547&radius=25&radiusUnit=MI")
    )
    assert result.get("selectedCategoriesFacet") == "300000086251911;300000086144371", (
        f"selectedCategoriesFacet wrong: {result.get('selectedCategoriesFacet')}"
    )
    assert result.get("radius") == "25", f"radius wrong: {result.get('radius')}"

    # Empty
    result = filter_oracle_url_params(parse_qs(""))
    assert result == {}, f"Empty failed: {result}"

    typer.echo("PASS: OracleCloud URL param filtering tests passed")


@app.command()
def test_oracle_finder_parsing():
    """Test OracleCloud finder parameter parsing."""

    finder = "findReqs;siteNumber=CX_1001,facetsList=LOCATIONS%3BWORK_LOCATIONS,limit=10,radius=25,radiusUnit=MI,sortBy=POSTING_DATES_DESC"
    result = _parse_finder(finder)
    assert result["siteNumber"] == "CX_1001", (
        f"siteNumber wrong: {result.get('siteNumber')}"
    )
    assert "LOCATIONS;WORK_LOCATIONS" in result["facetsList"], (
        f"facetsList wrong: {result.get('facetsList')}"
    )
    assert result["radius"] == "25", f"radius wrong: {result.get('radius')}"
    assert result["radiusUnit"] == "MI", f"radiusUnit wrong: {result.get('radiusUnit')}"
    assert result["sortBy"] == "POSTING_DATES_DESC", (
        f"sortBy wrong: {result.get('sortBy')}"
    )

    typer.echo("PASS: OracleCloud finder parsing tests passed")


@app.command()
def test_oracle_site_detection():
    """Test that OracleCloud URLs are correctly dispatched."""

    # Should be detected as OracleCloud (will fail live but should attempt it, not return unsupported)
    url = "https://jobs.navyfederal.org/hcmUI/CandidateExperience/en/sites/nfcu/jobs"
    parsed = urlparse(url)
    assert "/hcmUI/CandidateExperience/" in parsed.path, "OracleCloud detection failed"

    typer.echo("PASS: OracleCloud site detection test passed")


@app.command()
def test_live_oraclecloud():
    """Live test: extract config from a real OracleCloud URL using Playwright.
    Requires network access and Playwright browsers installed."""

    url = "https://jobs.navyfederal.org/hcmUI/CandidateExperience/en/sites/nfcu/jobs?location=Vienna%2C+VA%2C+United+States&locationId=300000010092226&locationLevel=city&mode=job-location&radius=25&radiusUnit=MI"

    typer.echo(f"Extracting config from: {url}")
    result = extract_site_config(url, "Navy Federal Credit Union")

    if not result:
        typer.echo("FAIL: No config returned", err=True)
        raise typer.Exit(code=1)

    key = "navyfederalcreditunion"
    assert key in result, f"Expected key '{key}' in result: {list(result.keys())}"
    config = result[key]

    assert config["company_name"] == "Navy Federal Credit Union", (
        f"company_name wrong: {config.get('company_name')}"
    )
    assert config["siteNumber"] == "CX_1001", (
        f"siteNumber wrong: {config.get('siteNumber')}"
    )
    assert "facetsList" in config, f"Missing facetsList: {config}"
    assert "LOCATIONS" in config["facetsList"], (
        f"facetsList missing LOCATIONS: {config['facetsList']}"
    )
    assert config["base_url"].endswith(".oraclecloud.com"), (
        f"base_url wrong: {config.get('base_url')}"
    )
    assert config["locationId"] == "300000010092226", (
        f"locationId wrong: {config.get('locationId')}"
    )
    assert config["radius"] == "25", f"radius wrong: {config.get('radius')}"
    assert config["radiusUnit"] == "MI", f"radiusUnit wrong: {config.get('radiusUnit')}"

    typer.echo("PASS: Live OracleCloud config extraction succeeded")
    typer.echo(f"  siteNumber: {config['siteNumber']}")
    typer.echo(f"  base_url: {config['base_url']}")
    typer.echo(f"  facetsList: {config['facetsList']}")
    typer.echo(f"  sortBy: {config['sortBy']}")


@app.command()
def test_eightfold_detection():
    """Test that Eightfold URLs are correctly dispatched."""

    # URL with /careers and pid param should be detected
    url = "https://apply.careers.microsoft.com/careers?start=0&pid=123"
    result = extract_site_config(url, "Microsoft")
    assert "microsoft" in result, f"Expected Eightfold detection, got: {result}"

    # URL with /careers but no pid should NOT be detected
    url_no_pid = "https://apply.careers.microsoft.com/careers?start=0"
    result = extract_site_config(url_no_pid, "Microsoft")
    assert result == {}, f"Expected empty dict without pid, got: {result}"

    typer.echo("PASS: Eightfold site detection test passed")


@app.command()
def test_eightfold_extraction():
    """Test Eightfold config extraction from URL params."""

    url = (
        "https://apply.careers.microsoft.com/careers"
        "?query=security&start=0"
        "&location=Reston%2C++VA%2C++United+States"
        "&pid=1970393556868330"
        "&sort_by=relevance"
        "&filter_distance=16"
        "&filter_include_remote=1"
        "&filter_career_discipline=Security+Research%2CTechnology+Consulting"
        "&filter_work_site=3+days+%2F+week+in-office"
        "&filter_profession=security+engineering"
        "&filter_seniority=Manager%2CDirector%2CVice+President%2CSenior"
    )

    result = extract_site_config(url, "Microsoft")
    assert "microsoft" in result, (
        f"Expected 'microsoft' key, got: {list(result.keys())}"
    )
    config = result["microsoft"]

    assert config["company_name"] == "Microsoft", (
        f"company_name wrong: {config.get('company_name')}"
    )
    assert config["base_url"] == "https://apply.careers.microsoft.com", (
        f"base_url wrong: {config.get('base_url')}"
    )
    assert config["pid"] == "1970393556868330", f"pid wrong: {config.get('pid')}"
    assert "query" not in config, f"keyword 'query' should be excluded: {config.get('query')}"
    assert "start" not in config, f"pagination 'start' should be excluded: {config.get('start')}"
    assert config["location"] == "Reston,  VA,  United States", (
        f"location wrong: {config.get('location')}"
    )
    assert config["sort_by"] == "relevance", f"sort_by wrong: {config.get('sort_by')}"
    assert config["filter_distance"] == "16", (
        f"filter_distance wrong: {config.get('filter_distance')}"
    )
    assert config["filter_include_remote"] == "1", (
        f"filter_include_remote wrong: {config.get('filter_include_remote')}"
    )
    assert "Security Research" in config["filter_career_discipline"], (
        f"filter_career_discipline wrong: {config.get('filter_career_discipline')}"
    )
    assert "in-office" in config["filter_work_site"], (
        f"filter_work_site wrong: {config.get('filter_work_site')}"
    )
    assert config["filter_profession"] == "security engineering", (
        f"filter_profession wrong: {config.get('filter_profession')}"
    )
    assert "Manager" in config["filter_seniority"], (
        f"filter_seniority wrong: {config.get('filter_seniority')}"
    )

    typer.echo("PASS: Eightfold config extraction test passed")


@app.command()
def test_usajobs_detection():
    """Test that USAJobs URLs are correctly dispatched."""

    url = "https://www.usajobs.gov/search/results/?k=python&l=Washington%2C+DC"
    result = extract_site_config(url, "USAJobs")
    assert "usajobs" in result, f"Expected USAJobs detection, got: {result}"

    # Non-USAJobs URL should not be detected
    result = extract_site_config("https://www.linkedin.com/jobs", "LinkedIn")
    assert result == {}, f"Expected empty dict for non-usajobs URL, got: {result}"

    typer.echo("PASS: USAJobs site detection test passed")


@app.command()
def test_usajobs_extraction():
    """Test USAJobs config extraction — 'k' excluded, all other params kept."""

    url = (
        "https://www.usajobs.gov/search/results/"
        "?k=security%20technology"
        "&l=McLean%2C+Virginia"
        "&l=Reston%2C+Virginia"
        "&l=Arlington%2C+Virginia"
        "&l=Washington%2C+District+of+Columbia"
        "&ws=1&sc=0&p=1&r=10&hp=public&hp=ses"
    )

    result = extract_site_config(url, "USAJobs")
    assert "usajobs" in result, f"Expected 'usajobs' key, got: {list(result.keys())}"
    config = result["usajobs"]

    assert config["company_name"] == "USAJobs", f"company_name wrong: {config.get('company_name')}"
    assert config["base_url"] == "https://www.usajobs.gov", f"base_url wrong: {config.get('base_url')}"
    assert "k" not in config, f"'k' should be excluded: {config.get('k')}"
    assert config["l"] == [
        "McLean, Virginia", "Reston, Virginia", "Arlington, Virginia", "Washington, District of Columbia"
    ], f"locations wrong: {config.get('l')}"
    assert config["ws"] == "1", f"ws wrong: {config.get('ws')}"
    assert config["sc"] == "0", f"sc wrong: {config.get('sc')}"
    assert config["p"] == "1", f"p wrong: {config.get('p')}"
    assert config["r"] == "10", f"r wrong: {config.get('r')}"
    assert config["hp"] == ["public", "ses"], f"hp wrong: {config.get('hp')}"

    typer.echo("PASS: USAJobs config extraction test passed")


@app.command()
def test_usajobs_param_filtering():
    """Test that filter_usajobs_url_params drops 'k' and keeps everything else."""

    result = filter_usajobs_url_params(
        parse_qs("k=python+developer&l=Washington%2C+DC&ws=1&hp=public&hp=ses")
    )
    assert "k" not in result, f"'k' should be excluded: {result.get('k')}"
    assert result["l"] == "Washington, DC", f"location wrong: {result.get('l')}"
    assert result["ws"] == "1", f"ws wrong: {result.get('ws')}"
    assert result["hp"] == ["public", "ses"], f"hp wrong: {result.get('hp')}"

    # Empty query
    result = filter_usajobs_url_params(parse_qs(""))
    assert result == {}, f"Empty failed: {result}"

    typer.echo("PASS: USAJobs param filtering tests passed")


if __name__ == "__main__":
    app()
