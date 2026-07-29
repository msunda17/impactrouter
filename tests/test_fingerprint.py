"""Determinism, header precedence, and fallback correctness (PRD M1)."""

from impactrouter.fingerprint import resolve_parent_hash


def test_same_input_same_hash_across_repeated_calls(make_request):
    req = make_request(parent_context="the shared idea description")
    h1 = resolve_parent_hash(req, header_hash=None)
    h2 = resolve_parent_hash(req, header_hash=None)
    h3 = resolve_parent_hash(req, header_hash=None)
    assert h1 == h2 == h3


def test_different_parent_context_different_hash(make_request):
    req_a = make_request(parent_context="idea A")
    req_b = make_request(parent_context="idea B")
    assert resolve_parent_hash(req_a, header_hash=None) != resolve_parent_hash(
        req_b, header_hash=None
    )


def test_header_takes_precedence_over_computed_hash(make_request):
    req = make_request(parent_context="idea A")
    header_hash = "deadbeefcafe0000"
    assert resolve_parent_hash(req, header_hash=header_hash) == header_hash


def test_header_precedence_even_with_no_parent_context(make_request):
    req = make_request(parent_context=None)
    header_hash = "abc123"
    assert resolve_parent_hash(req, header_hash=header_hash) == header_hash


def test_fallback_path_when_header_and_parent_context_absent(make_request):
    req = make_request(parent_context=None)
    result = resolve_parent_hash(req, header_hash=None)
    assert isinstance(result, str)
    assert len(result) > 0


def test_fallback_ignores_final_message_content(make_request):
    """Two requests sharing every message except the last should fall back to
    the same hash, since the fallback treats everything but the final turn as
    the shared parent."""
    shared_prefix = [
        {"role": "system", "content": "shared parent instructions"},
    ]
    req_a = make_request(messages=shared_prefix + [{"role": "user", "content": "role A instruction"}])
    req_b = make_request(messages=shared_prefix + [{"role": "user", "content": "role B instruction"}])
    assert resolve_parent_hash(req_a, header_hash=None) == resolve_parent_hash(
        req_b, header_hash=None
    )


def test_fallback_differs_when_shared_prefix_differs(make_request):
    req_a = make_request(
        messages=[
            {"role": "system", "content": "prefix A"},
            {"role": "user", "content": "final"},
        ]
    )
    req_b = make_request(
        messages=[
            {"role": "system", "content": "prefix B"},
            {"role": "user", "content": "final"},
        ]
    )
    assert resolve_parent_hash(req_a, header_hash=None) != resolve_parent_hash(
        req_b, header_hash=None
    )


def test_parent_context_takes_precedence_over_fallback(make_request):
    """If parent_context is set, it should be used even though messages could
    also produce a fallback hash -- parent_context wins."""
    req = make_request(
        messages=[
            {"role": "system", "content": "prefix"},
            {"role": "user", "content": "final"},
        ],
        parent_context="explicit parent context",
    )
    expected = resolve_parent_hash(make_request(parent_context="explicit parent context"), None)
    assert resolve_parent_hash(req, header_hash=None) == expected
