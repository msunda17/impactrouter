"""Backend health polling (PRD 6.5).

v1 health checking is intentionally minimal: a periodic GET on each backend's
health path, cached for N seconds, boolean up/down. No load-based scoring, no
latency-weighted selection -- this is a prototype proving a routing
hypothesis, not a robust load balancer.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

logger = logging.getLogger("impactrouter.health")


class HealthChecker:
    def __init__(
        self,
        backend_id_to_url: dict[str, str],
        health_path: str = "/health",
        interval_s: float = 5.0,
        timeout_s: float = 3.0,
    ) -> None:
        self._backend_id_to_url = backend_id_to_url
        self._health_path = health_path
        self._interval_s = interval_s
        self._timeout_s = timeout_s
        # Assume healthy until proven otherwise, so routing works before the
        # first poll completes.
        self._status: dict[str, bool] = {bid: True for bid in backend_id_to_url}
        self._task: asyncio.Task | None = None

    def is_healthy(self, backend_id: str) -> bool:
        return self._status.get(backend_id, False)

    async def _poll_once(self) -> None:
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            for backend_id, base_url in self._backend_id_to_url.items():
                try:
                    resp = await client.get(f"{base_url}{self._health_path}")
                    self._status[backend_id] = resp.status_code < 500
                except httpx.HTTPError as exc:
                    logger.debug("Health check failed for %s: %s", backend_id, exc)
                    self._status[backend_id] = False

    async def _run(self) -> None:
        while True:
            await self._poll_once()
            await asyncio.sleep(self._interval_s)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
