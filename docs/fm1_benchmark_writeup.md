# FM-1 Benchmark Writeup — ImpactRouter Affinity Routing TTFT Delta

> **Status: TEMPLATE — not yet filled in with a real run.**
>
> Per PRD Section 11, the acceptance criterion for this project is a written,
> reproducible FM-1 TTFT delta measurement — not "code merged." This document
> is the source of truth for any number quoted in a blog post or portfolio
> update. Do not publish a number that doesn't trace back to a completed
> version of this writeup, with `bench/run_fm1_benchmark.py` executed against
> a **real** backend pool (not the mock backend).

## 1. Methodology

- **Model used:** _TODO (e.g. Qwen2.5-1.5B-Instruct)_
- **Serving engine:** _TODO (SGLang / vLLM, version)_
- **Backend pool size:** _TODO (e.g. 2 real backends)_
- **Backend pool configuration:** _TODO — state clearly whether this is a
  small-model backend pool (all real) or a mixed real/mock pool, per PRD
  Section 12. A 2-real-backend result is a valid, honest data point; describe
  it as what it is._
- **Prompt / parent context size:** _TODO (approx. char/token length of the
  shared parent prefix used in `bench/simulate_siblings.py`)_
- **Sibling fan-out width (N):** _TODO (default 3, matching ImpactScientists
  Phase 3's Theorist/Engineer/DA pattern)_
- **Trial count per mode:** _TODO (PRD minimum: 30)_
- **Hardware:** _TODO (GPU model, VRAM)_
- **Command used:**

```bash
python bench/run_fm1_benchmark.py --backends <REAL_BACKEND_URLS> --trials 30
python bench/analyze_log.py logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
```

## 2. Results

_TODO — paste the `analyze_log.py` output table here verbatim._

| Mode | n (siblings) | Mean TTFT | Median TTFT | p95 TTFT | Stdev |
|---|---|---|---|---|---|
| affinity | | | | | |
| round_robin | | | | | |

**FM-1 delta (sibling requests, cold-start excluded):** _TODO ms (TODO% improvement)_

## 3. Honest Confounds and Caveats

- **Pool size sensitivity:** _TODO — note whether this result was run at more
  than one pool size (PRD Section 14 recommends 2 and 4 backends if time
  allows), and whether the delta shrank as pool size grew, as expected._
- **Cold-start exclusion:** first-request-per-parent TTFT is reported
  separately above and excluded from the delta calculation, since it is a
  cache miss under both modes by construction (PRD Section 14).
- **Cache eviction under load:** _TODO — note trial spacing used and whether
  it's plausible the backend's own prefix cache evicted a "sticky" entry
  between sibling calls (PRD Section 14)._
- **Any other confound observed during the run:** _TODO_

## 4. Conclusion

_TODO — one paragraph stating whether the FM-1 hypothesis (pre-hoc affinity
routing measurably reduces sibling TTFT vs naive round-robin) was supported,
with the number and its honest caveats, suitable for direct reuse in the
ImpactRouter blog post / portfolio update._
