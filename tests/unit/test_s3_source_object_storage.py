"""Unit tests for the ADR-0011 S3 Source object storage adapter (STORAGE-002).

Uses ``botocore.stub.Stubber`` (official botocore test support, no ``moto``)
against a real ``boto3`` client with no network access. Finalize/read/
verify/delete tests are plain sync functions driving the adapter with
``asyncio.run`` -- Stubber's context manager is itself sync, and this keeps
each test's control flow linear. Event-loop/concurrency tests use
``@pytest.mark.asyncio`` where genuine concurrent async behavior must be
observed under one running loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import threading
import time
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from sofias_memory.config import Settings
from sofias_memory.infrastructure.storage.port import (
    InvalidSourceStorageUriError,
    SourceStorageConflictError,
    SourceStorageUnavailableError,
    StorageDeleteStatus,
)
from sofias_memory.infrastructure.storage.s3 import (
    BYTE_SIZE_METADATA_KEY,
    SHA256_METADATA_KEY,
    S3SourceObjectStorage,
    parse_s3_storage_uri,
    s3_object_key,
    s3_object_uri,
)

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

CONTENT = b"stored source bytes"
CONTENT_SHA256 = sha256(CONTENT).hexdigest()
BUCKET = "test-bucket"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
        "storage_backend": "s3",
        "storage_s3_bucket": BUCKET,
        "storage_s3_region": "us-east-1",
        # Pinned explicitly (STORAGE-009 finding): this Stubber-based suite's
        # hand-built S3 keys assume an empty prefix -- must not silently
        # inherit whatever STORAGE_S3_PREFIX the ambient process/OS
        # environment happens to carry.
        "storage_s3_prefix": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


def _client_and_stubber() -> tuple[object, Stubber]:
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="fake",
        aws_secret_access_key="fake",
    )
    return client, Stubber(client)


def _adapter(tmp_path: Path, client: object, **overrides: object) -> S3SourceObjectStorage:
    settings = make_settings(tmp_path, **overrides)
    return S3SourceObjectStorage(settings, client=client)


def _streaming_body(data: bytes) -> StreamingBody:
    return StreamingBody(io.BytesIO(data), len(data))


def _identity_metadata(content_sha256: str, byte_size: int) -> dict[str, str]:
    return {SHA256_METADATA_KEY: content_sha256, BYTE_SIZE_METADATA_KEY: str(byte_size)}


# ---------------------------------------------------------------------------
# URI / key construction and parsing (pure, no I/O)
# ---------------------------------------------------------------------------


def test_s3_object_key_is_deterministic_and_uses_canonical_extension() -> None:
    dataset_id, source_id = uuid4(), uuid4()

    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    assert key == f"v1/sources/{dataset_id}/{source_id}/original.txt"


def test_s3_object_key_includes_normalized_prefix() -> None:
    dataset_id, source_id = uuid4(), uuid4()

    key = s3_object_key(
        prefix="sources", dataset_id=dataset_id, source_id=source_id, storage_extension=".pdf"
    )

    assert key == f"sources/v1/sources/{dataset_id}/{source_id}/original.pdf"


def test_s3_object_uri_is_canonical() -> None:
    assert s3_object_uri(bucket="b", key="v1/sources/a/b/original.txt") == (
        "s3://b/v1/sources/a/b/original.txt"
    )


def test_parse_s3_storage_uri_round_trips_with_construction() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="p", dataset_id=dataset_id, source_id=source_id, storage_extension=".json"
    )
    uri = s3_object_uri(bucket="b", key=key)

    bucket, parsed_key = parse_s3_storage_uri(
        uri, prefix="p", dataset_id=dataset_id, source_id=source_id
    )

    assert bucket == "b"
    assert parsed_key == key


def test_parse_s3_storage_uri_rejects_non_s3_scheme() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    with pytest.raises(InvalidSourceStorageUriError):
        parse_s3_storage_uri("file:///tmp/x", prefix="", dataset_id=dataset_id, source_id=source_id)


def test_parse_s3_storage_uri_rejects_query_string() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    with pytest.raises(InvalidSourceStorageUriError):
        parse_s3_storage_uri(
            f"s3://b/{key}?X-Amz-Signature=fake",
            prefix="",
            dataset_id=dataset_id,
            source_id=source_id,
        )


def test_parse_s3_storage_uri_rejects_fragment() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    with pytest.raises(InvalidSourceStorageUriError):
        parse_s3_storage_uri(
            f"s3://b/{key}#frag", prefix="", dataset_id=dataset_id, source_id=source_id
        )


def test_parse_s3_storage_uri_rejects_traversal_segment() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    with pytest.raises(InvalidSourceStorageUriError):
        parse_s3_storage_uri(
            f"s3://b/v1/sources/{dataset_id}/../{source_id}/original.txt",
            prefix="",
            dataset_id=dataset_id,
            source_id=source_id,
        )


def test_parse_s3_storage_uri_rejects_wrong_source_identity() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    other_source_id = uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=other_source_id, storage_extension=".txt"
    )
    with pytest.raises(InvalidSourceStorageUriError):
        parse_s3_storage_uri(f"s3://b/{key}", prefix="", dataset_id=dataset_id, source_id=source_id)


def test_parse_s3_storage_uri_rejects_nested_filename() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    with pytest.raises(InvalidSourceStorageUriError):
        parse_s3_storage_uri(
            f"s3://b/v1/sources/{dataset_id}/{source_id}/nested/original.txt",
            prefix="",
            dataset_id=dataset_id,
            source_id=source_id,
        )


def test_parse_s3_storage_uri_rejects_non_original_filename() -> None:
    dataset_id, source_id = uuid4(), uuid4()
    with pytest.raises(InvalidSourceStorageUriError):
        parse_s3_storage_uri(
            f"s3://b/v1/sources/{dataset_id}/{source_id}/not-original.txt",
            prefix="",
            dataset_id=dataset_id,
            source_id=source_id,
        )


def test_parse_s3_storage_uri_no_client_filename_participation() -> None:
    # The canonical filename is always "original<ext>" -- never derived from
    # anything client-controlled.
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".pdf"
    )
    assert key.rsplit("/", 1)[-1] == "original.pdf"


# ---------------------------------------------------------------------------
# finalize()
# ---------------------------------------------------------------------------


def test_finalize_absent_target_puts_object(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    stubber.add_response(
        "put_object",
        {},
        expected_params={
            "Bucket": BUCKET,
            "Key": key,
            "Body": CONTENT,
            "Metadata": _identity_metadata(CONTENT_SHA256, len(CONTENT)),
        },
    )
    stubber.add_response(
        "head_object",
        {
            "ContentLength": len(CONTENT),
            "Metadata": _identity_metadata(CONTENT_SHA256, len(CONTENT)),
        },
    )

    with stubber:
        result = asyncio.run(
            adapter.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=".txt",
                original_bytes=CONTENT,
            )
        )

    assert result.storage_uri == f"s3://{BUCKET}/{key}"
    assert result.already_present is False
    stubber.assert_no_pending_responses()


def test_finalize_existing_matching_target_is_idempotent(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()

    stubber.add_response(
        "head_object",
        {
            "ContentLength": len(CONTENT),
            "Metadata": _identity_metadata(CONTENT_SHA256, len(CONTENT)),
        },
    )

    with stubber:
        result = asyncio.run(
            adapter.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=".txt",
                original_bytes=CONTENT,
            )
        )

    assert result.storage_uri.endswith("original.txt")
    assert result.already_present is True
    stubber.assert_no_pending_responses()  # no put_object call at all


def test_finalize_existing_matching_target_is_idempotent_with_title_cased_metadata_keys(
    tmp_path: Path,
) -> None:
    """Regression (STORAGE-009 live-MinIO validation): not every real
    S3-compatible provider lower-cases custom metadata header names the way
    AWS S3 does -- one live target observed under STORAGE-009 echoed back
    ``Sofias-Memory-Sha256``/``Sofias-Memory-Byte-Size`` (title case) on
    ``head_object`` instead of the lower-case keys this adapter writes.
    Before the fix, ``metadata.get(SHA256_METADATA_KEY)`` silently returned
    ``None`` for a perfectly matching object, so an idempotent finalize
    replay was wrongly treated as a mismatch (D11 identity check must not be
    provider-casing-dependent)."""

    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()

    title_cased_metadata = {
        "Sofias-Memory-Sha256": CONTENT_SHA256,
        "Sofias-Memory-Byte-Size": str(len(CONTENT)),
    }
    stubber.add_response(
        "head_object",
        {"ContentLength": len(CONTENT), "Metadata": title_cased_metadata},
    )

    with stubber:
        result = asyncio.run(
            adapter.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=".txt",
                original_bytes=CONTENT,
            )
        )

    assert result.storage_uri.endswith("original.txt")
    assert result.already_present is True
    stubber.assert_no_pending_responses()  # no put_object call at all


def test_finalize_existing_conflicting_target_fails_closed(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()

    stubber.add_response(
        "head_object",
        {"ContentLength": 999, "Metadata": _identity_metadata("0" * 64, 999)},
    )

    with stubber, pytest.raises(SourceStorageConflictError):
        asyncio.run(
            adapter.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=".txt",
                original_bytes=CONTENT,
            )
        )


def test_finalize_ambiguous_put_then_matching_target_observed_is_success(
    tmp_path: Path,
) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()

    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    # PUT itself raises an ambiguous transport error...
    stubber.add_client_error("put_object", service_error_code="RequestTimeout")
    # ...but re-inspection proves it actually landed.
    stubber.add_response(
        "head_object",
        {
            "ContentLength": len(CONTENT),
            "Metadata": _identity_metadata(CONTENT_SHA256, len(CONTENT)),
        },
    )

    with stubber:
        result = asyncio.run(
            adapter.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=".txt",
                original_bytes=CONTENT,
            )
        )

    assert result.storage_uri.endswith("original.txt")
    assert result.already_present is True


def test_finalize_ambiguous_put_then_absent_target_fails_unavailable(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()

    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)
    stubber.add_client_error("put_object", service_error_code="RequestTimeout")
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(
            adapter.finalize(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_extension=".txt",
                original_bytes=CONTENT,
            )
        )


# ---------------------------------------------------------------------------
# verify()
# ---------------------------------------------------------------------------


def test_verify_true_for_matching_content(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_object", {"Body": _streaming_body(CONTENT)})

    with stubber:
        result = asyncio.run(
            adapter.verify(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                content_sha256=CONTENT_SHA256,
            )
        )

    assert result is True


def test_verify_false_for_hash_mismatch(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_object", {"Body": _streaming_body(CONTENT)})

    with stubber:
        result = asyncio.run(
            adapter.verify(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                content_sha256="0" * 64,
            )
        )

    assert result is False


def test_verify_false_for_missing_object(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

    with stubber:
        result = asyncio.run(
            adapter.verify(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                content_sha256=CONTENT_SHA256,
            )
        )

    assert result is False


# ---------------------------------------------------------------------------
# read()
# ---------------------------------------------------------------------------


def test_read_returns_verified_bytes(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response(
        "get_object", {"Body": _streaming_body(CONTENT), "ContentLength": len(CONTENT)}
    )

    with stubber:
        data = asyncio.run(
            adapter.read(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                expected_byte_size=len(CONTENT),
                expected_content_sha256=CONTENT_SHA256,
                max_bytes=1024,
            )
        )

    assert data == CONTENT


def test_read_rejects_hash_mismatch(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response(
        "get_object", {"Body": _streaming_body(CONTENT), "ContentLength": len(CONTENT)}
    )

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(
            adapter.read(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                expected_byte_size=len(CONTENT),
                expected_content_sha256="0" * 64,
                max_bytes=1024,
            )
        )


def test_read_rejects_byte_size_mismatch(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response(
        "get_object", {"Body": _streaming_body(CONTENT), "ContentLength": len(CONTENT)}
    )

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(
            adapter.read(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                expected_byte_size=len(CONTENT) + 1,
                expected_content_sha256=CONTENT_SHA256,
                max_bytes=1024,
            )
        )


def test_read_rejects_content_exceeding_max_bytes_before_any_call(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(
            adapter.read(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                expected_byte_size=len(CONTENT),
                expected_content_sha256=CONTENT_SHA256,
                max_bytes=1,
            )
        )
    stubber.assert_no_pending_responses()  # never even called get_object


def test_read_enforces_max_bytes_while_streaming_not_only_content_length(
    tmp_path: Path,
) -> None:
    # A broken/malicious provider response whose ContentLength lies must
    # still not be trusted past max_bytes while actually streaming bytes.
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    oversized = b"x" * 100

    stubber.add_response("get_object", {"Body": _streaming_body(oversized), "ContentLength": 10})

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(
            adapter.read(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                expected_byte_size=10,
                expected_content_sha256=sha256(oversized[:10]).hexdigest(),
                max_bytes=10,
            )
        )


def test_read_rejects_missing_object(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(
            adapter.read(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                expected_byte_size=len(CONTENT),
                expected_content_sha256=CONTENT_SHA256,
                max_bytes=1024,
            )
        )


# ---------------------------------------------------------------------------
# delete() -- unversioned bucket
# ---------------------------------------------------------------------------


def test_delete_unversioned_existing_object_is_deleted_now(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {})  # no Status = never versioned
    stubber.add_response(
        "head_object", {"ContentLength": len(CONTENT), "Metadata": {}}
    )  # existence check
    stubber.add_response("delete_object", {})
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.DELETED_NOW


def test_delete_unversioned_already_absent(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {})
    stubber.add_client_error("head_object", service_error_code="404", http_status_code=404)

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.ALREADY_ABSENT


def test_delete_unversioned_access_denied_on_precheck_head_is_unresolved(tmp_path: Path) -> None:
    """STORAGE-005 exception-classification audit: ``head_object`` raising a
    *non-404* ``ClientError`` (e.g. ``AccessDenied``) must not escape as a
    raw botocore exception -- it is a recognized operational inability
    (D37/D38) and must become ``UNRESOLVED``, not a step failure and not a
    false ``ALREADY_ABSENT``/``DELETED_NOW``."""

    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {})
    stubber.add_client_error("head_object", service_error_code="AccessDenied", http_status_code=403)

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.UNRESOLVED


def test_delete_unversioned_access_denied_on_postcheck_head_is_unresolved(tmp_path: Path) -> None:
    """Same recognized-condition requirement, at the post-delete
    verification HEAD: the object delete call succeeded, but D15/D38's
    positive-evidence requirement means an inability to verify absence must
    not be reported as ``DELETED_NOW``."""

    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {})
    stubber.add_response("head_object", {"ContentLength": len(CONTENT), "Metadata": {}})
    stubber.add_response("delete_object", {})
    stubber.add_client_error("head_object", service_error_code="AccessDenied", http_status_code=403)

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.UNRESOLVED


def test_delete_none_uri_is_not_requested(tmp_path: Path) -> None:
    client, _stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)

    result = asyncio.run(adapter.delete(dataset_id=uuid4(), source_id=uuid4(), storage_uri=None))

    assert result.status is StorageDeleteStatus.NOT_REQUESTED


# ---------------------------------------------------------------------------
# delete() -- versioned / versioning-suspended bucket (D15)
# ---------------------------------------------------------------------------


def _version_page(key: str, version_ids: list[str], is_last: bool) -> dict[str, object]:
    return {
        "Versions": [{"Key": key, "VersionId": vid} for vid in version_ids],
        "DeleteMarkers": [],
        "IsTruncated": not is_last,
        **({} if is_last else {"NextKeyMarker": key, "NextVersionIdMarker": version_ids[-1]}),
    }


def test_delete_versioned_purges_all_versions_and_markers(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "list_object_versions",
        {
            "Versions": [{"Key": key, "VersionId": "v1"}],
            "DeleteMarkers": [{"Key": key, "VersionId": "m1"}],
            "IsTruncated": False,
        },
    )
    stubber.add_response("delete_objects", {"Deleted": [{"Key": key}], "Errors": []})
    stubber.add_response(
        "list_object_versions",
        {"Versions": [], "DeleteMarkers": [], "IsTruncated": False},
    )

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.DELETED_NOW


def test_delete_versioned_handles_pagination(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "list_object_versions",
        {
            "Versions": [{"Key": key, "VersionId": "v1"}],
            "DeleteMarkers": [],
            "IsTruncated": True,
            "NextKeyMarker": key,
            "NextVersionIdMarker": "v1",
        },
    )
    stubber.add_response(
        "list_object_versions",
        {
            "Versions": [{"Key": key, "VersionId": "v2"}],
            "DeleteMarkers": [],
            "IsTruncated": False,
        },
    )
    stubber.add_response("delete_objects", {"Deleted": [{"Key": key}], "Errors": []})
    stubber.add_response(
        "list_object_versions",
        {"Versions": [], "DeleteMarkers": [], "IsTruncated": False},
    )

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.DELETED_NOW


def test_delete_versioned_ignores_neighboring_prefixed_keys(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    neighboring_key = key + "-neighbor"

    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "list_object_versions",
        {
            # list_object_versions is prefix-based -- a neighboring key
            # sharing the same prefix must never be included in the delete.
            "Versions": [
                {"Key": key, "VersionId": "v1"},
                {"Key": neighboring_key, "VersionId": "n1"},
            ],
            "DeleteMarkers": [],
            "IsTruncated": False,
        },
    )
    stubber.add_response(
        "delete_objects",
        {"Deleted": [{"Key": key}], "Errors": []},
        expected_params={
            "Bucket": BUCKET,
            "Delete": {"Objects": [{"Key": key, "VersionId": "v1"}], "Quiet": True},
        },
    )
    stubber.add_response(
        "list_object_versions",
        {"Versions": [], "DeleteMarkers": [], "IsTruncated": False},
    )

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.DELETED_NOW


def test_delete_versioned_initially_absent_is_already_absent(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "list_object_versions",
        {"Versions": [], "DeleteMarkers": [], "IsTruncated": False},
    )

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.ALREADY_ABSENT


def test_delete_versioning_check_denied_is_unresolved(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_client_error("get_bucket_versioning", service_error_code="AccessDenied")

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.UNRESOLVED


def test_delete_versioned_purge_denied_by_object_lock_is_unresolved(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "list_object_versions",
        {"Versions": [{"Key": key, "VersionId": "v1"}], "DeleteMarkers": [], "IsTruncated": False},
    )
    stubber.add_client_error("delete_objects", service_error_code="AccessDenied")

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.UNRESOLVED


def test_delete_versioned_partial_deletion_errors_is_unresolved(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {"Status": "Enabled"})
    stubber.add_response(
        "list_object_versions",
        {"Versions": [{"Key": key, "VersionId": "v1"}], "DeleteMarkers": [], "IsTruncated": False},
    )
    stubber.add_response(
        "delete_objects",
        {
            "Deleted": [],
            "Errors": [{"Key": key, "VersionId": "v1", "Code": "AccessDenied"}],
        },
    )

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.UNRESOLVED


def test_delete_timeout_is_unresolved(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    stubber.add_response("get_bucket_versioning", {})
    stubber.add_response("head_object", {"ContentLength": 1, "Metadata": {}})
    stubber.add_client_error("delete_object", service_error_code="RequestTimeout")

    with stubber:
        result = asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )

    assert result.status is StorageDeleteStatus.UNRESOLVED


def test_delete_unexpected_programming_defect_is_not_swallowed_as_unresolved(
    tmp_path: Path,
) -> None:
    client, _stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise TypeError("unexpected programming defect")

    # A genuine bug (not a recognized ClientError/BotoCoreError) must
    # propagate, never become StorageDeleteStatus.UNRESOLVED.
    adapter._get_bucket_versioning_sync = _boom  # type: ignore[method-assign]

    with pytest.raises(TypeError):
        asyncio.run(
            adapter.delete(
                dataset_id=dataset_id, source_id=source_id, storage_uri=f"s3://{BUCKET}/{key}"
            )
        )


# ---------------------------------------------------------------------------
# Event-loop safety / concurrency
# ---------------------------------------------------------------------------


def test_s3_operations_run_on_a_worker_thread_not_the_event_loop(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    observed_threads: list[int] = []

    real_get_object = client.get_object  # type: ignore[attr-defined]

    def _tracking_get_object(*args: object, **kwargs: object) -> object:
        observed_threads.append(threading.get_ident())
        return real_get_object(*args, **kwargs)

    client.get_object = _tracking_get_object  # type: ignore[attr-defined]
    stubber.add_response("get_object", {"Body": _streaming_body(CONTENT)})

    async def _run() -> bool:
        main_thread_id = threading.get_ident()
        with stubber:
            result = await adapter.verify(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                content_sha256=CONTENT_SHA256,
            )
        assert observed_threads and observed_threads[0] != main_thread_id
        return result

    assert asyncio.run(_run()) is True


def test_streaming_body_read_and_close_happen_in_worker_thread(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)
    dataset_id, source_id = uuid4(), uuid4()
    key = s3_object_key(
        prefix="", dataset_id=dataset_id, source_id=source_id, storage_extension=".txt"
    )
    body = _streaming_body(CONTENT)
    read_threads: list[int] = []
    close_threads: list[int] = []
    real_read = body.read
    real_close = body.close

    def _tracking_read(*args: object, **kwargs: object) -> bytes:
        read_threads.append(threading.get_ident())
        return real_read(*args, **kwargs)  # type: ignore[no-any-return]

    def _tracking_close(*args: object, **kwargs: object) -> None:
        close_threads.append(threading.get_ident())
        real_close(*args, **kwargs)

    body.read = _tracking_read  # type: ignore[method-assign]
    body.close = _tracking_close  # type: ignore[method-assign]
    stubber.add_response("get_object", {"Body": body, "ContentLength": len(CONTENT)})

    async def _run() -> None:
        main_thread_id = threading.get_ident()
        with stubber:
            data = await adapter.read(
                dataset_id=dataset_id,
                source_id=source_id,
                storage_uri=f"s3://{BUCKET}/{key}",
                expected_byte_size=len(CONTENT),
                expected_content_sha256=CONTENT_SHA256,
                max_bytes=1024,
            )
        assert data == CONTENT
        assert read_threads and all(t != main_thread_id for t in read_threads)
        assert close_threads and all(t != main_thread_id for t in close_threads)

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrent_s3_operations(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, storage_s3_max_concurrency=2)
    client, _stubber = _client_and_stubber()
    adapter = S3SourceObjectStorage(settings, client=client)

    active = 0
    peak_active = 0
    lock = threading.Lock()

    def _slow_head_object(*_args: object, **_kwargs: object) -> None:
        nonlocal active, peak_active
        with lock:
            active += 1
            peak_active = max(peak_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1

    adapter._head_object_sync = _slow_head_object  # type: ignore[method-assign]

    # finalize()'s pre-check calls head_object; the semaphore wraps the
    # whole offloaded call regardless of which S3 operation it performs.
    async def _finalize_once() -> None:
        with contextlib.suppress(Exception):  # only concurrency timing matters here
            await adapter.finalize(
                dataset_id=uuid4(),
                source_id=uuid4(),
                storage_extension=".txt",
                original_bytes=b"x",
            )

    await asyncio.gather(*(_finalize_once() for _ in range(6)))

    assert peak_active <= 2


def test_max_pool_connections_at_least_matches_configured_concurrency(tmp_path: Path) -> None:
    from sofias_memory.infrastructure.storage.s3 import build_s3_client

    settings = make_settings(tmp_path, storage_s3_max_concurrency=25)
    client = build_s3_client(settings)

    pool_config = client._client_config  # type: ignore[attr-defined]
    assert pool_config.max_pool_connections >= 25


def test_adapter_client_close_lifecycle_works(tmp_path: Path) -> None:
    client, _stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)

    adapter.close()  # must not raise


# ---------------------------------------------------------------------------
# probe() -- ADR-0011 D21 startup probe (STORAGE-007)
# ---------------------------------------------------------------------------


def test_probe_key_prefix_is_outside_the_managed_source_namespace() -> None:
    """Pure check (D6/D36): the probe's reserved key root is disjoint from
    the managed ``v1/sources/...`` Source namespace -- never a prefix-wide
    or bucket-wide operation, and never collides with any real Source key."""

    from sofias_memory.infrastructure.storage.s3 import (
        DETERMINISTIC_KEY_ROOT,
        PROBE_KEY_ROOT,
        _probe_key_prefix,
    )

    assert PROBE_KEY_ROOT != DETERMINISTIC_KEY_ROOT
    assert not PROBE_KEY_ROOT.startswith(DETERMINISTIC_KEY_ROOT)
    assert _probe_key_prefix("") == PROBE_KEY_ROOT
    assert _probe_key_prefix("myprefix") == f"myprefix/{PROBE_KEY_ROOT}"


def test_probe_exercises_put_get_delete_under_reserved_prefix(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)

    from sofias_memory.infrastructure.storage.s3 import PROBE_OBJECT_BODY

    stubber.add_response(
        "put_object",
        {},
        expected_params={"Bucket": BUCKET, "Key": ANY, "Body": PROBE_OBJECT_BODY},
    )
    stubber.add_response(
        "get_object",
        {"Body": _streaming_body(PROBE_OBJECT_BODY)},
        expected_params={"Bucket": BUCKET, "Key": ANY},
    )
    stubber.add_response("delete_object", {}, expected_params={"Bucket": BUCKET, "Key": ANY})

    with stubber:
        asyncio.run(adapter.probe())

    stubber.assert_no_pending_responses()


def test_probe_cleans_up_even_when_readback_fails(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)

    stubber.add_response("put_object", {})
    stubber.add_response("get_object", {"Body": _streaming_body(b"WRONG CONTENT")})
    stubber.add_response("delete_object", {})

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(adapter.probe())

    stubber.assert_no_pending_responses()  # delete still attempted (finally-block cleanup)


def test_probe_swallows_delete_failure_after_successful_put_get(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)

    from sofias_memory.infrastructure.storage.s3 import PROBE_OBJECT_BODY

    stubber.add_response("put_object", {})
    stubber.add_response("get_object", {"Body": _streaming_body(PROBE_OBJECT_BODY)})
    stubber.add_client_error("delete_object", service_error_code="AccessDenied")

    with stubber:
        asyncio.run(adapter.probe())  # must not raise -- D21: idempotent cleanup, best-effort


def test_probe_raises_on_put_failure(tmp_path: Path) -> None:
    client, stubber = _client_and_stubber()
    adapter = _adapter(tmp_path, client)

    stubber.add_client_error("put_object", service_error_code="AccessDenied")
    stubber.add_client_error("delete_object", service_error_code="NoSuchKey")

    with stubber, pytest.raises(SourceStorageUnavailableError):
        asyncio.run(adapter.probe())
