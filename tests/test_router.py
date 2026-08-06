"""Sticky affinity, round-robin fallback for new hashes, mode toggle, and
health-based fallback (PRD M2)."""

from impactrouter.router import AffinityRouter


def test_sticky_routing_for_previously_seen_hash():
    router = AffinityRouter(backends=["b0", "b1", "b2"], mode="affinity")
    backend_first, outcome_first = router.select_backend("hash_a")
    assert outcome_first == "affinity_miss_new"

    backend_second, outcome_second = router.select_backend("hash_a")
    assert backend_second == backend_first
    assert outcome_second == "affinity_hit"

    backend_third, outcome_third = router.select_backend("hash_a")
    assert backend_third == backend_first
    assert outcome_third == "affinity_hit"


def test_new_hash_uses_round_robin_among_backends():
    router = AffinityRouter(backends=["b0", "b1", "b2"], mode="affinity")
    seen_backends = []
    for i in range(3):
        backend, outcome = router.select_backend(f"hash_{i}")
        assert outcome == "affinity_miss_new"
        seen_backends.append(backend)
    assert seen_backends == ["b0", "b1", "b2"]


def test_round_robin_mode_ignores_routing_table_regardless_of_hash():
    router = AffinityRouter(backends=["b0", "b1"], mode="round_robin")
    backend1, outcome1 = router.select_backend("hash_a")
    backend2, outcome2 = router.select_backend("hash_a")
    backend3, outcome3 = router.select_backend("hash_a")

    assert outcome1 == outcome2 == outcome3 == "control_round_robin"
    # Same hash repeated, but round_robin mode must cycle backends anyway.
    assert [backend1, backend2, backend3] == ["b0", "b1", "b0"]
    # The routing table must never be populated in round_robin mode.
    assert router.table == {}


def test_hit_count_increments_on_repeated_hits():
    router = AffinityRouter(backends=["b0"], mode="affinity")
    router.select_backend("hash_a")
    router.select_backend("hash_a")
    router.select_backend("hash_a")
    assert router.table["hash_a"].hit_count == 2


def test_health_check_failure_falls_back_without_corrupting_table_entry():
    unhealthy_backends = {"b0"}

    def health_check(backend_id: str) -> bool:
        return backend_id not in unhealthy_backends

    router = AffinityRouter(
        backends=["b0", "b1"], mode="affinity", health_check=health_check
    )

    # First call: round-robins to b0 and stores it as sticky for hash_a.
    # This is a TRUE cold start (parent_hash never seen before) -> affinity_miss_new.
    backend_first, outcome_first = router.select_backend("hash_a")
    assert backend_first == "b0"
    assert outcome_first == "affinity_miss_new"
    assert router.table["hash_a"].backend_id == "b0"

    # b0 is now unhealthy: request for hash_a should fall back to round-robin
    # selection WITHOUT corrupting the sticky table entry for hash_a. This is
    # a DIFFERENT situation from a true cold start -- hash_a WAS seen before,
    # its sticky backend is just transiently unhealthy -- so it gets its own
    # distinct outcome, affinity_fallback_unhealthy, rather than being
    # collapsed into affinity_miss_new (which would make it impossible to
    # tell from the log alone whether a health blip occurred).
    backend_second, outcome_second = router.select_backend("hash_a")
    assert backend_second != "b0"
    assert outcome_second == "affinity_fallback_unhealthy"
    assert router.table["hash_a"].backend_id == "b0"  # untouched

    # Once b0 becomes healthy again, hash_a should resolve back to it.
    unhealthy_backends.clear()
    backend_third, outcome_third = router.select_backend("hash_a")
    assert backend_third == "b0"
    assert outcome_third == "affinity_hit"


def test_new_parent_hash_is_distinct_from_unhealthy_sticky_fallback():
    """A true cold start (affinity_miss_new) and a known parent_hash whose
    sticky backend is currently unhealthy (affinity_fallback_unhealthy) must
    produce two different outcome values, even though both round-robin to
    pick a backend under the hood."""
    unhealthy_backends = {"b0"}

    def health_check(backend_id: str) -> bool:
        return backend_id not in unhealthy_backends

    router = AffinityRouter(
        backends=["b0", "b1"], mode="affinity", health_check=health_check
    )

    # hash_new has never been seen -> true cold start, regardless of any
    # backend's health.
    _, outcome_new = router.select_backend("hash_new")
    assert outcome_new == "affinity_miss_new"

    # hash_a becomes sticky to a backend (b1, since b0 is unhealthy at
    # round-robin time here -- doesn't matter which, just needs an entry).
    router.select_backend("hash_a")
    # Force hash_a's sticky backend to be the unhealthy one for this check:
    router.table["hash_a"].backend_id = "b0"

    _, outcome_known_but_unhealthy = router.select_backend("hash_a")
    assert outcome_known_but_unhealthy == "affinity_fallback_unhealthy"
    assert outcome_known_but_unhealthy != outcome_new


def test_independent_hashes_do_not_interfere():
    router = AffinityRouter(backends=["b0", "b1"], mode="affinity")
    backend_a, _ = router.select_backend("hash_a")
    backend_b, _ = router.select_backend("hash_b")
    # Repeated lookups remain sticky to their own hash independently.
    assert router.select_backend("hash_a")[0] == backend_a
    assert router.select_backend("hash_b")[0] == backend_b
