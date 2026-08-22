from __future__ import annotations

import json
from hashlib import sha256

from sofias_memory.pipelines.hashing import (
    canonical_json,
    canonical_step_input_hash,
    canonical_work_payload_hash,
)


def test_canonical_json_sorts_keys() -> None:
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_canonical_json_uses_stable_compact_separators() -> None:
    assert canonical_json({"a": 1}) == '{"a":1}'


def test_canonical_step_input_hash_is_deterministic() -> None:
    first = canonical_step_input_hash(definition_id="step:v1", semantic_input={"x": 1})
    second = canonical_step_input_hash(definition_id="step:v1", semantic_input={"x": 1})
    assert first == second


def test_canonical_step_input_hash_is_order_independent() -> None:
    first = canonical_step_input_hash(definition_id="step:v1", semantic_input={"a": 1, "b": 2})
    second = canonical_step_input_hash(definition_id="step:v1", semantic_input={"b": 2, "a": 1})
    assert first == second


def test_canonical_step_input_hash_is_64_char_hex() -> None:
    digest = canonical_step_input_hash(definition_id="step:v1", semantic_input={"x": 1})
    assert len(digest) == 64
    int(digest, 16)  # must not raise


def test_canonical_step_input_hash_changes_with_input() -> None:
    first = canonical_step_input_hash(definition_id="step:v1", semantic_input={"x": 1})
    second = canonical_step_input_hash(definition_id="step:v1", semantic_input={"x": 2})
    assert first != second


def test_canonical_step_input_hash_changes_with_definition_id() -> None:
    """ADR-0009 SS 6: a step's own definition/version identity participates
    in the hash, so a registry behavior change is detectable as drift even
    for otherwise-identical semantic input."""

    first = canonical_step_input_hash(definition_id="step:v1", semantic_input={"x": 1})
    second = canonical_step_input_hash(definition_id="step:v2", semantic_input={"x": 1})
    assert first != second


def test_canonical_step_input_hash_semantically_equivalent_input_matches() -> None:
    first = canonical_step_input_hash(
        definition_id="step:v1", semantic_input={"a": 1, "nested": {"y": 2, "x": 1}}
    )
    second = canonical_step_input_hash(
        definition_id="step:v1", semantic_input={"nested": {"x": 1, "y": 2}, "a": 1}
    )
    assert first == second


# --- canonical_work_payload_hash: B4 payload_hash compatibility (SM-509 -----
# audit Finding 4). Deliberately NOT built on canonical_json(), which is
# ensure_ascii=True and would silently diverge from B4's historical
# services.remember.stable_payload_hash encoding.


def b4_stable_payload_hash(payload: object) -> str:
    """Reproduces B4's historical canonicalization exactly (mirrors
    ``services.remember.stable_payload_hash`` without importing it), so this
    test proves byte-for-byte compatibility rather than merely agreement
    with itself."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def test_canonical_work_payload_hash_matches_b4_canonicalization_for_ascii_payload() -> None:
    payload = {"dataset": "docs", "name": "note"}
    assert canonical_work_payload_hash(payload) == b4_stable_payload_hash(payload)


def test_canonical_work_payload_hash_matches_b4_canonicalization_for_unicode_payload() -> None:
    payload = {"dataset": "manutenção", "metadata": {"cidade": "São Paulo"}}
    assert canonical_work_payload_hash(payload) == b4_stable_payload_hash(payload)


def test_canonical_work_payload_hash_is_deterministic_for_unicode_payload() -> None:
    payload = {"dataset": "manutenção", "metadata": {"cidade": "São Paulo"}}
    assert canonical_work_payload_hash(payload) == canonical_work_payload_hash(dict(payload))


def test_canonical_work_payload_hash_differs_from_ascii_safe_canonical_json_encoding() -> None:
    """Proves this function is NOT reusing ``canonical_json`` (which would
    escape non-ASCII characters as ``\\uXXXX`` and produce a different
    digest for the same logical payload)."""

    payload = {"cidade": "São Paulo"}
    ascii_safe_digest = sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    assert canonical_work_payload_hash(payload) != ascii_safe_digest
