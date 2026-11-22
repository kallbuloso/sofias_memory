"""Contextual Evidence resolution, validation, persistence and hydration
(SM-CTX-004, ADR-0019, Contract v1 SS 11-13/58, handoff 12).

First-party and closed (no plugin registry): one :class:``EvidenceResolver``
maps each of the six source types to its authoritative lifecycle via
:mod:``sofias_memory.domain.contextual_evidence``. It is reused by Episode
Create, Checkpoint Create/Supersede and every read (and is designed to be
reused by SM-CTX-005/006/007).

Write-path protocol (inside the owner's transaction):

1. :func:``lock_and_resolve`` -- resolve every ref with the referenced source row
   taken ``FOR SHARE``. The availability observed is then stable for that row
   until COMMIT: a concurrent Forget/Invalidate of the same source waits, so the
   derived write and the source lifecycle change serialize. No global lock.
   This runs *before* the idempotency claim, so all checkpoint/episode
   mutations keep one acquisition order: Evidence rows -> (stream advisory |
   ledger key) -> checkpoint row.

   SM-CTX-005 global lock order (handoff 13 SS 136, Critical Gate 2). Evidence
   rows are share-locked in the same order Forget and its propagation take them
   exclusively, so neither side can wait on the other in a cycle:
   ``session_entry -> memory_item -> source -> document -> chunk`` (each by id;
   Knowledge Forget locks Dataset -> Sources -> Documents -> Chunks top-down by
   id) and then Episodes by ``(rank, id)`` (:func:``episode_ranks``), the order in
   which Forget propagation exclusively locks dependent Episodes. Knowledge
   parents need no extra lock: a Knowledge Forget updates every active Document
   and Chunk of its Sources, which conflicts with the referenced row's share
   lock (the SM-CTX-004 DEFERRED ancestor-lock question, resolved).
2. (caller resolves idempotency replay first -- replay never revalidates.)
3. :func:``validate_new_support`` -- missing -> 404 ``EVIDENCE_NOT_FOUND``;
   cross-project Episode/project-MemoryItem -> 422 ``INVALID_EVIDENCE_REFERENCE``;
   not AVAILABLE -> 409 ``EVIDENCE_UNAVAILABLE`` (also under ``any_sufficient``);
   self-reference/indirect cycle -> 422 ``INVALID_EVIDENCE_CYCLE``. Any failure
   aborts the whole transaction: no owner, link or ledger row survives.
4. :func:``persist_support`` -- race-safe canonical get-or-create + owner links.

Reads compute availability from the current authoritative state on every
call (never a cached value) and never mutate any lifecycle.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from uuid import UUID

from sqlalchemy.exc import DBAPIError

from sofias_memory.api.errors import DependencyUnavailableError, SofiasMemoryError
from sofias_memory.domain import (
    EvidenceAvailability,
    EvidenceGraphCorruptionError,
    EvidenceKey,
    EvidenceSourceType,
    SupportMode,
    canonical_evidence_keys,
    chunk_availability,
    document_availability,
    episode_availability,
    episode_ranks,
    evaluate_support,
    evidence_project_matches,
    memory_item_availability,
    session_entry_availability,
    source_availability,
)
from sofias_memory.infrastructure.postgres.repositories.contextual_evidence import (
    LinkedEvidence,
)
from sofias_memory.infrastructure.postgres.unit_of_work import PostgresUnitOfWork
from sofias_memory.schemas.common import ErrorCode
from sofias_memory.schemas.contextual_evidence import (
    EvidenceResult,
    SupportRequest,
    SupportResult,
)

_TYPE_ORDER: tuple[EvidenceSourceType, ...] = tuple(
    sorted(EvidenceSourceType, key=lambda t: t.value)
)
_LOCK_TYPE_ORDER: tuple[EvidenceSourceType, ...] = (
    EvidenceSourceType.SESSION_ENTRY,
    EvidenceSourceType.MEMORY_ITEM,
    EvidenceSourceType.SOURCE,
    EvidenceSourceType.DOCUMENT,
    EvidenceSourceType.CHUNK,
)
"""Non-Episode share-lock order; Episodes always come last, by (rank, id)."""


@dataclass(frozen=True, slots=True)
class EvidenceResolution:
    """Identity/lifecycle only -- never semantic content (handoff 12 SS 47/89)."""

    key: EvidenceKey
    exists: bool
    availability: EvidenceAvailability
    scope: str | None = None


# --- errors ------------------------------------------------------------------------


def _ref_details(key: EvidenceKey) -> dict[str, str]:
    return {"source_type": key.source_type.value, "source_id": str(key.source_id)}


def evidence_not_found_error(key: EvidenceKey) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.EVIDENCE_NOT_FOUND,
        status_code=HTTPStatus.NOT_FOUND,
        message="Referenced Evidence source does not exist.",
        details=_ref_details(key),
    )


def evidence_unavailable_error(resolution: EvidenceResolution) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.EVIDENCE_UNAVAILABLE,
        status_code=HTTPStatus.CONFLICT,
        message="Referenced Evidence source cannot currently support new content.",
        details={**_ref_details(resolution.key), "availability": resolution.availability.value},
    )


def invalid_evidence_reference_error(key: EvidenceKey, reason: str) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_EVIDENCE_REFERENCE,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="Evidence reference is not valid for this owner.",
        details={**_ref_details(key), "reason": reason},
    )


def invalid_evidence_cycle_error(key: EvidenceKey) -> SofiasMemoryError:
    return SofiasMemoryError(
        code=ErrorCode.INVALID_EVIDENCE_CYCLE,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        message="Evidence dependency graph must be acyclic.",
        details=_ref_details(key),
    )


def evidence_graph_corruption_error() -> SofiasMemoryError:
    """Sanitized fail-closed error: never exposes the dependency graph."""

    return SofiasMemoryError(
        code=ErrorCode.INTERNAL_ERROR,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        message="Contextual Evidence integrity check failed.",
    )


TRANSIENT_CONFLICT_SQLSTATES = frozenset({"40P01", "40001"})
"""deadlock_detected / serialization_failure: transient, never semantic."""


def transient_sqlstate(error: DBAPIError) -> str | None:
    """The SQLSTATE when ``error`` is a deadlock/serialization failure, else None."""

    orig = error.orig
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    if state is None:
        state = getattr(getattr(orig, "__cause__", None), "sqlstate", None)
    return state if state in TRANSIENT_CONFLICT_SQLSTATES else None


def raise_if_transient_conflict(error: DBAPIError) -> None:
    """Translate a PostgreSQL deadlock/serialization failure on an Evidence-aware
    write into ``503 DEPENDENCY_UNAVAILABLE`` (safe to retry with the same
    Idempotency-Key, Contract SS 63) instead of an opaque 500. Never retried
    blindly here and never turned into Evidence unavailability (handoff 12 SS 18)."""

    if transient_sqlstate(error) is not None:
        raise DependencyUnavailableError(
            "A concurrent contextual mutation conflicted; retry with the same Idempotency-Key."
        ) from error


# --- resolver ------------------------------------------------------------------------


class EvidenceResolver:
    """Closed first-party resolver over the six v1 source types."""

    def __init__(self, uow: PostgresUnitOfWork) -> None:
        self._repo = uow.contextual_evidence

    async def resolve(
        self, keys: Iterable[EvidenceKey], *, lock: bool = False
    ) -> dict[EvidenceKey, EvidenceResolution]:
        by_type: dict[EvidenceSourceType, list[UUID]] = defaultdict(list)
        for key in keys:
            by_type[key.source_type].append(key.source_id)
        out: dict[EvidenceKey, EvidenceResolution] = {}
        if not lock:
            for source_type in _TYPE_ORDER:
                ids = sorted(set(by_type.get(source_type, [])))
                if ids:
                    out.update(await self._resolve_type(source_type, ids, lock=False))
            return out
        for source_type in _LOCK_TYPE_ORDER:  # global lock order, see module doc
            ids = sorted(set(by_type.get(source_type, [])))
            if ids:
                out.update(await self._resolve_type(source_type, ids, lock=True))
        episode_ids = sorted(set(by_type.get(EvidenceSourceType.EPISODE, [])))
        for episode_id in await self.episode_lock_order(episode_ids):
            out.update(
                await self._resolve_type(EvidenceSourceType.EPISODE, [episode_id], lock=True)
            )
        return out

    async def episode_lock_order(self, episode_ids: Sequence[UUID]) -> list[UUID]:
        """Ascending ``(rank, id)%% -- the propagation lock order."""

        if len(episode_ids) <= 1:
            return list(episode_ids)
        parents = await self._repo.episode_parent_edges(episode_ids)
        try:
            ranks = episode_ranks(episode_ids, parents)
        except EvidenceGraphCorruptionError as error:
            raise evidence_graph_corruption_error() from error
        return sorted(episode_ids, key=lambda episode_id: (ranks[episode_id], episode_id))

    async def _resolve_type(
        self, source_type: EvidenceSourceType, ids: Sequence[UUID], *, lock: bool
    ) -> dict[EvidenceKey, EvidenceResolution]:
        repo = self._repo
        out: dict[EvidenceKey, EvidenceResolution] = {}
        if source_type is EvidenceSourceType.SESSION_ENTRY:
            present = await repo.session_entries(ids, lock=lock)
            for sid in ids:
                out[EvidenceKey(source_type, sid)] = EvidenceResolution(
                    EvidenceKey(source_type, sid),
                    sid in present,
                    session_entry_availability(exists=sid in present),
                )
        elif source_type is EvidenceSourceType.MEMORY_ITEM:
            items = await repo.memory_items(ids, lock=lock)
            for sid in ids:
                lifecycle, scope = items.get(sid, (None, None))
                out[EvidenceKey(source_type, sid)] = EvidenceResolution(
                    EvidenceKey(source_type, sid),
                    sid in items,
                    memory_item_availability(lifecycle),
                    scope,
                )
        elif source_type is EvidenceSourceType.EPISODE:
            episodes = await repo.episodes(ids, lock=lock)
            for sid in ids:
                lifecycle_e, scope = episodes.get(sid, (None, None))
                out[EvidenceKey(source_type, sid)] = EvidenceResolution(
                    EvidenceKey(source_type, sid),
                    sid in episodes,
                    episode_availability(lifecycle_e),
                    scope,
                )
        elif source_type is EvidenceSourceType.SOURCE:
            sources = await repo.sources(ids, lock=lock)
            for sid in ids:
                state = sources.get(sid)
                out[EvidenceKey(source_type, sid)] = EvidenceResolution(
                    EvidenceKey(source_type, sid),
                    state is not None,
                    source_availability(
                        source_status=state.source_status if state else None,
                        dataset_status=state.dataset_status if state else None,
                    ),
                )
        elif source_type is EvidenceSourceType.DOCUMENT:
            documents = await repo.documents(ids, lock=lock)
            for sid in ids:
                state = documents.get(sid)
                out[EvidenceKey(source_type, sid)] = EvidenceResolution(
                    EvidenceKey(source_type, sid),
                    state is not None,
                    document_availability(
                        document_is_active=state.document_is_active if state else None,
                        document_generation=state.document_generation if state else None,
                        source_status=state.source_status if state else None,
                        dataset_status=state.dataset_status if state else None,
                        dataset_active_generation=(
                            state.dataset_active_generation if state else None
                        ),
                    ),
                )
        else:
            chunks = await repo.chunks(ids, lock=lock)
            for sid in ids:
                state = chunks.get(sid)
                out[EvidenceKey(source_type, sid)] = EvidenceResolution(
                    EvidenceKey(source_type, sid),
                    state is not None,
                    chunk_availability(
                        chunk_is_active=state.chunk_is_active if state else None,
                        chunk_generation=state.chunk_generation if state else None,
                        document_is_active=state.document_is_active if state else None,
                        document_generation=state.document_generation if state else None,
                        source_status=state.source_status if state else None,
                        dataset_status=state.dataset_status if state else None,
                        dataset_active_generation=(
                            state.dataset_active_generation if state else None
                        ),
                    ),
                )
        return out


# --- write path ------------------------------------------------------------------------


def all_support_keys(supports: Iterable[SupportRequest | None]) -> list[EvidenceKey]:
    keys: set[EvidenceKey] = set()
    for support in supports:
        if support is not None:
            keys.update(support.keys())
    return sorted(keys, key=lambda k: (k.source_type.value, str(k.source_id)))


async def lock_and_resolve(
    uow: PostgresUnitOfWork, keys: Sequence[EvidenceKey], *, lock: bool = True
) -> dict[EvidenceKey, EvidenceResolution]:
    if not keys:
        return {}
    return await EvidenceResolver(uow).resolve(keys, lock=lock)


async def validate_new_support(
    uow: PostgresUnitOfWork,
    *,
    owner_scope: str,
    supports: Iterable[SupportRequest | None],
    resolutions: Mapping[EvidenceKey, EvidenceResolution],
    owner_episode_id: UUID | None = None,
) -> None:
    """Deterministic validation (canonical order) for new derived content."""

    keys = all_support_keys(supports)
    for key in keys:
        if owner_episode_id is not None and key == EvidenceKey(
            EvidenceSourceType.EPISODE, owner_episode_id
        ):
            raise invalid_evidence_cycle_error(key)
    for key in keys:
        resolution = resolutions[key]
        if not resolution.exists:
            raise evidence_not_found_error(key)
        if not evidence_project_matches(
            owner_scope=owner_scope, source_type=key.source_type, source_scope=resolution.scope
        ):
            raise invalid_evidence_reference_error(key, "Evidence belongs to another Project Scope")
        if resolution.availability is not EvidenceAvailability.AVAILABLE:
            raise evidence_unavailable_error(resolution)
    if owner_episode_id is not None:
        episode_refs = [k.source_id for k in keys if k.source_type is EvidenceSourceType.EPISODE]
        if episode_refs and await uow.contextual_evidence.episode_reaches(
            episode_refs, owner_episode_id
        ):
            raise invalid_evidence_cycle_error(
                EvidenceKey(EvidenceSourceType.EPISODE, owner_episode_id)
            )


async def persist_episode_support(
    uow: PostgresUnitOfWork, episode_id: UUID, support: SupportRequest
) -> None:
    keys = support.canonical_keys()
    ref_ids = await uow.contextual_evidence.get_or_create(keys)
    await uow.contextual_evidence.add_episode_links(episode_id, [ref_ids[k] for k in keys])


async def persist_statement_support(
    uow: PostgresUnitOfWork, supports: Mapping[UUID, SupportRequest]
) -> None:
    keys = all_support_keys(supports.values())
    ref_ids = await uow.contextual_evidence.get_or_create(keys)
    links = [
        (statement_id, ref_ids[key])
        for statement_id, support in supports.items()
        for key in support.canonical_keys()
    ]
    await uow.contextual_evidence.add_statement_links(links)


# --- read path ---------------------------------------------------------------------------


def _support_results(
    links: Sequence[LinkedEvidence],
    modes: Mapping[UUID, SupportMode | None],
    resolutions: Mapping[EvidenceKey, EvidenceResolution],
) -> dict[UUID, SupportResult | None]:
    grouped: dict[UUID, list[LinkedEvidence]] = defaultdict(list)
    for link in links:
        grouped[link.owner_id].append(link)
    out: dict[UUID, SupportResult | None] = {}
    for owner_id, mode in modes.items():
        owner_links = grouped.get(owner_id, [])
        if mode is None or not owner_links:
            out[owner_id] = None
            continue
        ordered = sorted(
            owner_links, key=lambda link: (link.key.source_type.value, str(link.key.source_id))
        )
        out[owner_id] = SupportResult(
            mode=mode,
            evidence=[
                EvidenceResult(
                    evidence_id=link.evidence_ref_id,
                    source_type=link.key.source_type,
                    source_id=link.key.source_id,
                    availability=resolutions[link.key].availability,
                )
                for link in ordered
            ],
        )
    return out


async def hydrate_episode_support(
    uow: PostgresUnitOfWork, modes: Mapping[UUID, SupportMode | None]
) -> dict[UUID, SupportResult | None]:
    """Current availability, computed now; never mutates any lifecycle."""

    supported = [owner for owner, mode in modes.items() if mode is not None]
    links = await uow.contextual_evidence.list_episode_links(supported)
    resolutions = await lock_and_resolve(uow, [link.key for link in links], lock=False)
    return _support_results(links, modes, resolutions)


async def hydrate_statement_support(
    uow: PostgresUnitOfWork, modes: Mapping[UUID, SupportMode | None]
) -> dict[UUID, SupportResult | None]:
    supported = [owner for owner, mode in modes.items() if mode is not None]
    links = await uow.contextual_evidence.list_statement_links(supported)
    resolutions = await lock_and_resolve(uow, [link.key for link in links], lock=False)
    return _support_results(links, modes, resolutions)


def is_supported(support: SupportResult | None) -> bool | None:
    """Internal deterministic sufficiency (reused by SM-CTX-005+). ``None`` means
    no declared support (independent assertion), never "unsupported"."""

    if support is None:
        return None
    return evaluate_support(support.mode, [e.availability for e in support.evidence])


def canonical_keys_or_empty(support: SupportRequest | None) -> list[EvidenceKey]:
    return [] if support is None else canonical_evidence_keys(support.keys())
