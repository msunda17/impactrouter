"""Streaming passthrough proxy (PRD 6.4).

TTFT is only measurable if the first token is observable independently from
the full response, so this module streams response bytes chunk-by-chunk as
they arrive from the backend rather than buffering the full response.

Design note: opening the backend request is split from consuming its body so
that the caller (app.py) can inspect the backend's status code and headers
(e.g. Content-Type: text/event-stream) *before* constructing the client-facing
StreamingResponse, while still measuring TTFT against the first real chunk
byte and firing `on_complete` only after every byte has been forwarded.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

import httpx

# Headers we don't blindly forward from the backend response: httpx handles
# decoding transparently, and re-chunking the stream invalidates Content-Length.
_EXCLUDED_RESPONSE_HEADERS = {
    "content-length",
    "content-encoding",
    "transfer-encoding",
    "connection",
}


@dataclass
class ProxyResult:
    ttft_ms: float | None
    total_latency_ms: float
    status_code: int
    response_headers: dict[str, str]


@dataclass
class OpenedProxyStream:
    status_code: int
    response_headers: dict[str, str]
    body: AsyncIterator[bytes]


async def open_proxy_stream(
    client: httpx.AsyncClient,
    backend_url: str,
    path: str,
    body: dict,
    headers: dict[str, str],
    timeout_s: float,
    on_complete: Callable[[ProxyResult], Awaitable[None]] | None = None,
) -> OpenedProxyStream:
    """Opens a streamed POST to `backend_url + path` and returns the backend's
    status code / headers immediately, plus an async iterator of raw response
    bytes.

    `on_complete` fires once the byte iterator is fully exhausted (i.e. after
    the client-facing response has finished sending), with TTFT measured from
    the moment this function is called to the first non-empty chunk.
    """
    start = time.perf_counter()
    stream_cm = client.stream(
        "POST",
        f"{backend_url}{path}",
        json=body,
        headers=headers,
        timeout=timeout_s,
    )
    resp = await stream_cm.__aenter__()
    status_code = resp.status_code
    response_headers = {
        k: v
        for k, v in resp.headers.items()
        if k.lower() not in _EXCLUDED_RESPONSE_HEADERS
    }

    async def _body_iterator() -> AsyncIterator[bytes]:
        first_token_time: float | None = None
        try:
            async for chunk in resp.aiter_bytes():
                if first_token_time is None and chunk.strip():
                    first_token_time = time.perf_counter()
                yield chunk
        finally:
            await stream_cm.__aexit__(None, None, None)
            total_ms = (time.perf_counter() - start) * 1000
            ttft_ms = (
                (first_token_time - start) * 1000
                if first_token_time is not None
                else None
            )
            if on_complete is not None:
                await on_complete(
                    ProxyResult(
                        ttft_ms=ttft_ms,
                        total_latency_ms=total_ms,
                        status_code=status_code,
                        response_headers=response_headers,
                    )
                )

    return OpenedProxyStream(
        status_code=status_code,
        response_headers=response_headers,
        body=_body_iterator(),
    )
