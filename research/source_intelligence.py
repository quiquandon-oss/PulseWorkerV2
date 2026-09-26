"""
Experiment 5, Part 2: deterministic source-level intelligence.

Builds on, and deliberately does not duplicate:
  - source_analysis.discover_sources / extract_source_matrix /
    pairwise_source_redundancy (source enumeration + redundancy over
    `history` -- reused, never reimplemented, for the redundancy
    question).
  - event_source_relevance.source_topic_affinity / classify_relevance
    (which of the 21 keys has any direct textual bridge to PR4's real
    evidence feeds -- reused verbatim for cross-referencing).
  - evidence_collector's own content-hash-based article deduplication
    (PR4 already refuses to store a duplicate article in
    research_event_evidence; this module reads that table as ALREADY
    deduplicated at the article level -- it does not re-deduplicate
    articles, only reasons about sources).

This module answers the questions those three do not:
  - is a key present in a source's own sources_json but NOT one of the
    21 known V1 ids (a genuine new-source CANDIDATE, never auto-added)?
  - how long since a source's value last changed at all (STALE)?
  - what fraction of the observation window actually carries each
    source at all (RECURRENCE / coverage)?
"""
from source_analysis import pairwise_source_redundancy

# The 21 known V1 source ids, copied verbatim from CryptoPulse/index.html's
# COMPOSITE_SOURCES_DEFAULTS (read directly from source during the
# forensic phase, not memorized). Used ONLY as a comparison set to flag
# an unrecognized key as a candidate -- this module never imports,
# writes to, or otherwise touches that frontend file.
KNOWN_V1_SOURCE_IDS = frozenset([
    "fng", "funding", "longshort", "global", "cryptonews", "macrogeo",
    "geopolitics", "regulatory", "sosovalue", "onchain", "oil", "yield10y",
    "usd", "nasdaq", "sp500", "ninemag", "foufi", "etfflows", "hypefunding",
    "gold", "strc",
])

STALE_AFTER_N_OBSERVATIONS = 10
# A source whose value has not changed AT ALL across this many
# consecutive observations (where it was present) is flagged STALE.
# This is a descriptive flag, not a claim the feed is broken -- the
# same "small, mechanical, disclosed" discipline as
# event_source_relevance.py's own topic-affinity bridge. Distinct from
# source_coverage_report's own all-time NO_VARIATION flag: this is the
# same idea applied to a trailing window, so a source that WAS varying
# months ago but has gone flat recently is still caught.


def detect_candidate_sources(archive_rows):
    """archive_rows: rows from sentiment_archive.get_archive_range(),
    each with a parsed "sources" dict. Returns the sorted set of keys
    present in ANY row's sources but NOT in KNOWN_V1_SOURCE_IDS -- a
    candidate for future human consideration, never auto-added
    anywhere. Deterministic: identical input always produces the
    identical candidate set."""
    seen = set()
    for row in archive_rows:
        seen.update((row.get("sources") or {}).keys())
    return sorted(seen - KNOWN_V1_SOURCE_IDS)


def detect_stale_sources(archive_rows, min_observations=STALE_AFTER_N_OBSERVATIONS):
    """For each source key present at all in the window, checks whether
    its trailing min_observations values (chronological, only among
    rows where the key is present -- a source that simply isn't
    reported some cycles is MISSING for those rows, not evidence of
    staleness) are all identical. Returns {source_key: bool}; a source
    with fewer than min_observations total appearances is reported
    False (not enough history to call it stale either way -- never
    guessed)."""
    per_source_values = {}
    for row in archive_rows:
        for key, value in (row.get("sources") or {}).items():
            per_source_values.setdefault(key, []).append(value)

    stale = {}
    for key, values in per_source_values.items():
        trailing = values[-min_observations:]
        stale[key] = len(trailing) >= min_observations and len(set(trailing)) == 1
    return stale


def source_recurrence(archive_rows):
    """Coverage of each source key across the window -- how many rows
    actually carry it, out of how many rows exist at all. Mirrors
    source_coverage_report's own coverage/missingness question, computed
    directly over the archive so Experiment 5 does not need a second,
    parallel read path into `history` itself."""
    n_rows = len(archive_rows)
    counts = {}
    for row in archive_rows:
        for key in (row.get("sources") or {}).keys():
            counts[key] = counts.get(key, 0) + 1
    return {
        key: {
            "n_present": n,
            "n_rows": n_rows,
            "coverage_pct": round(100.0 * n / n_rows, 1) if n_rows else None,
        }
        for key, n in counts.items()
    }


def redundant_source_pairs(archive_rows, source_keys):
    """Thin adapter over source_analysis.pairwise_source_redundancy --
    reshapes archive rows into that function's expected {"sources": ...}
    input shape and delegates entirely; does not reimplement
    correlation. Returns only the pairs flagged strong_redundancy (the
    same >=0.7 threshold source_analysis.py already uses)."""
    shaped = [{"sources": row.get("sources") or {}} for row in archive_rows]
    raw = pairwise_source_redundancy(shaped, source_keys)
    return {f"{a}|{b}": v for (a, b), v in raw.items() if v.get("strong_redundancy")}
