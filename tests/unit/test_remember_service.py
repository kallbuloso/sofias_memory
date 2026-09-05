"""Unit tests for Remember's pure/reusable primitives (SM-513).

Since ``services.remember`` no longer owns run lifecycle (that moved to
``pipelines.steps.remember``, ADR-0009 SS O), this file exercises the pure
helpers: mode validation, work identity, B4-legacy intent compatibility, and
durable ingress/final-storage path helpers. Step-level (execute/persist)
behavior lives in ``test_remember_pipeline_steps.py``; route/submission
behavior lives in ``test_remember_routes.py``; durability/concurrency/
filesystem/Neo4j behavior lives in the integration suite.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from sofias_memory.api.errors import SofiasMemoryError
from sofias_memory.pipelines.hashing import canonical_work_payload_hash
from sofias_memory.services.remember import (
    delete_ingress_artifact,
    final_storage_content_matches,
    final_storage_path,
    final_storage_uri,
    ingress_artifact_exists,
    ingress_artifact_path,
    read_ingress_bytes,
    read_ingress_filename,
    remember_file_run_input,
    remember_semantic_intent_from_run_input,
    remember_text_run_input,
    remember_url_run_input,
    same_remember_intent,
    source_name,
    validate_remember_mode,
    write_final_storage_bytes,
    write_ingress_bytes,
)

# ---------------------------------------------------------------------------
# Mode validation (SM-513 SS 3)
# ---------------------------------------------------------------------------


def test_ingest_and_full_modes_are_accepted() -> None:
    validate_remember_mode("ingest")
    validate_remember_mode("full")


def test_unsupported_mode_is_stable_bad_request() -> None:
    with pytest.raises(SofiasMemoryError) as excinfo:
        validate_remember_mode("partial")
    assert excinfo.value.status_code == 400


# ---------------------------------------------------------------------------
# Work identity (SM-513 SS 5): wait/confirm/request-id never participate.
# ---------------------------------------------------------------------------


def test_text_run_input_excludes_wait() -> None:
    work_input = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name="note",
        metadata={"k": "v"},
        session_id="s1",
        mode="ingest",
        force=False,
    )
    assert "wait" not in work_input
    assert work_input["source_kind"] == "text"


def test_file_run_input_excludes_wait() -> None:
    work_input = remember_file_run_input(
        dataset="main",
        content_sha256="a" * 64,
        filename="doc.txt",
        metadata={},
        session_id=None,
        mode="full",
        force=True,
    )
    assert "wait" not in work_input
    assert work_input["source_kind"] == "file"
    assert work_input["mode"] == "full"
    assert work_input["force"] is True


def test_url_run_input_excludes_wait_and_content_hash() -> None:
    work_input = remember_url_run_input(
        dataset="main",
        url="https://example.com/a",
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    assert "wait" not in work_input
    assert "content_sha256" not in work_input
    assert work_input["source_kind"] == "url"
    assert work_input["url"] == "https://example.com/a"


def test_mode_changes_the_work_identity() -> None:
    ingest = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    full = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="full",
        force=False,
    )
    assert ingest != full


def test_force_changes_the_work_identity() -> None:
    base = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    forced = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=True,
    )
    assert base != forced


def test_session_id_changes_the_work_identity_and_payload_hash() -> None:
    """SM-605 SS 9: Session is part of Remember's semantic identity -- a
    request differing only in session_id must never resolve to the same
    idempotent work, and this must hold at both layers: the raw work_input
    dict (what `same_remember_intent`'s legacy fallback also inspects) and
    the actual `payload_hash` the submission layer compares first."""

    session_a = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id="session-a",
        mode="ingest",
        force=False,
    )
    session_b = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id="session-b",
        mode="ingest",
        force=False,
    )
    assert session_a != session_b
    assert canonical_work_payload_hash(session_a) != canonical_work_payload_hash(session_b)
    assert not same_remember_intent(session_a, session_b)


# ---------------------------------------------------------------------------
# B4 -> B5 semantic intent compatibility (SM-513 SS 6)
# ---------------------------------------------------------------------------


def test_legacy_text_without_source_kind_is_compatible_with_b5() -> None:
    legacy_b4_text = {
        "dataset": "main",
        "content_sha256": "a" * 64,
        "name": "note",
        "metadata": {},
        "session_id": None,
        "mode": "ingest",
        "force": False,
    }
    b5_text = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name="note",
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    assert same_remember_intent(legacy_b4_text, b5_text)


def test_legacy_file_with_wait_is_compatible_with_b5_wait_excluded() -> None:
    legacy_b4_file = {
        "dataset": "main",
        "content_sha256": "b" * 64,
        "filename": "doc.pdf",
        "metadata": {},
        "session_id": None,
        "mode": "ingest",
        "wait": True,
        "force": False,
        "source_kind": "file",
        "mime_type": "application/pdf",
    }
    b5_file = remember_file_run_input(
        dataset="main",
        content_sha256="b" * 64,
        filename="doc.pdf",
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    assert same_remember_intent(legacy_b4_file, b5_file)


def test_legacy_url_with_wait_is_compatible_with_b5_wait_excluded() -> None:
    legacy_b4_url = {
        "dataset": "main",
        "url": "https://example.com/a",
        "metadata": {},
        "session_id": None,
        "mode": "full",
        "wait": True,
        "force": False,
        "source_kind": "url",
    }
    b5_url = remember_url_run_input(
        dataset="main",
        url="https://example.com/a",
        metadata={},
        session_id=None,
        mode="full",
        force=False,
    )
    assert same_remember_intent(legacy_b4_url, b5_url)


def test_different_content_hash_is_incompatible_intent() -> None:
    legacy = {
        "dataset": "main",
        "content_sha256": "a" * 64,
        "filename": "doc.pdf",
        "metadata": {},
        "session_id": None,
        "mode": "ingest",
        "wait": True,
        "force": False,
    }
    b5 = remember_file_run_input(
        dataset="main",
        content_sha256="c" * 64,
        filename="doc.pdf",
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    assert not same_remember_intent(legacy, b5)


def test_different_mode_is_incompatible_intent() -> None:
    legacy = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    other = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="full",
        force=False,
    )
    assert not same_remember_intent(legacy, other)


def test_different_force_is_incompatible_intent() -> None:
    legacy = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    other = remember_text_run_input(
        dataset="main",
        content_sha256="a" * 64,
        name=None,
        metadata={},
        session_id=None,
        mode="ingest",
        force=True,
    )
    assert not same_remember_intent(legacy, other)


def test_malformed_run_input_is_never_considered_the_same_intent() -> None:
    assert not same_remember_intent(None, {"dataset": "main"})
    assert not same_remember_intent({"dataset": "main", "mode": "ingest"}, {"dataset": "main"})
    assert remember_semantic_intent_from_run_input(None) is None
    assert remember_semantic_intent_from_run_input({"dataset": "main"}) is None


def test_url_intent_ignores_name() -> None:
    left = remember_url_run_input(
        dataset="main",
        url="https://example.com/a",
        metadata={},
        session_id=None,
        mode="ingest",
        force=False,
    )
    intent = remember_semantic_intent_from_run_input(left)
    assert intent is not None
    assert intent.name is None


# ---------------------------------------------------------------------------
# Durable ingress staging (SM-513 SS 9)
# ---------------------------------------------------------------------------


def test_ingress_round_trips_bytes_and_filename(tmp_path: Path) -> None:
    run_id = uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"hello", filename="a.txt")
    assert ingress_artifact_exists(tmp_path, run_id=run_id)
    assert read_ingress_bytes(tmp_path, run_id=run_id) == b"hello"
    assert read_ingress_filename(tmp_path, run_id=run_id) == "a.txt"


def test_ingress_without_filename_has_no_filename_metadata(tmp_path: Path) -> None:
    run_id = uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"hello")
    assert read_ingress_filename(tmp_path, run_id=run_id) is None


def test_ingress_missing_artifact_reports_absent(tmp_path: Path) -> None:
    run_id = uuid4()
    assert not ingress_artifact_exists(tmp_path, run_id=run_id)


def test_delete_ingress_artifact_is_idempotent_and_never_raises(tmp_path: Path) -> None:
    run_id = uuid4()
    delete_ingress_artifact(tmp_path, run_id=run_id)  # never staged -- must not raise
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"hi")
    delete_ingress_artifact(tmp_path, run_id=run_id)
    assert not ingress_artifact_exists(tmp_path, run_id=run_id)
    delete_ingress_artifact(tmp_path, run_id=run_id)  # already gone -- still must not raise


def test_ingress_write_replaces_prior_artifact(tmp_path: Path) -> None:
    run_id = uuid4()
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"first")
    write_ingress_bytes(tmp_path, run_id=run_id, raw_bytes=b"second")
    assert read_ingress_bytes(tmp_path, run_id=run_id) == b"second"


def test_two_run_ids_never_collide(tmp_path: Path) -> None:
    first, second = uuid4(), uuid4()
    write_ingress_bytes(tmp_path, run_id=first, raw_bytes=b"one")
    write_ingress_bytes(tmp_path, run_id=second, raw_bytes=b"two")
    assert read_ingress_bytes(tmp_path, run_id=first) == b"one"
    assert read_ingress_bytes(tmp_path, run_id=second) == b"two"
    assert ingress_artifact_path(tmp_path, run_id=first) != ingress_artifact_path(
        tmp_path, run_id=second
    )


# ---------------------------------------------------------------------------
# Final storage helpers (SM-513 SS 16/17)
# ---------------------------------------------------------------------------


def test_write_final_storage_bytes_round_trips(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    uri = write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=b"content",
    )
    assert uri.startswith("file://")
    path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert path.read_bytes() == b"content"


def test_final_storage_content_matches_true_for_correct_hash(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=b"content",
    )
    from hashlib import sha256

    path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert final_storage_content_matches(path, content_sha256=sha256(b"content").hexdigest())


def test_final_storage_content_matches_false_for_wrong_hash(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=b"content",
    )
    path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert not final_storage_content_matches(path, content_sha256="0" * 64)


def test_final_storage_content_matches_false_for_missing_file(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    path = final_storage_path(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert not final_storage_content_matches(path, content_sha256="0" * 64)


def test_final_storage_uri_is_pure_and_matches_write(tmp_path: Path) -> None:
    dataset_id, source_id = uuid4(), uuid4()
    written_uri = write_final_storage_bytes(
        tmp_path,
        dataset_id=dataset_id,
        source_id=source_id,
        storage_extension=".txt",
        original_bytes=b"content",
    )
    computed_uri = final_storage_uri(
        tmp_path, dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    assert written_uri == computed_uri


# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------


def test_source_name_falls_back_to_content_hash_prefix() -> None:
    assert source_name(name=None, content_sha256="a" * 64) == "text-aaaaaaaaaaaa"
    assert source_name(name="explicit", content_sha256="a" * 64) == "explicit"
