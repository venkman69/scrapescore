import contextvars
import json
import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from scrapescore.lib import utils
from scrapescore.lib.config import APP_CONFIG
from scrapescore.lib.gemini_client import GeminiClient
from scrapescore.lib.models import (
    ATSResumeResult,
    ATSScoreResult,
    CitizenshipStatus,
    ClearanceStatus,
    JobDescriptionToDetails,
    JobTitleScores,
)

logger = logging.getLogger(__name__)

# Current job_finder run id, used to tag LLM usage log lines so Loki/Grafana can group
# per-run token usage. Set once at the start of a run via set_llm_run_id().
_current_run_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "llm_run_id", default=""
)

# In-process accumulator of this run's LLM token usage, summed across every _log_usage()
# call so emit_run_usage_summary() can emit a single per-run rollup at the end of the run.
# All usage-producing LLM calls happen in the main process, so a module-level dict suffices.
_run_usage: dict = {}


def _reset_run_usage() -> None:
    """Clear the per-run LLM usage accumulator (called at the start of each run)."""
    _run_usage.clear()
    _run_usage.update(
        {
            "call_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": 0,
            "duration_ms": 0,
            "by_call_type": {},  # call_type -> same numeric fields incl. call_count
        }
    )


_reset_run_usage()


def set_llm_run_id(run_id: str) -> None:
    """Tag subsequent LLM usage log lines with this job_finder run id."""
    _current_run_id.set(run_id or "")
    _reset_run_usage()


def extract_and_validate_json(response_text: str, model: Any) -> dict:
    """
    Extracts JSON from the response text and validates it against the provided Pydantic model.

    Raises:
        ValueError: If JSON content is not found in the response
        ValidationError: If the JSON fails pydantic validation
    """
    logger.info("Extracting and validating JSON...")
    # Try to find JSON block
    # Pattern looks for ```json (or nothing), content, then ```
    # match = re.search(r"```(?:json)?\s*(.*?)\s*```", response_text, re.DOTALL)

    # Try object extraction: find first '{' and last '}'
    obj_start = response_text.find("{")
    obj_end = response_text.rfind("}")
    json_str = response_text[obj_start : obj_end + 1] if obj_start != -1 and obj_end != -1 else ""

    # Try array extraction: find first '[' and last ']'
    arr_start = response_text.find("[")
    arr_end = response_text.rfind("]")
    arr_str = response_text[arr_start : arr_end + 1] if arr_start != -1 and arr_end != -1 else ""

    # Prefer object; fall back to array if object is a fragment inside the array
    if arr_str and (not json_str or (arr_start < obj_start)):
        json_str = arr_str

    if not json_str:
        logger.error(
            f"JSON content not found in the response. Response: {response_text}"
        )
        raise ValueError("No JSON content found in the response.")

    # Validate with Pydantic
    try:
        validated_data = model.model_validate_json(json_str)
        return validated_data.model_dump()
    except Exception as e:
        # Log detailed error information including the response payload
        logger.error(
            f"Pydantic validation failed for model {model.__name__}. "
            f"Error: {type(e).__name__}: {e}"
        )
        logger.error(f"Response text that caused validation error:\n{response_text}")
        logger.error(f"Extracted JSON string that caused validation error:\n{json_str}")
        raise e  # Re-raise the exception to be handled upstream


def _run_gemini_redis_automation(prompt_str: str, model: Any) -> tuple[dict, dict]:
    client = GeminiClient()
    timeout = 180

    logger.info("Submitting prompt to gemini_ai_runner via Redis...")
    response = client.submit_and_wait(prompt_str, timeout=timeout)

    if response.get("status") == "error":
        logger.error(f"gemini_ai_runner returned error: {response.get('error')}")
        return {"error": response.get("error", "Unknown error from gemini_ai_runner")}, {}

    if response.get("result") and isinstance(response["result"], dict):
        try:
            if model:
                validated = model.model_validate(response["result"])
                return validated.model_dump(), {}
        except Exception:
            logger.debug("Server-provided result failed validation, trying raw_text")

    raw_text = response.get("raw_text", "")
    if raw_text:
        return extract_and_validate_json(raw_text, model), {}

    return {"error": "No response content from gemini_ai_runner"}, {}


_CANDIDATE_SPLIT_MARKER = "<!-- CANDIDATE -->"
_USER_SPLIT_MARKER = "<!-- USER -->"


def _run_openai_automation(prompt_str: str, model: Any, llm_cfg: dict) -> tuple[dict, dict]:
    import openai

    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    base_url = llm_cfg.get("base_url")
    llm_model = llm_cfg.get("model")

    api_timeout = llm_cfg.get("timeout", 180)
    client = openai.OpenAI(api_key=api_key, base_url=base_url, timeout=api_timeout)
    logger.info(f"Calling OpenAI-compatible API: {base_url} model={llm_model} timeout={api_timeout}s")

    if _CANDIDATE_SPLIT_MARKER in prompt_str and _USER_SPLIT_MARKER in prompt_str:
        # Three-part split: instructions | candidate profile | job data
        # system = instructions + candidate profile (stable per user → cached)
        # user   = job-specific data (varies per call)
        instructions, rest = prompt_str.split(_CANDIDATE_SPLIT_MARKER, 1)
        candidate_part, user_part = rest.split(_USER_SPLIT_MARKER, 1)
        system_content = instructions.strip() + "\n\n" + candidate_part.strip()
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_part.strip()},
        ]
        logger.debug("Using system(instructions+candidate)/user(job) split for prompt caching")
    elif _USER_SPLIT_MARKER in prompt_str:
        # Two-part split: instructions | variable data
        system_part, user_part = prompt_str.split(_USER_SPLIT_MARKER, 1)
        messages = [
            {"role": "system", "content": system_part.strip()},
            {"role": "user", "content": user_part.strip()},
        ]
        logger.debug("Using system/user message split for prompt caching")
    else:
        messages = [{"role": "user", "content": prompt_str}]

    response = client.chat.completions.create(
        model=llm_model,
        messages=messages,
        max_tokens=llm_cfg.get("max_tokens", 1024)
    )

    usage_dict = {}
    usage = response.usage
    if usage:
        extra = getattr(usage, "model_extra", {}) or {}
        cache_hit = extra.get("prompt_cache_hit_tokens", 0)
        cache_miss = extra.get("prompt_cache_miss_tokens", 0)
        logger.info(
            f"LLM usage — prompt: {usage.prompt_tokens}, completion: {usage.completion_tokens}, "
            f"cache hit: {cache_hit}, cache miss: {cache_miss}"
        )
        usage_dict = {
            "prompt_tokens": usage.prompt_tokens or 0,
            "completion_tokens": usage.completion_tokens or 0,
            "total_tokens": usage.total_tokens or 0,
            "cache_hit_tokens": cache_hit,
            "cache_miss_tokens": cache_miss,
        }

    raw_text = response.choices[0].message.content
    if model:
        try:
            return extract_and_validate_json(raw_text, model), usage_dict
        except Exception as e:
            logger.error(f"Parse/validation failed after OpenAI API call: {type(e).__name__}: {e}")
            return {"error": str(e)}, usage_dict
    return {"raw_text": raw_text}, usage_dict


def _log_usage(usage: dict, provider: str, model: str, call_type: str, duration_ms: int = 0):
    """Insert a row into llm_usage_log for the given usage dict."""
    if not usage:
        return
    try:
        from scrapescore.db_setup import get_db_connection
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO llm_usage_log
                (provider, model, call_type, prompt_tokens, completion_tokens,
                 total_tokens, cache_hit_tokens, cache_miss_tokens, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                provider,
                model,
                call_type,
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
                usage.get("cache_hit_tokens", 0),
                usage.get("cache_miss_tokens", 0),
                duration_ms,
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning(f"Failed to log LLM usage to DB: {e}")

    # Emit one structured JSON log line (mirrors llm_usage_log fields, plus run_id) so the
    # journald/Vector/Loki pipeline can group per-run token usage in Grafana by run_id.
    logger.info(
        "llm_usage",
        extra={
            "event": "llm_usage",
            "run_id": _current_run_id.get(),
            "provider": provider,
            "model": model,
            "call_type": call_type,
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "cache_hit_tokens": usage.get("cache_hit_tokens", 0),
            "cache_miss_tokens": usage.get("cache_miss_tokens", 0),
            "duration_ms": duration_ms,
        },
    )

    # Fold this call into the per-run accumulator for the end-of-run summary.
    _accumulate_run_usage(usage, call_type, duration_ms)


_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cache_hit_tokens",
    "cache_miss_tokens",
)


def _accumulate_run_usage(usage: dict, call_type: str, duration_ms: int) -> None:
    """Add one call's usage into the grand totals and the per-call_type breakdown."""
    _run_usage["call_count"] += 1
    _run_usage["duration_ms"] += duration_ms
    for field in _TOKEN_FIELDS:
        _run_usage[field] += usage.get(field, 0)

    bucket = _run_usage["by_call_type"].setdefault(
        call_type or "",
        {"call_count": 0, "duration_ms": 0, **{f: 0 for f in _TOKEN_FIELDS}},
    )
    bucket["call_count"] += 1
    bucket["duration_ms"] += duration_ms
    for field in _TOKEN_FIELDS:
        bucket[field] += usage.get(field, 0)


def emit_run_usage_summary() -> None:
    """Emit one JSON log line summarizing this run's total LLM usage + call count."""
    logger.info(
        "llm_usage_summary",
        extra={
            "event": "llm_usage_summary",
            "run_id": _current_run_id.get(),
            "llm_call_count": _run_usage.get("call_count", 0),
            "prompt_tokens": _run_usage.get("prompt_tokens", 0),
            "completion_tokens": _run_usage.get("completion_tokens", 0),
            "total_tokens": _run_usage.get("total_tokens", 0),
            "cache_hit_tokens": _run_usage.get("cache_hit_tokens", 0),
            "cache_miss_tokens": _run_usage.get("cache_miss_tokens", 0),
            "duration_ms": _run_usage.get("duration_ms", 0),
            "by_call_type": _run_usage.get("by_call_type", {}),
        },
    )


def run_gemini_automation(
    prompt_str: str, model: Any = None, headless: bool = True, call_type: str = ""
) -> tuple[dict, dict]:
    """
    Submit a prompt to the configured LLM provider.

    Routes to either the gemini_ai_runner Redis queue or an OpenAI-compatible
    API directly, based on the `llm_provider.provider` setting in config.yaml.

    Args:
        prompt_str: The prompt string to submit
        model: The Pydantic model to validate the response against
        headless: Ignored (legacy parameter)
        call_type: Label for the type of call (logged to llm_usage_log)

    Returns:
        tuple[dict, dict]: (result, usage_dict)
    """
    llm_cfg = APP_CONFIG.get("llm_provider", {})
    provider = llm_cfg.get("provider", "gemini")
    llm_model = llm_cfg.get("model", "")

    if provider == "openai":
        logger.info(f"LLM provider: openai ({llm_cfg.get('base_url')} / {llm_model})")
        try:
            result, usage = _run_openai_automation(prompt_str, model, llm_cfg)
            _log_usage(usage, provider, llm_model, call_type)
            return result, usage
        except Exception as e:
            import openai as _openai
            is_quota = isinstance(e, _openai.RateLimitError) or (
                isinstance(e, _openai.APIStatusError) and e.status_code == 402
            )
            if is_quota:
                logger.warning(
                    f"OpenAI-compatible API quota exhausted ({type(e).__name__}: {e}), "
                    "falling back to Redis/Gemini"
                )
                result, usage = _run_gemini_redis_automation(prompt_str, model)
                return result, usage
            raise
    else:
        logger.info("LLM provider: gemini (Redis queue)")
        result, usage = _run_gemini_redis_automation(prompt_str, model)
        return result, usage


def analyze_resume_ats(resume_text: str) -> dict:
    """
    Evaluate a resume against 24 ATS quality criteria using Gemini.

    Returns a dict matching ATSResumeResult, or {"error": "..."} on failure.
    """
    schema = ATSResumeResult.model_json_schema()
    prompt = f"""As an automated applicant tracking system resume quality evaluator, \
evaluate the resume below and assign a score (1-10) for each category. \
Also provide a one-sentence analysis for each scored category.

Categories:
Resume Basics
1. Resume Clarity
2. Contact Information
3. Chronological Order
4. Formatting
5. Resume Length
---
Summary Strength
6. Headline
7. Summary
---
Experience Audit
8. Experience Details
9. Recent Experience
10. Role Separation
---
Achievements
11. Quantified Achievements
12. Technologies
13. Numbers Placement
---
Language and Tone
14. Verb Usage
15. Grammar
16. Punctuation
17. Voice and Terse
---
Visual Impact
18. Text Format
19. Layout
20. Font Styles
21. File Size
---
ATS Summary
22. How well ATS interprets the resume (score + analysis)
23. Top matching job titles for the resume (list of strings, no scores)
24. Key Skills ATS recognizes in your resume (list of strings)

Respond ONLY with valid JSON matching this exact schema — no markdown, no explanation:
{json.dumps(schema, indent=2)}

<!-- USER -->

Resume:
{resume_text}
"""
    result, _ = run_gemini_automation(prompt, ATSResumeResult, call_type="resume_ats")
    return result


def redact_resume_or_jd(md_text: str) -> str:
    """
    Redact the job description.

    Args:
        job_description: The job description.

    Returns:
        str: The redacted job description.
    """
    lines = md_text.split("\n")
    redacted_lines = []
    reject_words = [
        "![SVG",
        "http",
        "The Fair Chance Act",
        "**Reasonable Accommodation:**",
        "**Disability Employment:**",
        "**IMPORTANT INFORMATION FOR SURPLUS OR DISPLACED FEDERAL EMPLOYEES:**",
        "PERMANENT CHANGE OF STATION (PCS)",
        "DIRECT DEPOSIT:",
        "**Equal Employment Opportunity (EEO)",
    ]
    for line in lines:
        # stop when encounter "Benefits" in the usa jobs after this it is junk
        if line.startswith("Benefits"):
            break
        if len(line.strip()) == 0:
            continue
        if any(word in line for word in reject_words):
            continue
        if "**Reasonable Accommodation:**" in line:
            continue
        redacted_lines.append(line)

    return "\n".join(redacted_lines)


def job_description_to_details_extractor_gemini(
    job_description: str, headless: bool = True
) -> dict:
    """
    Extract job details from the job description using Gemini AI.

    Args:
        job_description: The job description.
        headless: Whether to run Playwright in headless mode (default: True)

    Returns:
        dict: A dictionary containing job details.
    """
    config = utils.read_resource_as_yaml("job_finder_config.yaml")
    gemini_template = utils.get_resource_file_path(
        config["gemini_job_description_extract_details_template"]
    )
    with open(gemini_template, "r") as f:
        gemini_prompt_str = f.read()
    prompt_str = gemini_prompt_str.format(job_description=job_description)
    start_time = utils.current_time_millis()
    result, _ = run_gemini_automation(
        prompt_str, model=JobDescriptionToDetails, headless=headless
    )
    end_time = utils.current_time_millis()
    logger.info(f"Gemini AI analysis took {end_time - start_time} ms")
    return result


def hr_title_analyzer_gemini(
    job_titles: list[str], desired_role_description: str
) -> list[dict[str, str]]:
    """
    Evaluate job titles compatibility with the desired role description.

    Args:
        job_titles: A list of job titles.
        desired_role_description: The desired role description.

    Returns:
        list[dict]: A list of job titles with their compatibility score.
    """
    config = utils.read_resource_as_yaml("job_finder_config.yaml")
    gemini_template = utils.get_resource_file_path(
        config["gemini_title_vs_desired_role_template"]
    )
    with open(gemini_template, "r") as f:
        gemini_prompt_str = f.read()
    prompt_str = gemini_prompt_str.format(
        job_titles=json.dumps(job_titles, indent=2),
        desired_role_description=desired_role_description,
    )
    start_time = utils.current_time_millis()
    result, _ = run_gemini_automation(prompt_str, model=JobTitleScores, call_type="title_scoring")
    end_time = utils.current_time_millis()
    logger.info(f"Gemini AI analysis took {end_time - start_time} ms")
    # Extract scores from the result dict to match the function's return type
    if isinstance(result, dict) and "scores" in result:
        return result["scores"]
    elif isinstance(result, dict) and "error" in result:
        logger.error(
            f"Error in Gemini analysis, retrying 1 more time: {result['error']}"
        )
        # retry one more time:
        result, _ = run_gemini_automation(prompt_str, model=JobTitleScores, call_type="title_scoring")
        if isinstance(result, dict) and "scores" in result:
            return result["scores"]
        elif isinstance(result, list):
            return result
        else:
            logger.error(
                f"Unexpected result type after retry: {type(result)}, result: {result}"
            )
            return []
    elif isinstance(result, list):
        return result
    else:
        logger.error(f"Unexpected result type: {type(result)}, result: {result}")
        return []


# def save_gemini_analysis(
#     prompt_str: str, job_details: dict, prefix: str, result_json: dict
# ) -> str:
#     if result_json is None:
#         return "Not Saved"
#     job_filename = prefix + "_" + get_job_file_name(result_json, job_details)
#     logger.info(f"Writing {prefix} task output")
#     output_path = Path("./work/crew_output") / job_filename
#     with open(output_path, "w", encoding="utf-8") as f:
#         f.write(prompt_str)
#         f.write("-------------")
#         f.write(json.dumps(result_json, indent=2))
#     return job_filename


# ============================================================================
# ATS Scoring Functions (JIRA-093)
# ============================================================================

def _normalize_enum_value(value: str, enum_type: type) -> str:
    """
    Normalize an enum string to its human-readable value.
    Converts "ClearanceStatus.NONE_REQUIRED" to "None Required".

    Args:
        value: The value string (may be enum format or plain value)
        enum_type: The Enum class to check against

    Returns:
        The human-readable enum value (e.g., "None Required")
    """
    if not value or not isinstance(value, str):
        return value

    # Check for enum format like "EnumType.VALUE"
    if "." in value:
        parts = value.split(".")
        if len(parts) >= 2:
            # Try to match against enum member names (case-insensitive)
            enum_name_part = parts[0]
            member_name_part = parts[-1]  # Get the last part as the member name
            if enum_name_part == enum_type.__name__:
                for member in enum_type:
                    if member.name.upper() == member_name_part.upper():
                        logger.info(f"Normalized enum value: {value} -> {member.value}")
                        return member.value
        logger.warning(f"Unrecognized enum format: {value}")
        return value

    # If no enum format, return as-is
    return value


def _normalize_ats_result_enums(result: dict) -> dict:
    """
    Normalize all enum values in the ATS scoring result.
    Converts enum names like "ClearanceStatus.NONE_REQUIRED" to human-readable values
    like "None Required" before saving to the database.

    Args:
        result: The ATS scoring result dictionary

    Returns:
        The result with normalized enum values
    """
    if not isinstance(result, dict):
        return result

    # Debug logging
    clearance_assessment = result.get("clearance_assessment")
    if clearance_assessment and isinstance(clearance_assessment, dict):
        original_status = clearance_assessment.get("status", "")
        logger.info(f"Original clearance status before normalization: {repr(original_status)}")

    # Normalize clearance_assessment.status
    if isinstance(clearance_assessment, dict):
        status = clearance_assessment.get("status")
        if status:
            normalized = _normalize_enum_value(status, ClearanceStatus)
            clearance_assessment["status"] = normalized
            logger.info(f"Normalized clearance status: {repr(status)} -> {repr(normalized)}")

    # Normalize citizenship_assessment.status
    citizenship_assessment = result.get("citizenship_assessment")
    if isinstance(citizenship_assessment, dict):
        status = citizenship_assessment.get("status")
        if status:
            normalized = _normalize_enum_value(status, CitizenshipStatus)
            citizenship_assessment["status"] = normalized
            logger.info(f"Normalized citizenship status: {repr(status)} -> {repr(normalized)}")

    return result


def ats_score_analyzer_gemini(
    job_description: str,
    resume: str,
    desired_role_description: str,
    job_details: dict,
    us_citizen: bool,
    security_clearance: str,
    headless: bool = True,
) -> tuple[dict, str, dict]:
    """
    Analyze the job vs resume using Gemini AI with the new ATS scoring schema.

    This function uses the ATS scoring prompt template to get a comprehensive
    analysis including clearance assessment, job requirements, detailed scoring
    breakdown, and strategic pivots.

    Args:
        job_description: The job description text
        resume: The resume text
        desired_role_description: The candidate's desired role description
        job_details: Dictionary containing job details (job_url, job_id, job_source, etc.)
        us_citizen: Whether the candidate is a US citizen
        security_clearance: The candidate's security clearance status
        headless: Whether to run Playwright in headless mode (default: True)

    Returns:
        tuple[dict, str, dict]:
            - result_json: The ATS scoring result with decision and schema_version
            - save_path: The path where the result was saved
            - usage_metrics: Empty dict (for compatibility with legacy function)
    """
    config = utils.read_resource_as_yaml("job_finder_config.yaml")
    gemini_template_file_name = config.get(
        "gemini_ats_score_template", "gemini_ats_score_template.md"
    )
    gemini_template = utils.get_resource_file_path(gemini_template_file_name)

    # Redact sensitive information from resume and job description
    job_description = redact_resume_or_jd(job_description)
    resume = utils.remove_pii(resume)
    resume = redact_resume_or_jd(resume)

    # Build the prompt
    with open(gemini_template, "r") as f:
        gemini_prompt_str = f.read()

    prompt_str = gemini_prompt_str.format(
        resume=resume,
        us_citizen="Yes" if us_citizen else "No",
        security_clearance=security_clearance,
        job_url=job_details.get("job_url", "none"),
        job_id=job_details.get("job_id", "none"),
        job_source=job_details.get("job_source", "none"),
        job_description=job_description,
    )

    logger.debug(f"Prompt str length: {len(prompt_str)}")

    # Check prompt size limit
    max_gemini_text_size = 32061
    if len(prompt_str) > max_gemini_text_size:
        logger.warning(
            f"Prompt string exceeds paste area in gemini and will be truncated to {max_gemini_text_size} chars"
        )

    # Run Gemini analysis
    start_time = utils.current_time_millis()
    result, usage = run_gemini_automation(
        prompt_str, model=ATSScoreResult, headless=headless, call_type="ats_scoring"
    )
    end_time = utils.current_time_millis()
    logger.info(f"Gemini AI ATS analysis took {end_time - start_time} ms")

    # Post-process the result
    if result and isinstance(result, dict) and "error" not in result:
        # 0. Normalize enum values (convert "ClearanceStatus.NONE_REQUIRED" to "None Required")
        result = _normalize_ats_result_enums(result)

        # 1. Inject schema_version
        result["schema_version"] = "1.0"

        # Trust Gemini's decision - it has full context including "Ability to Obtain" clearance logic
        decision = result.get("decision", "Unknown")
        logger.info(f"ATS Analysis complete - Decision: {decision}, Score: {result.get('ats_score_estimate', {}).get('total_overall_score', 'N/A')}")

        # Save the result
        # save_path = save_gemini_analysis(prompt_str, job_details, "ats_analysis", result)

        return result, "", usage
    else:
        logger.error(f"ATS analysis failed or returned error: {result}")
        return result, "", usage


# ============================================================================
# End of ATS Scoring Functions
# ============================================================================
