"""Unit tests for the exact-match title score cache.

Covers: exact hit (with normalization), miss, per-user isolation, per-role
isolation (an edited role starts fresh), disabled cache, and prune.

Usage:
    uv run pytest tests/test_title_score_cache.py
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from scrapescore.db_setup import create_title_score_cache_table  # noqa: E402
from scrapescore.lib import title_score_cache as tsc  # noqa: E402

ROLE = "Senior backend software engineer building distributed Python services"
ROLE_EDITED = "Senior backend software engineer building distributed Java services"
ALICE, BOB = "user-alice", "user-bob"


@pytest.fixture
def cache_env(tmp_path, monkeypatch):
    db_path = tmp_path / "cache.db"
    conn = sqlite3.connect(db_path)
    create_title_score_cache_table(conn.cursor())
    conn.commit()
    conn.close()

    monkeypatch.setattr(tsc, "get_db_connection", lambda *a, **k: sqlite3.connect(db_path))
    monkeypatch.setattr(
        tsc, "get_title_score_cache_config", lambda: {"enabled": True, "retention_days": 90}
    )
    return db_path


def test_exact_hit_with_normalization(cache_env):
    tsc.store(ROLE, {"Senior Software Engineer": "high"}, ALICE)
    # Different case/whitespace on both title and role still normalizes to a hit.
    resolved, misses, stats = tsc.resolve(
        ["  senior   software engineer "], ROLE.upper(), ALICE
    )
    assert misses == [] and list(resolved.values()) == ["high"]
    assert stats["exact"] == 1 and stats["llm"] == 0


def test_miss_on_new_title(cache_env):
    tsc.store(ROLE, {"Senior Software Engineer": "high"}, ALICE)
    resolved, misses, stats = tsc.resolve(["Registered Nurse"], ROLE, ALICE)
    assert misses == ["Registered Nurse"] and stats["llm"] == 1 and stats["exact"] == 0


def test_isolated_per_user(cache_env):
    tsc.store(ROLE, {"Senior Software Engineer": "high"}, ALICE)
    resolved, misses, stats = tsc.resolve(["Senior Software Engineer"], ROLE, BOB)
    assert misses == ["Senior Software Engineer"] and resolved == {}
    assert stats["llm"] == 1


def test_isolated_per_role_edit(cache_env):
    # An edited desired role is a different namespace -> cache does not carry over.
    tsc.store(ROLE, {"Senior Software Engineer": "high"}, ALICE)
    resolved, misses, stats = tsc.resolve(["Senior Software Engineer"], ROLE_EDITED, ALICE)
    assert misses == ["Senior Software Engineer"] and stats["exact"] == 0


def test_mixed_batch_partitions(cache_env):
    tsc.store(ROLE, {"Senior Software Engineer": "high", "Data Analyst": "low"}, ALICE)
    resolved, misses, stats = tsc.resolve(
        ["Senior Software Engineer", "Data Analyst", "Chef"], ROLE, ALICE
    )
    assert stats["total"] == 3 and stats["exact"] == 2 and stats["llm"] == 1
    assert misses == ["Chef"]
    assert resolved == {"Senior Software Engineer": "high", "Data Analyst": "low"}


def test_disabled_cache_sends_everything_to_llm(cache_env, monkeypatch):
    monkeypatch.setattr(
        tsc, "get_title_score_cache_config", lambda: {"enabled": False, "retention_days": 90}
    )
    tsc.store(ROLE, {"Senior Software Engineer": "high"}, ALICE)  # no-op when disabled
    resolved, misses, stats = tsc.resolve(["Senior Software Engineer"], ROLE, ALICE)
    assert resolved == {} and misses == ["Senior Software Engineer"]


def test_prune_removes_old_rows(cache_env):
    tsc.store(ROLE, {"Senior Software Engineer": "high"}, ALICE)
    conn = sqlite3.connect(cache_env)
    conn.execute("UPDATE title_score_cache SET date_updated = datetime('now', '-200 days')")
    conn.commit()
    conn.close()

    assert tsc.prune(retention_days=90) == 1

    conn = sqlite3.connect(cache_env)
    remaining = conn.execute("SELECT COUNT(*) FROM title_score_cache").fetchone()[0]
    conn.close()
    assert remaining == 0
