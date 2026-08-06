"""Regression tests for the P0 fix: cold-start/sibling classification must be
based on `sibling_index` (dispatch order), not completion timestamp.

Completion order can legitimately differ from dispatch order under the
concurrent sibling fan-out this proxy exists to serve -- a warm sibling can
finish its whole response before the cold-start request finishes prefilling.
Sorting by completion timestamp instead of dispatch order can mislabel a
genuinely-warm sibling as the cold start (or vice versa), which corrupts the
exact split the FM-1 measurement depends on. See
bench/analyze_log.py:classify_cold_start_vs_sibling and ARCHITECTURE.md §9.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bench"))

from analyze_log import classify_cold_start_vs_sibling  # noqa: E402

from impactrouter.models import RequestLogEntry


def _entry(**overrides) -> dict:
    defaults = dict(
        timestamp="2026-01-01T00:00:00.000000+00:00",
        request_id="req0000",
        parent_hash="hash_a",
        scope=None,
        backend_id="backend_0",
        routing_mode="affinity",
        routing_outcome="affinity_hit",
        ttft_ms=100.0,
        total_latency_ms=200.0,
        prompt_char_len=42,
        sibling_index=None,
    )
    defaults.update(overrides)
    return RequestLogEntry(**defaults).model_dump()


def test_classification_uses_sibling_index_despite_out_of_order_completion():
    """sibling_index=1 and sibling_index=2 both complete BEFORE
    sibling_index=0 -- a legitimate outcome under concurrent affinity-routed
    dispatch. Classification must still identify index 0 as the cold start,
    regardless of completion timestamp order."""
    cold_start_dispatched_first = _entry(
        request_id="req0000",
        sibling_index=0,
        timestamp="2026-01-01T00:00:00.500000+00:00",  # completes LAST
        routing_outcome="affinity_miss_new",
        ttft_ms=300.0,
    )
    sibling_completes_first = _entry(
        request_id="req0001",
        sibling_index=1,
        timestamp="2026-01-01T00:00:00.100000+00:00",  # completes FIRST
        routing_outcome="affinity_hit",
        ttft_ms=50.0,
    )
    sibling_completes_second = _entry(
        request_id="req0002",
        sibling_index=2,
        timestamp="2026-01-01T00:00:00.200000+00:00",
        routing_outcome="affinity_hit",
        ttft_ms=60.0,
    )

    # Deliberately shuffled input order too, to prove classification doesn't
    # depend on list order either.
    group = [sibling_completes_first, cold_start_dispatched_first, sibling_completes_second]

    cold_start, siblings = classify_cold_start_vs_sibling(group)

    assert cold_start["request_id"] == "req0000"
    assert {s["request_id"] for s in siblings} == {"req0001", "req0002"}


def test_classification_falls_back_to_timestamp_when_sibling_index_absent():
    """Ad hoc / manual testing (Tier 2 curl commands) doesn't set
    sibling_index -- classification must still work via timestamp fallback."""
    entry_a = _entry(
        request_id="req_a", sibling_index=None, timestamp="2026-01-01T00:00:00.000000+00:00"
    )
    entry_b = _entry(
        request_id="req_b", sibling_index=None, timestamp="2026-01-01T00:00:00.100000+00:00"
    )

    cold_start, siblings = classify_cold_start_vs_sibling([entry_b, entry_a])

    assert cold_start["request_id"] == "req_a"
    assert [s["request_id"] for s in siblings] == ["req_b"]


def test_classification_falls_back_when_only_some_entries_have_sibling_index():
    """A partially-tagged group (mixed real traffic + manual testing, or a
    schema migration in progress) must not silently trust a partial
    sibling_index signal -- fall back to timestamp for the whole group."""
    entry_with_index = _entry(
        request_id="req_x", sibling_index=0, timestamp="2026-01-01T00:00:00.900000+00:00"
    )
    entry_without_index = _entry(
        request_id="req_y", sibling_index=None, timestamp="2026-01-01T00:00:00.100000+00:00"
    )

    cold_start, siblings = classify_cold_start_vs_sibling([entry_with_index, entry_without_index])

    assert cold_start["request_id"] == "req_y"
    assert [s["request_id"] for s in siblings] == ["req_x"]


def test_classification_single_entry_group_has_no_siblings():
    entry = _entry(request_id="req_solo", sibling_index=0)
    cold_start, siblings = classify_cold_start_vs_sibling([entry])
    assert cold_start["request_id"] == "req_solo"
    assert siblings == []
