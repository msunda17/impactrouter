"""Parent-hash fingerprint resolution (PRD 6.2).

Precedence, highest to lowest:
  1. `X-ImpactRouter-Parent-Hash` header, if present -- used verbatim as the
     routing key. The caller has already computed this; ImpactRouter does
     zero comparison overhead in this path.
  2. `parent_context` field in the request body, if present -- hashed with
     xxHash3.
  3. Fallback: hash the concatenation of every message's content except the
     last one (i.e. treat everything but the final turn as "the parent").
     This keeps the proxy usable by clients that haven't been updated to pass
     explicit topology hints, at the cost of lower affinity quality.

Determinism is the load-bearing property here: identical input must always
produce identical output within a single process run (see test_fingerprint.py).
"""

from __future__ import annotations

import xxhash

from impactrouter.models import ChatCompletionRequest


def resolve_parent_hash(
    request: ChatCompletionRequest, header_hash: str | None
) -> str:
    if header_hash:
        return header_hash

    if request.parent_context:
        return xxhash.xxh3_64_hexdigest(request.parent_context.encode("utf-8"))

    fallback_content = "".join(
        (m.content or "") for m in request.messages[:-1]
    )
    return xxhash.xxh3_64_hexdigest(fallback_content.encode("utf-8"))
