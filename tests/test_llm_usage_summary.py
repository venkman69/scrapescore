"""Verify emit_run_usage_summary emits one per-run rollup of LLM token usage + call count.

_log_usage() folds every call into an in-process accumulator (reset per run by
set_llm_run_id); emit_run_usage_summary() emits a single 'llm_usage_summary' JSON log line
tagged with run_id, carrying grand totals, the LLM call count, and a per-call_type
breakdown. These ride the JSON console handler (journald -> Vector/Loki) so Grafana can
report per-run token usage without summing individual llm_usage lines.

Usage:
    uv run pytest tests/test_llm_usage_summary.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scrapescore.lib import gemini_ai_runner


def _log(call_type, prompt, completion, duration=0):
    gemini_ai_runner._log_usage(
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "cache_hit_tokens": 0,
            "cache_miss_tokens": prompt,
        },
        provider="openai",
        model="test-model",
        call_type=call_type,
        duration_ms=duration,
    )


def test_summary_totals_call_count_and_breakdown(caplog):
    gemini_ai_runner.set_llm_run_id("summary-run-1")

    _log("resume_ats", 100, 10, duration=5)
    _log("title_scoring", 20, 4, duration=3)
    _log("title_scoring", 30, 6, duration=2)

    with caplog.at_level(logging.INFO, logger="scrapescore.lib.gemini_ai_runner"):
        gemini_ai_runner.emit_run_usage_summary()

    records = [
        r for r in caplog.records if getattr(r, "event", None) == "llm_usage_summary"
    ]
    assert len(records) == 1, "expected exactly one llm_usage_summary log record"

    rec = records[0]
    assert rec.run_id == "summary-run-1"
    assert rec.llm_call_count == 3
    assert rec.prompt_tokens == 150
    assert rec.completion_tokens == 20
    assert rec.total_tokens == 170
    assert rec.cache_miss_tokens == 150
    assert rec.duration_ms == 10

    breakdown = rec.by_call_type
    assert breakdown["resume_ats"]["call_count"] == 1
    assert breakdown["resume_ats"]["total_tokens"] == 110
    assert breakdown["title_scoring"]["call_count"] == 2
    assert breakdown["title_scoring"]["total_tokens"] == 60


def test_set_run_id_resets_accumulator(caplog):
    gemini_ai_runner.set_llm_run_id("summary-run-2")
    _log("ats_scoring", 42, 8)

    # Starting a new run must clear the previous run's totals.
    gemini_ai_runner.set_llm_run_id("summary-run-3")

    with caplog.at_level(logging.INFO, logger="scrapescore.lib.gemini_ai_runner"):
        gemini_ai_runner.emit_run_usage_summary()

    rec = [
        r for r in caplog.records if getattr(r, "event", None) == "llm_usage_summary"
    ][-1]
    assert rec.run_id == "summary-run-3"
    assert rec.llm_call_count == 0
    assert rec.total_tokens == 0
    assert rec.by_call_type == {}
