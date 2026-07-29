"""ImpactRouter: a drop-in proxy that routes sibling LLM agent calls to the
same backend instance to maximize prefix-cache reuse (RadixAttention /
PagedAttention prefix cache).

ImpactRouter is complementary to, not competing with, workflow-aware eviction
(e.g. KVFlow) or KV-cache TTL scheduling (e.g. Continuum). Those systems solve
*what to keep* and *for how long*. ImpactRouter solves *where to send it* so
the serving engine's own cache has a chance to help in the first place.
"""

__version__ = "0.1.0"
