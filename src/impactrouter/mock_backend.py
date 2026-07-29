"""A trivial mock OpenAI-compatible backend (PRD M3, Section 12).

Used two ways:
  1. Imported directly in tests via httpx's ASGITransport, so proxy
     correctness can be verified with zero real sockets and zero GPU.
  2. Run standalone (`python -m impactrouter.mock_backend`) as one of the
     lightweight echo backends in a "mixed real/mock pool" (PRD Section 12)
     to validate routing *logic* correctness before spending real-backend
     trials on the actual TTFT number.

This is intentionally not a faithful OpenAI server -- it echoes a fixed
streamed response with configurable artificial latency, just enough to make
TTFT observable and distinct from total latency.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

from fastapi import FastAPI
from fastapi.responses import StreamingResponse


def create_mock_backend_app(
    initial_delay_s: float = 0.05,
    inter_chunk_delay_s: float = 0.01,
    num_chunks: int = 5,
    chunk_text: str = "token ",
) -> FastAPI:
    app = FastAPI(title="ImpactRouter Mock Backend")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat_completions() -> StreamingResponse:
        completion_id = f"mock-{uuid.uuid4().hex[:8]}"
        created = int(time.time())

        async def event_stream():
            await asyncio.sleep(initial_delay_s)
            for i in range(num_chunks):
                chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": "mock-model",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": chunk_text},
                            "finish_reason": None if i < num_chunks - 1 else "stop",
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
                if i < num_chunks - 1:
                    await asyncio.sleep(inter_chunk_delay_s)
            yield b"data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


if __name__ == "__main__":
    import os

    import uvicorn

    port = int(os.environ.get("MOCK_BACKEND_PORT", "9000"))
    delay = float(os.environ.get("MOCK_BACKEND_INITIAL_DELAY_S", "0.05"))
    uvicorn.run(create_mock_backend_app(initial_delay_s=delay), host="0.0.0.0", port=port)
