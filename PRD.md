# ImpactRouter — Product Requirements Document (v1 Prototype)

**Status:** Draft for implementation · **Owner:** Manikandan Sundararaman · **Codename history:** StateFlow Gateway → ImpactRouter
**Target:** Python-only research prototype. Not a production system. See Non-Goals.

---

## 0. How to Use This Document

This PRD is written to be consumed by an IDE coding agent (e.g., Cursor) as the primary spec for scaffolding and implementing ImpactRouter v1. Every section that describes behavior includes enough detail to generate code directly — data models, algorithms, file layout, and acceptance criteria. Sections marked **[AGENT: START HERE]** indicate the recommended build order.

Drop this file at the repository root as `PRD.md`. If your agent tooling supports a rules/context file (e.g., `.cursorrules`, `AGENTS.md`), point it at this document rather than duplicating content.

---

## 1. One-Paragraph Summary

ImpactRouter is a lightweight, drop-in HTTP proxy that sits in front of a pool of LLM inference backends (SGLang/vLLM instances) and routes requests so that sibling agent calls sharing the same parent context land on the same backend instance — maximizing prefix-cache reuse (e.g., SGLang's RadixAttention) without requiring any change to the inference engine itself. It exists to produce one specific, falsifiable measurement: the time-to-first-token (TTFT) delta between affinity-routed and naively-routed sibling fan-out in a real multi-agent workflow. That measurement is the deliverable — not a production router.

---

## 2. Problem Statement

### 2.1 The Sibling Routing Problem (FM-1)

In a multi-agent workflow, it's common for several agents to be spawned in parallel from the same parent context — for example, ImpactScientists' Phase 3 tournament, where Quick Theorist, Quick Engineer, and Quick DA all independently evaluate the same candidate idea concurrently. Each of these three calls shares an identical (often long) prefix: the idea description, prior corpus context, and shared instructions. Only the final instruction differs per agent role.

A standard load balancer or round-robin router has no knowledge of this shared topology. It distributes the three sibling requests across whatever backend instances are least loaded — which, in a multi-instance serving pool, frequently means the three requests land on three different backends. Each backend must then independently prefill the identical shared prefix from scratch, even though the serving engine's own prefix-cache mechanism (SGLang's RadixAttention, vLLM's PagedAttention prefix cache) would have reused it automatically if the requests had landed on the same instance.

The waste is not a correctness bug — it's a missed optimization that the serving engine cannot fix on its own, because the serving engine has no visibility into application-layer topology. It doesn't know that these three requests are siblings. Only the application harness that spawned them knows that.

### 2.2 Why Existing Work Doesn't Close This Gap

TokenDance and KVCOMM both address cross-request KV-cache sharing, but both operate **post-hoc and probabilistically** — they inspect token overlap after requests arrive and attempt to discover shareable prefixes retroactively. This works, but it requires comparison overhead and cannot guarantee the sharing opportunity is caught before the redundant computation happens.

ImpactRouter's differentiation: **deterministic, pre-hoc sharing.** The calling harness already knows, before any request is sent, that three siblings are about to fan out from the same parent. That topology information can be passed explicitly via a header, and ImpactRouter can guarantee affinity routing with zero comparison overhead — no token inspection, no similarity scoring, just a hash lookup.

### 2.3 Explicit Positioning

ImpactRouter is **complementary to, not competing with**, KVFlow (workflow-aware eviction) and Continuum (KV-cache TTL scheduling). Those systems solve *what to keep* and *for how long*. ImpactRouter solves *where to send it* so that the engine's own cache has a chance to help in the first place. All prose, docstrings, and READMEs generated for this project should preserve this framing — do not describe ImpactRouter as a replacement for engine-level optimization.

---

## 3. Goals

1. Prove that pre-hoc, topology-aware routing produces a measurable TTFT improvement over naive round-robin routing for sibling agent fan-out.
2. Ship a working proxy that is a true drop-in — any OpenAI-compatible client (LiteLLM, raw `httpx`, ImpactScientists' internal LLM client) can point at it with no code changes beyond the base URL and one optional header.
3. Produce a reproducible benchmark script and a written-up FM-1 TTFT delta number with real methodology — this is the blog-post artifact and the empirical contribution.
4. Keep the codebase small enough that every line is defensible in an interview or a README — this is a research proxy proving a point, not infrastructure someone else has to maintain.

## 4. Non-Goals (Read Before Writing Any Code)

- **No Go rewrite.** This stays a Python prototype indefinitely. Do not scaffold a Go version, do not plan for one.
- **No `/v1/cache/purge` veto endpoint in v1.** This is an FM-2 (semantic eviction) feature and is explicitly deferred to a post-v1 phase. Do not implement it now even if it seems easy to add alongside routing.
- **No speculative fallback warming in v1.** Same reason — FM-2 territory, deferred.
- **No persistent routing table.** In-memory only. Restarting the proxy is an acceptable way to reset state for v1.
- **No multi-tenant auth, rate limiting, or production hardening.** This will never run anywhere but a local dev machine or a benchmark environment.
- **No modification to SGLang/vLLM internals.** ImpactRouter is strictly an external proxy. The "real" fix — a harness-signal API inside the scheduler — is future work in the form of an SGLang RFC, not something this codebase implements.
- **No support for non-OpenAI-compatible backend APIs** in v1.
- **`X-ImpactRouter-Scope` is observability-only in v1** — it is logged for session grouping but does **not** participate in routing decisions. Using it for routing is a v2 idea, not v1 scope.

If an implementation detail in this document seems to imply scope beyond the above, the Non-Goals list wins. Ask before expanding scope.

---

## 5. System Architecture

```
                     ┌─────────────────────────────────────────┐
                     │   Calling Harness                        │
                     │   (ImpactScientists Phase 3 fan-out,      │
                     │    or bench/simulate_siblings.py)         │
                     └───────────────────┬───────────────────────┘
                                          │ POST /v1/chat/completions
                                          │ Headers:
                                          │   X-ImpactRouter-Scope (optional, log-only)
                                          │   X-ImpactRouter-Parent-Hash (optional)
                                          │ Body: OpenAI-compatible chat completion request
                                          │   (+ optional "parent_context" field)
                                          ▼
        ┌──────────────────────────────────────────────────────────────┐
        │                     ImpactRouter Proxy (FastAPI)                │
        │                                                                  │
        │  ┌────────────────┐   ┌──────────────────┐   ┌───────────────┐ │
        │  │ Fingerprint     │   │ Affinity Router   │   │ TTFT Logger   │ │
        │  │ Resolver        │──▶│ (routing table +  │──▶│ (JSONL)       │ │
        │  │ (xxHash3)       │   │  mode toggle)      │   │               │ │
        │  └────────────────┘   └──────────────────┘   └───────────────┘ │
        │                                │                                 │
        │                                ▼                                 │
        │                     ┌────────────────────┐                       │
        │                     │ Streaming Proxy      │                      │
        │                     │ (httpx passthrough)   │                     │
        │                     └──────────┬───────────┘                     │
        └────────────────────────────────┼─────────────────────────────────┘
                                          │
                       ┌──────────────────┼──────────────────┐
                       ▼                  ▼                  ▼
                ┌────────────┐    ┌────────────┐    ┌────────────┐
                │ Backend 0   │    │ Backend 1   │    │ Backend N   │
                │ (SGLang/    │    │ (SGLang/    │    │ (SGLang/    │
                │  vLLM)      │    │  vLLM)      │    │  vLLM)      │
                └────────────┘    └────────────┘    └────────────┘
```

**Single process, single async event loop for v1.** No need for multi-worker deployment — this runs on a dev laptop against a small backend pool.

---

## 6. Core Mechanism Design

### 6.1 Headers

| Header | Required | v1 Behavior |
|---|---|---|
| `X-ImpactRouter-Scope` | No | Logged for session/observability grouping only. Format suggestion: `session:{run_id}/idea:{idea_id}`. **Does not affect routing in v1.** |
| `X-ImpactRouter-Parent-Hash` | No | If present, used directly as the routing key (already-hex-encoded xxHash3 digest). If absent, ImpactRouter computes it itself — see 6.2. |

### 6.2 Fingerprint Resolution

If the client does not supply `X-ImpactRouter-Parent-Hash`, ImpactRouter computes it from a `parent_context` field expected in the request body (a string — the shared prefix content the caller considers "the parent"). If neither the header nor `parent_context` is present, fall back to hashing the full message list minus the last message (treat everything but the final turn as "parent" by default). This fallback exists so the proxy is still usable by clients that haven't been updated to pass explicit topology hints, but affinity quality will be lower.

```python
import xxhash

def resolve_parent_hash(request: ChatCompletionRequest, header_hash: str | None) -> str:
    if header_hash:
        return header_hash
    if request.parent_context:
        return xxhash.xxh3_64_hexdigest(request.parent_context.encode("utf-8"))
    # Fallback: hash everything except the final message
    fallback_content = "".join(m.content for m in request.messages[:-1])
    return xxhash.xxh3_64_hexdigest(fallback_content.encode("utf-8"))
```

**Determinism requirement:** identical input must always produce identical output within a single process run. This is the first thing to unit test.

### 6.3 Affinity Routing Table

In-memory dict, no persistence, no eviction policy needed for v1 (prototype-scale traffic, process restarts reset it).

```python
@dataclass
class RoutingEntry:
    parent_hash: str
    backend_id: str
    created_at: float
    last_used_at: float
    hit_count: int = 0
```

```python
class AffinityRouter:
    def __init__(self, backends: list[str], mode: Literal["affinity", "round_robin"]):
        self.backends = backends
        self.mode = mode
        self.table: dict[str, RoutingEntry] = {}
        self._rr_counter = 0

    def select_backend(self, parent_hash: str) -> tuple[str, str]:
        """Returns (backend_id, routing_outcome) where routing_outcome is one of:
        'affinity_hit', 'affinity_miss_new', 'control_round_robin'"""
        if self.mode == "round_robin":
            backend = self._next_round_robin()
            return backend, "control_round_robin"

        entry = self.table.get(parent_hash)
        if entry and self._is_healthy(entry.backend_id):
            entry.last_used_at = time.time()
            entry.hit_count += 1
            return entry.backend_id, "affinity_hit"

        backend = self._next_round_robin()
        self.table[parent_hash] = RoutingEntry(
            parent_hash=parent_hash, backend_id=backend,
            created_at=time.time(), last_used_at=time.time(),
        )
        return backend, "affinity_miss_new"

    def _next_round_robin(self) -> str:
        backend = self.backends[self._rr_counter % len(self.backends)]
        self._rr_counter += 1
        return backend

    def _is_healthy(self, backend_id: str) -> bool:
        # v1: simple reachability check, see 6.5
        ...
```

**The `mode` toggle is the entire benchmark mechanism.** Running the same traffic pattern through `mode="affinity"` and `mode="round_robin"` is how the FM-1 delta gets measured — this is not an implementation detail, it's the point of the whole artifact. Make it a config/env var (`IMPACTROUTER_MODE`), not a hardcoded value, so the benchmark harness can flip it between runs without touching code.

### 6.4 Streaming Proxy / Passthrough

Requests and responses must support streaming (SSE / chunked transfer), since TTFT is only measurable if the first token is observable independently from the full response. Use `httpx.AsyncClient` with `stream=True`, forward chunks as they arrive, and record the timestamp of the first non-empty chunk as `ttft`.

```python
async def proxy_request(backend_url: str, body: dict, log_context: dict):
    start = time.perf_counter()
    first_token_time = None
    async with httpx.AsyncClient() as client:
        async with client.stream("POST", f"{backend_url}/v1/chat/completions", json=body) as resp:
            async for chunk in resp.aiter_bytes():
                if first_token_time is None and chunk.strip():
                    first_token_time = time.perf_counter()
                yield chunk
    ttft_ms = (first_token_time - start) * 1000 if first_token_time else None
    total_ms = (time.perf_counter() - start) * 1000
    log_request(log_context | {"ttft_ms": ttft_ms, "total_latency_ms": total_ms})
```

### 6.5 Backend Health

v1 health check is intentionally minimal: a simple periodic `GET /health` (or equivalent) poll on each backend, cached for N seconds, boolean up/down. No load-based scoring, no latency-weighted selection. This is a prototype whose job is to prove a routing hypothesis, not to be a robust load balancer.

### 6.6 TTFT Logging

One JSONL line per request, written to `logs/impactrouter_requests.jsonl`:

```json
{
  "timestamp": "2026-07-14T18:32:01.123Z",
  "request_id": "a1b2c3d4",
  "parent_hash": "9f8e7d6c5b4a3210",
  "scope": "session:run_042/idea:idea_03",
  "backend_id": "backend_1",
  "routing_mode": "affinity",
  "routing_outcome": "affinity_hit",
  "ttft_ms": 187.4,
  "total_latency_ms": 1204.9,
  "prompt_char_len": 4821
}
```

This log is the raw material for the benchmark report. Keep the schema stable — the benchmark analysis script depends on every field being present.

---

## 7. API Surface

| Endpoint | Method | Purpose |
|---|---|---|
| `/v1/chat/completions` | POST | Main proxy passthrough. OpenAI-compatible request/response shape, with optional `parent_context` field and optional `X-ImpactRouter-*` headers. |
| `/v1/router/stats` | GET | Introspection: current routing table size, per-backend hit counts, mode. For debugging and live demo, not for production monitoring. |
| `/healthz` | GET | Liveness check for the proxy itself. |

### 7.1 Request Schema Addendum

Extend the standard OpenAI chat completion request with one optional field:

```python
class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[Message]
    stream: bool = True
    parent_context: str | None = None  # ImpactRouter-specific, optional
    # ...standard OpenAI fields passed through untouched
```

Unknown/extra fields should pass through to the backend untouched — do not strip fields the proxy doesn't understand.

---

## 8. Tech Stack

- **Python 3.11+**
- **FastAPI** — proxy HTTP server
- **httpx** (async) — backend forwarding, streaming support
- **xxhash** (`pip install xxhash`) — xxHash3 fingerprinting
- **pydantic v2** — request/response/log schemas
- **pytest** + **pytest-asyncio** — test suite
- Plain Python `logging` or manual file writes for JSONL — no need for a logging framework dependency

No database. No message queue. No containerization requirement for v1 — a `uv run` or `python -m` local process is sufficient.

---

## 9. Repository Structure

```
impactrouter/
├── README.md
├── PRD.md                        # this document
├── pyproject.toml
├── .env.example
├── src/
│   └── impactrouter/
│       ├── __init__.py
│       ├── app.py                # FastAPI app + route definitions
│       ├── config.py             # env-based config: backends, mode, ports
│       ├── fingerprint.py        # xxHash3 resolution logic (6.2)
│       ├── router.py             # AffinityRouter + RoutingEntry (6.3)
│       ├── proxy.py              # streaming passthrough (6.4)
│       ├── health.py             # backend health polling (6.5)
│       ├── logging_utils.py      # JSONL request logger (6.6)
│       └── models.py             # pydantic schemas
├── bench/
│   ├── simulate_siblings.py      # synthetic sibling fan-out load generator
│   ├── run_fm1_benchmark.py      # runs affinity vs round_robin, computes delta
│   └── analyze_log.py            # JSONL -> summary stats / report
├── tests/
│   ├── conftest.py
│   ├── test_fingerprint.py       # determinism tests
│   ├── test_router.py            # sticky routing, mode toggle, fallback behavior
│   └── test_proxy_streaming.py   # TTFT measurement correctness against a mock backend
├── logs/                         # gitignored
└── docs/
    └── fm1_benchmark_writeup.md  # filled in after bench run — the blog-post source
```

---

## 10. Implementation Milestones **[AGENT: START HERE]**

Build in this order. Each milestone should be independently runnable and testable before moving to the next.

### M0 — Scaffold
Repo structure per Section 9. `pyproject.toml` with dependencies. `config.py` reading backend URLs and mode from environment variables (`IMPACTROUTER_BACKENDS`, `IMPACTROUTER_MODE`). `/healthz` endpoint returning 200. Confirm the app boots with `uvicorn`.

### M1 — Fingerprint Module
Implement `fingerprint.py` per 6.2. Write `test_fingerprint.py` first: same input → same hash across repeated calls; different `parent_context` → different hash; header takes precedence over computed hash; fallback path exercised when both header and `parent_context` are absent.

### M2 — Affinity Router
Implement `AffinityRouter` per 6.3. Write `test_router.py`: a request with a previously-seen `parent_hash` routes to the same backend as before (sticky); a new `parent_hash` uses round-robin selection among backends; `mode="round_robin"` ignores the routing table entirely regardless of hash; health-check failure on a "sticky" backend causes fallback to round-robin selection for that request without corrupting the table entry for future healthy retries.

### M3 — Streaming Proxy + Logging
Implement `proxy.py` and `logging_utils.py`. For initial development, stand up a trivial mock backend (a second FastAPI app that echoes a fixed streamed response with an artificial delay) so proxy correctness can be tested without needing a real SGLang instance running. Write `test_proxy_streaming.py` against the mock backend, asserting `ttft_ms` is captured and is less than `total_latency_ms`.

### M4 — Wire to a Real Backend
Point the backend pool at a real local SGLang instance (see Section 12 for hardware-specific guidance). Manually verify end-to-end: a `curl` or small script sending a chat completion through the proxy returns a real streamed response and produces a correct JSONL log line.

### M5 — Benchmark Harness
Implement `bench/simulate_siblings.py`: given a shared parent prompt and N sibling suffix instructions (default N=3, matching ImpactScientists Phase 3's Theorist/Engineer/DA), fire all N concurrently through the proxy. Implement `bench/run_fm1_benchmark.py`: runs the simulation repeatedly (suggest ≥30 trials per mode for a defensible sample size) once with `IMPACTROUTER_MODE=affinity` and once with `IMPACTROUTER_MODE=round_robin`, writing both logs separately. Implement `bench/analyze_log.py`: parses both JSONL logs, computes mean/median/p95 TTFT per mode, computes the delta, and prints a summary table.

### M6 — Real-Workload Validation (stretch, post-v1)
Wire ImpactRouter in front of ImpactScientists' actual Phase 3 tournament traffic instead of the synthetic simulator, and re-run the M5 benchmark against real fan-out. This is explicitly out of scope for the initial "v1 done" bar but is the natural next step once the synthetic result is validated and the blog post methodology is proven sound.

---

## 11. Benchmark & Validation Plan

**The acceptance criterion for this project is not "code merged." It is a written, reproducible FM-1 TTFT delta measurement.**

Minimum bar for calling this done:
1. `bench/run_fm1_benchmark.py` executes cleanly against a real local backend (not the mock).
2. At least 30 trials per routing mode, same synthetic sibling-fan-out pattern across both modes.
3. `docs/fm1_benchmark_writeup.md` states: the exact methodology (model used, backend pool size, prompt/parent size, trial count), the raw delta with a measure of spread (not just a single point estimate), and an honest note on any confound (e.g., if backend pool size is small, note that affinity's advantage may shrink as pool size grows — don't oversell a 2-backend result as generalizing to a 20-backend production pool).
4. The number that goes in any public blog post or portfolio update must trace directly back to this writeup — no rounding up, no cherry-picking the best trial.

This writeup is the source material for the ImpactRouter blog post referenced in the broader portfolio plan, and for updating the portfolio page's "Prototype" status badge to something more specific once real numbers exist.

---

## 12. Environment Notes (Local Hardware)

Development and benchmarking are expected to run on a local machine with a single consumer GPU (16GB VRAM class). Running multiple full-size real backend instances simultaneously may not fit in available VRAM. Two acceptable approaches, in order of preference:

1. **Small-model backend pool:** run N backend instances of a small model (1–3B parameters) that comfortably fit N-at-a-time in available VRAM, for a fully "real" N-way affinity test.
2. **Mixed real/mock pool:** run one real backend plus N−1 lightweight mock backends (simple FastAPI echo servers with configurable artificial latency) to validate routing *logic* correctness, and reserve a smaller number of real-backend trials for the actual TTFT number.

State clearly in the benchmark writeup which configuration was used — this is a legitimate methodological detail, not something to obscure. A benchmark run on a 2-real-backend pool is still a valid, honest data point; it just needs to be described as what it is.

---

## 13. Testing Strategy Summary

| Layer | Test Focus |
|---|---|
| Fingerprint | Determinism, header precedence, fallback correctness |
| Router | Sticky affinity, round-robin fallback for new hashes, mode toggle, health-based fallback |
| Proxy | Streaming passthrough correctness, TTFT timestamp accuracy against mock backend |
| Bench | Not unit-tested in the traditional sense — validated by manual inspection of a short trial run before committing to a full 30+ trial benchmark |

Run the full suite (`pytest`) before every milestone is considered complete. Do not proceed to M4 (real backend) until M1–M3 pass cleanly against the mock backend.

---

## 14. Open Questions / Risks

- **Pool size sensitivity:** affinity routing's advantage is expected to shrink as backend pool size grows (fewer collisions in naive round-robin). The benchmark should be run against at least two different pool sizes (e.g., 2 and 4 backends) if time allows, to characterize this rather than report a single number that may not generalize.
- **Cold-start confound:** the very first request for any given `parent_hash` is always a cache miss regardless of routing mode (nothing has been prefilled yet). Make sure the benchmark measures *sibling* requests (2nd, 3rd, ... of a given parent) separately from the first, since the first request's TTFT should be roughly equal across both modes by construction — mixing them in dilutes the real signal.
- **SGLang cache eviction under load:** if the backend's own cache is small relative to the working set, a "hit" at the routing layer may still be a miss at the cache layer if the entry was evicted between sibling calls. Worth a footnote in the writeup if trial spacing is large enough for this to plausibly happen.

---

## 15. Appendix: Glossary

- **FM-1** — Sibling Routing Problem, one of five failure modes in the broader Inference Awareness research taxonomy.
- **Affinity routing** — routing strategy that sends requests sharing a parent context to the same backend instance.
- **Pre-hoc / post-hoc** — pre-hoc means the sharing opportunity is known before computation starts (ImpactRouter's approach); post-hoc means it's discovered after the fact by inspecting completed or in-flight requests (TokenDance/KVCOMM's approach).
- **TTFT** — Time to First Token, the latency from request submission to the first streamed token being observable.
- **Parent hash** — the xxHash3 fingerprint identifying a shared parent context across sibling requests.
