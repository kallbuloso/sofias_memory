from __future__ import annotations

from uuid import UUID, uuid4

from sofias_memory.infrastructure.postgres.advisory_lock_keys import (
    GLOBAL_BARRIER_KEY,
    dataset_lock_key,
)

_SIGNED_BIGINT_MIN = -(1 << 63)
_SIGNED_BIGINT_MAX = (1 << 63) - 1
_DATASET_KEY_MAX = (1 << 63) - 1  # 63-bit mask -> always non-negative


def test_same_dataset_id_yields_same_key() -> None:
    dataset_id = uuid4()
    assert dataset_lock_key(dataset_id) == dataset_lock_key(dataset_id)


def test_different_dataset_ids_usually_yield_different_keys() -> None:
    ids = [uuid4() for _ in range(50)]
    keys = {dataset_lock_key(dataset_id) for dataset_id in ids}
    # A collision among 50 independent ~63-bit keys is astronomically
    # unlikely; this guards against an accidentally degenerate derivation
    # (e.g. always returning a constant) rather than proving no collision
    # is theoretically possible (ADR-0009 accepts that risk for the MVP).
    assert len(keys) == len(ids)


def test_dataset_key_is_deterministic_across_process_boundary_style_recompute() -> None:
    """Locks in the exact algorithm: recomputing from raw UUID bytes via a
    fresh sha256 digest, masked to 63 bits, must match dataset_lock_key's own
    output, proving the derivation is a pure, stable function of the UUID --
    not seeded by anything process-local like Python's hash()."""

    import hashlib

    dataset_id = UUID("12345678-1234-5678-1234-567812345678")
    namespace = b"sofias_memory:adr-0009:pipeline_runs:dataset:"
    digest = hashlib.sha256(namespace + dataset_id.bytes).digest()[:8]
    value = int.from_bytes(digest, byteorder="big", signed=False) & _DATASET_KEY_MAX
    assert dataset_lock_key(dataset_id) == value


def test_global_barrier_key_is_a_stable_constant() -> None:
    from sofias_memory.infrastructure.postgres.advisory_lock_keys import (
        GLOBAL_BARRIER_KEY as reimported_key,
    )

    assert reimported_key == GLOBAL_BARRIER_KEY


def test_dataset_key_always_within_postgresql_signed_bigint_range() -> None:
    for _ in range(200):
        key = dataset_lock_key(uuid4())
        assert _SIGNED_BIGINT_MIN <= key <= _SIGNED_BIGINT_MAX


def test_global_barrier_key_within_postgresql_signed_bigint_range() -> None:
    assert _SIGNED_BIGINT_MIN <= GLOBAL_BARRIER_KEY <= _SIGNED_BIGINT_MAX


def test_dataset_key_uses_close_to_the_full_63_bit_space() -> None:
    """A derivation accidentally truncated to a much narrower range would
    never produce a key using the high bits across many samples; sampling
    enough UUIDs should find at least one key using bit 62."""

    keys = [dataset_lock_key(uuid4()) for _ in range(200)]
    assert any(key >= (1 << 62) for key in keys)


# --- namespace disjunction: by construction, not by sampling -----------------


def test_global_barrier_key_is_negative() -> None:
    assert GLOBAL_BARRIER_KEY < 0


def test_dataset_key_is_always_non_negative() -> None:
    for _ in range(200):
        assert dataset_lock_key(uuid4()) >= 0


def test_global_and_dataset_keys_are_disjoint_by_construction() -> None:
    """Not a sampled "no collision found" check: GLOBAL_BARRIER_KEY is
    negative and dataset_lock_key() is always non-negative, so no dataset_id
    -- sampled or not -- could ever produce a key equal to the global
    barrier's. The two ranges themselves cannot intersect."""

    assert GLOBAL_BARRIER_KEY < 0 <= _DATASET_KEY_MAX
    sample = {dataset_lock_key(uuid4()) for _ in range(50)}
    assert all(key >= 0 for key in sample)
    assert GLOBAL_BARRIER_KEY not in sample
