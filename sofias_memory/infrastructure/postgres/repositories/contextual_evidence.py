"""Contextual Evidence PostgreSQL repository (SM-CTX-004, handoff 12 SS 13/46-48/56/59-62).

Persistence and authoritative source lookups only -- availability rules live
in :mod:``sofias_memory.domain.contextual_evidence``, validation in
:mod:``sofias_memory.services.contextual_evidence``.

- Canonical get-or-create relies on ``UNIQUE(source_type, source_id)`` via
  ``INSERT ... ON CONFLICT DO NOTHING`` followed by a fresh SELECT (READ
  COMMITTED): concurrent creators of the same identity converge on one row.
- Source lookups are batched per type (no N+1). With ``lock=True`` the target
  rows are taken ``FOR SHARE`` (only the referenced row; the caller imposes the
  SM-CTX-005 global order, see services.contextual_evidence), so a concurrent
  Forget/Invalidate of that source serializes with the derived write.
- Episode cycle detection is a recursive CTE over the Evidence links (never
  Neo4j).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from sofias_memory.domain import (
    CognitiveMemoryLifecycle,
    DatasetStatus,
    EpisodeLifecycle,
    EvidenceKey,
    EvidenceSourceType,
    SourceStatus,
)
from sofias_memory.infrastructure.postgres.models import (
    Chunk,
    ContextCheckpoint,
    ContextCheckpointStatement,
    ContextStatementEvidenceRef,
    ContextualEvidenceRef,
    Dataset,
    Document,
    Episode,
    EpisodeEvidenceRef,
    EpisodeProvenance,
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
class KnowledgeLocation:
    """Structural identity of a Knowledge Evidence target (never content)."""

    dataset_id: UUID
    source_id: UUID
    document_id: UUID | None = None
    chunk_id: UUID | None = None


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

    async def knowledge_locations(
        self, keys: Sequence[EvidenceKey]
    ) -> dict[EvidenceKey, KnowledgeLocation]:
        """Dataset/Source/Document/Chunk identities of Source, Document and Chunk
        Evidence keys (SM-CTX-007 Evidence-backed Knowledge references, handoff
        15 SS 62/66). Structural only and unlocked: current availability is
        decided by the caller from the Evidence resolution, never here."""

        by_type: dict[EvidenceSourceType, list[UUID]] = {}
        for key in keys:
            by_type.setdefault(key.source_type, []).append(key.source_id)
        out: dict[EvidenceKey, KnowledgeLocation] = {}
        chunk_ids = by_type.get(EvidenceSourceType.CHUNK, [])
        if chunk_ids:
            rows = await self._session.execute(
                select(Chunk.id, Chunk.dataset_id, Chunk.source_id, Chunk.document_id).where(
                    Chunk.id.in_(chunk_ids)
                )
            )
            for chunk_id, dataset_id, source_id, document_id in rows.all():
                out[EvidenceKey(EvidenceSourceType.CHUNK, chunk_id)] = KnowledgeLocation(
                    dataset_id, source_id, document_id, chunk_id
                )
        document_ids = by_type.get(EvidenceSourceType.DOCUMENT, [])
        if document_ids:
            rows = await self._session.execute(
                select(Document.id, Document.dataset_id, Document.source_id).where(
                    Document.id.in_(document_ids)
                )
            )
            for document_id, dataset_id, source_id in rows.all():
                out[EvidenceKey(EvidenceSourceType.DOCUMENT, document_id)] = KnowledgeLocation(
                    dataset_id, source_id, document_id
                )
        source_ids = by_type.get(EvidenceSourceType.SOURCE, [])
        if source_ids:
            rows = await self._session.execute(
                select(Source.id, Source.dataset_id).where(Source.id.in_(source_ids))
            )
            for source_id, dataset_id in rows.all():
                out[EvidenceKey(EvidenceSourceType.SOURCE, source_id)] = KnowledgeLocation(
                    dataset_id, source_id
                )
        return out

    # --- SM-CTX-005: reverse dependency lookup and propagation locks -------------
    #
    # Handoff 13 SS 32/41/81-84/143: every query starts from the canonical refs
    # that actually exist (indexed UNIQUE(source_type, source_id)) and walks the
    # indexed ``evidence_ref_id`` link columns -- never "all Documents/Chunks".

    @staticmethod
    def _key_arrays(keys: Sequence[EvidenceKey]) -> dict[str, list[str] | list[UUID]]:
        return {
            "types": [key.source_type.value for key in keys],
            "ids": [key.source_id for key in keys],
        }

    async def knowledge_seed_keys(self, source_ids: Sequence[UUID]) -> list[EvidenceKey]:
        """Canonical refs structurally belonging to ``source_ids``: the Sources
        themselves plus every Document/Chunk of those Sources in *any*
        generation and activity state (handoff 13 SS 31) -- only refs that some
        owner actually declared exist here, so nothing else is loaded."""

        if not source_ids:
            return []
        statement = text(
            """
            SELECT r.source_type::text, r.source_id
            FROM contextual_evidence_refs r
            WHERE r.source_type = 'source' AND r.source_id = ANY(CAST(:sources AS uuid[]))
            UNION
            SELECT r.source_type::text, r.source_id
            FROM contextual_evidence_refs r
            JOIN documents d ON d.id = r.source_id
            WHERE r.source_type = 'document' AND d.source_id = ANY(CAST(:sources AS uuid[]))
            UNION
            SELECT r.source_type::text, r.source_id
            FROM contextual_evidence_refs r
            JOIN chunks c ON c.id = r.source_id
            WHERE r.source_type = 'chunk' AND c.source_id = ANY(CAST(:sources AS uuid[]))
            """
        )
        rows = await self._session.execute(statement, {"sources": list(source_ids)})
        return sorted(EvidenceKey(EvidenceSourceType(stype), sid) for stype, sid in rows.all())

    async def source_ids_for_dataset(self, dataset_id: UUID) -> list[UUID]:
        """Every Source of a Dataset in any status (DELETED included): Dataset
        scope seeds must also converge refs into already-tombstoned Sources."""

        rows = await self._session.scalars(
            select(Source.id).where(Source.dataset_id == dataset_id).order_by(Source.id)
        )
        return list(rows.all())

    async def episode_owners(self, keys: Sequence[EvidenceKey]) -> list[UUID]:
        """Episodes whose declared support links any of ``keys``, ascending id."""

        if not keys:
            return []
        statement = text(
            """
            SELECT DISTINCT l.episode_id
            FROM unnest(CAST(:types AS text[]), CAST(:ids AS uuid[])) AS k(stype, sid)
            JOIN contextual_evidence_refs r
              ON r.source_type = CAST(k.stype AS evidence_source_type) AND r.source_id = k.sid
            JOIN episode_evidence_refs l ON l.evidence_ref_id = r.id
            ORDER BY l.episode_id
            """
        )
        rows = await self._session.execute(statement, self._key_arrays(keys))
        return [row[0] for row in rows.all()]

    async def statement_owners(self, keys: Sequence[EvidenceKey]) -> list[tuple[UUID, UUID]]:
        """``(checkpoint_id, statement_id)`` of every ContextStatement whose declared
        support links any of ``keys``, ascending."""

        if not keys:
            return []
        statement = text(
            """
            SELECT DISTINCT s.checkpoint_id, s.id
            FROM unnest(CAST(:types AS text[]), CAST(:ids AS uuid[])) AS k(stype, sid)
            JOIN contextual_evidence_refs r
              ON r.source_type = CAST(k.stype AS evidence_source_type) AND r.source_id = k.sid
            JOIN context_statement_evidence_refs l ON l.evidence_ref_id = r.id
            JOIN context_checkpoint_statements s ON s.id = l.statement_id
            ORDER BY s.checkpoint_id, s.id
            """
        )
        rows = await self._session.execute(statement, self._key_arrays(keys))
        return [(row[0], row[1]) for row in rows.all()]

    async def episode_parent_edges(self, episode_ids: Sequence[UUID]) -> dict[UUID, list[UUID]]:
        """Every Episode -> Episode Evidence edge among the ancestors of
        ``episode_ids`` (recursive CTE with set semantics: terminates even on a
        corrupt cycle, which :func:``episode_ranks`` then rejects)."""

        if not episode_ids:
            return {}
        statement = text(
            """
            WITH RECURSIVE anc(episode_id) AS (
                SELECT unnest(CAST(:starts AS uuid[]))
                UNION
                SELECT r.source_id
                FROM anc
                JOIN episode_evidence_refs l ON l.episode_id = anc.episode_id
                JOIN contextual_evidence_refs r ON r.id = l.evidence_ref_id
                WHERE r.source_type = 'episode'
            )
            SELECT l.episode_id, r.source_id
            FROM anc
            JOIN episode_evidence_refs l ON l.episode_id = anc.episode_id
            JOIN contextual_evidence_refs r ON r.id = l.evidence_ref_id
            WHERE r.source_type = 'episode'
            """
        )
        rows = await self._session.execute(statement, {"starts": list(episode_ids)})
        parents: dict[UUID, list[UUID]] = {}
        for child, parent in rows.all():
            parents.setdefault(child, []).append(parent)
        return parents

    async def lock_episode_for_propagation(
        self, episode_id: UUID
    ) -> tuple[Episode | None, EpisodeProvenance | None]:
        """``FOR UPDATE`` on the Episode, then its 1:1 provenance (the same
        Episode -> provenance order as direct Forget). ``populate_existing``:
        the decision must use the row as locked, never a stale identity-map copy."""

        episode = await self._session.scalar(
            select(Episode)
            .where(Episode.id == episode_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if episode is None:
            return None, None
        provenance = await self._session.scalar(
            select(EpisodeProvenance)
            .where(EpisodeProvenance.episode_id == episode_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return episode, provenance

    async def lock_checkpoint_for_propagation(
        self, checkpoint_id: UUID
    ) -> ContextCheckpoint | None:
        checkpoint = await self._session.scalar(
            select(ContextCheckpoint)
            .where(ContextCheckpoint.id == checkpoint_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return cast(ContextCheckpoint | None, checkpoint)

    async def statements_by_ids(
        self, statement_ids: Sequence[UUID]
    ) -> list[ContextCheckpointStatement]:
        if not statement_ids:
            return []
        result = await self._session.scalars(
            select(ContextCheckpointStatement)
            .where(ContextCheckpointStatement.id.in_(list(statement_ids)))
            .order_by(
                ContextCheckpointStatement.statement_type,
                ContextCheckpointStatement.ordinal,
                ContextCheckpointStatement.id,
            )
            .execution_options(populate_existing=True)
        )
        return list(result.all())

    async def delete_statements(self, statement_ids: Sequence[UUID]) -> None:
        """Whole-unit removal (handoff 13 SS 53/106): the row goes, its outgoing
        Evidence links follow by ``ON DELETE CASCADE``; surviving statements keep
        their ids and ordinals (no renumbering, SS 55)."""

        if not statement_ids:
            return
        await self._session.execute(
            delete(ContextCheckpointStatement).where(
                ContextCheckpointStatement.id.in_(list(statement_ids))
            )
        )
