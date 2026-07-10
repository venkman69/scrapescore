"""Verify _log_usage emits a structured 'llm_usage' log line tagged with run_id.

These fields ride the JSON console handler (journald -> Vector/Loki) so Grafana can
group per-run token usage by run_id.

Usage:
    uv run pytest tests/test_llm_usage_logging.py
"""

import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scrapescore.lib import gemini_ai_runner


def test_log_usage_emits_run_id_and_token_fields(caplog):
    gemini_ai_runner.set_llm_run_id("test-run-123")

    usage = {
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "total_tokens": 15,
        "cache_hit_tokens": 2,
        "cache_miss_tokens": 8,
    }

    with caplog.at_level(logging.INFO, logger="scrapescore.lib.gemini_ai_runner"):
        gemini_ai_runner._log_usage(
            usage, provider="openai", model="test-model",
            call_type="ats_scoring", duration_ms=42,
        )

    records = [r for r in caplog.records if getattr(r, "event", None) == "llm_usage"]
    assert len(records) == 1, "expected exactly one llm_usage log record"

    rec = records[0]
    assert rec.run_id == "test-run-123"
    assert rec.provider == "openai"
    assert rec.model == "test-model"
    assert rec.call_type == "ats_scoring"
    assert rec.prompt_tokens == 10
    assert rec.completion_tokens == 5
    assert rec.total_tokens == 15
    assert rec.cache_hit_tokens == 2
    assert rec.cache_miss_tokens == 8
    assert rec.duration_ms == 42


def test_log_usage_skips_empty_usage(caplog):
    with caplog.at_level(logging.INFO, logger="scrapescore.lib.gemini_ai_runner"):
        gemini_ai_runner._log_usage({}, provider="openai", model="m", call_type="x")

    assert not [r for r in caplog.records if getattr(r, "event", None) == "llm_usage"]
