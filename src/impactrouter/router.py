"""AffinityRouter: the sibling-routing mechanism (PRD 6.3).

The `mode` toggle ("affinity" vs "round_robin") is the entire benchmark
mechanism -- see PRD 6.3. It must stay a runtime-configurable value (driven by
IMPACTROUTER_MODE), never a hardcoded branch, so bench/run_fm1_benchmark.py
can flip it between runs without touching code.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Literal

RoutingOutcome = Literal["affinity_hit", "affinity_miss_new", "control_round_robin"]


@dataclass
class RoutingEntry:
    parent_hash: str
    backend_id: str
    created_at: float
    last_used_at: float
    hit_count: int = 0


@dataclass
class AffinityRouter:
    """In-memory, non-persistent affinity routing table (PRD 6.3).

    No eviction policy in v1: prototype-scale traffic, process restart resets
    state. This is intentional -- see PRD Section 4, Non-Goals.
    """

    backends: list[str]
    mode: Literal["affinity", "round_robin"] = "affinity"
    health_check: Callable[[str], bool] = field(default=lambda backend_id: True)
    table: dict[str, RoutingEntry] = field(default_factory=dict)
    _rr_counter: int = field(default=0, repr=False)

    def select_backend(self, parent_hash: str) -> tuple[str, RoutingOutcome]:
        """Returns (backend_id, routing_outcome).

        routing_outcome is one of 'affinity_hit', 'affinity_miss_new',
        'control_round_robin'.
        """
        if self.mode == "round_robin":
            backend = self._next_round_robin()
            return backend, "control_round_robin"

        entry = self.table.get(parent_hash)
        if entry is not None and self._is_healthy(entry.backend_id):
            entry.last_used_at = time.time()
            entry.hit_count += 1
            return entry.backend_id, "affinity_hit"

        backend = self._next_round_robin()
        if entry is not None:
            # Sticky backend is unhealthy: fall back to round-robin for THIS
            # request without corrupting the table entry, so future healthy
            # retries can still resolve to the original sticky backend.
            return backend, "affinity_miss_new"

        self.table[parent_hash] = RoutingEntry(
            parent_hash=parent_hash,
            backend_id=backend,
            created_at=time.time(),
            last_used_at=time.time(),
        )
        return backend, "affinity_miss_new"

    def _next_round_robin(self) -> str:
        backend = self.backends[self._rr_counter % len(self.backends)]
        self._rr_counter += 1
        return backend

    def _is_healthy(self, backend_id: str) -> bool:
        return self.health_check(backend_id)
