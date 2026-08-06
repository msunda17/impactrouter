"""Parses ImpactRouter JSONL request logs and computes the FM-1 TTFT summary
(PRD M5, Section 11, Section 14).

Splits each log into "first" (cold-start, unavoidable cache miss regardless
of routing mode) vs "sibling" (2nd, 3rd, ... request for the same parent)
requests by grouping on `parent_hash` and classifying each group with
`classify_cold_start_vs_sibling` -- see that function's docstring for why
dispatch order (`sibling_index`), not completion timestamp, is the primary
signal. The FM-1 delta is computed on sibling requests only, since that's
where affinity routing's advantage should show up.

Usage:
    python bench/analyze_log.py logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Optional


def load_jsonl(path: Path) -> list[dict]:
    entries = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def classify_cold_start_vs_sibling(group: list[dict]) -> tuple[dict, list[dict]]:
    """Given all log entries sharing one `parent_hash`, returns
    `(cold_start_entry, sibling_entries)`.

    Primary path -- used for anything produced by bench/run_fm1_benchmark.py:
    if every entry in the group has `sibling_index` set, classify by that.
    Index 0 is the cold start, every other index is a sibling. This is
    dispatch-order, not completion-order, and that distinction matters:
    `simulate_siblings.py` fires all siblings concurrently, and under
    concurrent load completion order can legitimately differ from dispatch
    order (e.g. in `affinity` mode, a warm sibling can finish its whole
    response before the cold-start request finishes prefilling). Classifying
    by completion timestamp instead of dispatch order can therefore mislabel
    a genuinely-warm sibling as the cold start or vice versa, silently
    corrupting the exact split the FM-1 measurement depends on.

    Fallback path -- used only when `sibling_index` is missing on some or all
    entries in the group (e.g. ad hoc Tier 2 manual `curl` testing, which
    doesn't set it): sort by completion `timestamp` instead. Lower fidelity
    under concurrency, but sufficient for a quick manual sanity check where
    there's no real concurrent fan-out to mis-order in the first place.
    """
    if not group:
        raise ValueError("classify_cold_start_vs_sibling() called with an empty group")

    if all(e.get("sibling_index") is not None for e in group):
        ordered = sorted(group, key=lambda e: e["sibling_index"])
    else:
        ordered = sorted(group, key=lambda e: e["timestamp"])

    return ordered[0], ordered[1:]


def split_first_vs_sibling(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    by_parent: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_parent[e["parent_hash"]].append(e)

    first_entries: list[dict] = []
    sibling_entries: list[dict] = []
    for group in by_parent.values():
        cold_start, siblings = classify_cold_start_vs_sibling(group)
        first_entries.append(cold_start)
        sibling_entries.extend(siblings)
    return first_entries, sibling_entries


def _ttft_values(entries: list[dict]) -> list[float]:
    return [e["ttft_ms"] for e in entries if e.get("ttft_ms") is not None]


def summarize(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None, "stdev": None}
    sorted_vals = sorted(values)
    p95_index = min(len(sorted_vals) - 1, int(round(0.95 * (len(sorted_vals) - 1))))
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "p95": sorted_vals[p95_index],
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def format_stats(label: str, stats: dict) -> str:
    if stats["n"] == 0:
        return f"{label}: n=0 (no data)"
    return (
        f"{label}: n={stats['n']:>4}  "
        f"mean={stats['mean']:7.1f}ms  median={stats['median']:7.1f}ms  "
        f"p95={stats['p95']:7.1f}ms  stdev={stats['stdev']:6.1f}ms"
    )


def analyze_single_log(path: Path) -> dict:
    entries = load_jsonl(path)
    first_entries, sibling_entries = split_first_vs_sibling(entries)
    return {
        "path": path,
        "mode": entries[0]["routing_mode"] if entries else "unknown",
        "total_requests": len(entries),
        "first_stats": summarize(_ttft_values(first_entries)),
        "sibling_stats": summarize(_ttft_values(sibling_entries)),
    }


def _find_mode(results: list[dict], mode: str) -> Optional[dict]:
    return next((r for r in results if r["mode"] == mode), None)


def print_report(results: list[dict]) -> None:
    print("=" * 78)
    print("ImpactRouter FM-1 TTFT Summary")
    print("=" * 78)
    for r in results:
        print(f"\n[{r['mode']}] {r['path']}  (total requests: {r['total_requests']})")
        print("  " + format_stats("first (cold-start)   ", r["first_stats"]))
        print("  " + format_stats("sibling (2nd, 3rd...)", r["sibling_stats"]))

    affinity = _find_mode(results, "affinity")
    round_robin = _find_mode(results, "round_robin")
    if affinity and round_robin:
        a_mean = affinity["sibling_stats"]["mean"]
        rr_mean = round_robin["sibling_stats"]["mean"]
        print("\n" + "-" * 78)
        print("FM-1 DELTA (sibling requests only, cold-start excluded)")
        print("-" * 78)
        if a_mean is not None and rr_mean is not None:
            delta_ms = rr_mean - a_mean
            delta_pct = (delta_ms / rr_mean * 100) if rr_mean else 0.0
            print(f"  affinity mean TTFT:     {a_mean:.1f}ms")
            print(f"  round_robin mean TTFT:  {rr_mean:.1f}ms")
            print(f"  delta:                  {delta_ms:.1f}ms ({delta_pct:.1f}% improvement)")
            print(
                "\n  NOTE: this is a point estimate. Report spread (stdev/p95 above) and "
                "trial count alongside any number quoted externally -- see PRD Section 11."
            )
        else:
            print("  Not enough sibling data in both modes to compute a delta.")
    else:
        print(
            "\nNote: pass one log for 'affinity' mode and one for 'round_robin' mode "
            "to get the FM-1 delta computed automatically."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("log_paths", nargs="+", type=Path, help="One or more JSONL log files to analyze.")
    args = parser.parse_args()

    results = [analyze_single_log(p) for p in args.log_paths]
    print_report(results)


if __name__ == "__main__":
    main()
