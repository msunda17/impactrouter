# ImpactRouter

ImpactRouter is a lightweight, drop-in HTTP proxy that sits in front of a
pool of OpenAI-compatible LLM inference backends (SGLang / vLLM instances)
and routes requests so that **sibling agent calls sharing the same parent
context land on the same backend instance** — maximizing prefix-cache reuse
(e.g. SGLang's RadixAttention, vLLM's PagedAttention prefix cache) without
requiring any change to the inference engine itself.

It exists to produce one specific, falsifiable measurement: **the
time-to-first-token (TTFT) delta between affinity-routed and naively-routed
sibling fan-out** in a real multi-agent workflow. That measurement is the
deliverable — not a production router. See [`PRD.md`](./PRD.md) for the full
spec, rationale, and non-goals.

> **Status:** v1 prototype. Python-only, in-memory, single-process. Not
> production infrastructure — see [Non-Goals](./PRD.md#4-non-goals-read-before-writing-any-code)
> in the PRD.

## Why this exists (in one paragraph)

When a multi-agent harness fans out several sibling calls from the same
parent context (e.g. a Theorist, an Engineer, and a Data Analyst all
evaluating the same candidate idea concurrently), a naive load balancer
scatters those requests across backend instances with no awareness that they
share an identical long prefix. Each backend then redundantly re-prefills
that shared prefix. ImpactRouter closes this gap **deterministically and
pre-hoc**: the calling harness already knows the requests are siblings before
any of them are sent, so ImpactRouter can route by a simple hash lookup — no
token inspection, no similarity scoring, no post-hoc cache-overlap discovery
(contrast with TokenDance/KVCOMM's post-hoc approach).

ImpactRouter is complementary to, not competing with, workflow-aware KV-cache
eviction (e.g. KVFlow) or TTL-based cache scheduling (e.g. Continuum). Those
systems decide *what to keep* and *for how long*. ImpactRouter decides
*where to send a request* so the serving engine's own cache has a chance to
help in the first place.

## Architecture

```
Calling Harness → ImpactRouter (FastAPI) → { Backend 0, Backend 1, ..., Backend N }
                     │
                     ├─ Fingerprint Resolver (xxHash3, parent_hash)
                     ├─ Affinity Router (in-memory sticky routing table + mode toggle)
                     ├─ Streaming Proxy (httpx passthrough, TTFT capture)
                     └─ TTFT Logger (JSONL)
```

See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) for the full system
architecture (workflows, diagrams, ADRs, foundational knowledge markers), and
[`PRD.md`](./PRD.md) Sections 5–6 for the product-spec diagram and core
mechanism design.

## Requirements

- Python 3.11+
- A pool of one or more OpenAI-compatible backends (real SGLang/vLLM
  instances, and/or the bundled mock backend for logic testing — see below)

## Installation

Using [`uv`](https://docs.astral.sh/uv/) (recommended):

```bash
uv venv --python 3.11 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Or with plain `pip`:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Configuration

ImpactRouter is configured entirely via environment variables. Copy
`.env.example` to `.env` and adjust:

| Variable | Default | Purpose |
|---|---|---|
| `IMPACTROUTER_BACKENDS` | `http://localhost:30000` | Comma-separated backend base URLs |
| `IMPACTROUTER_MODE` | `affinity` | `affinity` or `round_robin` — **the benchmark toggle** |
| `IMPACTROUTER_PORT` | `8000` | Proxy listen port |
| `IMPACTROUTER_HEALTH_CHECK_INTERVAL_S` | `5.0` | Backend health poll interval |
| `IMPACTROUTER_HEALTH_PATH` | `/health` | Path appended to each backend URL for health checks |
| `IMPACTROUTER_LOG_PATH` | `logs/impactrouter_requests.jsonl` | JSONL request log path |
| `IMPACTROUTER_BACKEND_TIMEOUT_S` | `120.0` | Per-request backend timeout |

## Running

```bash
export IMPACTROUTER_BACKENDS=http://localhost:30000,http://localhost:30001
export IMPACTROUTER_MODE=affinity
uvicorn impactrouter.app:app --port 8000
```

Check liveness:

```bash
curl localhost:8000/healthz
```

Send a chat completion through the proxy (any OpenAI-compatible client works
— point the base URL at ImpactRouter and optionally add `parent_context` or
the `X-ImpactRouter-*` headers):

```bash
curl -N localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-ImpactRouter-Scope: session:demo/idea:demo_01" \
  -d '{
    "model": "your-model",
    "parent_context": "shared idea description and instructions...",
    "messages": [
      {"role": "system", "content": "shared idea description and instructions..."},
      {"role": "user", "content": "Evaluate this idea as the Quick Theorist."}
    ]
  }'
```

Inspect routing state live:

```bash
curl localhost:8000/v1/router/stats
```

### Don't have a real backend handy?

`impactrouter.mock_backend` is a trivial FastAPI echo server with
configurable artificial latency, useful for exercising routing logic without
a GPU:

```bash
MOCK_BACKEND_PORT=30000 python -m impactrouter.mock_backend
```

## Testing

```bash
pytest
```

Tests cover fingerprint determinism (`test_fingerprint.py`), sticky
affinity/mode-toggle/health-fallback routing logic (`test_router.py`), and
TTFT measurement correctness against the mock backend
(`test_proxy_streaming.py`) — see [`PRD.md`](./PRD.md) Section 13.

## Benchmarking (FM-1)

Once a real backend pool is wired up (PRD M4), the benchmark harness in
`bench/` produces the actual FM-1 TTFT delta measurement:

```bash
# Simulate a single sibling fan-out (N=3 by default) through the proxy:
python bench/simulate_siblings.py --base-url http://localhost:8000

# Run the full affinity-vs-round_robin benchmark (>=30 trials/mode recommended):
python bench/run_fm1_benchmark.py --trials 30 --base-url http://localhost:8000

# Summarize results:
python bench/analyze_log.py logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
```

See [`PRD.md`](./PRD.md) Sections 11–12 for the full methodology bar
(trial count, honest confound reporting, pool-size sensitivity) and
`docs/fm1_benchmark_writeup.md` for the actual written-up result once a real
run has been completed.

## Documentation

| Document | What it's for |
|---|---|
| [`PRD.md`](./PRD.md) | Requirements SSOT — why ImpactRouter exists, goals, non-goals, milestones |
| [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) | Implementation SSOT — module responsibilities, data flow, algorithms, diagrams |
| [`docs/E2E_TESTING.md`](./docs/E2E_TESTING.md) | Copy-paste command reference for testing every layer, from unit tests to a full real-backend FM-1 run |
| [`docs/fm1_benchmark_writeup.md`](./docs/fm1_benchmark_writeup.md) | The actual FM-1 result once a real benchmark has been run |

## Repository Layout

See [`docs/ARCHITECTURE.md`](./docs/ARCHITECTURE.md) Section 14 for the
canonical, up-to-date file map, and [`PRD.md`](./PRD.md) Section 10 for the
milestone-by-milestone build order this codebase follows.

## Non-Goals

This is a research prototype proving one specific hypothesis, not
infrastructure. Notably out of scope for v1: a Go rewrite, semantic
cache-eviction (`/v1/cache/purge`), speculative fallback warming, persistent
routing state, multi-tenant auth/rate-limiting, and any modification to
SGLang/vLLM internals. Full list in [`PRD.md`](./PRD.md) Section 4.
