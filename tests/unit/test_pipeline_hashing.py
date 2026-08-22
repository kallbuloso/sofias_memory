from __future__ import annotations

from sofias_memory.pipelines.hashing import canonical_json, canonical_step_input_hash


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
