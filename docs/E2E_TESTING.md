# ImpactRouter — End-to-End Testing Command Reference

This is a runnable command reference, ordered from fastest/cheapest to
slowest/most-realistic. Each tier assumes the previous one passed. See
[`ARCHITECTURE.md`](./ARCHITECTURE.md) for *why* each layer behaves the way
it does, and [`PRD.md`](../PRD.md) §13 for the testing strategy this follows.

All commands below assume you're at the repo root with the venv created
(Tier 0). Commands that start a long-running server show how to run it in
the foreground (for a single terminal, run each server block in its own
terminal tab) — adjust to background (`&`) if you're scripting this.

> **Port note:** some machines have Docker Desktop's proxy bound to port
> `8000` on IPv6, which will silently swallow requests intended for a local
> `uvicorn` process on `8000`. If `curl localhost:8000/...` returns a
> mysterious `404` even though your server logs show no incoming request,
> switch to a less common port (e.g. `8123`) and confirm with
> `lsof -i :8000` first.

---

## Tier 0 — Environment Setup

```bash
# Create the venv (Python 3.11+ required; uv will download an interpreter if needed)
uv venv --python 3.11 .venv
source .venv/bin/activate

# Install the package + dev/test dependencies
uv pip install -e ".[dev]"

# Sanity check: package imports, CLI entrypoints exist
python -c "import impactrouter; print(impactrouter.__version__)"
```

---

## Tier 1 — Unit & Integration Tests (no servers, no network)

```bash
# Full suite
pytest -v

# By layer (matches ARCHITECTURE.md §10)
pytest tests/test_fingerprint.py -v        # pure-function determinism/precedence
pytest tests/test_router.py -v             # sticky routing, mode toggle, health fallback
pytest tests/test_proxy_streaming.py -v    # TTFT capture via in-process ASGITransport mock backend

# With coverage (optional; requires pytest-cov if you want this)
pytest --tb=short -q
```

**Expected result:** all tests pass in well under a second. This tier requires
no real network calls, no real backend, no GPU — if this tier fails, do not
proceed to later tiers.

---

## Tier 2 — Manual E2E Smoke Test Against Mock Backends

Validates the full request lifecycle (Section 3 of `ARCHITECTURE.md`) with
zero GPU/model dependency. Run each block below in its own terminal (or
background with `&` / `nohup` if scripting).

```bash
# Terminal A: mock backend 0
MOCK_BACKEND_PORT=9000 python -m impactrouter.mock_backend

# Terminal B: mock backend 1
MOCK_BACKEND_PORT=9001 python -m impactrouter.mock_backend

# Terminal C: the proxy itself, pointed at both mocks, mode=affinity
IMPACTROUTER_BACKENDS=http://localhost:9000,http://localhost:9001 \
IMPACTROUTER_MODE=affinity \
uvicorn impactrouter.app:app --host 127.0.0.1 --port 8123
```

```bash
# Terminal D: exercise it

# 1. Liveness
curl -s 127.0.0.1:8123/healthz
# expect: {"status":"ok"}

# 2. Router introspection (before any traffic)
curl -s 127.0.0.1:8123/v1/router/stats
# expect: routing_table_size=0, both backends healthy=true after ~1 health-poll interval

# 3. First sibling for parent "idea A" — expect affinity_miss_new
curl -s -N 127.0.0.1:8123/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-ImpactRouter-Scope: session:demo/idea:demo_01" \
  -d '{"model":"mock-model","parent_context":"shared idea A","messages":[{"role":"system","content":"shared idea A"},{"role":"user","content":"Theorist instruction"}]}'

# 4. Second sibling, SAME parent_context — expect it to stick to the same backend
curl -s -N 127.0.0.1:8123/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-ImpactRouter-Scope: session:demo/idea:demo_01" \
  -d '{"model":"mock-model","parent_context":"shared idea A","messages":[{"role":"system","content":"shared idea A"},{"role":"user","content":"Engineer instruction"}]}'

# 5. Confirm stickiness: routing_table_size=1, one backend has hit_count=1
curl -s 127.0.0.1:8123/v1/router/stats

# 6. Inspect the actual log lines written for the two requests above
tail -n 2 logs/impactrouter_requests.jsonl | python -m json.tool 2>/dev/null || tail -n 2 logs/impactrouter_requests.jsonl
# expect: same parent_hash on both lines, same backend_id, routing_outcome
#         goes affinity_miss_new -> affinity_hit, ttft_ms < total_latency_ms on both
```

### Tier 2b — Verify round_robin mode ignores the hash

```bash
# Restart the proxy (Terminal C) with mode=round_robin, same backends
IMPACTROUTER_BACKENDS=http://localhost:9000,http://localhost:9001 \
IMPACTROUTER_MODE=round_robin \
uvicorn impactrouter.app:app --host 127.0.0.1 --port 8123
```

```bash
# Fire the SAME parent_context three times — expect backend_id to alternate
# (backend_0, backend_1, backend_0), not stick, and routing_outcome to always
# read "control_round_robin". routing_table_size should stay 0 throughout.
for i in 1 2 3; do
  curl -s -N 127.0.0.1:8123/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{"model":"mock-model","parent_context":"same idea every time","messages":[{"role":"user","content":"final"}]}' \
    > /dev/null
done
curl -s 127.0.0.1:8123/v1/router/stats
# expect: routing_table_size=0
tail -n 3 logs/impactrouter_requests.jsonl
```

### Tier 2c — Verify health-based fallback (kill a sticky backend)

```bash
# With the proxy back in mode=affinity and a warm sticky entry for some
# parent_hash pointing at, say, backend_0:

# 1. Kill backend 0's process (Ctrl-C in its terminal, or):
#    find its PID and kill it
lsof -i :9000
kill <PID_OF_MOCK_BACKEND_0>

# 2. Wait one health-check interval (default 5s), then confirm it's marked unhealthy
sleep 6
curl -s 127.0.0.1:8123/v1/router/stats
# expect: backend_0 healthy=false

# 3. Re-send a request for the SAME parent_hash that was previously sticky to backend_0
#    -> expect it to route to backend_1 instead, with
#       routing_outcome=affinity_fallback_unhealthy (NOT affinity_miss_new --
#       that value is reserved for a TRUE cold start; this parent_hash was
#       already known, its sticky backend is just transiently unhealthy),
#       WITHOUT the table entry being deleted.
curl -s -N 127.0.0.1:8123/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-model","parent_context":"shared idea A","messages":[{"role":"user","content":"another sibling"}]}'
tail -n 1 logs/impactrouter_requests.jsonl
# expect: "routing_outcome":"affinity_fallback_unhealthy"

# 4. Restart backend 0, wait a health-poll interval, re-send once more for the
#    same parent -> expect it to route back to backend_0 (routing_outcome=affinity_hit).
MOCK_BACKEND_PORT=9000 python -m impactrouter.mock_backend &
sleep 6
curl -s -N 127.0.0.1:8123/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"mock-model","parent_context":"shared idea A","messages":[{"role":"user","content":"yet another sibling"}]}'
```

### Tier 2 cleanup

```bash
# Stop the proxy and both mock backends (Ctrl-C in each terminal, or by PID)
pkill -f "uvicorn impactrouter.app" 2>/dev/null
pkill -f "impactrouter.mock_backend" 2>/dev/null
rm -f logs/impactrouter_requests.jsonl
```

---

## Tier 3 — Benchmark Harness Logic Validation (mock backends, short run)

This validates that the *harness itself* (subprocess orchestration, trial
loop, fresh-`idea_id`-per-trial, log splitting) works correctly. **A delta
computed against the mock backend is not a real FM-1 measurement** — the
mock backend has fixed artificial latency and no real prefix cache, so you
should expect the delta to be ~0. That's the correct, expected outcome here.

```bash
# Start two mock backends (separate terminals or backgrounded)
MOCK_BACKEND_PORT=9000 python -m impactrouter.mock_backend &
MOCK_BACKEND_PORT=9001 python -m impactrouter.mock_backend &
sleep 1

# Clear any stale logs from a previous run
rm -f logs/impactrouter_requests.jsonl logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl

# Run a SHORT validation pass (3 trials/mode) -- do this before ever running
# a full 30+ trial benchmark, per PRD §13.
# Console output now includes a readiness smoke-probe line per mode
# ("Probing backend(s) for real generation readiness...") before trials
# start -- see Tier 5 below for what that step does and why.
python bench/run_fm1_benchmark.py \
  --backends http://localhost:9000,http://localhost:9001 \
  --trials 3 \
  --port 8123 \
  --model mock-model

# Analyze it
python bench/analyze_log.py logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
# expect: both modes' sibling-mean TTFT are close to each other (mock backend
# has no real cache to reuse) -- a large, consistent delta here would actually
# indicate a BUG in the harness, not a real signal.
```

```bash
# Cleanup
pkill -f "impactrouter.mock_backend" 2>/dev/null
rm -f logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
```

---

## Tier 4 — Wire to a Real Backend (PRD M4)

Requires a real SGLang or vLLM instance. Example with SGLang serving a small
model (see PRD §12 for hardware sizing guidance — pick a 1–3B model if
running multiple instances on one consumer GPU):

```bash
# Install SGLang (in a separate env from ImpactRouter's, if you prefer)
pip install "sglang[all]"

# Launch a real backend (adjust --model-path to whatever you're benchmarking)
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-1.5B-Instruct \
  --port 30000 --host 0.0.0.0

# (Optional second instance, if VRAM allows, on a different port)
python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-1.5B-Instruct \
  --port 30001 --host 0.0.0.0
```

```bash
# Confirm SGLang's OpenAI-compatible health/completions endpoints directly first.
# NOTE: /health is a shallow LIVENESS check -- it can return 200 before model
# weights finish loading. /health_generate performs a real single-token
# generation and is a much better READINESS signal (see ARCHITECTURE.md §6.4).
# If you want ImpactRouter's own HealthChecker to reflect real readiness
# rather than bare liveness, set IMPACTROUTER_HEALTH_PATH=/health_generate
# below instead of the default /health.
curl -s http://localhost:30000/health
curl -s http://localhost:30000/health_generate
curl -s http://localhost:30000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-1.5B-Instruct","messages":[{"role":"user","content":"say hi"}],"stream":false}'
```

```bash
# Now point ImpactRouter at it/them
IMPACTROUTER_BACKENDS=http://localhost:30000,http://localhost:30001 \
IMPACTROUTER_MODE=affinity \
uvicorn impactrouter.app:app --host 127.0.0.1 --port 8123
```

```bash
# Manual verification: one real streamed completion through the proxy
curl -s -N 127.0.0.1:8123/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-1.5B-Instruct",
    "parent_context": "You are evaluating a candidate research idea...",
    "messages": [
      {"role": "system", "content": "You are evaluating a candidate research idea..."},
      {"role": "user", "content": "Give a one-sentence theoretical critique."}
    ]
  }'

# Confirm a correct log line was written
tail -n 1 logs/impactrouter_requests.jsonl
```

```bash
# Sibling fan-out sanity check with the real bench script (client-side timings only)
python bench/simulate_siblings.py \
  --base-url http://127.0.0.1:8123 \
  --model Qwen/Qwen2.5-1.5B-Instruct \
  --n-siblings 3 \
  --idea-id smoke_test_01
```

---

## Tier 5 — Full FM-1 Benchmark Run (PRD §11 acceptance bar)

Only run this once Tiers 1–4 have all passed cleanly.

`run_fm1_benchmark.py` performs an explicit **readiness smoke-probe** before
starting trials for each mode: it sends one real, minimal, non-streaming
completion request directly to every backend (bypassing the proxy entirely)
and blocks until every backend answers or `--readiness-timeout-s` (default
120s) elapses. This protects against a real inference engine that's still
loading model weights — a shallow liveness check (`/healthz`, `/health`) can
report success well before the engine can actually generate, which would
otherwise corrupt early-trial timing data. Expect console output like:

```
=== Launching proxy in mode=affinity (log -> logs/fm1_affinity.jsonl) ===
Proxy liveness OK. Probing backend(s) for real generation readiness (not just liveness) -- timeout 120s per backend...
All 2 backend(s) confirmed ready to generate.
Running 30 trials of 3-way sibling fan-out...
```

If a backend is still loading, you'll instead see the harness **wait and
retry silently until it succeeds or times out** (no output per retry, by
design -- the point is a clean single confirmation line once it's actually
ready, not a noisy retry log). If it never becomes ready, you'll get a clear,
specific failure naming the exact backend, e.g.:

```
RuntimeError: Backend 'http://localhost:30001' did not become ready to serve a real
completion within 120s (last error: HTTP 503: '...'). Refusing to start trials
against a backend that may still be loading -- increase --readiness-timeout-s
if this backend is known to take longer to load, or check the backend's own logs.
```

To manually exercise this failure path (e.g. to confirm the harness waits
correctly rather than hanging), start `run_fm1_benchmark.py` immediately
after launching a real backend, before it's finished loading weights, or
point `--backends` at a port nothing is listening on with a short
`--readiness-timeout-s`:

```bash
# Should fail fast (~5s) with a clear error naming the unreachable backend --
# does NOT proceed to trials, does NOT hang.
python bench/run_fm1_benchmark.py \
  --backends http://localhost:19999 \
  --trials 1 \
  --readiness-timeout-s 5 \
  --readiness-poll-interval-s 1
```

```bash
# 30+ trials per mode, against the REAL backend pool from Tier 4
python bench/run_fm1_benchmark.py \
  --backends http://localhost:30000,http://localhost:30001 \
  --model Qwen/Qwen2.5-1.5B-Instruct \
  --trials 30 \
  --n-siblings 3 \
  --port 8123 \
  --readiness-timeout-s 180 \
  --out-dir logs

# Analyze and get the FM-1 delta with spread
python bench/analyze_log.py logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
```

```bash
# Optional: repeat at a different pool size to characterize pool-size
# sensitivity (PRD §14) -- e.g. once with 2 backends, once with 4
python bench/run_fm1_benchmark.py \
  --backends http://localhost:30000,http://localhost:30001,http://localhost:30002,http://localhost:30003 \
  --model Qwen/Qwen2.5-1.5B-Instruct \
  --trials 30 \
  --port 8123 \
  --out-dir logs/pool_size_4
python bench/analyze_log.py logs/pool_size_4/fm1_affinity.jsonl logs/pool_size_4/fm1_round_robin.jsonl
```

**After a real run:** transcribe the output of `analyze_log.py`, the exact
commands used, and any observed confounds into
[`docs/fm1_benchmark_writeup.md`](./fm1_benchmark_writeup.md) — per PRD §11,
that document (not this one) is the source of truth for any number quoted
externally.

---

## Full-Suite One-Liner (Tiers 0–3 only — no GPU required)

For CI or a quick "did I break anything" pass that doesn't need a real
backend:

```bash
set -e
uv venv --python 3.11 .venv && source .venv/bin/activate
uv pip install -e ".[dev]"
pytest -v
MOCK_BACKEND_PORT=9000 python -m impactrouter.mock_backend & B0=$!
MOCK_BACKEND_PORT=9001 python -m impactrouter.mock_backend & B1=$!
sleep 1
rm -f logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
python bench/run_fm1_benchmark.py --backends http://localhost:9000,http://localhost:9001 --trials 3 --port 8123 --model mock-model
python bench/analyze_log.py logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl
kill $B0 $B1
rm -f logs/fm1_affinity.jsonl logs/fm1_round_robin.jsonl logs/impactrouter_requests.jsonl
echo "All tiers 0-3 passed."
```
