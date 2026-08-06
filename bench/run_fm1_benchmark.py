"""Runs the FM-1 benchmark end to end (PRD M5, Section 11).

For each routing mode ("affinity", then "round_robin"), this script:
  1. Launches a fresh ImpactRouter proxy subprocess with IMPACTROUTER_MODE set
     accordingly and IMPACTROUTER_LOG_PATH pointed at a mode-specific JSONL file.
  2. Waits for the proxy to report healthy (shallow liveness check).
  3. Probes every backend DIRECTLY (bypassing the proxy) with a real, minimal,
     non-streaming completion request, and blocks until every backend
     actually answers or a configurable timeout elapses. This is a distinct,
     stronger check than step 2: a shallow `/health`-style liveness check can
     report success while a real inference backend is still loading model
     weights and cannot yet serve a generation request. Trials never start
     against a backend that hasn't passed this probe -- see
     ARCHITECTURE.md §6.4/§9 for the liveness-vs-readiness rationale.
  4. Runs N trials (default 30, per PRD Section 11's minimum bar) of
     bench.simulate_siblings.simulate_sibling_fanout, each trial using a fresh
     idea_id so every trial gets a brand-new parent_hash (this matters: reusing
     an idea_id across trials would let a later trial's "first" request hit an
     already-warm routing-table entry from an earlier trial, corrupting the
     cold-start-vs-sibling split described in PRD Section 14).
  5. Shuts the subprocess down cleanly.

Run this against a REAL backend pool for a result that counts toward the PRD
Section 11 acceptance bar. Running it against the bundled mock backend is
useful for validating the harness itself, but a mock-backend TTFT delta is not
a valid FM-1 measurement (the mock backend has no real prefix cache to reuse).

Usage:
    python bench/run_fm1_benchmark.py \\
        --backends http://localhost:30000,http://localhost:30001 \\
        --trials 30
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from simulate_siblings import DEFAULT_SIBLING_ROLES, simulate_sibling_fanout  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


async def _wait_for_healthy(base_url: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{base_url}/healthz")
                if resp.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError(f"Proxy at {base_url} did not become healthy within {timeout_s}s")


def _parse_backend_list(raw: str) -> list[str]:
    return [b.strip().rstrip("/") for b in raw.split(",") if b.strip()]


async def _probe_backend_ready(
    backend_url: str,
    model: str,
    timeout_s: float,
    poll_interval_s: float = 2.0,
) -> None:
    """Blocks until `backend_url` can actually serve a real generation
    request, or raises with a clear, specific error naming this backend if it
    doesn't within `timeout_s`.

    This is deliberately independent of whatever ImpactRouter's own
    HealthChecker (health.py) reports: a shallow `/health`-style liveness
    check can report success as soon as a server's HTTP listener is up, well
    before a real inference engine (e.g. SGLang) has finished loading model
    weights and can actually generate. Sending directly to the backend here
    (bypassing the proxy entirely) confirms real request-serving readiness,
    not just process liveness -- see ARCHITECTURE.md §6.4.

    Note: SGLang exposes a deeper single-token-generation health check at
    `/health_generate` (see ARCHITECTURE.md §6.4), which can be pointed at by
    IMPACTROUTER_HEALTH_PATH if you want ImpactRouter's own HealthChecker to
    use it too. This probe is the authoritative check regardless of that
    configuration -- it doesn't rely on `/health_generate` existing or
    behaving consistently across serving-engine versions.
    """
    deadline = time.monotonic() + timeout_s
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "readiness probe"}],
        "max_tokens": 1,
        "stream": False,
    }
    last_error = "no attempt made"
    async with httpx.AsyncClient(timeout=30.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.post(f"{backend_url}/v1/chat/completions", json=body)
                if resp.status_code == 200:
                    return
                last_error = f"HTTP {resp.status_code}: {resp.text[:200]!r}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(poll_interval_s)

    raise RuntimeError(
        f"Backend {backend_url!r} did not become ready to serve a real "
        f"completion within {timeout_s:.0f}s (last error: {last_error}). "
        "Refusing to start trials against a backend that may still be "
        "loading -- increase --readiness-timeout-s if this backend is known "
        "to take longer to load, or check the backend's own logs."
    )


async def _probe_all_backends_ready(
    backends: list[str],
    model: str,
    timeout_s: float,
    poll_interval_s: float = 2.0,
) -> None:
    """Probes every backend in the pool sequentially (not concurrently) so a
    failure names exactly which backend didn't come up, rather than an
    ambiguous "one of N backends" error."""
    for backend_url in backends:
        await _probe_backend_ready(backend_url, model, timeout_s, poll_interval_s)


def _launch_proxy(
    mode: str,
    backends: str,
    log_path: Path,
    host: str,
    port: int,
    health_path: str,
) -> subprocess.Popen:
    env = os.environ.copy()
    env["IMPACTROUTER_BACKENDS"] = backends
    env["IMPACTROUTER_MODE"] = mode
    env["IMPACTROUTER_LOG_PATH"] = str(log_path)
    env["IMPACTROUTER_HEALTH_PATH"] = health_path
    cmd = [
        sys.executable,
        "-m",
        "uvicorn",
        "impactrouter.app:app",
        "--host",
        host,
        "--port",
        str(port),
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _run_trials(
    base_url: str,
    model: str,
    mode: str,
    trials: int,
    n_siblings: int,
    inter_trial_delay_s: float,
) -> None:
    roles = list(DEFAULT_SIBLING_ROLES[:n_siblings])
    while len(roles) < n_siblings:
        roles.append(f"You are sibling agent #{len(roles)}. Evaluate this idea in two sentences.")

    run_id = uuid.uuid4().hex[:6]
    for trial_index in range(trials):
        idea_id = f"fm1_{mode}_{run_id}_{trial_index}"
        scope = f"session:fm1_bench_{run_id}/mode:{mode}/trial:{trial_index}"
        results = await simulate_sibling_fanout(
            base_url=base_url,
            model=model,
            idea_id=idea_id,
            idea_body=f"FM-1 benchmark trial {trial_index} synthetic idea body.",
            scope=scope,
            sibling_roles=roles,
        )
        failures = [r for r in results if r.status_code != 200]
        status = "OK" if not failures else f"{len(failures)} FAILED"
        print(f"  [{mode}] trial {trial_index + 1}/{trials}: {status}")
        if inter_trial_delay_s > 0:
            await asyncio.sleep(inter_trial_delay_s)


async def _run_mode(
    mode: str,
    args: argparse.Namespace,
    log_path: Path,
) -> None:
    if log_path.exists():
        log_path.unlink()

    base_url = f"http://{args.host}:{args.port}"
    print(f"\n=== Launching proxy in mode={mode} (log -> {log_path}) ===")
    proc = _launch_proxy(
        mode=mode,
        backends=args.backends,
        log_path=log_path,
        host=args.host,
        port=args.port,
        health_path=args.health_path,
    )
    try:
        await _wait_for_healthy(base_url, timeout_s=args.startup_timeout_s)
        print("Proxy liveness OK. Probing backend(s) for real generation readiness "
              f"(not just liveness) -- timeout {args.readiness_timeout_s:.0f}s per backend...")
        backend_list = _parse_backend_list(args.backends)
        await _probe_all_backends_ready(
            backends=backend_list,
            model=args.model,
            timeout_s=args.readiness_timeout_s,
            poll_interval_s=args.readiness_poll_interval_s,
        )
        print(f"All {len(backend_list)} backend(s) confirmed ready to generate.")
        print(f"Running {args.trials} trials of {args.n_siblings}-way sibling fan-out...")
        await _run_trials(
            base_url=base_url,
            model=args.model,
            mode=mode,
            trials=args.trials,
            n_siblings=args.n_siblings,
            inter_trial_delay_s=args.inter_trial_delay_s,
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--backends", required=True, help="Comma-separated backend base URLs.")
    parser.add_argument("--model", default="mock-model")
    parser.add_argument("--trials", type=int, default=30, help="Trials per mode (PRD minimum: 30).")
    parser.add_argument("--n-siblings", type=int, default=3)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--health-path", default="/health")
    parser.add_argument(
        "--startup-timeout-s",
        type=float,
        default=30.0,
        help="How long to wait for the proxy process itself to report /healthz liveness.",
    )
    parser.add_argument(
        "--readiness-timeout-s",
        type=float,
        default=120.0,
        help=(
            "How long to wait, per backend, for a REAL completion request to succeed "
            "before giving up. Distinct from --startup-timeout-s: a real inference "
            "engine can take a couple of minutes to load model weights after its HTTP "
            "server is already answering /health checks (default: 120s / 2 minutes)."
        ),
    )
    parser.add_argument(
        "--readiness-poll-interval-s",
        type=float,
        default=2.0,
        help="Delay between readiness-probe retries against a single backend.",
    )
    parser.add_argument(
        "--inter-trial-delay-s",
        type=float,
        default=0.1,
        help="Delay between trials, to avoid saturating a small backend pool.",
    )
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "logs"))
    return parser


async def _main_async(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    log_paths = {}
    for mode in ("affinity", "round_robin"):
        log_path = out_dir / f"fm1_{mode}.jsonl"
        await _run_mode(mode, args, log_path)
        log_paths[mode] = log_path

    print("\n=== Benchmark complete ===")
    for mode, path in log_paths.items():
        print(f"  {mode}: {path}")
    print(
        "\nNext: python bench/analyze_log.py "
        f"{log_paths['affinity']} {log_paths['round_robin']}"
    )


def main() -> None:
    args = _build_arg_parser().parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
