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
from uuid import UUID

import httpx
import pytest

from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.domain import DatasetStatus, PipelineRunStatus, PipelineType
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
            targets = await prepare(FakeSubmissionUnitOfWork())
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
