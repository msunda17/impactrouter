"""FastAPI app + route definitions for ImpactRouter (PRD Section 7).

Endpoints:
  POST /v1/chat/completions  Main proxy passthrough.
  GET  /v1/router/stats      Introspection for debugging/demo.
  GET  /healthz              Liveness check for the proxy itself.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from impactrouter.config import Settings, load_settings
from impactrouter.fingerprint import resolve_parent_hash
from impactrouter.health import HealthChecker
from impactrouter.logging_utils import JsonlRequestLogger
from impactrouter.models import (
    BackendStats,
    ChatCompletionRequest,
    HealthResponse,
    RequestLogEntry,
    RouterStatsResponse,
)
from impactrouter.proxy import ProxyResult, open_proxy_stream
from impactrouter.router import AffinityRouter

PARENT_HASH_HEADER = "X-ImpactRouter-Parent-Hash"
SCOPE_HEADER = "X-ImpactRouter-Scope"

# Request headers we don't forward to the backend as-is (host/routing/hop-by-hop).
_EXCLUDED_FORWARD_HEADERS = {
    "host",
    "content-length",
    "connection",
    PARENT_HASH_HEADER.lower(),
    SCOPE_HEADER.lower(),
}


class AppState:
    """Container for the process-wide singletons (routing table, http client,
    health checker, request logger). Non-persistent by design (PRD Section 4).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.health_checker = HealthChecker(
            backend_id_to_url=settings.backend_id_to_url,
            health_path=settings.health_path,
            interval_s=settings.health_check_interval_s,
        )
        self.router = AffinityRouter(
            backends=settings.backend_ids,
            mode=settings.mode,
            health_check=self.health_checker.is_healthy,
        )
        self.request_logger = JsonlRequestLogger(settings.log_path)
        self.http_client = httpx.AsyncClient()


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    state = AppState(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.health_checker.start()
        try:
            yield
        finally:
            await state.health_checker.stop()
            await state.http_client.aclose()

    app = FastAPI(title="ImpactRouter", lifespan=lifespan)
    app.state.impactrouter = state

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return HealthResponse()

    @app.get("/v1/router/stats", response_model=RouterStatsResponse)
    async def router_stats() -> RouterStatsResponse:
        backend_hit_counts: dict[str, int] = {bid: 0 for bid in settings.backend_ids}
        for entry in state.router.table.values():
            backend_hit_counts[entry.backend_id] = (
                backend_hit_counts.get(entry.backend_id, 0) + entry.hit_count
            )
        backends = [
            BackendStats(
                backend_id=bid,
                backend_url=settings.backend_id_to_url[bid],
                healthy=state.health_checker.is_healthy(bid),
                hit_count=backend_hit_counts.get(bid, 0),
            )
            for bid in settings.backend_ids
        ]
        return RouterStatsResponse(
            mode=state.router.mode,
            routing_table_size=len(state.router.table),
            backends=backends,
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> StreamingResponse:
        raw_body = await request.json()
        chat_request = ChatCompletionRequest.model_validate(raw_body)

        header_hash = request.headers.get(PARENT_HASH_HEADER)
        scope = request.headers.get(SCOPE_HEADER)

        parent_hash = resolve_parent_hash(chat_request, header_hash)
        backend_id, routing_outcome = state.router.select_backend(parent_hash)
        backend_url = settings.backend_id_to_url[backend_id]

        request_id = uuid.uuid4().hex[:8]
        prompt_char_len = sum(len(m.content or "") for m in chat_request.messages) + len(
            chat_request.parent_context or ""
        )

        # ImpactRouter's own fields are consumed here and not forwarded --
        # the backend doesn't understand `parent_context`. Everything else
        # (including fields this proxy doesn't explicitly model) passes
        # through untouched, per PRD 7.1.
        forward_body = chat_request.model_dump(exclude={"parent_context"}, exclude_none=True)

        forward_headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _EXCLUDED_FORWARD_HEADERS
        }
        forward_headers["content-type"] = "application/json"

        async def on_complete(result: ProxyResult) -> None:
            entry = RequestLogEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                request_id=request_id,
                parent_hash=parent_hash,
                scope=scope,
                backend_id=backend_id,
                routing_mode=state.router.mode,
                routing_outcome=routing_outcome,
                ttft_ms=result.ttft_ms,
                total_latency_ms=result.total_latency_ms,
                prompt_char_len=prompt_char_len,
            )
            await state.request_logger.log(entry)

        opened = await open_proxy_stream(
            client=state.http_client,
            backend_url=backend_url,
            path="/v1/chat/completions",
            body=forward_body,
            headers=forward_headers,
            timeout_s=settings.backend_timeout_s,
            on_complete=on_complete,
        )

        response_headers = dict(opened.response_headers)
        media_type = response_headers.pop("content-type", "application/json")

        return StreamingResponse(
            opened.body,
            status_code=opened.status_code,
            media_type=media_type,
            headers=response_headers,
        )

    return app


app = create_app()
