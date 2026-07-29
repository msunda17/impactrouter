"""Pydantic schemas for ImpactRouter.

The request schema extends the standard OpenAI chat completion shape with one
ImpactRouter-specific optional field (`parent_context`, see PRD 7.1). Unknown
fields are preserved so the proxy can pass them through to the backend
untouched (PRD 7.1: "do not strip fields the proxy doesn't understand").
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict


class Message(BaseModel):
    """A single chat message. Extra fields (e.g. name, tool_calls) pass through."""

    model_config = ConfigDict(extra="allow")

    role: str
    content: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    """OpenAI-compatible chat completion request, plus ImpactRouter's optional
    `parent_context` hint (PRD 6.2, 7.1).

    Extra/unknown fields are allowed and preserved so they can be forwarded to
    the backend untouched.
    """

    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message]
    stream: bool = True
    parent_context: Optional[str] = None


RoutingOutcome = Literal["affinity_hit", "affinity_miss_new", "control_round_robin"]


class RequestLogEntry(BaseModel):
    """Schema for one JSONL line in the TTFT request log (PRD 6.6).

    The schema is intentionally stable -- bench/analyze_log.py depends on
    every field being present on every line.
    """

    timestamp: str
    request_id: str
    parent_hash: str
    scope: Optional[str] = None
    backend_id: str
    routing_mode: str
    routing_outcome: RoutingOutcome
    ttft_ms: Optional[float] = None
    total_latency_ms: float
    prompt_char_len: int


class BackendStats(BaseModel):
    backend_id: str
    backend_url: str
    healthy: bool
    hit_count: int


class RouterStatsResponse(BaseModel):
    mode: str
    routing_table_size: int
    backends: list[BackendStats]


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
