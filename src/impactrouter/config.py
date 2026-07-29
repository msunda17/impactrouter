"""Environment-based configuration for ImpactRouter.

v1 is intentionally in-memory and single-process (see PRD Section 4,
Non-Goals): no persistent config store, no multi-worker deployment. Everything
here is read once from environment variables at process start.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

RoutingMode = Literal["affinity", "round_robin"]

_VALID_MODES: tuple[RoutingMode, ...] = ("affinity", "round_robin")


def _parse_backends(raw: str) -> list[str]:
    backends = [b.strip().rstrip("/") for b in raw.split(",") if b.strip()]
    if not backends:
        raise ValueError(
            "IMPACTROUTER_BACKENDS must contain at least one backend URL "
            "(comma-separated), e.g. 'http://localhost:30000,http://localhost:30001'."
        )
    return backends


def _parse_mode(raw: str) -> RoutingMode:
    mode = raw.strip().lower()
    if mode not in _VALID_MODES:
        raise ValueError(
            f"IMPACTROUTER_MODE must be one of {_VALID_MODES}, got {raw!r}."
        )
    return mode  # type: ignore[return-value]


@dataclass(frozen=True)
class Settings:
    backends: list[str] = field(default_factory=lambda: ["http://localhost:30000"])
    mode: RoutingMode = "affinity"
    port: int = 8000
    health_check_interval_s: float = 5.0
    health_path: str = "/health"
    log_path: str = "logs/impactrouter_requests.jsonl"
    backend_timeout_s: float = 120.0

    @property
    def backend_ids(self) -> list[str]:
        """Stable, human-readable identifiers for each backend (index-based)."""
        return [f"backend_{i}" for i in range(len(self.backends))]

    @property
    def backend_id_to_url(self) -> dict[str, str]:
        return dict(zip(self.backend_ids, self.backends))


def load_settings() -> Settings:
    """Read ImpactRouter configuration from environment variables.

    Env vars (all optional, sensible defaults for local dev):
      IMPACTROUTER_BACKENDS                 comma-separated backend base URLs
      IMPACTROUTER_MODE                     "affinity" | "round_robin"
      IMPACTROUTER_PORT                     proxy listen port
      IMPACTROUTER_HEALTH_CHECK_INTERVAL_S  seconds between backend health polls
      IMPACTROUTER_HEALTH_PATH              path appended to backend URL for health checks
      IMPACTROUTER_LOG_PATH                 path to the JSONL request log
      IMPACTROUTER_BACKEND_TIMEOUT_S        per-request timeout to a backend
    """
    backends_raw = os.environ.get("IMPACTROUTER_BACKENDS", "http://localhost:30000")
    mode_raw = os.environ.get("IMPACTROUTER_MODE", "affinity")

    return Settings(
        backends=_parse_backends(backends_raw),
        mode=_parse_mode(mode_raw),
        port=int(os.environ.get("IMPACTROUTER_PORT", "8000")),
        health_check_interval_s=float(
            os.environ.get("IMPACTROUTER_HEALTH_CHECK_INTERVAL_S", "5.0")
        ),
        health_path=os.environ.get("IMPACTROUTER_HEALTH_PATH", "/health"),
        log_path=os.environ.get("IMPACTROUTER_LOG_PATH", "logs/impactrouter_requests.jsonl"),
        backend_timeout_s=float(os.environ.get("IMPACTROUTER_BACKEND_TIMEOUT_S", "120.0")),
    )
