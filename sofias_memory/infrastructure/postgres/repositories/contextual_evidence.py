"""Contextual Evidence PostgreSQL repository (SM-CTX-004, handoff 12 SS 13/46-48/56/59-62).

Persistence and authoritative source lookups only -- availability rules live
in :mod:``sofias_memory.domain.contextual_evidence``, validation in
:mod:``sofias_memory.services.contextual_evidence``.

- Canonical get-or-create relies on ``UNIQUE(source_type, source_id)`` via
  ``INSERT ... ON CONFLICT DO NOTHING`` followed by a fresh SELECT (READ
  COMMITTED): concurrent creators of the same identity converge on one row.
- Source lookups are batched per type (no N+1). With ``lock=True`` the target
  rows are taken ``FOR SHARE`` (only the referenced row, never its parents),
  in a deterministic order (type, id), so a concurrent Forget/Invalidate of that
  source serializes with the derived write.
- Episode cycle detection is a recursive CTE over the Evidence links (never
  Neo4j).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import (
    CognitiveMemoryLifecycle,
    DatasetStatus,
    EpisodeLifecycle,
    EvidenceKey,
    SourceStatus,
)
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    ContextStatementEvidenceRef,
    ContextualEvidenceRef,
    Dataset,
    Document,
    Episode,
    EpisodeEvidenceRef,
    MemoryItem,
    SessionEntry,
    Source,
)


@dataclass(frozen=True, slots=True)
class LinkedEvidence:
    owner_id: UUID
    evidence_ref_id: UUID
    key: EvidenceKey


@dataclass(frozen=True, slots=True)
class KnowledgeState:
    source_status: SourceStatus
    dataset_status: DatasetStatus
    dataset_active_generation: int
    document_is_active: bool | None = None
    document_generation: int | None = None
    chunk_is_active: bool | None = None
    chunk_generation: int | None = None


class ContextualEvidenceRepository:
    """Canonical Evidence identities, owner links and source state lookups."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # --- canonical identity / links ---------------------------------------------

    async def get_or_create(self, keys: Sequence[EvidenceKey]) -> dict[EvidenceKey, UUID]:
        if not keys:
            return {}
        values = [{"source_type": k.source_type, "source_id": k.source_id} for k in keys]
        statement = (
            insert(ContextualEvidenceRef)
            .values(values)
            .on_conflict_do_nothing(index_elements=["source_type", "source_id"])
        )
        await self._session.execute(statement)
        found: dict[EvidenceKey, UUID] = {}
        for key in keys:
            ref_id = await self._session.scalar(
                select(ContextualEvidenceRef.id).where(
                    ContextualEvidenceRef.source_type == key.source_type,
                    ContextualEvidenceRef.source_id == key.source_id,
                )
            )
            if ref_id is None:  # pragma: no cover - guarded by ON CONFLICT semantics
                raise RuntimeError("canonical Evidence identity vanished after upsert")
            found[key] = ref_id
        return found

    async def add_episode_links(self, episode_id: UUID, ref_ids: Sequence[UUID]) -> None:
        self._session.add_all(
            [EpisodeEvidenceRef(episode_id=episode_id, evidence_ref_id=r) for r in ref_ids]
        )
        await self._session.flush()

    async def add_statement_links(self, links: Sequence[tuple[UUID, UUID]]) -> None:
        self._session.add_all(
            [ContextStatementEvidenceRef(statement_id=s, evidence_ref_id=r) for s, r in links]
        )
        await self._session.flush()

    async def delete_episode_links(self, episode_id: UUID) -> None:
        """Direct Episode Forget removes the Episode's own outgoing support;
        inbound refs from other owners point at the canonical identity of the
        Episode and stay resolvable (as FORGOTTEN) -- handoff 12 SS 62/63."""

        await self._session.execute(
            delete(EpisodeEvidenceRef).where(EpisodeEvidenceRef.episode_id == episode_id)
        )

    async def list_episode_links(self, episode_ids: Sequence[UUID]) -> list[LinkedEvidence]:
        if not episode_ids:
            return []
        rows = await self._session.execute(
            select(
                EpisodeEvidenceRef.episode_id,
                ContextualEvidenceRef.id,
                ContextualEvidenceRef.source_type,
                ContextualEvidenceRef.source_id,
            )
            .join(
                ContextualEvidenceRef,
                ContextualEvidenceRef.id == EpisodeEvidenceRef.evidence_ref_id,
            )
            .where(EpisodeEvidenceRef.episode_id.in_(list(episode_ids)))
        )
        return [
            LinkedEvidence(owner, ref_id, EvidenceKey(stype, sid))
            for owner, ref_id, stype, sid in rows.all()
        ]

    async def list_statement_links(self, statement_ids: Sequence[UUID]) -> list[LinkedEvidence]:
        if not statement_ids:
            return []
        rows = await self._session.execute(
            select(
                ContextStatementEvidenceRef.statement_id,
                ContextualEvidenceRef.id,
                ContextualEvidenceRef.source_type,
                ContextualEvidenceRef.source_id,
            )
            .join(
                ContextualEvidenceRef,
                ContextualEvidenceRef.id == ContextStatementEvidenceRef.evidence_ref_id,
            )
            .where(ContextStatementEvidenceRef.statement_id.in_(list(statement_ids)))
        )
        return [
            LinkedEvidence(owner, ref_id, EvidenceKey(stype, sid))
            for owner, ref_id, stype, sid in rows.all()
        ]

    async def episode_reaches(self, start_episode_ids: Sequence[UUID], target_id: UUID) -> bool:
        """True when ``target_id`` is reachable from any start Episode through
        Episode -> Episode Evidence links (recursive CTE, cycle-safe via UNION)."""

        if not start_episode_ids:
            return False
        statement = text(
            """
            WITH RECURSIVE reach(episode_id) AS (
                SELECT unnest(CAST(:starts AS uuid[]))
                UNION
                SELECT r.source_id
                FROM reach
                JOIN episode_evidence_refs l ON l.episode_id = reach.episode_id
                JOIN contextual_evidence_refs r ON r.id = l.evidence_ref_id
                WHERE r.source_type = 'episode'
            )
            SELECT EXISTS (SELECT 1 FROM reach WHERE episode_id = :target)
            """
        )
        result = await self._session.scalar(
            statement, {"starts": list(start_episode_ids), "target": target_id}
        )
        return bool(result)

    # --- authoritative source lookups (batched) -----------------------------------

    async def session_entries(self, ids: Sequence[UUID], *, lock: bool) -> set[UUID]:
        statement = (
            select(SessionEntry.id).where(SessionEntry.id.in_(list(ids))).order_by(SessionEntry.id)
        )
        if lock:
            statement = statement.with_for_update(read=True)
        return set((await self._session.scalars(statement)).all())

    async def memory_items(
        self, ids: Sequence[UUID], *, lock: bool
    ) -> dict[UUID, tuple[CognitiveMemoryLifecycle, str | None]]:
        statement = (
            select(MemoryItem.id, MemoryItem.lifecycle, MemoryItem.scope)
            .where(MemoryItem.id.in_(list(ids)))
            .order_by(MemoryItem.id)
        )
        if lock:
            statement = statement.with_for_update(read=True)
        rows = await self._session.execute(statement)
        return {rid: (lifecycle, scope) for rid, lifecycle, scope in rows.all()}

    async def episodes(
        self, ids: Sequence[UUID], *, lock: bool
    ) -> dict[UUID, tuple[EpisodeLifecycle, str | None]]:
        statement = (
            select(Episode.id, Episode.lifecycle, Episode.scope)
            .where(Episode.id.in_(list(ids)))
            .order_by(Episode.id)
        )
        if lock:
            statement = statement.with_for_update(read=True)
        rows = await self._session.execute(statement)
        return {rid: (lifecycle, scope) for rid, lifecycle, scope in rows.all()}

    async def sources(self, ids: Sequence[UUID], *, lock: bool) -> dict[UUID, KnowledgeState]:
        statement = (
            select(Source.id, Source.status, Dataset.status, Dataset.active_generation)
            .join(Dataset, Dataset.id == Source.dataset_id)
            .where(Source.id.in_(list(ids)))
            .order_by(Source.id)
        )
        if lock:
            statement = statement.with_for_update(read=True, of=Source)
        rows = await self._session.execute(statement)
        return {
            rid: KnowledgeState(source_status=s, dataset_status=d, dataset_active_generation=g)
            for rid, s, d, g in rows.all()
        }

    async def documents(self, ids: Sequence[UUID], *, lock: bool) -> dict[UUID, KnowledgeState]:
        statement = (
            select(
                Document.id,
                Document.is_active,
                Document.generation,
                Source.status,
                Dataset.status,
                Dataset.active_generation,
            )
            .join(Source, Source.id == Document.source_id)
            .join(Dataset, Dataset.id == Document.dataset_id)
            .where(Document.id.in_(list(ids)))
            .order_by(Document.id)
        )
        if lock:
            statement = statement.with_for_update(read=True, of=Document)
        rows = await self._session.execute(statement)
        return {
            rid: KnowledgeState(
                source_status=s,
                dataset_status=d,
                dataset_active_generation=g,
                document_is_active=active,
                document_generation=gen,
            )
            for rid, active, gen, s, d, g in rows.all()
        }

    async def chunks(self, ids: Sequence[UUID], *, lock: bool) -> dict[UUID, KnowledgeState]:
        statement = (
            select(
                Chunk.id,
                Chunk.is_active,
                Chunk.generation,
                Document.is_active,
                Document.generation,
                Source.status,
                Dataset.status,
                Dataset.active_generation,
            )
            .join(Document, Document.id == Chunk.document_id)
            .join(Source, Source.id == Chunk.source_id)
            .join(Dataset, Dataset.id == Chunk.dataset_id)
            .where(Chunk.id.in_(list(ids)))
            .order_by(Chunk.id)
        )
        if lock:
            statement = statement.with_for_update(read=True, of=Chunk)
        rows = await self._session.execute(statement)
        return {
            rid: KnowledgeState(
                source_status=s,
                dataset_status=d,
                dataset_active_generation=g,
                document_is_active=d_active,
                document_generation=d_gen,
                chunk_is_active=c_active,
                chunk_generation=c_gen,
            )
            for rid, c_active, c_gen, d_active, d_gen, s, d, g in rows.all()
        }
