import pytest

from impactrouter.models import ChatCompletionRequest, Message


@pytest.fixture
def make_request():
    """Builds a ChatCompletionRequest with sensible defaults, overridable per test."""

    def _make(
        messages: list[dict] | None = None,
        parent_context: str | None = None,
        model: str = "test-model",
    ) -> ChatCompletionRequest:
        messages = messages or [
            {"role": "system", "content": "shared parent instructions"},
            {"role": "user", "content": "final unique instruction"},
        ]
        return ChatCompletionRequest(
            model=model,
            messages=[Message(**m) for m in messages],
            parent_context=parent_context,
        )

    return _make
