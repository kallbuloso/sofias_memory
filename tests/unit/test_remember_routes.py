"""Route-level contract for POST /api/v1/remember(/file|/url) after the B5
migration (SM-513).

Mirrors ``test_forget_routes.py``: the submission service, waiter and
PostgreSQL reads are substituted so the HTTP contract can be asserted
precisely; durable behavior is proven against real PostgreSQL/Neo4j/
filesystem in the integration suite.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType, SessionStatus
from sofias_memory.infrastructure.postgres.models import Session
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.services.pipeline_submission import (
    SubmissionOutcome,
    worker_disabled_error,
)
from sofias_memory.services.pipeline_waiter import WaitOutcome
from sofias_memory.services.remember import REMEMBER_RESULT_METRIC_KEY
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
DATABASE_URL = "postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory"
NEO4J_PASSWORD = "fake-neo4j-password"
LLM_API_KEY = "sk-fake-test-key"

DATASET_ID = UUID("77777777-7777-7777-7777-777777777771")
SOURCE_ID = UUID("88888888-8888-8888-8888-888888888881")
DOCUMENT_ID = UUID("66666666-6666-6666-6666-666666666661")
RUN_ID = UUID("99999999-9999-9999-9999-999999999991")

REMEMBER_MODULE = "sofias_memory.api.routes.remember"


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": EXPECTED_API_KEY,
        "database_url": DATABASE_URL,
        "neo4j_password": NEO4J_PASSWORD,
        "llm_api_key": LLM_API_KEY,
        "app_env": "test",
        "data_directory": tmp_path,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


@dataclass
class FakeRun:
    id: UUID = RUN_ID
    metrics: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None


@dataclass
class Recorder:
    submits: list[dict[str, Any]] = field(default_factory=list)
    waits: list[UUID] = field(default_factory=list)
    outcome: SubmissionOutcome = field(
        default_factory=lambda: SubmissionOutcome(
            run_id=RUN_ID,
            pipeline_type=PipelineType.REMEMBER,
            dataset_id=DATASET_ID,
            source_id=None,
            status=PipelineRunStatus.QUEUED,
            created=True,
        )
    )
    wait_status: PipelineRunStatus = PipelineRunStatus.SUCCEEDED
    wait_timed_out: bool = False
    submit_error: Exception | None = None
    run: FakeRun = field(default_factory=FakeRun)
    sessions: dict[str, Session] = field(default_factory=dict)


def install_fakes(monkeypatch: pytest.MonkeyPatch, recorder: Recorder) -> None:
    class FakeSubmissionService:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def submit(
            self,
            *,
            pipeline_type: PipelineType,
            work_input: Mapping[str, Any],
            idempotency_key: str | None,
            prepare: Any,
            run_id: UUID | None = None,
            legacy_intent_equivalent: Any = None,
        ) -> SubmissionOutcome:
            del legacy_intent_equivalent
            if recorder.submit_error is not None:
                raise recorder.submit_error
            targets = await prepare(FakeSubmissionUnitOfWork(recorder.sessions))
            recorder.submits.append(
                {
                    "pipeline_type": pipeline_type,
                    "work_input": dict(work_input),
                    "idempotency_key": idempotency_key,
                    "targets": targets,
                    "run_id": run_id,
                }
            )
            return recorder.outcome

    class FakeWaiter:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        async def wait_for_terminal(self, run_id: UUID, *, timeout_seconds: float) -> WaitOutcome:
            del timeout_seconds
            recorder.waits.append(run_id)
            return WaitOutcome(
                run_id=run_id, status=recorder.wait_status, timed_out=recorder.wait_timed_out
            )

    class FakeUnitOfWork:
        def __init__(self, session_factory: Any) -> None:
            del session_factory

        async def __aenter__(self) -> FakeUnitOfWork:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        @property
        def pipeline_runs(self) -> Any:
            class _Runs:
                async def get_by_id(self, run_id: UUID) -> FakeRun | None:
                    return recorder.run if run_id == recorder.run.id else None

            return _Runs()

    monkeypatch.setattr(f"{REMEMBER_MODULE}.PipelineSubmissionService", FakeSubmissionService)
    monkeypatch.setattr(f"{REMEMBER_MODULE}.PipelineRunWaiter", FakeWaiter)
    monkeypatch.setattr(f"{REMEMBER_MODULE}.PostgresUnitOfWork", FakeUnitOfWork)


@dataclass
class FakeDataset:
    id: UUID = DATASET_ID
    status: Any = DatasetStatus.ACTIVE


class FakeSubmissionUnitOfWork:
    def __init__(self, sessions_store: dict[str, Session] | None = None) -> None:
        self._sessions_store = sessions_store if sessions_store is not None else {}

    @property
    def datasets(self) -> Any:
        class _Datasets:
            async def get_by_slug(self, slug: str) -> FakeDataset | None:
                if slug == "missing":
                    return None
                return FakeDataset()

            async def get_or_create_by_slug(self, candidate: Any) -> FakeDataset:
                del candidate
                return FakeDataset()

        return _Datasets()

    @property
    def sessions(self) -> Any:
        store = self._sessions_store

        class _Sessions:
            async def get_or_create_by_key(self, candidate: Session) -> Session:
                existing = store.get(candidate.key)
                if existing is not None:
                    return existing
                store[candidate.key] = candidate
                return candidate

            async def get_by_id_for_update(self, session_id: UUID) -> Session | None:
                return next((s for s in store.values() if s.id == session_id), None)

        return _Sessions()


def make_app(tmp_path: Path, **settings_overrides: object) -> Any:
    return create_app(
        make_settings(tmp_path, **settings_overrides),
        enable_postgres_readiness=False,
        enable_neo4j=False,
    )


def build_client(app: Any) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        headers={API_KEY_HEADER: EXPECTED_API_KEY},
    )


# ---------------------------------------------------------------------------
# TEXT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_text_wait_false_returns_202(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember", json={"dataset": "main", "content": "hello", "wait": False}
        )
    assert response.status_code == 202
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert len(recorder.submits) == 1
    submit = recorder.submits[0]
    assert submit["work_input"]["source_kind"] == "text"
    assert "wait" not in submit["work_input"]
    assert submit["run_id"] is not None


@pytest.mark.asyncio
async def test_remember_text_ingress_staged_before_submission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sofias_memory.services.remember import ingress_artifact_exists

    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        await client.post(
            "/api/v1/remember", json={"dataset": "main", "content": "hello", "wait": False}
        )
    run_id = recorder.submits[0]["run_id"]
    assert ingress_artifact_exists(tmp_path, run_id=run_id)


@pytest.mark.asyncio
async def test_remember_text_succeeded_reconstructs_result_from_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    recorder.outcome = SubmissionOutcome(
        run_id=RUN_ID,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        status=PipelineRunStatus.SUCCEEDED,
        created=False,
    )
    recorder.run = FakeRun(
        id=RUN_ID,
        metrics={
            REMEMBER_RESULT_METRIC_KEY: {
                "dataset_id": str(DATASET_ID),
                "source_id": str(SOURCE_ID),
                "document_id": str(DOCUMENT_ID),
                "content_hash": "a" * 64,
                "chunks": 0,
                "entities": 0,
                "relations": 0,
                "deduplicated": False,
            }
        },
    )
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={"dataset": "main", "content": "hello", "wait": True},
            headers={"Idempotency-Key": "k1"},
        )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "succeeded"
    assert data["dataset_id"] == str(DATASET_ID)
    assert data["source_id"] == str(SOURCE_ID)
    assert data["deduplicated"] is False


@pytest.mark.asyncio
async def test_remember_text_existing_replay_cleans_up_candidate_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sofias_memory.services.remember import ingress_artifact_exists

    recorder = Recorder()
    recorder.outcome = SubmissionOutcome(
        run_id=RUN_ID,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        status=PipelineRunStatus.SUCCEEDED,
        created=False,
    )
    recorder.run = FakeRun(
        id=RUN_ID,
        metrics={
            REMEMBER_RESULT_METRIC_KEY: {
                "dataset_id": str(DATASET_ID),
                "source_id": str(SOURCE_ID),
                "document_id": str(DOCUMENT_ID),
                "content_hash": "a" * 64,
                "chunks": 0,
                "entities": 0,
                "relations": 0,
                "deduplicated": True,
            }
        },
    )
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        await client.post(
            "/api/v1/remember",
            json={"dataset": "main", "content": "hello", "wait": True},
            headers={"Idempotency-Key": "k1"},
        )
    run_id = recorder.submits[0]["run_id"]
    assert not ingress_artifact_exists(tmp_path, run_id=run_id)


@pytest.mark.asyncio
async def test_remember_text_invalid_mode_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={"dataset": "main", "content": "hello", "mode": "partial"},
        )
    assert response.status_code == 400
    assert not recorder.submits


@pytest.mark.asyncio
async def test_remember_text_worker_disabled_leaves_no_ingress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    recorder = Recorder()
    recorder.submit_error = worker_disabled_error()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember", json={"dataset": "main", "content": "hello", "wait": False}
        )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == ErrorCode.WORKER_DISABLED.value
    ingress_root = tmp_path / "_ingress"
    assert not ingress_root.exists() or not any(ingress_root.iterdir())


@pytest.mark.asyncio
async def test_remember_text_failed_run_is_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    recorder.outcome = SubmissionOutcome(
        run_id=RUN_ID,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=DATASET_ID,
        source_id=None,
        status=PipelineRunStatus.FAILED,
        created=False,
    )
    recorder.run = FakeRun(id=RUN_ID, error_code="REMEMBER_CONTENT_REJECTED")
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={"dataset": "main", "content": "hello", "wait": True},
            headers={"Idempotency-Key": "k1"},
        )
    assert response.status_code == 500
    assert response.json()["error"]["details"]["step_error_code"] == "REMEMBER_CONTENT_REJECTED"


# ---------------------------------------------------------------------------
# FILE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_file_unsupported_extension_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember/file",
            data={"dataset": "main", "wait": "false"},
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        )
    assert response.status_code == 400
    assert not recorder.submits


@pytest.mark.asyncio
async def test_remember_file_wait_false_stages_ingress_with_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sofias_memory.services.remember import read_ingress_filename

    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember/file",
            data={"dataset": "main", "wait": "false"},
            files={"file": ("note.txt", b"hello world", "text/plain")},
        )
    assert response.status_code == 202
    run_id = recorder.submits[0]["run_id"]
    assert read_ingress_filename(tmp_path, run_id=run_id) == "note.txt"
    assert recorder.submits[0]["work_input"]["filename"] == "note.txt"
    assert recorder.submits[0]["work_input"]["source_kind"] == "file"


# ---------------------------------------------------------------------------
# URL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_url_invalid_scheme_is_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember/url",
            json={"dataset": "main", "url": "http://example.com/a", "wait": False},
        )
    assert response.status_code == 400
    assert not recorder.submits


@pytest.mark.asyncio
async def test_remember_url_wait_false_never_fetches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SM-513 SS 13: the route must never perform network I/O -- proven here
    by never staging any ingress bytes for the URL kind."""

    from sofias_memory.services.remember import ingress_artifact_exists

    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember/url",
            json={"dataset": "main", "url": "https://example.com/a", "wait": False},
        )
    assert response.status_code == 202
    run_id = recorder.submits[0]["run_id"]
    assert not ingress_artifact_exists(tmp_path, run_id=run_id)
    assert recorder.submits[0]["work_input"]["source_kind"] == "url"
    assert "content_sha256" not in recorder.submits[0]["work_input"]


# ---------------------------------------------------------------------------
# SESSION (SM-605)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remember_text_without_session_id_has_null_session_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember", json={"dataset": "main", "content": "hello", "wait": False}
        )
    assert response.status_code == 202
    assert response.json()["data"]["session_uuid"] is None
    assert recorder.submits[0]["targets"].session_id is None
    assert recorder.sessions == {}


@pytest.mark.asyncio
async def test_remember_text_lazily_creates_session_and_targets_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={
                "dataset": "main",
                "content": "hello",
                "wait": False,
                "session_id": "  conversation-1  ",
            },
        )
    assert response.status_code == 202
    assert len(recorder.sessions) == 1
    created = recorder.sessions["conversation-1"]
    assert recorder.submits[0]["targets"].session_id == created.id
    assert recorder.submits[0]["work_input"]["session_id"] == "conversation-1"


@pytest.mark.asyncio
async def test_remember_text_reuses_existing_active_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    existing = Session(id=uuid4(), key="conversation-1", status=SessionStatus.ACTIVE)
    recorder.sessions["conversation-1"] = existing
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={
                "dataset": "main",
                "content": "hello",
                "wait": False,
                "session_id": "conversation-1",
            },
        )
    assert response.status_code == 202
    assert len(recorder.sessions) == 1
    assert recorder.submits[0]["targets"].session_id == existing.id


@pytest.mark.asyncio
async def test_remember_text_archived_session_rejects_with_zero_run_and_response_carries_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    archived = Session(id=uuid4(), key="conversation-1", status=SessionStatus.ARCHIVED)
    recorder.sessions["conversation-1"] = archived
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={
                "dataset": "main",
                "content": "hello",
                "wait": False,
                "session_id": "conversation-1",
            },
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "SESSION_ARCHIVED"
    assert not recorder.submits


@pytest.mark.asyncio
async def test_remember_text_invalid_dataset_with_new_session_creates_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SM-605 SS 14: Dataset resolution runs before Session resolution inside
    the preparation hook -- an invalid Dataset must never leave a lazily
    materialized Session behind."""

    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={
                "dataset": "missing",
                "content": "hello",
                "wait": False,
                "session_id": "brand-new-session",
            },
        )
    assert response.status_code == 404
    assert recorder.sessions == {}
    assert not recorder.submits


@pytest.mark.asyncio
async def test_remember_text_session_uuid_surfaces_in_succeeded_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RememberTextResult.session_uuid is read from the SubmissionOutcome
    (authoritative), never re-derived from persisted metrics."""

    recorder = Recorder()
    session_uuid = uuid4()
    recorder.outcome = SubmissionOutcome(
        run_id=RUN_ID,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=DATASET_ID,
        source_id=SOURCE_ID,
        status=PipelineRunStatus.SUCCEEDED,
        created=True,
        session_id=session_uuid,
    )
    recorder.run = FakeRun(
        id=RUN_ID,
        metrics={
            REMEMBER_RESULT_METRIC_KEY: {
                "dataset_id": str(DATASET_ID),
                "source_id": str(SOURCE_ID),
                "document_id": str(DOCUMENT_ID),
                "content_hash": "a" * 64,
                "chunks": 0,
                "entities": 0,
                "relations": 0,
                "deduplicated": False,
            }
        },
    )
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={
                "dataset": "main",
                "content": "hello",
                "wait": True,
                "session_id": "conversation-1",
            },
        )
    assert response.status_code == 200
    assert response.json()["data"]["session_uuid"] == str(session_uuid)


@pytest.mark.asyncio
async def test_remember_text_queued_response_surfaces_session_uuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    session_uuid = uuid4()
    recorder.outcome = SubmissionOutcome(
        run_id=RUN_ID,
        pipeline_type=PipelineType.REMEMBER,
        dataset_id=DATASET_ID,
        source_id=None,
        status=PipelineRunStatus.QUEUED,
        created=True,
        session_id=session_uuid,
    )
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={
                "dataset": "main",
                "content": "hello",
                "wait": False,
                "session_id": "conversation-1",
            },
        )
    assert response.status_code == 202
    assert response.json()["data"]["session_uuid"] == str(session_uuid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_id", "expected_status"),
    [
        ("", 422),
        ("   ", 422),
        ("x" * 256, 422),
    ],
)
async def test_remember_text_session_id_normalization_rejects_invalid_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_id: str, expected_status: int
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={"dataset": "main", "content": "hello", "wait": False, "session_id": session_id},
        )
    assert response.status_code == expected_status
    assert not recorder.submits


@pytest.mark.asyncio
async def test_remember_text_session_id_255_chars_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    session_id = "x" * 255
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={"dataset": "main", "content": "hello", "wait": False, "session_id": session_id},
        )
    assert response.status_code == 202
    assert recorder.submits[0]["work_input"]["session_id"] == session_id


@pytest.mark.asyncio
async def test_remember_text_session_id_preserves_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember",
            json={
                "dataset": "main",
                "content": "hello",
                "wait": False,
                "session_id": "Conversation-MixedCase-1",
            },
        )
    assert response.status_code == 202
    assert recorder.submits[0]["work_input"]["session_id"] == "Conversation-MixedCase-1"


@pytest.mark.asyncio
async def test_remember_url_session_id_uses_shared_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember/url",
            json={
                "dataset": "main",
                "url": "https://example.com/a",
                "wait": False,
                "session_id": "  conversation-1  ",
            },
        )
        blank = await client.post(
            "/api/v1/remember/url",
            json={
                "dataset": "main",
                "url": "https://example.com/b",
                "wait": False,
                "session_id": "   ",
            },
        )
    assert response.status_code == 202
    assert recorder.submits[0]["work_input"]["session_id"] == "conversation-1"
    assert blank.status_code == 422


@pytest.mark.asyncio
async def test_remember_file_session_id_uses_shared_normalization_not_bare_strip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SM-605 SS 7: File must reject a blank/too-long session_id exactly like
    Text/URL, proving it no longer uses a bare ``.strip()``."""

    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        trimmed = await client.post(
            "/api/v1/remember/file",
            data={"dataset": "main", "wait": "false", "session_id": "  conversation-1  "},
            files={"file": ("note.txt", b"hello world", "text/plain")},
        )
        blank = await client.post(
            "/api/v1/remember/file",
            data={"dataset": "main", "wait": "false", "session_id": "   "},
            files={"file": ("note2.txt", b"hello world", "text/plain")},
        )
        too_long = await client.post(
            "/api/v1/remember/file",
            data={"dataset": "main", "wait": "false", "session_id": "x" * 256},
            files={"file": ("note3.txt", b"hello world", "text/plain")},
        )
    assert trimmed.status_code == 202
    assert recorder.submits[0]["work_input"]["session_id"] == "conversation-1"
    assert blank.status_code == 400
    assert too_long.status_code == 400


@pytest.mark.asyncio
async def test_remember_file_session_id_normalized_before_reading_upload_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SM-605 SS 7: an invalid session_id must reject before any upload
    bytes are read/staged -- proven by asserting zero ingress artifacts are
    ever written for the rejected request."""

    recorder = Recorder()
    install_fakes(monkeypatch, recorder)
    app = make_app(tmp_path)
    async with build_client(app) as client:
        response = await client.post(
            "/api/v1/remember/file",
            data={"dataset": "main", "wait": "false", "session_id": "   "},
            files={"file": ("note.txt", b"hello world", "text/plain")},
        )
    assert response.status_code == 400
    ingress_root = tmp_path / "_ingress"
    assert not ingress_root.exists() or not any(ingress_root.iterdir())


# ---------------------------------------------------------------------------
# Structural / registry guards
# ---------------------------------------------------------------------------


def test_remember_route_module_does_not_import_legacy_lifecycle() -> None:
    import sofias_memory.api.routes.remember as module

    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        content = handle.read()
    assert "RememberService" not in content
    assert "_create_running_run" not in content
    assert "_mark_run_succeeded" not in content
    assert "_mark_run_failed" not in content


def test_remember_service_module_no_longer_owns_run_lifecycle() -> None:
    import sofias_memory.services.remember as module

    source = module.__file__
    assert source is not None
    with open(source, encoding="utf-8") as handle:
        content = handle.read()
    assert "class RememberService" not in content
    assert "PipelineRunStatus.RUNNING" not in content


def test_default_registry_contains_remember() -> None:
    from sofias_memory.pipelines.registry import build_default_pipeline_registry

    registry = build_default_pipeline_registry()
    assert registry.get(PipelineType.REMEMBER) is not None
    assert len(registry) == 5
