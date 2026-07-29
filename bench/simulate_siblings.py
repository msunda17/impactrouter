"""Synthetic sibling fan-out load generator (PRD M5).

Simulates ImpactScientists' Phase 3 tournament pattern: N sibling agents
(default N=3, matching Quick Theorist / Quick Engineer / Quick DA) all
evaluate the same candidate idea concurrently, sharing an identical parent
context and differing only in their final instruction.

Can be run standalone against a live ImpactRouter proxy for a quick manual
sanity check before committing to a full run_fm1_benchmark.py trial run (see
PRD Section 13: bench scripts are validated by manual inspection, not
unit-tested in the traditional sense).
"""

from __future__ import annotations

import argparse
import asyncio
import time
from dataclasses import dataclass

import httpx

DEFAULT_SIBLING_ROLES = [
    "You are the Quick Theorist. Evaluate this idea's theoretical soundness in two sentences.",
    "You are the Quick Engineer. Evaluate this idea's technical feasibility in two sentences.",
    "You are the Quick DA. Evaluate this idea's data/analysis implications in two sentences.",
]

# A deliberately verbose shared prefix so the prefix-cache reuse opportunity
# (the entire point of ImpactRouter) is large enough to be measurable.
DEFAULT_PARENT_CONTEXT_TEMPLATE = (
    "You are evaluating candidate research idea #{idea_id} for a tournament. "
    "Idea description: {idea_body}\n\n"
    "Shared corpus context follows -- treat this as background you must "
    "consider before answering:\n" + ("Prior related finding filler text. " * 60)
)


@dataclass
class SiblingResult:
    role_index: int
    status_code: int
    client_ttfb_ms: float
    client_total_ms: float


async def _fire_sibling(
    client: httpx.AsyncClient,
    base_url: str,
    model: str,
    parent_context: str,
    scope: str,
    role_instruction: str,
    role_index: int,
) -> SiblingResult:
    body = {
        "model": model,
        "parent_context": parent_context,
        "stream": True,
        "messages": [
            {"role": "system", "content": parent_context},
            {"role": "user", "content": role_instruction},
        ],
    }
    headers = {"X-ImpactRouter-Scope": scope}

    start = time.perf_counter()
    first_byte_time: float | None = None
    status_code = 0
    async with client.stream(
        "POST", f"{base_url}/v1/chat/completions", json=body, headers=headers
    ) as resp:
        status_code = resp.status_code
        async for chunk in resp.aiter_bytes():
            if first_byte_time is None and chunk.strip():
                first_byte_time = time.perf_counter()
    total_ms = (time.perf_counter() - start) * 1000
    ttfb_ms = (first_byte_time - start) * 1000 if first_byte_time is not None else -1.0
    return SiblingResult(role_index, status_code, ttfb_ms, total_ms)


async def simulate_sibling_fanout(
    base_url: str,
    model: str,
    idea_id: str,
    idea_body: str,
    scope: str,
    sibling_roles: list[str],
    timeout_s: float = 60.0,
) -> list[SiblingResult]:
    """Fires `len(sibling_roles)` sibling requests concurrently, all sharing
    the same parent_context, and returns their client-observed TTFB/total
    latency. The authoritative TTFT measurement lives in the proxy's JSONL
    log (see logging_utils.py) -- these client-side numbers are a convenience
    for the manual sanity-check use case, not the benchmark's source of truth.
    """
    parent_context = DEFAULT_PARENT_CONTEXT_TEMPLATE.format(idea_id=idea_id, idea_body=idea_body)
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        tasks = [
            _fire_sibling(client, base_url, model, parent_context, scope, role, i)
            for i, role in enumerate(sibling_roles)
        ]
        return await asyncio.gather(*tasks)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--model", default="mock-model")
    parser.add_argument("--n-siblings", type=int, default=3)
    parser.add_argument("--idea-id", default="demo_idea")
    parser.add_argument("--scope", default="session:manual_run/idea:demo_idea")
    return parser


async def _main_async(args: argparse.Namespace) -> None:
    roles = list(DEFAULT_SIBLING_ROLES[: args.n_siblings])
    while len(roles) < args.n_siblings:
        roles.append(f"You are sibling agent #{len(roles)}. Evaluate this idea in two sentences.")

    results = await simulate_sibling_fanout(
        base_url=args.base_url,
        model=args.model,
        idea_id=args.idea_id,
        idea_body="A candidate research idea used for FM-1 benchmark simulation.",
        scope=args.scope,
        sibling_roles=roles,
    )
    for r in sorted(results, key=lambda r: r.role_index):
        print(
            f"sibling[{r.role_index}] status={r.status_code} "
            f"ttfb={r.client_ttfb_ms:.1f}ms total={r.client_total_ms:.1f}ms"
        )


def main() -> None:
    args = _build_arg_parser().parse_args()
    asyncio.run(_main_async(args))


if __name__ == "__main__":
    main()
