"""Contextual Project Memory Contract v1 -- longitudinal public-API scenario
(Contract hardening gate, steps A-V).

One real scenario across every contextual concept, driven only through the
public HTTP API of real application instances over a real PostgreSQL:

persist -> restart #1 -> resume -> explain -> history -> supersede -> stale
conflict -> idempotent replay -> history -> Knowledge composition -> Forget
root Evidence -> transitive propagation -> resume/explain/history after Forget
(no fallback) -> explicit recovery -> restart #2 -> final resume/explain/history
(no resurrection).

A *restart* closes the whole application boundary -- the app object, its
SQLAlchemy engine and connection pool -- and builds a brand-new instance; no
ORM session or app state survives it. Only the embedding provider is
replaced, by a deterministic bag-of-words embedding (ranking stability only).
Rows are inspected only to *complement* public-API assertions: privacy
scrub, read-only Recall and the absence of manual repair. The one technical
fixture is the processed Knowledge content of the explicit dataset (Source,
Document and Chunks are seeded directly: processing a Source needs the async
LLM pipeline, which is not part of this Contract).

Opt-in: ``SOFIAS_MEMORY_RUN_CONTEXT_CHECKPOINTS_POSTGRES_TESTS=1`` plus a
dedicated, discardable ``DATABASE_URL`` migrated to head.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import AsyncIterator, Sequence
from io import StringIO
from typing import Any
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from sofias_memory.config import load_settings
from sofias_memory.infrastructure.postgres import (
    create_async_engine_from_settings,
    create_session_factory,
    dispose_async_engine,
)
from sofias_memory.infrastructure.postgres.types import AsyncSessionFactory
from sofias_memory.observability.logging import clear_log_context, configure_logging
from tests.integration.test_context_checkpoints_postgres_integration import (
    HEADERS,
    make_settings,
    session_factory,
)
from tests.unit._app_factory import create_app

__all__ = ["session_factory"]

DIM = 3072
EMBEDDING_ROUTE_MODULES = (
    "sofias_memory.api.routes.episodes",
    "sofias_memory.api.routes.memories",
    "sofias_memory.api.routes.context_recall",
)
READ_ONLY_TABLES = (
    "queries",
    "pipeline_runs",
    "pipeline_steps",
    "sessions",
    "session_entries",
    "graph_outbox",
    "memory_items",
    "memory_provenance",
    "episodes",
    "episode_provenance",
    "context_checkpoints",
    "context_checkpoint_statements",
    "context_checkpoint_provenance",
    "contextual_memory_idempotency",
    "cognitive_memory_idempotency",
    "contextual_evidence_refs",
    "episode_evidence_refs",
    "context_statement_evidence_refs",
)
PRIVACY_TABLES = (
    ("memory_items", "id"),
    ("memory_provenance", "memory_id"),
    ("episodes", "id"),
    ("episode_provenance", "episode_id"),
    ("context_checkpoints", "id"),
    ("context_checkpoint_statements", "id"),
    ("context_checkpoint_provenance", "checkpoint_id"),
    ("contextual_memory_idempotency", "id"),
    ("cognitive_memory_idempotency", "id"),
    ("contextual_evidence_refs", "id"),
)


def embed(value: str) -> list[float]:
    """Deterministic bag-of-words unit vector (stable ranking, no provider)."""

    vector = [0.0] * DIM
    for token in re.findall(r"\w+", value.lower()):
        vector[int(hashlib.md5(token.encode()).hexdigest(), 16) % DIM] += 1.0  # noqa: S324
    norm = math.sqrt(sum(v * v for v in vector))
    if norm == 0.0:
        vector[0] = 1.0
        return vector
    return [v / norm for v in vector]


class BagOfWordsEmbeddingClient:
    def __init__(self) -> None:
        self.calls = 0

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls += 1
        return [embed(value) for value in texts]


class Instance:
    """One application instance: app + its own engine and connection pool."""

    def __init__(self) -> None:
        self.engine = create_async_engine_from_settings(load_settings())
        self.app = create_app(
            make_settings(),
            enable_postgres_readiness=False,
            enable_neo4j=False,
            postgres_session_factory=create_session_factory(self.engine),
        )
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://testserver"
        )

    async def stop(self) -> None:
        await self.client.aclose()
        await dispose_async_engine(self.engine)


class Scenario:
    def __init__(self, factory: AsyncSessionFactory, provider: BagOfWordsEmbeddingClient) -> None:
        self.factory = factory
        self.provider = provider
        self.instance = Instance()
        self.restarts = 0
        self.responses: list[str] = []

    async def restart(self) -> None:
        old = self.instance
        await old.stop()
        self.instance = Instance()
        assert self.instance.app is not old.app and self.instance.engine is not old.engine
        self.restarts += 1

    async def call(
        self, method: str, path: str, *, body: Any = None, key: str | None = None
    ) -> httpx.Response:
        headers = dict(HEADERS)
        if key is not None:
            headers["Idempotency-Key"] = key
        response = await self.instance.client.request(
            method, f"/api/v1{path}", json=body, headers=headers
        )
        self.responses.append(response.text)
        return response

    async def ok(self, method: str, path: str, *, status: int = 200, **kwargs: Any) -> Any:
        response = await self.call(method, path, **kwargs)
        assert response.status_code == status, response.text
        return response.json()["data"]

    async def counts(self) -> dict[str, int]:
        async with self.factory() as session:
            return {
                table: int(await session.scalar(text(f"SELECT count(*) FROM {table}")) or 0)  # noqa: S608
                for table in READ_ONLY_TABLES
            }

    async def recall(self, scope: str, intent: str, query: str, **extra: Any) -> Any:
        """Contextual Recall is read-only: every call leaves every table unchanged."""

        before = await self.counts()
        calls = self.provider.calls
        data = await self.ok(
            "POST",
            "/context/recall",
            body={"scope": scope, "intent": intent, "query": query, **extra},
        )
        assert await self.counts() == before, f"{intent} recall mutated state"
        assert self.provider.calls - calls == 1  # exactly one embedding attempt
        return data


async def revisions(s: Scenario, scope: str) -> list[int]:
    """Every revision of the stream through the public listing (all lifecycles)."""

    found: list[int] = []
    for lifecycle in ("active", "superseded", "invalidated", "forgotten"):
        page = await s.ok(
            "GET", f"/context/checkpoints?scope={scope}&context_key=main&lifecycle={lifecycle}"
        )
        found.extend(item["revision"] for item in page["items"])
    return sorted(found)


def owners(bundle: dict[str, Any], source_id: str) -> list[tuple[str, str]]:
    for item in bundle["evidence"]:
        if item["source_id"] == source_id:
            return [(o["owner_type"], o["owner_id"]) for o in item["owners"]]
    raise AssertionError(f"{source_id} not in evidence")


def evidence_ids(bundle: dict[str, Any]) -> set[str]:
    return {item["source_id"] for item in bundle["evidence"]}


def episode_ids(bundle: dict[str, Any]) -> list[str]:
    return [item["episode"]["episode_id"] for item in bundle["episodes"]]


def support_of(resource: dict[str, Any] | None) -> list[str]:
    if not resource or not resource.get("support"):
        return []
    return [e["source_id"] for e in resource["support"]["evidence"]]


@pytest_asyncio.fixture()
async def scenario(
    session_factory: AsyncSessionFactory, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Scenario]:
    provider = BagOfWordsEmbeddingClient()
    for module in EMBEDDING_ROUTE_MODULES:
        monkeypatch.setattr(f"{module}.OpenAIEmbeddingClient", lambda settings: provider)
    value = Scenario(session_factory, provider)
    try:
        yield value
    finally:
        await value.instance.stop()


async def seed_processed_knowledge(
    factory: AsyncSessionFactory, dataset_id: str, bodies: Sequence[str]
) -> tuple[str, list[str]]:
    """Technical fixture: the processed content (Source/Document/Chunks) of an
    explicit ACTIVE dataset created through the public API."""

    source_id, document_id = uuid4(), uuid4()
    chunk_ids: list[str] = []
    async with factory() as session:
        generation = await session.scalar(
            text("SELECT active_generation FROM datasets WHERE id = :d"), {"d": dataset_id}
        )
        await session.execute(
            text(
                "INSERT INTO sources (id, dataset_id, kind, name, mime_type, content_sha256, "
                "byte_size, metadata, status, version) VALUES (:id, :d, 'text', 'adr.md', "
                "'text/markdown', :sha, 1, '{}', 'active', 1)"
            ),
            {"id": source_id, "d": dataset_id, "sha": uuid4().hex + uuid4().hex},
        )
        await session.execute(
            text(
                "INSERT INTO documents (id, dataset_id, source_id, generation, title, language, "
                "normalized_text, text_sha256, token_count, metadata, is_active) VALUES "
                "(:id, :d, :src, :g, 'ADR', 'pt', 'texto', :sha, 1, '{}', true)"
            ),
            {
                "id": document_id,
                "d": dataset_id,
                "src": source_id,
                "g": generation,
                "sha": "b" * 64,
            },
        )
        for ordinal, body in enumerate(bodies):
            chunk_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO chunks (id, dataset_id, document_id, source_id, generation, "
                    "ordinal, text, content_sha256, token_count, start_char, end_char, "
                    "section_path, metadata, embedding, lexical, is_active) VALUES (:id, :d, "
                    ":doc, :src, :g, :o, :t, :sha, 1, 0, 10, '{}', '{}', CAST(:v AS vector), "
                    "to_tsvector('simple', :t), true)"
                ),
                {
                    "id": chunk_id,
                    "d": dataset_id,
                    "doc": document_id,
                    "src": source_id,
                    "g": generation,
                    "o": ordinal,
                    "t": body,
                    "sha": uuid4().hex + uuid4().hex,
                    "v": "[" + ",".join(str(v) for v in embed(body)) + "]",
                },
            )
            chunk_ids.append(str(chunk_id))
        await session.commit()
    return str(source_id), chunk_ids


async def sentinel_rows(factory: AsyncSessionFactory, sentinel: str) -> list[str]:
    hits: list[str] = []
    async with factory() as session:
        for table, _ in PRIVACY_TABLES:
            found = await session.scalar(
                text(f"SELECT count(*) FROM {table} t WHERE t::text LIKE :s"),  # noqa: S608
                {"s": f"%{sentinel}%"},
            )
            if found:
                hits.append(table)
    return hits


PROV = {
    "origin_kind": "user_asserted",
    "source_system": "ctx-longitudinal",
    "conversation_ref": "conv-longitudinal",
    "turn_ref": "turn-1",
}


def checkpoint_body(scope: str, **sections: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "scope": scope,
        "context_key": "main",
        "objective": {"text": "Entregar memória contextual persistente por projeto."},
        "approved_decisions": [],
        "pending_work": [],
        "open_items": [],
        "blockers": [],
        "next_action": {"text": "Executar a validação longitudinal."},
        "provenance": PROV,
    }
    body.update(sections)
    return body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_contextual_project_memory_longitudinal_scenario(scenario: Scenario) -> None:
    s = scenario
    run = uuid4().hex[:10]
    scope = f"project:ctx-longitudinal-{run}"
    sentinel = f"CTX_FORGET_SENTINEL_{uuid4().hex}"
    query = "por que postgresql é autoridade da memória contextual"
    log_stream = StringIO()
    clear_log_context()
    configure_logging("INFO", stream=log_stream)

    # --- Setup: explicit dataset ctx-docs with processed Knowledge ------------------
    slug = f"ctx-docs-{run}"
    dataset = await s.ok("POST", "/datasets", status=201, body={"name": slug, "slug": slug})
    dataset_id = str(dataset.get("dataset_id") or dataset["id"])
    _, (chunk_k, _distractor) = await seed_processed_knowledge(
        s.factory,
        dataset_id,
        [
            "ADR: postgresql é a autoridade da memória contextual por projeto",
            "Política de backup semanal e retenção de snapshots",
        ],
    )

    # --- A: Cognitive truth -----------------------------------------------------------
    m1 = (
        await s.ok(
            "POST",
            "/memories",
            status=201,
            key=f"m1-{run}",
            body={
                "memory_type": "semantic",
                "scope": scope,
                "content": f"PostgreSQL is authoritative {sentinel} memória contextual",
                "provenance": {
                    "origin_kind": "user_asserted",
                    "source_system": "ctx-longitudinal",
                    "source_ref": "longitudinal",
                },
            },
        )
    )["memory_id"]

    # --- B: Episode E1 supported by M1 (and E2, independently supported) --------------
    async def episode(summary: str, occurred_at: str, support: dict[str, Any]) -> str:
        data = await s.ok(
            "POST",
            "/episodes",
            status=201,
            key=f"ep-{uuid4().hex}",
            body={
                "scope": scope,
                "context_key": "main",
                "kind": "decision",
                "summary": summary,
                "occurred_at": occurred_at,
                "provenance": PROV,
                "support": support,
            },
        )
        return str(data["episode_id"])

    e1 = await episode(
        f"Decisão: postgresql é autoridade {sentinel}",
        "2026-11-10T10:00:00Z",
        {"mode": "all_required", "refs": [{"source_type": "memory_item", "source_id": m1}]},
    )
    e2 = await episode(
        "Decisão: memória contextual usa evidence explícita",
        "2026-11-12T10:00:00Z",
        {
            "mode": "any_sufficient",
            "refs": [
                {"source_type": "memory_item", "source_id": m1},
                {"source_type": "chunk", "source_id": chunk_k},
            ],
        },
    )

    # --- C: initial checkpoint C1 (revision 1, statement-level Evidence) --------------
    c1 = await s.ok(
        "POST",
        "/context/checkpoints",
        status=201,
        key=f"c1-{run}",
        body=checkpoint_body(
            scope,
            current_state={"text": "Estado inicial: modelagem contextual aprovada."},
            approved_decisions=[
                {
                    "text": f"PostgreSQL é autoridade {sentinel}",
                    "support": {
                        "mode": "all_required",
                        "refs": [{"source_type": "episode", "source_id": e1}],
                    },
                }
            ],
            pending_work=[{"text": "Validar Forget propagation."}],
        ),
    )
    assert (c1["revision"], c1["lifecycle"]) == (1, "active")
    c1_id = c1["checkpoint_id"]
    c1_decision = c1["approved_decisions"][0]["statement_id"]

    # --- D: restart #1 ------------------------------------------------------------------
    await s.restart()

    # --- E: resume ----------------------------------------------------------------------
    resume = await s.recall(scope, "resume", query)
    assert resume["continuity_available"] is True
    assert resume["current_checkpoint"]["checkpoint_id"] == c1_id
    assert m1 in [m["memory"]["memory_id"] for m in resume["current_memories"]]
    assert e1 in episode_ids(resume)

    # --- F: explain -- the declared causal trace, never chronology ---------------------
    explain = await s.recall(scope, "explain", query, include_evidence=True)
    assert ("context_statement", c1_decision) in owners(explain, e1)
    assert ("episode", e1) in owners(explain, m1)
    assert explain["historical_checkpoints"] == []  # lineage is never explanation
    assert explain["warnings"] == []

    # --- G: history (initial) ------------------------------------------------------------
    history = await s.recall(scope, "history", query)
    assert history["historical_checkpoints"] == []
    assert history["current_checkpoint"]["checkpoint_id"] == c1_id
    assert e1 in episode_ids(history)
    assert history["current_memories"] == []

    # --- H: Consumers A and B read C1; A supersedes C1 -> C2 ---------------------------
    seen_by_a = await s.ok("GET", f"/context/checkpoints/current?scope={scope}&context_key=main")
    seen_by_b = await s.ok("GET", f"/context/checkpoints/current?scope={scope}&context_key=main")
    assert seen_by_a["checkpoint_id"] == seen_by_b["checkpoint_id"] == c1_id
    c2_body = checkpoint_body(
        scope,
        current_state={
            "text": f"Estado atual: PostgreSQL autoritativo {sentinel}",
            "support": {
                "mode": "all_required",
                "refs": [{"source_type": "episode", "source_id": e1}],
            },
        },
        approved_decisions=[
            {
                "text": "Evidence explícita sustenta o contexto.",
                "support": {
                    "mode": "all_required",
                    "refs": [{"source_type": "episode", "source_id": e2}],
                },
            }
        ],
        pending_work=[{"text": "Ativar o Contract após o hardening."}],
    )
    a_key = f"a-supersede-{run}"
    c2 = await s.ok(
        "POST", f"/context/checkpoints/{c1_id}/supersede", status=201, key=a_key, body=c2_body
    )
    assert (c2["revision"], c2["lifecycle"]) == (2, "active")
    c2_id = c2["checkpoint_id"]
    c1_after = await s.ok("GET", f"/context/checkpoints/{c1_id}")
    assert c1_after["lifecycle"] == "superseded" and c1_after["superseded_by"] == c2_id

    # --- I: stale conflict -- B supersedes the C1 it read, with a new key --------------
    stale = await s.call(
        "POST",
        f"/context/checkpoints/{c1_id}/supersede",
        key=f"b-supersede-{run}",
        body=checkpoint_body(scope, current_state={"text": "Estado concorrente de B."}),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "CONTEXT_STATE_CONFLICT"
    assert await revisions(s, scope) == [1, 2]  # C3 not created
    current = await s.ok("GET", f"/context/checkpoints/current?scope={scope}&context_key=main")
    assert current["checkpoint_id"] == c2_id

    # --- J: replay of A (same key, same payload) -> the same C2 ------------------------
    replay = await s.ok(
        "POST", f"/context/checkpoints/{c1_id}/supersede", status=201, key=a_key, body=c2_body
    )
    assert replay["checkpoint_id"] == c2_id and replay["revision"] == 2
    assert await revisions(s, scope) == [1, 2]

    # --- K: history after supersede ------------------------------------------------------
    history = await s.recall(scope, "history", query)
    assert history["current_checkpoint"]["checkpoint_id"] == c2_id
    assert [h["checkpoint_id"] for h in history["historical_checkpoints"]] == [c1_id]

    # --- L: Knowledge composition (explicit dataset only) -------------------------------
    without = await s.recall(scope, "context", query)
    assert without["knowledge_references"] == []  # never an implicit dataset
    composed = await s.recall(scope, "context", query, datasets=[slug])
    refs = composed["knowledge_references"]
    assert refs and refs[0]["chunk_id"] == chunk_k and refs[0]["dataset_slug"] == slug
    assert all(not ref["evidence_backed"] or ref["chunk_id"] == chunk_k for ref in refs)
    assert "Política de backup" not in json.dumps(composed)  # identities, never chunk text

    # --- M: Forget the root Evidence M1 --------------------------------------------------
    forget_mark = len(s.responses)
    forget = await s.ok("POST", f"/memories/{m1}/forget", key=f"forget-m1-{run}")
    assert "forgotten" in json.dumps(forget)

    # --- N: transitive propagation --------------------------------------------------------
    e1_after = await s.ok("GET", f"/episodes/{e1}")
    assert e1_after["lifecycle"] == "forgotten" and e1_after["summary"] is None
    e2_after = await s.ok("GET", f"/episodes/{e2}")
    assert e2_after["lifecycle"] == "active"  # any_sufficient: still supported by K
    c2_after = await s.ok("GET", f"/context/checkpoints/{c2_id}")
    assert c2_after["lifecycle"] == "invalidated" and c2_after["current_state"] is None
    assert [d["text"] for d in c2_after["approved_decisions"]] == [
        "Evidence explícita sustenta o contexto."
    ]

    # --- O: resume after Forget -- no fallback to C1 ------------------------------------
    resume = await s.recall(scope, "resume", query)
    assert resume["continuity_available"] is False and resume["current_checkpoint"] is None
    assert "NO_CURRENT_CHECKPOINT" in resume["warnings"]

    # --- P: explain after Forget -- nothing reconstructed through the chain -------------
    explain = await s.recall(scope, "explain", query, include_evidence=True)
    assert e1 not in episode_ids(explain)
    assert all(
        o[1] != e1 for item in explain["evidence"] for o in owners(explain, item["source_id"])
    )

    # --- Q: history after Forget -----------------------------------------------------------
    history = await s.recall(scope, "history", query)
    by_id = {h["checkpoint_id"]: h for h in history["historical_checkpoints"]}
    assert set(by_id) == {c1_id, c2_id}
    assert by_id[c2_id]["lifecycle"] == "invalidated" and by_id[c2_id]["current_state"] is None
    assert by_id[c1_id]["lifecycle"] == "superseded"
    assert e1 not in episode_ids(history)

    # --- R: explicit recovery -- a new current C3, never a Supersede of C2 -------------
    c3 = await s.ok(
        "POST",
        "/context/checkpoints",
        status=201,
        key=f"c3-{run}",
        body=checkpoint_body(
            scope,
            current_state={
                "text": "Estado recuperado: contexto apoiado por evidence existente.",
                "support": {
                    "mode": "all_required",
                    "refs": [{"source_type": "episode", "source_id": e2}],
                },
            },
        ),
    )
    assert (c3["revision"], c3["lifecycle"]) == (3, "active")
    c3_id = c3["checkpoint_id"]
    c2_final = await s.ok("GET", f"/context/checkpoints/{c2_id}")
    assert c2_final["superseded_by"] is None  # no lineage on recovery create

    # --- S: restart #2 ------------------------------------------------------------------
    await s.restart()

    # --- T: final resume ----------------------------------------------------------------
    resume = await s.recall(scope, "resume", query)
    assert resume["continuity_available"] is True
    assert (
        resume["current_checkpoint"]["checkpoint_id"],
        resume["current_checkpoint"]["revision"],
    ) == (
        c3_id,
        3,
    )
    assert m1 not in [m["memory"]["memory_id"] for m in resume["current_memories"]]
    assert e1 not in episode_ids(resume)

    # --- U: final explain -- only Evidence that still exists ----------------------------
    explain = await s.recall(scope, "explain", query, include_evidence=True, datasets=[slug])
    assert support_of(explain["current_checkpoint"]["current_state"]) == [e2]
    assert (
        "context_statement",
        explain["current_checkpoint"]["current_state"]["statement_id"],
    ) in owners(explain, e2)
    assert ("episode", e2) in owners(explain, chunk_k)
    for item in explain["evidence"]:
        assert set(item) == {
            "evidence_id",
            "source_type",
            "source_id",
            "availability",
            "result_class",
            "owners",
        }
        if item["source_id"] == m1:
            assert item["availability"] == "forgotten"  # identity only, never content
        else:
            assert item["availability"] == "available"
    assert e1 not in evidence_ids(explain)

    # --- V: final history ----------------------------------------------------------------
    history = await s.recall(scope, "history", query)
    assert history["current_checkpoint"]["checkpoint_id"] == c3_id
    assert [(h["revision"], h["lifecycle"]) for h in history["historical_checkpoints"]] == [
        (2, "invalidated"),
        (1, "superseded"),
    ]
    assert episode_ids(history) == [e2]  # E1 forgotten; chronology occurred_at DESC
    assert await revisions(s, scope) == [1, 2, 3]  # revision sequence preserved

    # --- Non-resurrection and privacy across every surface ---------------------------
    assert s.restarts == 2
    after_forget = s.responses[forget_mark:]
    for rendered in after_forget:
        assert sentinel not in rendered
    for path in (f"/episodes/{e1}", f"/memories/{m1}", f"/context/checkpoints/{c2_id}"):
        assert sentinel not in (await s.call("GET", path)).text
    assert await sentinel_rows(s.factory, sentinel) == []
    rendered_logs = log_stream.getvalue()
    assert "context_recall" in rendered_logs  # the capture really observed the scenario
    assert sentinel not in rendered_logs
