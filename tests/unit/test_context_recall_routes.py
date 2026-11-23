"""Route-level proof for SM-CTX-006 ``POST /api/v1/context/recall``: auth,
envelope, validation codes, partial-contract gates and degradation. The real
``ContextualRecallService`` runs over a fake unit of work and a counting fake
embedding provider -- no PostgreSQL, provider, LLM or Neo4j is exercised."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

import httpx
import pytest

from sofias_memory.api.errors import DependencyUnavailableError
from sofias_memory.api.middleware import API_KEY_HEADER
from sofias_memory.config import Settings
from sofias_memory.services import contextual_recall as module
from sofias_memory.services.contextual_recall import ContextualRecallService
from tests.unit._app_factory import create_app

EXPECTED_API_KEY = "sf-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HEADERS = {API_KEY_HEADER: EXPECTED_API_KEY}
PATH = "/api/v1/context/recall"
DIM = 3072


def make_settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        api_key=EXPECTED_API_KEY,
        database_url="postgresql+asyncpg://sofias_memory:fake@postgres:5432/sofias_memory",
        neo4j_password="fake-neo4j-password",
        llm_api_key="sk-fake-test-key",
        cognitive_idempotency_hmac_key="test-cognitive-idempotency-hmac-key-0123456789abcdef",
        app_env="test",
    )


class CountingEmbeddingClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail:
            raise TimeoutError("provider timeout")
        return [[0.2] * DIM for _ in texts]


class EmptyUow:
    """Every source empty; counts how often a unit of work is opened."""

    opened = 0

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

        async def empty(**kwargs: Any) -> list[Any]:
            del kwargs
            return []

        self.context_checkpoints = type("C", (), {"list_active": staticmethod(empty)})()
        self.memory_items = type(
            "M",
            (),
            {"typed_recall": staticmethod(empty), "list_current_for_scope": staticmethod(empty)},
        )()
        self.episodes = type(
            "E",
            (),
            {
                "contextual_semantic_search": staticmethod(empty),
                "list_recent_contextual": staticmethod(empty),
            },
        )()

    async def __aenter__(self) -> EmptyUow:
        EmptyUow.opened += 1
        if self.fail:
            raise DependencyUnavailableError("PostgreSQL is unavailable.")
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def build_app(
    monkeypatch: pytest.MonkeyPatch, *, provider_fails: bool = False, db_fails: bool = False
) -> tuple[Any, CountingEmbeddingClient]:
    client = CountingEmbeddingClient(fail=provider_fails)
    EmptyUow.opened = 0

    async def no_support(uow: Any, modes: Any) -> dict[Any, None]:
        return {owner: None for owner in modes}

    monkeypatch.setattr(module, "hydrate_episode_support", no_support)

    def factory(settings: Settings, **kwargs: Any) -> ContextualRecallService:
        del kwargs
        return ContextualRecallService(
            settings,
            embedding_client=client,
            unit_of_work_factory=lambda: EmptyUow(fail=db_fails),  # type: ignore[arg-type,return-value]
        )

    monkeypatch.setattr("sofias_memory.api.routes.context_recall.ContextualRecallService", factory)
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)
    return app, client


async def post(app: Any, body: dict[str, Any], headers: dict[str, str] | None = None) -> Any:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.post(PATH, json=body, headers=HEADERS if headers is None else headers)


def valid(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"scope": "project:x", "intent": "resume", "query": "Onde paramos?"}
    body.update(overrides)
    return body


def error(response: httpx.Response) -> dict[str, Any]:
    return cast(dict[str, Any], response.json()["error"])


@pytest.mark.asyncio
async def test_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = build_app(monkeypatch)
    response = await post(app, valid(), headers={})
    assert response.status_code == 401
    assert error(response)["code"] == "MISSING_API_KEY"
    assert client.calls == 0


@pytest.mark.asyncio
async def test_resume_returns_bundle_in_success_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    app, client = build_app(monkeypatch)
    response = await post(app, valid())
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "meta"}
    data = body["data"]
    assert set(data) == {
        "scope",
        "context_key",
        "intent",
        "continuity_available",
        "current_checkpoint",
        "current_memories",
        "episodes",
        "historical_checkpoints",
        "knowledge_references",
        "evidence",
        "warnings",
        "degraded_sources",
    }
    assert data["context_key"] == "main"  # resolved explicitly
    assert data["continuity_available"] is False
    assert data["warnings"] == ["NO_CURRENT_CHECKPOINT"]
    assert data["degraded_sources"] == []
    assert "answer" not in data
    assert client.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["__missing__", None, "   "])
async def test_missing_null_or_blank_query_is_invalid_request_without_provider_call(
    monkeypatch: pytest.MonkeyPatch, query: object
) -> None:
    app, client = build_app(monkeypatch)
    body = valid()
    if query == "__missing__":
        del body["query"]
    else:
        body["query"] = query
    response = await post(app, body)
    assert response.status_code == 422
    assert error(response)["code"] == "INVALID_REQUEST"
    assert client.calls == 0 and EmptyUow.opened == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"scope": "global"}, "INVALID_SCOPE"),
        ({"scope": "projeto"}, "INVALID_SCOPE"),
        ({"context_key": "Feature A"}, "INVALID_CONTEXT_KEY"),
        ({"intent": "answer"}, "INVALID_REQUEST"),
        ({"include_session_context": True}, "INVALID_REQUEST"),
        ({"limits": {"memories": 0}}, "INVALID_REQUEST"),
    ],
)
async def test_validation_codes(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], code: str
) -> None:
    app, client = build_app(monkeypatch)
    response = await post(app, valid(**overrides))
    assert response.status_code == 422
    assert error(response)["code"] == code
    assert client.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"intent": "explain"}, "intent"),
        ({"intent": "history"}, "intent"),
        ({"datasets": ["docs"]}, "datasets"),
    ],
)
async def test_partial_contract_inputs_fail_explicitly(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], field: str
) -> None:
    app, client = build_app(monkeypatch)
    response = await post(app, valid(**overrides))
    assert response.status_code == 422
    assert error(response)["code"] == "INVALID_REQUEST"
    assert error(response)["details"]["field"] == field
    assert client.calls == 0 and EmptyUow.opened == 0


@pytest.mark.asyncio
async def test_context_intent_and_empty_datasets_are_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _ = build_app(monkeypatch)
    response = await post(
        app, valid(intent="context", datasets=[], context_key="feature:a", include_evidence=True)
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["intent"] == "context" and data["context_key"] == "feature:a"


@pytest.mark.asyncio
async def test_provider_failure_returns_degraded_bundle_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, client = build_app(monkeypatch, provider_fails=True)
    response = await post(app, valid())
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["warnings"] == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]
    assert data["degraded_sources"] == ["memories", "episodes"]
    assert client.calls == 1


@pytest.mark.asyncio
async def test_postgres_unavailable_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _ = build_app(monkeypatch, db_fails=True)
    response = await post(app, valid())
    assert response.status_code == 503
    assert error(response)["code"] == "DEPENDENCY_UNAVAILABLE"


@pytest.mark.asyncio
async def test_no_llm_no_knowledge_recall_no_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(*args: object, **kwargs: object) -> None:
        raise AssertionError("must not be constructed by Contextual Recall")

    monkeypatch.setattr("sofias_memory.services.recall.RecallService.__init__", explode)
    monkeypatch.setattr("sofias_memory.infrastructure.llm.OpenAIRagAnswerClient.__init__", explode)
    app, _ = build_app(monkeypatch)
    assert getattr(app.state, "neo4j_resource", None) is None  # no graph resource at all
    response = await post(app, valid(intent="context"))
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_route_builds_a_single_attempt_embedding_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handoff 14 SS 57 (found in Human Validation step F): the provider SDK's
    transport retries must not turn one failed attempt into several provider calls."""

    seen: list[Settings] = []

    class RecordingEmbeddingClient(CountingEmbeddingClient):
        def __init__(self, settings: Settings) -> None:
            super().__init__(fail=True)
            seen.append(settings)

    def factory(settings: Settings, **kwargs: Any) -> ContextualRecallService:
        return ContextualRecallService(
            settings,
            embedding_client=kwargs["embedding_client"],
            unit_of_work_factory=lambda: EmptyUow(),  # type: ignore[arg-type,return-value]
        )

    async def no_support(uow: Any, modes: Any) -> dict[Any, None]:
        return {owner: None for owner in modes}

    monkeypatch.setattr(module, "hydrate_episode_support", no_support)
    monkeypatch.setattr(
        "sofias_memory.api.routes.context_recall.OpenAIEmbeddingClient", RecordingEmbeddingClient
    )
    monkeypatch.setattr("sofias_memory.api.routes.context_recall.ContextualRecallService", factory)
    app = create_app(make_settings(), enable_postgres_readiness=False, enable_neo4j=False)

    response = await post(app, {"scope": "project:a", "intent": "resume", "query": "Onde paramos?"})

    assert response.status_code == 200
    assert response.json()["data"]["warnings"] == ["NO_CURRENT_CHECKPOINT", "PARTIAL_CONTEXT"]
    assert [s.llm_max_retries for s in seen] == [0]
    assert app.state.settings.llm_max_retries == 3  # application settings untouched
