"""TTFT measurement correctness against a mock backend (PRD M3).

Uses httpx's ASGITransport to talk to the mock backend in-process (no real
sockets, no GPU needed) while still exercising the real streaming code path.
"""

import httpx
import pytest

from impactrouter.mock_backend import create_mock_backend_app
from impactrouter.proxy import ProxyResult, open_proxy_stream


@pytest.mark.asyncio
async def test_ttft_captured_and_less_than_total_latency():
    mock_app = create_mock_backend_app(
        initial_delay_s=0.05, inter_chunk_delay_s=0.02, num_chunks=5
    )
    transport = httpx.ASGITransport(app=mock_app)
    results: list[ProxyResult] = []

    async def on_complete(result: ProxyResult) -> None:
        results.append(result)

    async with httpx.AsyncClient(transport=transport, base_url="http://mockbackend") as client:
        opened = await open_proxy_stream(
            client=client,
            backend_url="",
            path="/v1/chat/completions",
            body={
                "model": "mock-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            headers={},
            timeout_s=10,
            on_complete=on_complete,
        )
        chunks = [chunk async for chunk in opened.body]

    assert opened.status_code == 200
    assert len(chunks) > 0
    assert b"[DONE]" in b"".join(chunks)

    assert len(results) == 1
    result = results[0]
    assert result.ttft_ms is not None
    assert result.total_latency_ms is not None
    assert result.ttft_ms < result.total_latency_ms
    # TTFT should reflect the ~50ms initial delay, not the full multi-chunk stream.
    assert result.ttft_ms >= 40


@pytest.mark.asyncio
async def test_on_complete_fires_only_after_full_stream_consumed():
    mock_app = create_mock_backend_app(
        initial_delay_s=0.01, inter_chunk_delay_s=0.01, num_chunks=3
    )
    transport = httpx.ASGITransport(app=mock_app)
    fired = False

    async def on_complete(result: ProxyResult) -> None:
        nonlocal fired
        fired = True

    async with httpx.AsyncClient(transport=transport, base_url="http://mockbackend") as client:
        opened = await open_proxy_stream(
            client=client,
            backend_url="",
            path="/v1/chat/completions",
            body={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={},
            timeout_s=10,
            on_complete=on_complete,
        )
        assert fired is False  # not consumed yet
        async for _ in opened.body:
            pass

    assert fired is True


@pytest.mark.asyncio
async def test_response_headers_and_status_forwarded():
    mock_app = create_mock_backend_app(initial_delay_s=0.0, num_chunks=1)
    transport = httpx.ASGITransport(app=mock_app)

    async with httpx.AsyncClient(transport=transport, base_url="http://mockbackend") as client:
        opened = await open_proxy_stream(
            client=client,
            backend_url="",
            path="/v1/chat/completions",
            body={"model": "mock-model", "messages": [{"role": "user", "content": "hi"}]},
            headers={},
            timeout_s=10,
        )
        async for _ in opened.body:
            pass

    assert opened.status_code == 200
    assert opened.response_headers.get("content-type", "").startswith("text/event-stream")
