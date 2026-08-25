"""Unit tests for Forget's pure/reusable primitives (SM-512).

Since ``services.forget`` no longer owns run lifecycle (that moved to
``pipelines.steps.forget``, ADR-0009 SS O), this file exercises the pure
business-logic helpers: scope derivation, work identity, B4-legacy intent
compatibility, storage-path safety, projection command building, and the
authoritative-mutation helpers against a fake unit of work. Step-level
(execute/persist) behavior lives in ``test_forget_pipeline_steps.py``;
route/submission behavior lives in ``test_forget_routes.py``; durability/
concurrency/filesystem/Neo4j behavior lives in the integration suite.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.infrastructure.postgres.models import Chunk
from sofias_memory.services.forget import (
    ForgetScope,
    determine_forget_scope,
    forget_dataset_run_input,
    forget_everything_run_input,
    forget_projection_commands,
    forget_semantic_intent_from_run_input,
    forget_source_run_input,
    invalid_storage_uri_error,
    reset_document_for_recognify,
    same_forget_intent,
    source_storage_path,
)

# ---------------------------------------------------------------------------
# Scope derivation (FR-090)
# ---------------------------------------------------------------------------


def test_source_scope_allows_default_dataset() -> None:
    scope = determine_forget_scope(
        dataset="main",
        fields_set=set(),
        source_id=uuid4(),
        everything=False,
        confirm=None,
        memory_only=False,
    )
    assert scope is ForgetScope.SOURCE


def test_dataset_scope_requires_explicit_dataset() -> None:
    with pytest.raises(SofiasMemoryError):
        determine_forget_scope(
            dataset="main",
            fields_set=set(),
            source_id=None,
            everything=False,
            confirm=None,
            memory_only=False,
        )
    scope = determine_forget_scope(
        dataset="main",
        fields_set={"dataset"},
        source_id=None,
        everything=False,
        confirm=None,
        memory_only=False,
    )
    assert scope is ForgetScope.DATASET


def test_everything_requires_exact_confirm_phrase() -> None:
    with pytest.raises(SofiasMemoryError):
        determine_forget_scope(
            dataset="main",
            fields_set=set(),
            source_id=None,
            everything=True,
            confirm=None,
            memory_only=False,
        )
    with pytest.raises(SofiasMemoryError):
        determine_forget_scope(
            dataset="main",
            fields_set=set(),
            source_id=None,
            everything=True,
            confirm="delete everything",
            memory_only=False,
        )
    scope = determine_forget_scope(
        dataset="main",
        fields_set=set(),
        source_id=None,
        everything=True,
        confirm="DELETE EVERYTHING",
        memory_only=False,
    )
    assert scope is ForgetScope.EVERYTHING


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_id": uuid4()},
        {"fields_set": {"dataset"}},
        {"memory_only": True},
    ],
)
def test_everything_rejects_incompatible_fields(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {
        "dataset": "main",
        "fields_set": set(),
        "source_id": None,
        "everything": True,
        "confirm": "DELETE EVERYTHING",
        "memory_only": False,
    }
    base.update(kwargs)
    with pytest.raises(SofiasMemoryError):
        determine_forget_scope(**base)  # type: ignore[arg-type]


def test_confirm_rejected_outside_everything() -> None:
    with pytest.raises(SofiasMemoryError):
        determine_forget_scope(
            dataset="main",
            fields_set=set(),
            source_id=uuid4(),
            everything=False,
            confirm="DELETE EVERYTHING",
            memory_only=False,
        )


# ---------------------------------------------------------------------------
# Work identity (SM-512 SS 4): wait/confirm excluded, memory_only included
# ---------------------------------------------------------------------------


def test_source_run_input_excludes_wait_and_confirm() -> None:
    source_id = uuid4()
    work_input = forget_source_run_input(dataset="main", source_id=source_id, memory_only=True)
    assert work_input == {
        "scope": "source",
        "dataset": "main",
        "source_id": str(source_id),
        "memory_only": True,
    }
    assert "wait" not in work_input
    assert "confirm" not in work_input


def test_dataset_run_input_excludes_wait() -> None:
    work_input = forget_dataset_run_input(dataset="main", memory_only=False)
    assert work_input == {"scope": "dataset", "dataset": "main", "memory_only": False}


def test_everything_run_input_is_scope_only() -> None:
    assert forget_everything_run_input() == {"scope": "everything"}


def test_memory_only_changes_the_work_identity() -> None:
    source_id = uuid4()
    full = forget_source_run_input(dataset="main", source_id=source_id, memory_only=False)
    memory_only = forget_source_run_input(dataset="main", source_id=source_id, memory_only=True)
    assert full != memory_only


# ---------------------------------------------------------------------------
# B4 -> B5 semantic intent compatibility (SM-512 SS 5)
# ---------------------------------------------------------------------------


def test_b4_legacy_intent_with_wait_is_compatible_with_b5_intent() -> None:
    source_id = uuid4()
    b4_legacy_input = {
        "scope": "source",
        "dataset": "main",
        "source_id": str(source_id),
        "memory_only": False,
        "wait": True,
    }
    b5_input = forget_source_run_input(dataset="main", source_id=source_id, memory_only=False)
    assert same_forget_intent(b4_legacy_input, b5_input)


def test_different_memory_only_is_incompatible_intent() -> None:
    source_id = uuid4()
    b4_legacy_full = {
        "scope": "source",
        "dataset": "main",
        "source_id": str(source_id),
        "memory_only": False,
        "wait": True,
    }
    b5_memory_only = forget_source_run_input(dataset="main", source_id=source_id, memory_only=True)
    assert not same_forget_intent(b4_legacy_full, b5_memory_only)


def test_different_source_is_incompatible_intent() -> None:
    b4_legacy = {
        "scope": "source",
        "dataset": "main",
        "source_id": str(uuid4()),
        "memory_only": False,
        "wait": True,
    }
    b5 = forget_source_run_input(dataset="main", source_id=uuid4(), memory_only=False)
    assert not same_forget_intent(b4_legacy, b5)


def test_malformed_run_input_is_never_considered_the_same_intent() -> None:
    assert not same_forget_intent(None, {"scope": "source"})
    assert not same_forget_intent({"scope": "unknown"}, {"scope": "source"})
    assert forget_semantic_intent_from_run_input(None) is None
    assert forget_semantic_intent_from_run_input({"scope": "not-a-real-scope"}) is None


def test_dataset_intent_ignores_source_id() -> None:
    b4_legacy = {"scope": "dataset", "dataset": "main", "memory_only": False, "wait": True}
    b5 = forget_dataset_run_input(dataset="main", memory_only=False)
    assert same_forget_intent(b4_legacy, b5)


def test_everything_intent_ignores_confirm_and_wait() -> None:
    b4_legacy = {"scope": "everything", "wait": True}
    b5 = forget_everything_run_input()
    assert same_forget_intent(b4_legacy, b5)


# ---------------------------------------------------------------------------
# Storage path safety (unchanged guards, SM-512 SS 25)
# ---------------------------------------------------------------------------


def test_storage_path_rejects_non_file_scheme(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    with pytest.raises(SofiasMemoryError):
        source_storage_path(
            tmp_path,
            dataset_id=dataset_id,
            source_id=source_id,
            storage_uri="http://example.com/file.txt",
        )


def test_storage_path_rejects_traversal_outside_expected_directory(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    escape_target = tmp_path / "escaped.txt"
    escape_target.write_text("outside")
    traversal_path = tmp_path / str(dataset_id) / str(source_id) / ".." / ".." / "escaped.txt"
    traversal_uri = traversal_path.as_uri()
    with pytest.raises(SofiasMemoryError):
        source_storage_path(
            tmp_path, dataset_id=dataset_id, source_id=source_id, storage_uri=traversal_uri
        )


def test_storage_path_missing_file_is_none_not_an_error(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    expected_dir = tmp_path / str(dataset_id) / str(source_id)
    expected_dir.mkdir(parents=True)
    missing_uri = (expected_dir / "missing.txt").as_uri()
    assert (
        source_storage_path(
            tmp_path, dataset_id=dataset_id, source_id=source_id, storage_uri=missing_uri
        )
        is None
    )


def test_storage_path_rejects_directory_target(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    expected_dir = tmp_path / str(dataset_id) / str(source_id)
    expected_dir.mkdir(parents=True)
    inner_dir = expected_dir / "a_directory"
    inner_dir.mkdir()
    directory_uri = inner_dir.as_uri()
    with pytest.raises(SofiasMemoryError):
        source_storage_path(
            tmp_path, dataset_id=dataset_id, source_id=source_id, storage_uri=directory_uri
        )


def test_storage_path_accepts_real_file_in_expected_directory(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    expected_dir = tmp_path / str(dataset_id) / str(source_id)
    expected_dir.mkdir(parents=True)
    real_file = expected_dir / "source.txt"
    real_file.write_text("content")
    file_uri = real_file.as_uri()
    resolved = source_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_uri=file_uri
    )
    assert resolved is not None
    assert resolved.samefile(real_file)


def test_invalid_storage_uri_error_is_bad_request() -> None:
    error = invalid_storage_uri_error()
    assert error.status_code == 400


# ---------------------------------------------------------------------------
# Projection command building (ADR-0008 identities)
# ---------------------------------------------------------------------------


def test_forget_projection_commands_dedupes_and_orders_chunk_next() -> None:
    dataset_id = uuid4()
    document_id = uuid4()
    chunk_a = Chunk(
        id=uuid4(),
        dataset_id=dataset_id,
        document_id=document_id,
        source_id=uuid4(),
        generation=0,
        ordinal=0,
        text="a",
        content_sha256="a" * 64,
        token_count=1,
        embedding=[0.0] * 3072,
        lexical="a",
    )
    chunk_b = Chunk(
        id=uuid4(),
        dataset_id=dataset_id,
        document_id=document_id,
        source_id=uuid4(),
        generation=0,
        ordinal=1,
        text="b",
        content_sha256="b" * 64,
        token_count=1,
        embedding=[0.0] * 3072,
        lexical="b",
    )
    commands = forget_projection_commands(
        dataset_id=dataset_id, chunks=[chunk_a, chunk_b], mentions=[], relations=[], entities=[]
    )
    kinds = [command.aggregate_type for command in commands]
    assert kinds.count("chunk") == 2
    assert kinds.count("chunk_next") == 1


def test_reset_document_placeholder_has_no_forgotten_content() -> None:
    from sofias_memory.infrastructure.postgres.models import Document

    original = Document(
        id=uuid4(),
        dataset_id=uuid4(),
        source_id=uuid4(),
        generation=0,
        title="Original",
        language="en",
        normalized_text="secret content",
        text_sha256="c" * 64,
        token_count=42,
        is_active=False,
    )
    placeholder = reset_document_for_recognify(original)

    assert placeholder.id != original.id
    assert placeholder.normalized_text == ""
    assert placeholder.title == ""
    assert placeholder.language == "und"
    assert placeholder.is_active is True
    assert placeholder.source_id == original.source_id
    assert placeholder.dataset_id == original.dataset_id
