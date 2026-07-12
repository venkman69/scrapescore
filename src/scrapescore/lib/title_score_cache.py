"""Exact-match cache for job-title compatibility scores.

Title scoring maps ``(title, desired_role_description)`` to a coarse
``high|medium|low`` bucket via the LLM. The same titles recur across scrape runs,
so caching prior decisions and reusing them on an exact repeat avoids most of that
LLM spend — the reliable, dominant saving.

Scope is per ``(owning_user, normalized role, normalized title)``:

- **Hit** — the same user has this exact (normalized) title cached under this exact
  (normalized) role -> reuse the score, no LLM.
- **Miss** — anything else (new title, edited role, different user) goes to the LLM,
  then :func:`store`.

Design note: an earlier revision tried embedding-similarity layers (semantic title
match, role-similarity for typo tolerance, cross-user sharing). Calibration showed
static embeddings cannot reliably separate the distinctions that matter here (a typo
vs. a meaningful one-word role edit; an abbreviation vs. a different seniority), so
those layers were dropped in favor of this simple, correct, dependency-free cache.
A role edit therefore starts a fresh namespace and the cache re-warms — bounded and
always correct (a score is never reused for a different role).

Everything is best-effort: on any DB error :func:`resolve` returns all titles as
misses so the LLM path still runs.
"""

from __future__ import annotations

import logging
import re

from scrapescore.db_setup import get_db_connection
from scrapescore.lib.config import get_title_score_cache_config

logger = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")


def is_enabled() -> bool:
    return get_title_score_cache_config()["enabled"]


def _normalize(text: str) -> str:
    """Lowercase, strip, and collapse internal whitespace."""
    return _WHITESPACE_RE.sub(" ", (text or "").strip().lower())


def resolve(titles: list[str], desired_role_description: str, owning_user: str):
    """Split ``titles`` into cache-resolved scores and LLM misses.

    Returns ``(resolved, misses, stats)`` where ``resolved`` maps the original title
    string -> score, ``misses`` are titles the caller must LLM-score, and ``stats``
    has counts ``{total, exact, llm}``. Only this user's entries under this exact
    (normalized) role are considered.

    On any failure this degrades to "everything is a miss" so scoring proceeds.
    """
    stats = {"total": len(titles), "exact": 0, "llm": 0}
    if not titles:
        return {}, [], stats
    if not is_enabled():
        stats["llm"] = len(titles)
        return {}, list(titles), stats

    role_norm = _normalize(desired_role_description)
    try:
        conn = get_db_connection()
        try:
            rows = conn.execute(
                "SELECT title_normalized, score FROM title_score_cache "
                "WHERE owning_user = ? AND role_normalized = ?",
                (owning_user, role_norm),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("title cache resolve failed (%s); scoring all via LLM", e)
        stats["llm"] = len(titles)
        return {}, list(titles), stats

    cached = {r[0]: r[1] for r in rows}
    resolved: dict[str, str] = {}
    misses: list[str] = []
    for title in titles:
        score = cached.get(_normalize(title))
        if score is not None:
            resolved[title] = score
            stats["exact"] += 1
        else:
            misses.append(title)

    stats["llm"] = len(misses)
    return resolved, misses, stats


def store(
    desired_role_description: str,
    title_scores: dict[str, str],
    owning_user: str,
    source: str = "llm",
) -> None:
    """Upsert ``{title: score}`` for this user under this (normalized) role."""
    if not title_scores or not is_enabled():
        return

    role_norm = _normalize(desired_role_description)
    rows = [
        (owning_user, role_norm, _normalize(t), t, score, source)
        for t, score in title_scores.items()
    ]
    try:
        conn = get_db_connection()
        try:
            conn.executemany(
                "INSERT OR REPLACE INTO title_score_cache "
                "(owning_user, role_normalized, title_normalized, title_raw, score, source, date_updated) "
                "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                rows,
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("title cache store failed: %s", e)


def prune(retention_days: int | None = None) -> int:
    """Delete cache rows older than retention_days. Returns rows deleted."""
    if retention_days is None:
        retention_days = get_title_score_cache_config()["retention_days"]
    try:
        conn = get_db_connection()
        try:
            cur = conn.execute(
                "DELETE FROM title_score_cache WHERE date_updated < datetime('now', ?)",
                (f"-{int(retention_days)} days",),
            )
            deleted = cur.rowcount
            conn.commit()
            return deleted
        finally:
            conn.close()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("title cache prune failed: %s", e)
        return 0
