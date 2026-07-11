import logging
import logging.handlers
import os
import random
import re
import string
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import yaml
from dotenv import load_dotenv
from markitdown import MarkItDown
from pythonjsonlogger.json import JsonFormatter

from scrapescore.lib.config import BASE_PREFIX, get_storage_dir_config
from scrapescore.lib.downloader import get_markdown_from_url


def current_time_millis():
    return int(round(time.time() * 1000))


logger = logging.getLogger(__name__)


class RelativePathFormatter(logging.Formatter):
    """A formatter that uses a relative path for the log source."""

    def format(self, record):
        # Assuming utils.py is in the project root directory.
        project_root = os.path.dirname(os.path.abspath(__file__))
        record.relativepath = os.path.relpath(record.pathname, project_root)
        return super().format(record)


class JsonRelativePathFormatter(JsonFormatter):
    """JSON formatter that adds the same relative source path as RelativePathFormatter.

    Emits one JSON object per record so the systemd journal (and downstream
    Vector/Loki) can parse fields natively instead of regex-matching text lines.
    """

    def add_fields(self, log_data, record, message_dict):
        project_root = os.path.dirname(os.path.abspath(__file__))
        record.relativepath = os.path.relpath(record.pathname, project_root)
        super().add_fields(log_data, record, message_dict)


def config_logger(log_file_name: str, log_file_dir: Path):
    log_file_dir.mkdir(parents=True, exist_ok=True)
    log_file_path = log_file_dir / log_file_name

    # Get the root logger
    root_logger = logging.getLogger()

    # Clear existing handlers to avoid duplicate logs
    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Configure root logger
    root_logger.setLevel(logging.DEBUG)
    formatter = RelativePathFormatter(
        "%(asctime)s %(levelname)s [%(name)s] [%(relativepath)s:%(funcName)s():%(lineno)d] %(message)s"
    )

    # Add file handler
    file_handler = logging.handlers.RotatingFileHandler(
        log_file_path, maxBytes=10 * 1024 * 1024, backupCount=3
    )
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    # Add console handler (stderr -> systemd journal -> Vector/Loki). Emit JSON at
    # DEBUG so the journal carries full, machine-parseable detail; the file handler
    # above keeps the human-readable text format for local tailing.
    json_formatter = JsonRelativePathFormatter(
        "%(asctime)s %(levelname)s %(name)s %(relativepath)s %(funcName)s %(lineno)d %(message)s"
    )
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(json_formatter)
    console_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(console_handler)

    # Emit the breadcrumb through the configured handlers so it stays JSON on the
    # console stream rather than polluting the journal with a raw stderr line.
    logger.info("Log file path: %s", log_file_path)


def make_work_dirs():
    resume_storage_dir = get_storage_dir_config("resume_storage_dir")
    if not os.path.exists(resume_storage_dir):
        os.makedirs(resume_storage_dir)

    job_storage_dir = get_storage_dir_config("job_storage_dir")
    if not os.path.exists(job_storage_dir):
        os.makedirs(job_storage_dir)



def extract_text_from_various_sources(text_source: str, headless: bool = True) -> str:
    try:
        if Path(text_source).exists():
            if text_source.endswith(".pdf"):
                print(f"Extracting text from PDF: {text_source}")
                return extract_text_from_pdf(text_source)
            else:
                return extract_text_from_file(text_source)
        elif text_source.startswith("http"):
            print(f"Extracting text from URL: {text_source}")
            return get_markdown_from_url(text_source, headless=headless)
        else:
            print(f"Assuming the text source is already raw text: {text_source}")
            return text_source
    except Exception:
        # exception is thrown if it is already text and Path will throw an exception
        print("Assuming the text source is already raw text")
        return text_source


def extract_text_from_file(file_path: str) -> str:
    try:
        print(f"Extracting text from {file_path}")
        # Extracts all text from the file
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        return text
    except Exception as e:
        raise e


def extract_text_from_pdf(
    pdf_path, output_filename=None, remove_pii_from_markdown=False
):
    """
    Extracts text from a PDF file and returns it as a markdown string.
    also writes the markdown to a file in the resume storage directory
    """
    try:
        if output_filename:
            if not output_filename.endswith(".md"):
                output_filename += ".md"
            final_filename = output_filename
        else:
            final_filename = f"{Path(pdf_path).name}.md"

        text_file_path = os.path.join(
            get_storage_dir_config("resume_storage_dir"),
            final_filename,
        )
        logger.info(f"Extracting text from {pdf_path}")
        # Extracts all text from the PDF file
        md = MarkItDown()
        result = md.convert(pdf_path)
        markdown_text = result.markdown
        if remove_pii_from_markdown:
            logger.debug("Removing pii from markdown text")
            markdown_text = remove_pii(markdown_text)
        # there appear to be lots of tabs and junk in the pdf extraction
        # this somewhat cleans that up
        with open(text_file_path, "w", encoding="utf-8") as f:
            f.write(markdown_text)
            logger.info(f"Wrote parsed content to {text_file_path}")
        return markdown_text
    except Exception as e:
        return str(e)


def identify_job_source(url: str) -> dict[str, str]:
    """Identifies the job source from a URL.
    Input: URL
    Output: Dictionary with job source, job ID, and job URL
    """
    random_id = "".join(random.choices(string.ascii_letters + string.digits, k=10))
    job_source = {"job_source": "", "job_id": random_id, "job_url": ""}
    if url.startswith("http"):
        print(f"Identifying job source from URL: {url}")
        if "linkedin" in url:
            print("Source: LinkedIn")
            job_source["job_source"] = "LinkedIn"
            # example: https://www.linkedin.com/jobs/view/4308118213/?eBP=NON_CHARGEABLE_CHANNEL&refId=G%2BFao3NOlOSc8QUHNxJkvg%3D%3D&trackingId=aHsjCzSeKWJeghtTUZFbcQ%3D%3D&trk=flagship3_search_srp_jobs&lipi=urn%3Ali%3Apage%3Ad_flagship3_search_srp_jobs%3BN3X1wCt5SMqWow9PKjAieA%3D%3D&lici=aHsjCzSeKWJeghtTUZFbcQ%3D%3D
            job_id = url.split("/")[5]
            if job_id != "":
                job_source["job_id"] = job_id
                job_source["job_url"] = f"https://www.linkedin.com/jobs/view/{job_id}"
                return job_source
            return job_source
        elif "indeed" in url:
            # example: https://www.indeed.com/?vjk=ec1c9b9378ad1a8e&advn=4418968771450209
            # get the query parameter vjk using urlparse
            job_source["job_source"] = "Indeed"
            parsed_url = urlparse(url)
            query_params = parse_qs(parsed_url.query)
            job_id = query_params.get("vjk", [random_id])[0]
            if job_id != "":
                job_source["job_id"] = job_id
                job_source["job_url"] = f"https://www.indeed.com/?vjk={job_id}"
                return job_source
        elif "dice" in url:
            # example: https://www.dice.com/job-detail/6ab2ae92-d73e-4670-a846-a033cc1ac6b2
            job_id = url.split("/")[4]
            job_source["job_source"] = "Dice"
            if job_id != "":
                job_source["job_id"] = job_id
                job_source["job_url"] = f"https://www.dice.com/job-detail/{job_id}"
                return job_source
        elif "oraclecloud" in url and "nfcu" in url:
            # example: https://fa-etbx-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/nfcu/job/28116/?keyword=security&location=Vienna%252C+VA%252C+United+States&locationId=300000010092226&locationLevel=city&mode=job-location&radius=25&radiusUnit=MI
            job_id = url.split("/")[6]
            job_source["job_source"] = "OracleCloud-NFCU"

            job_id = url.split("/")[9]
            if job_id != "":
                job_source["job_id"] = job_id
                job_source["job_url"] = (
                    f"https://fa-etbx-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/nfcu/job/{job_id}"
                )
                return job_source
        elif "workday" in url:
            parsed_url = urlparse(url)
            job_url = parsed_url.path
            base_url = parsed_url.netloc
            company_name = base_url.split(".")[0]
            job_source["job_source"] = "Workday-" + company_name
            # match patter in url where the last underscore character is followed by 7 alphanumeric chars,capture that, and may have a hyphen which is to be ignored
            split_by_last_underscore = url.rsplit("_", 1)
            if len(split_by_last_underscore) > 1:
                job_id_match = re.search(r"([a-zA-Z0-9]+)", split_by_last_underscore[1])
                if job_id_match:
                    job_id = job_id_match.group(1)
                else:
                    job_id = random_id
            else:
                job_id = random_id

            job_source["job_id"] = job_id
            job_source["job_url"] = job_url
            return job_source

    return job_source


def get_list_of_files_desc(folder):
    files = os.listdir(folder)
    files.sort(
        key=lambda x: os.path.getmtime(os.path.join(folder, x)),
        reverse=True,
    )
    return files


def get_resource_folder_path() -> Path:
    return Path(str(resources.files("scrapescore.resources")))


def get_resource_file_path(filename: str) -> Path:
    resource_path = resources.files("scrapescore.resources").joinpath(filename)
    return Path(str(resource_path))



def get_resource_file_handle(filename: str):
    resource_path = get_resource_file_path(filename)
    return resource_path.open("r")


def read_resource_as_yaml(filename: str) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]

    return yaml.safe_load(get_resource_file_handle(filename))


def remove_pii(text: str, save_to_file: str | None = None) -> str:
    """Remove PII from the text."""
    # if the first line contains a candidate's first name and last name remove it
    lines = text.splitlines()
    if len(lines) > 0:
        text = (
            re.sub(
                r"^(\*\*)?[A-Za-z]+ [A-Za-z]+(\*\*)?", "firstname lastname", lines[0]
            )
            + "\n"
            + "\n".join(lines[1:])
        )

    # Remove email addresses
    text = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        "email@example.com",
        text,
    )
    # Remove phone numbers
    text = re.sub(
        r"(?:\(\d{3}\)[\s.-]*|\b\d{3}[\s.-]+)\d{3}[\s.-]+\d{4}\b", "123-456-7890", text
    )
    # Remove hyperlinks but leave text behind
    text = re.sub(r"<a\s[^>]*>(.*?)</a>", r"\1", text)
    # Now remove any remaining URLs
    text = re.sub(r"http\S+|www.\S+", "http://pii_replaced_example.com", text)
    if save_to_file:
        with open(save_to_file, "w", encoding="utf-8") as f:
            f.write(text)
    return text


if __name__ == "__main__":
    job_text = get_markdown_from_url(
        "https://fa-etbx-saasfaprod1.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/nfcu/job/28116/?keyword=security&location=Vienna%252C+VA%252C+United+States&locationId=300000010092226&locationLevel=city&mode=job-location&radius=25&radiusUnit=MI"
    )
    print(job_text)
    sys.exit()
