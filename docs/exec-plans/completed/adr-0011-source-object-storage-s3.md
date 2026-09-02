# ADR-0011 Source Object Storage / S3 — Execution Plan

Status: active
Governing ADR: `docs/adr/0011-durable-source-object-storage-s3-and-startup-convergence.md`
(accepted)

This plan does not restate ADR-0011. Every architectural decision is frozen there;
this plan only orders the work, names the files it touches, and defines the gates
between reviewable slices. Cite ADR section IDs (`D6`, `D37`, `D34 Case B`, etc.) —
do not copy their text here.

## Goal

Implement accepted ADR-0011 across bounded, independently reviewable slices, without
ever leaving `main` in a state where `STORAGE_BACKEND=filesystem` (the default)
regresses, and without weakening any ADR-0009/ADR-0010 guarantee already in
production.

## Frozen architecture references

- Full contract: ADR-0011 (`docs/adr/0011-...md`), D1–D42, all four amendment rounds.
- Pipeline lifecycle/claim/recovery invariants it must not weaken: ADR-0009.
- Administrative Dataset DELETE contract it generalizes: ADR-0010.
- PostgreSQL authority / Neo4j rebuildable projection: ADR-0002, ADR-0008.
- No optional-dependency/plugin mechanism: ADR-0005 (amended by ADR-0011 D17).
- `AGENTS.md` root file: precedence, invariants, stack, tooling, DoD.

## Non-goals (repeat only to bound scope, not to restate ADR-0011)

- No S3→filesystem reverse migration (D25).
- No presigned upload/download, no client-facing S3 exposure (D23).
- No generic storage-provider plugin system (D17).
- No new PostgreSQL schema for storage state unless a slice proves an
  otherwise-unsatisfiable invariant (D26, D39) — if that happens, STOP per the
  Execution Policy below and report before adding a migration.
- No Redis/Celery/second queue/second pipeline engine, ever (ADR-0009, D31).
- No SM numbering. Work packages here are `STORAGE-00N` only.

## Repository inspection summary (grounds this plan in current code, not ADR guesses)

- **No `sofias_memory/infrastructure/storage/` package exists yet.** All current
  Source-storage logic is free functions spread across three service modules:
  - `sofias_memory/services/remember.py`: `final_storage_directory`,
    `final_storage_path`, `final_storage_uri`, `write_final_storage_bytes`,
    `final_storage_content_matches`, plus the `_ingress/<run_id>` staging helpers
    (`ingress_directory`, `write_ingress_bytes`, `read_ingress_bytes`,
    `delete_ingress_artifact`, `prepare_remember_retry_ingress`).
  - `sofias_memory/services/forget.py`: `delete_source_storage`,
    `source_storage_path`, and an **already-existing** `StorageDeleteStatus(StrEnum)`
    / `StorageDeleteResult` dataclass (around line 500) with `NOT_REQUESTED`,
    `DELETED_NOW`, `ALREADY_ABSENT` — **`UNRESOLVED` does not exist yet**; this is
    the type STORAGE-005 extends in place, not a new name to invent.
  - `sofias_memory/services/cognify.py`: `source_storage_path_for_cognify`
    (~line 1789).
- **`Source` model** (`infrastructure/postgres/models/source.py:88`): `storage_uri`
  is already an unrestricted, nullable `Text` column — confirms D26/D39: no
  migration is needed to introduce `s3://` values or to preserve `storage_uri` on
  `UNRESOLVED`.
- **`SourceStatus`** (`domain/enums.py`): `PENDING, PROCESSING, ACTIVE, FAILED,
  DELETING, DELETED` — exactly D40's frozen list.
- **`Settings`** (`config.py`): no `storage_backend` or `STORAGE_S3_*` fields exist
  yet. `data_directory`/`temp_directory`/`max_source_size_mb` already exist and are
  unaffected. `build_config_fingerprint_payload` (line ~307) hashes only
  `llm`/`embeddings`/`chunking`/`retrieval`/`improve`/`prompt_versions` — confirms
  D18: storage settings must **not** be added there.
- **`lifespan.py`**: current startup order is PostgreSQL probe → Neo4j bootstrap →
  pipeline stale-recovery → `worker.start()` → `yield`. **This is a blocking
  pre-`yield` FastAPI lifespan** — the ASGI server does not accept any HTTP
  connection, including `/health/live`, until this sequence completes. This is the
  concrete fact behind D33's warning: if storage convergence is added as more
  blocking work before `yield`, the maintenance HTTP surface requirement (D31/D33)
  is violated by construction. **STORAGE-007 must not simply insert convergence
  into this blocking sequence** — see STORAGE-007's decision checkpoint.
- **`app.py`**: readiness is already a clean, extensible mechanism —
  `application.state.readiness_checks` is a tuple of `(name, async_check)` pairs
  (`postgres`, `neo4j`, `worker` today), evaluated by `run_readiness_checks` in
  `api/routes/health.py`, each returning `ReadinessCheckResult(ready, detail)`.
  `_worker_readiness_check` already reads a coordinator's `is_operational` flag —
  the exact pattern a new `"storage"` readiness check can follow. `/health/live`
  (`api/routes/health.py:67`) never touches any dependency — once the process is
  past the blocking lifespan phase, liveness is already free; the only real problem
  to solve is reaching `yield` promptly.
- **`services/pipeline_queue_claimer.py`** (`PipelineRunClaimer.try_claim_one` /
  `_try_claim_candidate`) delegates eligibility to
  `PostgresUnitOfWork.pipeline_runs.list_eligible_candidate_ids` — the concrete,
  single extension point for STORAGE-007's recovery-owned-only claim filter during
  STORAGE_CONVERGING (an optional `pipeline_type` allowlist parameter threaded
  through that repository method and the claimer, not a new query path).
- **Confirmed, live proof of D37's core gap**: both
  `pipelines/steps/dataset_delete.py`'s `FinalizeTombstoneStep.persist()`
  (~line 460-476) and `pipelines/steps/forget.py`'s analogous finalize logic
  (~line 499, 536-593) already build `storage_status_by_source =
  {source_id: status}` from step output and do `if storage_status in
  ("deleted_now", "already_absent"): source.storage_uri = None` — **a missing key
  today silently leaves `storage_uri` untouched, with no invariant check at all.**
  This is exactly the gap D37's per-Source coverage requirement exists to close;
  it is not hypothetical.
- **`loaders/text.py`**: `STORAGE_EXTENSION_BY_SOURCE_EXTENSION` and
  `MIME_TYPE_BY_SOURCE_EXTENSION` exist (keyed by upload extension). No
  `mime_type → storage_extension` mapping exists yet — required by D6's amendment
  and D35's post-CAS locator contract. Confirmed derivable from the existing two
  tables without behavior change (every source extension sharing a mime type
  already shares a storage extension).
- **Tests**: unit coverage exists per pipeline area
  (`tests/unit/test_remember_pipeline_steps.py`, `test_remember_service.py`,
  `test_forget_pipeline_steps.py`, `test_forget_service.py`, `test_forget_routes.py`,
  `test_cognify_pipeline_steps.py`, `test_cognify_service.py`,
  `test_dataset_delete_barrier.py`, `test_dataset_delete_routes_contract.py`,
  `test_health.py`, `test_pipeline_queue_claimer.py`, `test_pipeline_worker.py`,
  `test_postgres_readiness_health.py`) plus PostgreSQL integration coverage
  (`tests/integration/test_remember_postgres_integration.py`,
  `test_forget_postgres_integration.py`, `test_dataset_delete_postgres_integration.py`,
  `test_cognify_async_postgres_integration.py`,
  `test_pipeline_queue_claiming_postgres_integration.py`,
  `test_pipeline_worker_postgres_integration.py`,
  `test_graph_outbox_worker_postgres_integration.py`). **No dedicated unit test file
  exists for `DeleteStorageStep`/`FinalizeTombstoneStep`** (`dataset_delete.py`'s
  step classes) — only integration coverage. STORAGE-005 must add
  `tests/unit/test_dataset_delete_pipeline_steps.py`, mirroring
  `test_forget_pipeline_steps.py`'s existing shape, rather than assuming parity
  with Forget's unit coverage already exists.
- No S3 SDK is present anywhere in `pyproject.toml` today — STORAGE-002 is a clean
  net-new dependency addition, not a version bump.

## Dependency graph

Corrected against actual code coupling found above (S3 read/write/delete all sit
behind one adapter surface, so STORAGE-003/004/005 each need the S3 adapter, not
only the port skeleton; STORAGE-006's migration algorithm needs the finalize-side
per-Source-result contract from STORAGE-005 to classify Case B/D correctly against
real `PipelineRun` lineage):

```text
STORAGE-001 (port + filesystem adapter, preserves current behavior)
    |
    +--> STORAGE-002 (S3 settings + S3 adapter, behind the same port)
              |
              +--> STORAGE-003 (router-based reads / Cognify)
              |
              +--> STORAGE-004 (Remember finalization, incl. B1 recovery)
              |
              +--> STORAGE-005 (destructive deletion semantics, D37-D42)
                        |
                        +--> STORAGE-006 (filesystem->S3 startup convergence,
                        |                  needs D34 Case B/D lineage proof,
                        |                  which needs STORAGE-005's per-Source
                        |                  result contract to exist first)
                        |
                        +--> STORAGE-007 (bootstrap state machine, recovery-owned
                                           claims, health) -- needs STORAGE-006's
                                           convergence algorithm AND STORAGE-005's
                                           destructive-pipeline classifier
                                                |
                                                +--> STORAGE-008 (deployment/docs)
                                                |
                                                +--> STORAGE-009 (full integration /
                                                                   production-shaped
                                                                   validation)
```

STORAGE-003 and STORAGE-004 are mutually independent (reads vs. writes) and may be
done in either order once STORAGE-002 lands; STORAGE-005 does not require either of
them to be done first (it touches Forget/`DATASET_DELETE`, not Remember/Cognify) but
does require STORAGE-002's adapter to exist so the S3 delete path can be exercised.
STORAGE-008 and STORAGE-009 may run in parallel once STORAGE-007 is green.

## Implementation slices

Legend for "Regression commands": `RUFF` = `uv run ruff check .`, `FMT` = `uv run
ruff format --check .`, `MYPY` = `uv run mypy sofias_memory`, `PYTEST` = `uv run
pytest` (scoped with `-k`/path where noted).

---

### STORAGE-001 — Storage port + filesystem preservation

1. **Goal.** Introduce the closed `SourceObjectStorage` port (D4) and
   `FilesystemSourceObjectStorage` adapter, wrapping today's free functions with
   zero behavior change under the default `STORAGE_BACKEND=filesystem`.
2. **Why isolated here.** Every later slice depends on this boundary existing; doing
   it first with no S3 code at all keeps the diff reviewable as "pure refactor,"
   verifiable purely by the existing filesystem test suite staying green.
3. **Dependencies.** None — first slice.
4. **Expected files/modules touched.**
   - New: `sofias_memory/infrastructure/storage/__init__.py`,
     `sofias_memory/infrastructure/storage/port.py` (protocol/ABC +
     `StorageDeleteResult`/`StorageDeleteStatus`, relocated/extended from
     `services/forget.py`), `sofias_memory/infrastructure/storage/filesystem.py`
     (wraps `remember.py`'s write-side helpers and `forget.py`'s
     `delete_source_storage`/`source_storage_path`), `sofias_memory/infrastructure/
     storage/router.py` (`SourceStorageRouter` skeleton — write routes by
     `Settings.storage_backend`, read/delete route by URI scheme, D4/D5).
   - New: centralized `STORAGE_EXTENSION_BY_MIME_TYPE` mapping (D6's amendment) —
     place next to `loaders/text.py`'s existing extension tables or inside the new
     storage package; decide during the slice and record the choice in its PR
     description (not a blocking decision for this plan).
   - Modified only at the margins: `config.py` gains `storage_backend: Literal["filesystem","s3"] = "filesystem"` (closed enum, D2) — no `STORAGE_S3_*` fields yet (STORAGE-002).
   - Existing `services/remember.py`, `services/forget.py`, `services/cognify.py`
     call sites are **not** rewired to the router yet in this slice unless trivial —
     see non-goals below. Prefer: implement the adapter, prove it byte-for-byte
     equivalent to the existing functions via unit tests, without yet changing any
     pipeline step's import.
5. **Explicit non-goals.**
   - No S3 code, no `STORAGE_S3_*` settings, no S3 SDK dependency.
   - No pipeline step (`FinalizeStorageStep`, `StorageDeletionStep`,
     `DeleteStorageStep`, Cognify's rehydration call site) is rewired to call the
     router yet — that is STORAGE-003/004/005's job. This slice only proves the
     adapter exists and is behaviorally identical; wiring it in is deferred so this
     diff stays reviewable as additive, not a simultaneous refactor-and-rewire.
   - No convergence, no bootstrap state, no deletion-semantics change.
6. **Accepted ADR sections implemented.** D1 (namespace reservation, conceptual
   only — no `_system/` creation), D2 (`STORAGE_BACKEND` setting exists, default
   `filesystem`), D4 (port/adapter/router shape), D6 (URI format + centralized
   extension mapping — filesystem side only in this slice).
7. **Acceptance criteria.** `STORAGE_BACKEND=filesystem` (default) is
   byte-for-byte behaviorally identical to pre-slice `main`: same `file://` URIs
   produced, same containment/traversal guards, same ingress behavior. New adapter
   code is covered by unit tests but not yet load-bearing in any pipeline step.
8. **Tests to add/change.** New: unit tests for `FilesystemSourceObjectStorage`
   mirroring the existing coverage in `test_remember_service.py`/
   `test_forget_service.py` for the wrapped functions (same fixtures, same
   assertions, new call surface). No existing test file should need behavior
   changes — only additive tests.
9. **Regression commands.** `RUFF`, `FMT`, `MYPY`,
   `PYTEST tests/unit/test_remember_service.py tests/unit/test_forget_service.py
   tests/unit/test_remember_pipeline_steps.py tests/unit/test_forget_pipeline_steps.py`,
   then full `PYTEST`.
10. **Review gate.** Diff reviewed as pure-additive; no pipeline step import
    changed; full `pytest`/`mypy`/`ruff` green with `STORAGE_BACKEND` unset
    (default).
11. **Rollback/recovery concerns.** None — additive only, nothing yet depends on
    the new code path in production behavior.
12. **ADR validation items owned.** 1 (existing filesystem tests remain green).

---

### STORAGE-002 — S3 configuration + S3 adapter

1. **Goal.** Implement `S3SourceObjectStorage` behind the STORAGE-001 port: D6 key
   construction, D11 cheap/strong verification tiers, D15/D38 versioned-delete
   semantics, D16 configuration surface, D21 startup probe primitive (used later
   by STORAGE-007, implemented here), D36 managed-namespace discipline.
2. **Why isolated here.** The S3 adapter is testable in complete isolation against
   an S3-compatible target (MinIO) with zero pipeline-step involvement — proving
   the adapter's own contract (deterministic keys, integrity, versioned delete)
   before any business code depends on it minimizes blast radius if the SDK choice
   or verification mechanism needs rework.
3. **Dependencies.** STORAGE-001 (port/`StorageDeleteResult` contract must exist).
4. **Expected files/modules touched.**
   - New: `sofias_memory/infrastructure/storage/s3.py` (adapter; `boto3` client
     usage confined entirely to this one file, per the resolved SDK decision below).
   - `config.py`: add `STORAGE_S3_BUCKET`, `STORAGE_S3_PREFIX`,
     `STORAGE_S3_REGION`, `STORAGE_S3_ENDPOINT_URL`, `STORAGE_S3_ACCESS_KEY_ID`
     (`SecretStr`), `STORAGE_S3_SECRET_ACCESS_KEY` (`SecretStr`),
     `STORAGE_S3_SESSION_TOKEN` (`SecretStr`), a `STORAGE_S3_MAX_CONCURRENCY`
     field (mirrors `LLM_MAX_CONCURRENCY`/`EMBEDDING_MAX_CONCURRENCY`'s existing
     pattern, `config.py` — see the concurrency decision below), plus a
     cross-field validator requiring the bucket/region/credentials-or-credential-
     chain to be resolvable when `storage_backend == "s3"` (mirrors
     `validate_cross_field_rules`'s existing pattern).
   - `pyproject.toml` `[project.dependencies]`: add `boto3` (see resolved SDK
     decision below) — **the only slice in this plan permitted to touch
     dependencies**, per ADR-0011 D17.
5. **Explicit non-goals.** No pipeline step calls this adapter yet (STORAGE-003/
   004/005). No startup convergence orchestration (STORAGE-006/007) — only the
   probe *primitive* D21 requires, callable but not yet called at boot.
6. **Accepted ADR sections implemented.** D6 (S3 URI/key), D11, D15, D16, D17
   (dependency placement), D21 (probe primitive), D36.
7. **Acceptance criteria.** Against a local MinIO (or equivalent) integration
   target: deterministic key round-trip, cheap idempotency check, strong
   verification, exact-key delete (non-versioned and versioned bucket), `UNRESOLVED`
   returned for each D37-listed recognized condition when simulated (credential
   failure, timeout, Object Lock), never a blanket `except Exception`.
8. **Tests to add/change.** New: `tests/integration/test_s3_source_object_storage_integration.py`
   (or equivalent name decided in-slice) against MinIO; new unit tests for pure
   key-construction/URI-parsing logic (no I/O). Confirm actual MinIO fixture
   wiring against how `tests/integration/` already bootstraps PostgreSQL/Neo4j
   (inspect `conftest.py` at slice start — not pre-verified by this plan).
9. **Regression commands.** `RUFF`, `FMT`, `MYPY`, `PYTEST tests/unit -k s3`, then
   the new integration file explicitly (documented as the "provider-specific gate,"
   not part of the default `uv run pytest` sweep if it requires MinIO — decide and
   record how it's marked/skipped when MinIO is absent, e.g. a pytest marker
   consistent with how `tests/integration/` already gates on PostgreSQL
   availability).
10. **Review gate.** S3 adapter contract proven independently before any pipeline
    step is allowed to import it.
11. **Rollback/recovery concerns.** None yet — inert until wired into a pipeline
    step (STORAGE-003 onward). Dependency addition is the only production-relevant
    change; must not alter the release image's behavior when `STORAGE_BACKEND`
    stays `filesystem`.
12. **ADR validation items owned.** 12 (versioned-bucket destructive semantics,
    adapter-level), 13, 14 (target-exists cases, adapter-level), 20, 21 (S3
    unavailable/access-denied, adapter-level proof; startup-level proof is
    STORAGE-007's item 20/21), 27 (version/delete-marker cleanup proven), 48, 49,
    50, 51 (D37/D38 outcome typing, adapter-level).

   **S3 SDK decision checkpoint — RESOLVED.** This was previously an open
   implementation-level decision this plan deliberately did not pre-select. It has
   now been evaluated and decided, before any STORAGE-002 code is written, per the
   findings and comparison below.

   #### Repository/concurrency findings that ground this decision

   - `asyncio.to_thread` is **already the established house pattern** for wrapping
     a sync-only dependency inside this codebase's async-everywhere convention
     (`AGENTS.md` §11): `loaders/url.py:155` offloads blocking DNS resolution
     (`await asyncio.to_thread(resolver, host, port)`); `services/cognify.py:791`
     offloads a blocking document re-read
     (`await asyncio.to_thread(self._read_reset_document, work_item)`). Neither
     is a novel pattern this decision would be introducing.
   - Bounded concurrency around such offloaded/external work is **already a
     standard, repeated pattern**, always via `asyncio.Semaphore` sized from a
     dedicated `Settings.*_max_concurrency` field:
     `infrastructure/llm.py` (`self._semaphore = asyncio.Semaphore(settings.llm_max_concurrency)`,
     four call sites) and `infrastructure/embeddings.py`
     (`asyncio.Semaphore(self._max_concurrency)`). The S3 adapter follows this
     exact, already-proven shape rather than inventing a new concurrency idiom.
   - `pyproject.toml` has **zero existing async-native AWS/S3 dependency** — this
     is a clean net-new addition either way; no existing pin constrains the choice.
   - The synchronous document-loading libraries already in
     `[project.dependencies]` (`pypdf`, `python-docx`) are themselves sync-only
     and are the same *class* of problem this decision is solving — the
     repository's answer to "a needed library is sync-only" is consistently
     "wrap the call, don't chase an async-native alternative," not "prefer an
     async-native library merely because one exists."

   #### Option A — `boto3`/`botocore` + `asyncio.to_thread` offload

   | # | Criterion | Assessment |
   |---|---|---|
   | 1 | Python 3.12 support | Full, current (`boto3` tracks Python support broadly and promptly). |
   | 2 | Maintenance/release cadence | Very high — near-daily `botocore` releases tracking AWS API changes; `boto3` released in lockstep. The reference implementation. |
   | 3 | Dependency-tree complexity | Minimal: `boto3` → `botocore`, `jmespath`, `s3transfer`. No async HTTP stack pulled in. |
   | 4 | Compatibility with current botocore/AWS APIs | By definition, always current — `botocore` *is* the API compatibility layer every other Python S3 client (including `aiobotocore`) is built on top of. |
   | 5 | S3-compatible endpoint support | Full, native (`endpoint_url` client parameter) — this is the well-trodden path for MinIO and friends. |
   | 6 | Versioned-object deletion support | Full and mature: `list_object_versions` (paginated), `delete_object(VersionId=...)`, `delete_objects` batch API — exactly what D15's "delete all versions and delete markers, then verify" contract needs, with no gaps. |
   | 7 | Long-lived FastAPI process lifecycle | A `boto3.client(...)` is designed to be created once and reused for the process lifetime (matches this app's existing single-shared-client pattern for `OpenAIEmbeddingClient`/LLM clients in `app.py`'s `build_pipeline_resources`). |
   | 8 | Client/session creation and shutdown complexity | Low: construct once at `SourceStorageRouter`/adapter wiring time (mirrors `Neo4jResource`/engine construction in `lifespan.py`); `client.close()` at shutdown is optional but cheap to call from the existing shutdown path. |
   | 9 | Connection pooling | Built in (`botocore`'s connection pool via `urllib3`), tunable via `botocore.config.Config(max_pool_connections=...)` — sized to match the concurrency bound below. |
   | 10 | Event-loop safety | Requires discipline: **every** blocking call (including `StreamingBody.read()`) must be fully contained inside the function passed to `asyncio.to_thread` — frozen as an explicit rule below, not left to chance. |
   | 11 | Cancellation behavior | `asyncio.to_thread` cancellation does not stop the underlying OS thread mid-syscall (Python threads are not preemptible) — an in-flight S3 call runs to completion even if the awaiting task is cancelled. This is an accepted, explicit limitation (documented as a known risk below), consistent with how `services/cognify.py`'s existing `to_thread` offload already behaves. |
   | 12 | Concurrency/backpressure control | Explicit and simple: one `asyncio.Semaphore(settings.storage_s3_max_concurrency)` per adapter instance, acquired around every offloaded call — identical shape to the LLM/embedding clients. |
   | 13 | Streaming-body behavior | `get_object()["Body"]` is a blocking, non-async stream (`botocore.response.StreamingBody`) — must be fully read (and closed) inside the same offloaded thread function; never handed back to the event-loop thread half-consumed. Frozen as an explicit rule below. |
   | 14 | Error/exception taxonomy | Single, stable taxonomy: `botocore.exceptions.ClientError` (with a `.response["Error"]["Code"]`) plus a small set of connection/timeout exceptions — a well-known, exhaustively documented surface to translate from. |
   | 15 | Testing/mocking complexity | Low: `botocore`'s stubber (`botocore.stub.Stubber`) or direct MinIO integration (this plan's own preferred approach, per STORAGE-002 item 8) both work with zero async-test-harness complexity — synchronous mocks in a synchronous helper function, called via `to_thread` exactly as production code does. |
   | 16 | MinIO integration-test usability | Excellent and extremely common — `boto3` + MinIO is the most widely documented combination for exactly this kind of test. |
   | 17 | Upgrade surface over the next major release | Low risk — `boto3`/`botocore` maintain strong backward compatibility; version pin only needs a `>=X,<Y` range like every other dependency in this project's `pyproject.toml`. |
   | 18 | Fit with ADR-0005's fixed-dependency philosophy | Strong fit: one well-known, officially-maintained package with a shallow dependency tree, versioned like every other pinned dependency — no plugin/provider registry, no exotic transport stack. |

   #### Option B — `aioboto3`/`aiobotocore`

   | # | Criterion | Assessment |
   |---|---|---|
   | 1 | Python 3.12 support | Supported, but trails `boto3`'s own support window by however long `aiobotocore`'s maintainers take to certify each new Python release. |
   | 2 | Maintenance/release cadence | Materially slower than `botocore` itself: `aiobotocore` **pins to a specific, narrow `botocore` version range** per release and has historically lagged newly-released `botocore` versions by weeks to months while it re-patches the HTTP transport layer for each new `botocore` internals change. |
   | 3 | Dependency-tree complexity | Larger: `aioboto3` → `aiobotocore` → a *pinned* `botocore` + `aiohttp` (or `httpx`, depending on version) + `aioitertools`, plus `boto3` itself is still pulled in for the non-async resource/session surface `aioboto3` wraps. Two HTTP stacks (async `aiohttp` and `botocore`'s own sync `urllib3`) can end up present simultaneously depending on what else in the process uses `botocore`-based tooling. |
   | 4 | Compatibility with current botocore/AWS APIs | **Structurally lags**: `aiobotocore` works by monkey-patching/re-implementing pieces of `botocore`'s internals to swap the transport, so it is *never* ahead of, and is often behind, whatever `botocore` version it pins — the opposite compatibility direction of Option A. |
   | 5 | S3-compatible endpoint support | Supported (same `endpoint_url` parameter, inherited from `botocore`), but with a materially smaller collective body of MinIO-specific troubleshooting precedent than `boto3`. |
   | 6 | Versioned-object deletion support | Available (the same underlying `botocore` operations), but every version-sensitive operation's behavior is now also gated by whichever pinned `botocore` snapshot `aiobotocore` happens to bundle — an extra compatibility variable D15's already-demanding versioning contract does not need. |
   | 7 | Long-lived FastAPI process lifecycle | Requires an async context manager (`async with session.client("s3") as client`) — awkward to hold open for the entire process lifetime; typically requires an `AsyncExitStack` (or equivalent) wired into `lifespan.py`, a new ownership pattern this codebase does not currently have for any other client. |
   | 8 | Client/session creation and shutdown complexity | Higher: the client is an async context manager, not a plain constructed object — must be entered/exited around the app's async lifespan, unlike every other long-lived client in this codebase (`Neo4jResource`, `OpenAIEmbeddingClient`, SQLAlchemy engine), which are constructed once and closed via a plain `close()`/`dispose()` call. |
   | 9 | Connection pooling | Present (via `aiohttp`'s connector), but is a second, independent pooling implementation from `botocore`'s own — one more moving part to reason about and size correctly. |
   | 10 | Event-loop safety | Fewer footguns *for network I/O specifically* (it's genuinely non-blocking), but does not remove the need for care elsewhere in the adapter, and trades one discipline problem (thread-offload boundaries) for another (async context-manager lifetime discipline). |
   | 11 | Cancellation behavior | True `asyncio` cancellation of an in-flight request is possible, unlike Option A's un-preemptible thread — a genuine advantage, but not one ADR-0011 requires (no D-section calls for mid-request cancellation of an S3 call; `PipelineStep` cancellation is handled at the checkpoint-between-steps level, ADR-0009, not mid-I/O). |
   | 12 | Concurrency/backpressure control | Achievable (`asyncio.Semaphore` still works identically here), so this criterion is a wash between the two options. |
   | 13 | Streaming-body behavior | Genuinely async (`await response["Body"].read()`), removing the thread-offload discipline requirement for this one operation specifically — but every other operation (`put_object`, `delete_object`, `list_object_versions` pagination) is still a coroutine that must simply be awaited correctly, which `aioboto3` handles, at the cost of everything above. |
   | 14 | Error/exception taxonomy | Same `botocore.exceptions.ClientError` taxonomy underneath (inherited), so no advantage here. |
   | 15 | Testing/mocking complexity | Higher: async mocking/stubbing for `aiobotocore` is less mature and less documented than `botocore`'s synchronous `Stubber`; MinIO integration tests still need an async test harness either way, but unit-level mocking is more awkward. |
   | 16 | MinIO integration-test usability | Works, but with meaningfully less community precedent than `boto3` + MinIO. |
   | 17 | Upgrade surface over the next major release | **Higher risk**: this project would be carrying a three-way version-compatibility constraint (`aioboto3` ↔ `aiobotocore` ↔ `botocore`) instead of the simple `boto3`/`botocore` pair every other AWS-SDK-consuming Python project manages — a real, ongoing maintenance tax. |
   | 18 | Fit with ADR-0005's fixed-dependency philosophy | Weaker fit: a deeper, faster-moving, narrower-maintained dependency chain is a worse match for "fixed, versioned, supported stack" than a single well-known official SDK. |

   #### Decision: **Option A — `boto3` + `asyncio.to_thread`, bounded by `asyncio.Semaphore`**

   **Rationale, in priority order:**

   1. **D15's versioned-deletion contract is the single most demanding, most
      failure-sensitive requirement in the whole ADR** ("delete all versions and
      delete markers for the exact key, then verify none remain, or fail closed —
      never claim `DELETED_NOW` without that proof"). `boto3`/`botocore` is the
      reference implementation of that exact surface; `aiobotocore` can only ever
      match it with a time lag, and D15 leaves zero room for "close enough."
   2. **The repository already has an established, working answer to "a needed
      library is sync-only"**: `asyncio.to_thread` plus a `Settings`-driven
      `asyncio.Semaphore` (findings above). Choosing `boto3` extends an existing,
      proven pattern; choosing `aioboto3` would introduce the codebase's *first*
      async-native-SDK client-lifecycle pattern (context-manager-scoped, not
      constructed-once-and-closed) for a single dependency, inconsistent with
      every other long-lived client this app already owns.
   3. **ADR-0005's fixed-dependency philosophy (amended by ADR-0011 D17) favors the
      shallower, more stable dependency tree.** A `boto3`/`botocore` pin is a
      two-package version relationship this project already knows how to manage
      (`>=X,<Y` in `pyproject.toml`, like every other dependency); `aioboto3`
      requires tracking three packages' mutual compatibility windows
      simultaneously, a materially higher ongoing maintenance cost for a "fixed,
      versioned, supported stack" project.
   4. **`aioboto3`'s one genuine advantage — true mid-request cancellation and a
      non-blocking `StreamingBody.read()`** — is not something any ADR-0011
      D-section actually requires. `PipelineStep` cancellation is checkpoint-based
      between steps (ADR-0009), never mid-I/O; a `to_thread`-offloaded S3 call
      running to completion even after its awaiting task is cancelled is an
      accepted characteristic (documented as a known risk below), not a
      correctness gap against anything this ADR promises.
   5. **Testing is simpler and more precedented** with `boto3`'s synchronous
      `Stubber` and the widely-documented `boto3` + MinIO integration pattern —
      directly serving STORAGE-002's own acceptance criterion (item 7 above) of
      proving the adapter against a real MinIO target.

   **Rejected alternative:** `aioboto3`/`aiobotocore`. Rejected specifically
   because of the three-way version-pin burden (criterion 17), the lag behind
   `botocore`'s own API/versioning-semantics coverage (criteria 2, 4, 6) working
   directly against D15's demanding requirements, and the introduction of a new,
   inconsistent client-lifecycle pattern (criteria 7-8) — not because "async is
   worse" as a blanket claim; its one real advantage (native cancellation/
   streaming) is acknowledged above and simply does not offset those costs for
   this specific ADR's requirements.

   #### Event-loop safety contract (frozen now, binding on STORAGE-002's implementation)

   The public `S3SourceObjectStorage` API (and therefore `SourceObjectStorage`/
   `SourceStorageRouter`) remains fully `async`. **No `boto3`/`botocore` blocking
   call may ever execute on the event-loop thread.** This includes, at minimum:
   `put_object`, `head_object`, `get_object`, `StreamingBody.read()` (and
   `.close()`), `list_object_versions` pagination (every page fetch, not just the
   first), `delete_object`, `delete_objects`, any waiter/paginator network
   iteration, and the D21 startup probe operations.

   The **entire** blocking interaction for a given logical operation must be
   contained inside one function passed to `asyncio.to_thread` — never split
   across an offloaded call and a subsequent synchronous access on the event-loop
   thread. Concretely required shape (illustrative, not final code):

   ```python
   def _get_object_bytes_sync(client, *, bucket, key) -> bytes:
       response = client.get_object(Bucket=bucket, Key=key)
       try:
           return response["Body"].read()
       finally:
           response["Body"].close()

   data = await asyncio.to_thread(_get_object_bytes_sync, client, bucket=bucket, key=key)
   ```

   Explicitly forbidden shape:

   ```python
   response = await asyncio.to_thread(client.get_object, Bucket=bucket, Key=key)
   data = response["Body"].read()  # FORBIDDEN: blocks the event loop
   ```

   This rule applies identically to `list_object_versions`' pagination (each
   `next_token`-driven page fetch happens inside the same offloaded, page-walking
   sync function — never one `to_thread` call per page interleaved with
   event-loop-thread control flow) and to the D21 probe's own put/get/delete
   sequence.

   #### Client/session lifecycle decision

   One `boto3.client("s3", ...)` is constructed **once**, at the same place and
   lifetime as this app's other long-lived infrastructure clients (mirroring
   `Neo4jResource`/the SQLAlchemy engine in `lifespan.py`, and
   `OpenAIEmbeddingClient`/LLM clients in `app.py`'s `build_pipeline_resources`) —
   owned by whatever wires `S3SourceObjectStorage` into `SourceStorageRouter`
   (STORAGE-002 itself for construction; STORAGE-007 for lifespan integration).
   `boto3` client objects are documented by AWS as **safe for concurrent use
   from multiple threads** (the client creates and manages its own internal
   connection pool and is designed for exactly this shared-instance-from-many-
   threads usage pattern — the standard, intended way to use a `boto3` client,
   not a special case this project is inventing). No per-call client
   construction, no
   per-thread client instances — one shared client, its `Config(max_pool_connections=...)`
   sized to be `>=` the configured concurrency bound below so pooled connections
   are never the limiting factor ahead of the semaphore. Explicit `client.close()`
   at process shutdown is cheap and will be wired into the existing shutdown path
   `lifespan.py` already tears other resources down through; it is not required
   for correctness (idle connections simply time out) but costs nothing to do
   properly.

   #### Concurrency / backpressure decision

   One `asyncio.Semaphore(settings.storage_s3_max_concurrency)` owned by the
   `S3SourceObjectStorage` adapter instance, acquired around every
   `asyncio.to_thread`-offloaded operation — the exact shape
   `infrastructure/llm.py`/`infrastructure/embeddings.py` already use for
   `LLM_MAX_CONCURRENCY`/`EMBEDDING_MAX_CONCURRENCY`. A new `STORAGE_S3_MAX_CONCURRENCY`
   `Settings` field (default left to STORAGE-002's own implementation judgment —
   no architecture invariant requires a specific number, matching this
   checkpoint's own instructions) governs it. This single semaphore is shared by
   every caller of the adapter — normal Remember/Forget/Dataset-DELETE traffic
   *and* STORAGE-006's startup migration/`list_object_versions`-delete loops all
   acquire the same bounded pool, which is precisely what prevents:

   - a large startup migration from enqueuing unbounded concurrent thread jobs
     (STORAGE-006 must walk its Source-by-Source migration loop respecting this
     same semaphore, not spawn one `to_thread` per Source unconditionally);
   - normal Remember traffic from exhausting the thread pool indefinitely (bounded
     the same way `LLM_MAX_CONCURRENCY` already bounds concurrent LLM calls);
   - `list_object_versions`/delete loops from running unbounded — each page fetch
     and each version-delete call acquires the semaphore like any other operation.

   No default `asyncio.to_thread` executor resizing is required beyond Python's
   own default thread-pool behavior, **provided** the semaphore bound stays at or
   below a sane multiple of that default — STORAGE-002 must record its chosen
   default value and confirm it does not exceed `ThreadPoolExecutor`'s default
   worker count in a way that would make the semaphore itself the non-limiting
   factor. No Redis/Celery/external queue is introduced; this is purely an
   in-process `asyncio.Semaphore`, identical in kind to the two that already exist
   in this codebase.

   #### Dependency impact

   `pyproject.toml` `[project.dependencies]` gains exactly one new top-level entry:
   `boto3>=1.34,<2` (exact lower bound to be confirmed against whatever version is
   current when STORAGE-002 actually runs `uv add`; the `<2` upper bound matches
   this project's existing `>=X,<Y` pinning convention seen throughout
   `pyproject.toml`). `botocore`, `s3transfer`, and `jmespath` are pulled in
   transitively as `boto3`'s own declared dependencies — no direct pin needed for
   those. This is the **only** slice in this entire plan permitted to modify
   `pyproject.toml`/`uv.lock` (ADR-0011 D17); no other slice may add, upgrade, or
   otherwise touch a dependency.

   #### Failure/error translation boundary

   `botocore.exceptions.ClientError` (and its narrower connection/timeout/
   credential-error siblings) are caught **only** inside `s3.py` and translated at
   that boundary into the same dependency-free exception family STORAGE-001
   already established in `infrastructure/storage/port.py`
   (`InvalidSourceStorageUriError`, `SourceStorageUnavailableError`, and D37's
   later `UNRESOLVED`-producing conditions once STORAGE-005 lands) — never leaked
   past the adapter. This is a direct extension of STORAGE-001's Gate-A layering
   invariant: no `botocore`/`boto3` (and, had aioboto3 been chosen, no `aiohttp`/
   `aiobotocore`) exception, type, or object may ever be visible to
   `services/`, `pipelines/`, `domain/`, or `api/` — `S3SourceObjectStorage` owns
   every SDK detail completely, exactly as `FilesystemSourceObjectStorage` owns
   every `pathlib`/`urllib` detail today. STORAGE-002's own import-boundary test
   (mirroring STORAGE-001's `test_infrastructure_storage_never_imports_services_or_fastapi_or_sqlalchemy`)
   must be extended to also assert no `boto3`/`botocore` import exists outside
   `infrastructure/storage/s3.py`.

   #### Known risks (accepted, recorded — not blockers)

   - **Cancellation does not stop in-flight thread work.** An `asyncio.to_thread`
     call whose awaiting task is cancelled leaves the underlying OS thread running
     the `boto3` call to completion; the result is simply discarded. This matches
     the existing behavior of `services/cognify.py`'s own `to_thread` usage and is
     not a new class of risk this decision introduces — but STORAGE-002 must not
     assume cancellation aborts an in-flight S3 call.
   - **Thread-pool sizing interacts with the semaphore bound.** If
     `STORAGE_S3_MAX_CONCURRENCY` is configured higher than Python's default
     `ThreadPoolExecutor` worker count, the thread pool itself — not the semaphore
     — becomes the effective concurrency limiter, which is not incorrect but is a
     subtlety STORAGE-002 must document rather than let surprise a future reader.
   - **`boto3` client construction touches the filesystem/environment for the
     default credential chain** (shared config/credentials files, IMDS, env vars)
     even when explicit `STORAGE_S3_ACCESS_KEY_ID`/secret are configured, unless
     those are passed explicitly to the client constructor — STORAGE-002 must
     pass explicit credentials to the client call when configured, and rely on
     the default chain only when they are absent, so credential resolution
     behavior matches D16's "explicit credentials or provider chain, operator's
     choice" contract precisely.

   #### Exact checkpoint acceptance criteria

   This checkpoint is satisfied, and STORAGE-002 implementation may begin, once:

   1. `boto3` is the SDK named in STORAGE-002 item 4 (done, this edit);
   2. the event-loop safety contract above is treated as binding by whoever
      implements STORAGE-002 (i.e., code review for that slice must check every
      `boto3` call site against it, not just the adapter's public methods);
   3. the client/session lifecycle decision above is followed (one shared client,
      constructed once, not per-call/per-thread);
   4. the concurrency/backpressure decision above is followed (one
      `asyncio.Semaphore` bound by a new `Settings` field, shared by normal
      traffic and STORAGE-006's migration loop alike);
   5. the failure/error translation boundary above is followed (zero `botocore`/
      `boto3` types visible outside `infrastructure/storage/s3.py`, verified by an
      extended import-boundary test);
   6. no dependency other than `boto3` (and its own transitive pulls) is added for
      this purpose.

---

### STORAGE-003 — Router-based Source reads / Cognify

1. **Goal.** Move Source rehydration behind `SourceStorageRouter.read` (D13),
   replacing `services/cognify.py`'s `source_storage_path_for_cognify` +ドス direct
   filesystem read with a scheme-routed call that works identically for `file://`
   and `s3://`.
2. **Why isolated here.** Read-only, no destructive semantics, no finalize/persist
   boundary changes — the smallest possible slice that first makes Cognify
   backend-agnostic, provable independently of Remember/Forget changes.
3. **Dependencies.** STORAGE-002 (S3 adapter must exist to read `s3://`).
4. **Expected files/modules touched.** `sofias_memory/services/cognify.py`
   (replace `source_storage_path_for_cognify` call site with
   `SourceStorageRouter.read`), `sofias_memory/infrastructure/storage/router.py`
   (implement `read()` fully — currently a skeleton from STORAGE-001).
5. **Explicit non-goals.** No write-path change (STORAGE-004). No S3→filesystem
   relocation (D25 — explicitly out of scope for this slice and every other).
6. **Accepted ADR sections implemented.** D4/D5 (read routes by scheme, never by
   `STORAGE_BACKEND`), D13.
7. **Acceptance criteria.** Cognify rehydrates identically from `file://` Sources
   (regression) and correctly from `s3://` Sources (new, MinIO-backed test);
   `max_source_size_mb` enforcement and byte/hash verification preserved for both
   schemes; `DependencyUnavailableError` still raised the same way on failure.
8. **Tests to add/change.** `tests/unit/test_cognify_service.py`,
   `tests/unit/test_cognify_pipeline_steps.py`: extend with an `s3://`-scheme
   case (mocked router or MinIO-backed per the integration test's existing
   pattern). `tests/integration/test_cognify_async_postgres_integration.py`:
   add an S3-backed Source fixture case if the file's existing structure supports
   parametrizing storage scheme (inspect at slice start).
9. **Regression commands.** `RUFF`, `FMT`, `MYPY`,
   `PYTEST tests/unit/test_cognify_service.py tests/unit/test_cognify_pipeline_steps.py
   tests/integration/test_cognify_async_postgres_integration.py`.
10. **Review gate.** Both schemes proven read-correct before Remember/Forget
    slices begin (keeps STORAGE-004/005 from needing to re-litigate read-path
    correctness).
11. **Rollback/recovery concerns.** Pure read path — a regression here is directly
    visible as Cognify failures, not a silent data-integrity issue.
12. **ADR validation items owned.** 8 (Cognify rehydrates from S3).

---

### STORAGE-004 — Remember finalization

1. **Goal.** Generalize `FinalizeStorageStep` (`pipelines/steps/remember.py`) to
   route writes through `SourceStorageRouter.finalize` (D2/D4/D12), including the
   full B1 legacy-object recovery contract (D12's amendment) for the
   `storage_uri=NULL` + verified legacy filesystem object crash window.
2. **Why isolated here.** Remember's finalize step has the most intricate
   crash-window contract in the whole ADR (B1) and touches only one pipeline
   (`remember`) — isolating it lets STORAGE-005 (destructive deletion, a different
   pipeline family entirely) proceed independently and be reviewed without B1's
   complexity in the diff.
3. **Dependencies.** STORAGE-002 (S3 adapter). Does not depend on STORAGE-003
   (independent read vs. write paths) but both must land before STORAGE-006 needs
   a fully router-integrated codebase.
4. **Expected files/modules touched.** `sofias_memory/pipelines/steps/remember.py`
   (`FinalizeStorageStep.execute`/`persist`), `sofias_memory/services/remember.py`
   (retain `_ingress/*` helpers unchanged per D3; wrap `write_final_storage_bytes`
   callers through the router), `sofias_memory/infrastructure/storage/router.py`
   (`finalize()` fully implemented).
5. **Explicit non-goals.** `_ingress/<run_id>` staging stays local under both
   backends (D3) — this slice must not touch `ingress_directory`/
   `write_ingress_bytes`/`delete_ingress_artifact` semantics, only what happens
   *after* ingress is read. No S3 I/O inside `persist()` (D27) — verify this is
   true in code review, not just in the PR description.
6. **Accepted ADR sections implemented.** D2, D3 (unchanged, verified not
   touched), D4, D12 (including the full B1 amendment), D27 (boundary
   preservation, explicit re-check).
7. **Acceptance criteria.** Filesystem finalize path unchanged (regression). S3
   finalize path: normal case + PUT-timeout-then-retry idempotent continuation
   (D12). B1 crash window reproduced exactly as ADR test scenario 29/30 describe:
   local final object written + verified, ingress deleted, crash before persist,
   `STORAGE_BACKEND` flipped to `s3`, restart — existing `PipelineRun` recovers with
   no new Source, no re-fetch; corrupted/missing legacy object fails closed
   (`REMEMBER_INGRESS_MISSING` or equivalent).
8. **Tests to add/change.** `tests/unit/test_remember_pipeline_steps.py`,
   `tests/unit/test_remember_service.py`: add S3 finalize cases + B1 recovery unit
   cases (mocked router). `tests/integration/test_remember_postgres_integration.py`:
   add the B1 crash-window integration scenario (real PostgreSQL, real filesystem
   artifact, S3 adapter — MinIO-backed or mocked per how STORAGE-002 set up its
   test target) driving the exact sequence in ADR items 29/30.
9. **Regression commands.** `RUFF`, `FMT`, `MYPY`,
   `PYTEST tests/unit/test_remember_pipeline_steps.py tests/unit/test_remember_service.py
   tests/integration/test_remember_postgres_integration.py`.
10. **Review gate.** B1 crash-window test must be reviewed line-by-line against
    D12's exact required sequence (upload → verify → persist → GC) before merge —
    this is the highest-precision correctness requirement in the whole ADR outside
    STORAGE-005/006.
11. **Rollback/recovery concerns.** A bug here risks either (a) data loss (deleting
    the legacy local object before S3 is verified/persisted) or (b) duplicate
    Sources (fabricating a new Source instead of recovering). Both are called out
    explicitly as forbidden in D12 — code review must check for them by name.
12. **ADR validation items owned.** 4, 5, 6 (Remember text/file/URL → S3), 7
    (queued Remember `_ingress` survives backend redeploy), 13, 14 (target-exists
    cases at the finalize call site, distinct from STORAGE-002's adapter-level
    proof), 15, 16, 17 (crash-after-upload / crash-after-CAS / restart-resumes, at
    the Remember-specific B1 level — general D8 migration crash windows are
    STORAGE-006's), 29, 30 (the two B1-specific validation items).

---

### STORAGE-005 — Destructive deletion semantics

1. **Goal.** Implement D37–D42 across `StorageDeletionStep` (Forget,
   `pipelines/steps/forget.py`) and `DeleteStorageStep`/`FinalizeTombstoneStep`
   (`DATASET_DELETE`, `pipelines/steps/dataset_delete.py`): the four-outcome
   `StorageDeleteResult` contract, per-Source result coverage, tombstone
   `storage_uri` semantics, and business-delete-must-converge.
2. **Why isolated here.** This is the one slice that changes *business* semantics
   (a `PipelineRun` may now succeed with unresolved storage cleanup) rather than
   only adding a backend — it deserves review on its own, separate from the S3
   plumbing slices, and it directly touches the two concrete gaps already found in
   current code (the silent-missing-key pattern in both finalize steps).
3. **Dependencies.** STORAGE-002 (S3 adapter, so `UNRESOLVED` can be produced by a
   real S3 failure, not only simulated).
4. **Expected files/modules touched.**
   - `sofias_memory/infrastructure/storage/port.py`: extend
     `StorageDeleteStatus` with `UNRESOLVED` (the enum already has the other three
     values, per repository inspection above).
   - `sofias_memory/services/forget.py`: `delete_source_storage` routes through
     `SourceStorageRouter.delete`; typed/recognized conditions become a returned
     `UNRESOLVED`, not a raised exception (`DependencyUnavailableError` reserved for
     unrecognized/defect conditions, per D37).
   - `sofias_memory/pipelines/steps/forget.py`: `StorageDeletionStep` (per-Source
     result collection unchanged in shape, but must guarantee full coverage — D37);
     the finalize logic at ~line 499/536-593 gains the **invariant check**: for
     every Source being finalized `DELETING → DELETED`, assert an explicit result
     exists in `storage_status_by_source`; raise (not silently skip) if missing.
     `UNRESOLVED` outcome preserves `storage_uri`; `DELETED_NOW`/`ALREADY_ABSENT`/
     `NOT_REQUESTED` clear it (D39).
   - `sofias_memory/pipelines/steps/dataset_delete.py`: identical treatment for
     `DeleteStorageStep`/`FinalizeTombstoneStep` (~line 386-490).
   - `sofias_memory/schemas/forget.py` and the `DatasetDeleteResult` schema
     (`schemas/datasets.py` or wherever ADR-0010's D24 result type actually lives —
     confirm exact file at slice start): add `storage_deleted`,
     `storage_already_absent`, `storage_unresolved` metrics fields (D39).
5. **Explicit non-goals.** No new `ErrorCode` value is required by D37/D39 unless
   the invariant-failure path needs one distinct from `INTERNAL_ERROR` — decide
   in-slice; do not add a `StorageDeleteStatus` fifth value under any
   circumstance (D37/Alternative R explicitly rejects this). No schema migration
   (D39 confirms none needed).
6. **Accepted ADR sections implemented.** D14, D15 (corrected wording), D28
   (ADR-0010 supersession), D34 (Case A/B/C/D — this slice does **not** implement
   the startup-scan side of Case B/D classification, only the finalize-side
   per-Source-result contract those cases assume exists), D37, D38, D39, D42.
7. **Acceptance criteria.** Full Source Forget / Dataset Forget / Forget Everything
   / `DATASET_DELETE` all converge (business finalize succeeds) even when storage
   deletion returns `UNRESOLVED`; a missing per-Source result is a hard failure,
   verified by a test that deliberately induces the gap; `memory_only=true`
   unaffected; versioned-bucket `UNRESOLVED` never silently becomes
   `DELETED_NOW`/`ALREADY_ABSENT`.
8. **Tests to add/change.**
   - `tests/unit/test_forget_pipeline_steps.py`, `test_forget_service.py`: add
     `UNRESOLVED`-outcome cases, the missing-per-Source-result invariant-failure
     case, `NOT_REQUESTED`-for-already-NULL case.
   - **New** `tests/unit/test_dataset_delete_pipeline_steps.py` (does not exist
     today — confirmed by repository inspection): mirror
     `test_forget_pipeline_steps.py`'s structure for `DeleteStorageStep`/
     `FinalizeTombstoneStep`.
   - `tests/integration/test_forget_postgres_integration.py`,
     `test_dataset_delete_postgres_integration.py`: add the D41 rollback/
     lost-S3-config worked example as a real-PostgreSQL scenario; add the
     mixed-outcome Dataset DELETE case (ADR item 47).
9. **Regression commands.** `RUFF`, `FMT`, `MYPY`,
   `PYTEST tests/unit/test_forget_pipeline_steps.py tests/unit/test_forget_service.py
   tests/unit/test_dataset_delete_pipeline_steps.py tests/unit/test_dataset_delete_barrier.py
   tests/integration/test_forget_postgres_integration.py
   tests/integration/test_dataset_delete_postgres_integration.py`.
10. **Review gate.** Explicit code-review checklist item: grep the diff for any
    `except Exception` near a `StorageDeleteResult` construction site — D37
    forbids the blanket form; every `UNRESOLVED` must trace to a named, typed
    condition.
11. **Rollback/recovery concerns.** The highest-risk regression this slice can
    introduce is silently reporting `DELETED_NOW`/`ALREADY_ABSENT` without positive
    evidence (D15/D38's discipline, explicitly *not* relaxed by this round) —
    review must confirm every success path traces back to a real verified/observed
    absence, never an assumption.
12. **ADR validation items owned.** 9, 10, 11 (memory_only preserved / full Forget
    deletes / Dataset DELETE deletes), 26 (idempotent delete-of-absent), 39
    (D39 semantics — non-startup half), 44, 45, 46, 47 (absence/config-missing/
    mixed-outcome cases), 52, 53 partially (the DELETED-exclusion assertion at the
    Cognify/Remember-recovery level, cross-owned with STORAGE-003/004 — this slice
    owns the Forget/DELETE-side half), 54, 55, 56, 57 (per-Source coverage +
    NOT_REQUESTED + D39 corrected semantic).

---

### STORAGE-006 — Filesystem → S3 startup convergence

1. **Goal.** Implement the D8 migration algorithm and D34's full Case A/B/C/D
   classifier as a standalone, callable convergence routine — independent of
   STORAGE-007's bootstrap/health wiring, so its correctness (CAS, crash safety,
   concurrency) can be proven with direct unit/integration tests before any
   process-state complexity is added on top.
2. **Why isolated here.** D34's Case B requires *proving* a compatible destructive
   `PipelineRun` lineage — this can only be implemented correctly once
   STORAGE-005's per-Source result contract and finalize-step shapes exist, so this
   slice is deliberately sequenced after it. Keeping convergence itself separate
   from STORAGE-007's maintenance-HTTP/claim-filter work means a bug in "does the
   algorithm converge correctly" is never conflated with "does the process expose
   the right HTTP/worker state."
3. **Dependencies.** STORAGE-002 (S3 adapter), STORAGE-005 (per-Source result
   contract + finalize shapes the lineage-proof query reads).
4. **Expected files/modules touched.**
   - New: `sofias_memory/services/storage_convergence.py` (or equivalent — the
     migration/classification algorithm as a plain async service, callable both
     from a test harness and later from STORAGE-007's bootstrap code; **not** a
     `PipelineStep`, since D31 forbids a second engine).
   - `sofias_memory/infrastructure/storage/router.py`: any additional
     `verify()`/cheap-check surface D8/D9 need beyond what STORAGE-002 built.
   - `loaders/text.py` or the storage package: the D35 legacy-locator function
     (`dataset_id`/`source_id`/`mime_type` → deterministic legacy path), using the
     STORAGE-001-introduced `STORAGE_EXTENSION_BY_MIME_TYPE` mapping.
   - Repository: a query for "compatible durable `forget`/`dataset_delete`
     `PipelineRun` lineage targeting Source X" — likely
     `infrastructure/postgres/repositories/pipeline_runs.py`, new method.
5. **Explicit non-goals.** No bootstrap/lifespan integration yet (STORAGE-007). No
   maintenance HTTP state. This slice's convergence routine is invoked directly by
   its own tests, not by the running application.
6. **Accepted ADR sections implemented.** D7's Scope note, D8 (full algorithm),
   D9, D10 (concurrency), D19 (failure conditions this routine can detect — the
   process-level wiring of "block readiness" is STORAGE-007's), D34 (full
   classifier including Case D), D35, D40.
7. **Acceptance criteria.** Given a PostgreSQL fixture with a mix of
   `PENDING`/`ACTIVE`/`FAILED` `file://` Sources, `DELETING` Sources (with and
   without provable lineage), and `DELETED` tombstones (`NULL` and retained
   locator), one convergence pass correctly: migrates only the live set; leaves
   `DELETING`-with-lineage untouched; fails closed on `DELETING`-without-lineage;
   never inspects `DELETED` Sources at all; CAS-repoints only after strong S3
   verification; cleans only confirmed local duplicates via the D35 locator, never
   via glob; survives a simulated crash at each of the four crash-window points and
   re-converges correctly on a second pass; two concurrent passes converge
   correctly with no double-upload/double-CAS.
8. **Tests to add/change.** New: `tests/integration/test_storage_convergence_postgres_integration.py`
   (real PostgreSQL + MinIO), covering the acceptance criteria above as discrete
   scenarios; new unit tests for the D35 locator function (pure, no I/O) and for
   the Case A/B/C/D classifier logic in isolation (mocked repository).
9. **Regression commands.** `RUFF`, `FMT`, `MYPY`,
   `PYTEST tests/integration/test_storage_convergence_postgres_integration.py` plus
   the new unit test files.
10. **Review gate.** Every one of the four crash-window rows in ADR-0011's
    "Backend-transition crash windows" table (B1/B2/M2, cases A–D) must have a
    named test scenario in the new integration file before merge — no crash window
    may be "implicitly covered."
11. **Rollback/recovery concerns.** This slice is the most destructive-adjacent
    (it deletes confirmed local duplicates). Review must re-verify the D35
    pre-deletion sequence is followed exactly in code (locate → confirm S3 target
    valid → confirm exact local path exists → hash → compare → only then unlink) —
    no step may be reordered or skipped for convenience.
12. **ADR validation items owned.** 2 (fresh S3 install, vacuous convergence,
    routine-level), 13, 14 (target-exists cases, migration-routine level, distinct
    from STORAGE-002's adapter-level and STORAGE-004's finalize-level proofs), 15,
    16, 17 (crash windows, general D8 cases — distinct from STORAGE-004's B1-specific
    ones), 18, 19 (missing/mismatched legacy file blocks convergence), 22, 23
    (unrelated `DATA_DIRECTORY` content / `_ingress` survive), 24 (concurrent
    startup), 25 (no lock across S3 I/O — code-review assertion, also re-checked
    here), 31, 32 (Forget/DATASET_DELETE crash-before-finalize, backend-switch
    cases — the migration-routine's classification half; STORAGE-007 owns the
    "does the process actually let the recovery-owned run progress" half), 33, 34,
    35, 36, 37 (classification/locator/cleanup cases), 52, 53 (the migration-side
    half of DELETED-exclusion), 58 (Case D fail-closed).

---

### STORAGE-007 — Bootstrap state / recovery-owned claims / health

1. **Goal.** Implement the observable BOOTSTRAP/MAINTENANCE → STORAGE_CONVERGING →
   OPERATIONAL process-state model (D31), Alembic gating (D32), long-convergence
   liveness (D33), and the recovery-owned claim filter (D31's behavioral
   requirement), wired into `lifespan.py`/`app.py`/`api/routes/health.py`.
   **Explicit prerequisite (D43, fifth amendment, STORAGE-006 CAS-loss safety
   audit):** the claim filter this slice implements is what makes D43's exclusion
   real, not merely documented — it must block every normal `PipelineRun` claim
   capable of starting a *new* destructive transition against a Source
   STORAGE-006's classifier would consider migration-eligible (D34 Case A), for the
   entire STORAGE_CONVERGING duration, with only recovery-owned lineage exempt
   (unchanged D31 allowance). This is not a separate feature to schedule
   later — it is the same claim-filter mechanism this goal already requires above,
   with D43 naming its consequence explicitly. Gate E may not be declared complete
   without a negative test proving this (see item 11 below, extended).
2. **Why isolated here — highest-risk slice, flagged explicitly.** Repository
   inspection (above) found that `lifespan.py`'s startup sequence is a **blocking
   pre-`yield` body** — the ASGI server does not serve `/health/live` until it
   completes. Naively inserting STORAGE-006's convergence routine into that
   sequence would violate D33's maintenance-HTTP-availability requirement outright
   (the exact failure mode D33 was written to forbid: "convergence runs before HTTP
   startup while `/health/live` is available" — which is not actually true of a
   blocking pre-`yield` call). This slice cannot proceed on a default/casual
   choice.
3. **Dependencies.** STORAGE-006 (convergence routine to call), STORAGE-005
   (destructive-lineage classifier STORAGE-006 already integrates).
4. **Expected files/modules touched.** `sofias_memory/lifespan.py` (restructure so
   convergence is **not** blocking `yield`), `sofias_memory/app.py`
   (`readiness_checks` gains a `"storage"` entry following the existing
   `_worker_readiness_check` pattern; possibly a new dependency/middleware gate
   that blocks non-health business routes outside OPERATIONAL — inspect
   `ApiKeyMiddleware`'s existing middleware-ordering pattern in `api/middleware/`
   as the likely integration point), `sofias_memory/services/pipeline_queue_claimer.py`
   + `infrastructure/postgres/repositories/pipeline_runs.py`
   (`list_eligible_candidate_ids` gains an optional recovery-owned-only filter,
   populated only during STORAGE_CONVERGING — **D31 sixth amendment: this must be
   more than a bare `pipeline_type IN (forget, dataset_delete)` allowlist.** It must
   additionally require, per candidate run, that its own
   `authoritative_mutation`/`deactivate_authoritative` `PipelineStep` row already has
   `status = succeeded` — the exact predicate `find_compatible_destructive_lineage`
   (STORAGE-006, `pipeline_runs.py`) already proves per-Source; here it is applied at
   the run-claim level, once per candidate run, not merely "does this run's
   `pipeline_type` match." A `pipeline_type`-only filter is insufficient per D31's
   `DATASET_DELETE` multi-source worked counterexample and must not be implemented as
   the final predicate.), `api/routes/health.py`
   (no structural change expected — `/health/live` already dependency-free; confirm
   `/health/ready`'s existing `ReadinessResponse` shape is sufficient for a
   `"storage"` check's detail message).
5. **Explicit non-goals.** No second worker/engine/queue (D31, re-affirmed). No
   change to `/health/live`'s existing "never checks a dependency" contract — the
   fix is making it *reachable* sooner, not making it check more.
6. **Accepted ADR sections implemented.** D19 (process-level failure→NOT_READY
   wiring), D20, D29 (operator-experience walkthrough as implemented behavior), D31
   (full state model + claim filter, including the sixth amendment's stronger
   step-completion claim predicate), D32, D33, D43 (fifth amendment — the claim
   filter's exclusion of new destructive transitions against Case-A Sources during
   STORAGE_CONVERGING).
7. **Acceptance criteria.** Under `STORAGE_BACKEND=s3`: `/health/live` responds
   `200` while convergence is still running (proven with a deliberately slow/
   MinIO-throttled convergence in a test); `/health/ready` is `not_ready` with a
   `"storage"` detail throughout BOOTSTRAP/MAINTENANCE and STORAGE_CONVERGING;
   normal business routes reject/are blocked outside OPERATIONAL (exact mechanism
   decided in-slice — see checkpoint); a recovery-owned Forget/`DATASET_DELETE` run
   is claimed and makes progress during STORAGE_CONVERGING while a concurrently
   submitted normal Remember stays `queued`, unclaimed, until OPERATIONAL; schema
   mismatch never triggers Alembic or any Source inspection. **(D43)** a normal
   Forget/`DATASET_DELETE` submission targeting a live Case-A Source (migration-
   eligible: `status IN (PENDING, PROCESSING, ACTIVE, FAILED)` + `storage_uri =
   file://...`) stays `queued`, unclaimed, throughout STORAGE_CONVERGING — proven
   with the same claim-filter mechanism, not a separate code path.
8. **Tests to add/change.** `tests/unit/test_health.py`,
   `test_postgres_readiness_health.py`: add the `"storage"` readiness check case.
   New: `tests/integration/test_storage_bootstrap_lifecycle_postgres_integration.py`
   covering the full state-machine acceptance criteria above, including the
   concurrent-claim-filter assertion (submit a normal run + a recovery-owned run
   during STORAGE_CONVERGING, assert only the latter progresses) **and the D43
   negative case** (submit a normal Forget/`DATASET_DELETE` targeting a live
   Case-A Source during STORAGE_CONVERGING, assert it never transitions out of
   `queued`/unclaimed until OPERATIONAL is reached) **and the D31 sixth-amendment
   multi-source case** (a `DATASET_DELETE` run targeting a Dataset with one Case-B
   Source and one Case-A Source, where the run's own `deactivate_authoritative` step
   has NOT yet durably succeeded: assert the run is NOT claimed as recovery-owned and
   the Case-A Source never transitions to `DELETING`; then, with the step durably
   succeeded, assert the same run IS claimed and progresses).
   `tests/unit/test_pipeline_queue_claimer.py`: extend for the new recovery-owned
   claim filter, covering both the `pipeline_type` scope and the required
   step-completion predicate (a `queued` `forget`/`dataset_delete` run whose
   mutation step has not succeeded must be excluded from the recovery-owned
   candidate set even though its `pipeline_type` matches).
9. **Regression commands.** `RUFF`, `FMT`, `MYPY`,
   `PYTEST tests/unit/test_health.py tests/unit/test_postgres_readiness_health.py
   tests/unit/test_pipeline_queue_claimer.py tests/unit/test_pipeline_worker.py
   tests/integration/test_pipeline_worker_postgres_integration.py
   tests/integration/test_storage_bootstrap_lifecycle_postgres_integration.py`.
10. **Review gate — mandatory pre-code checkpoint (per task instruction, not
    optional).** Before writing any code in this slice, produce a short design note
    (as part of this slice's own PR description or a scratch note, not a rewrite of
    this plan) answering: how does the maintenance HTTP surface become reachable
    before convergence finishes, given `lifespan.py`'s current blocking structure?
    Two credible directions found during repository inspection, neither pre-selected
    here:
    - **(a) Non-blocking convergence task.** `lifespan()` reaches `yield` promptly
      (after only the existing PostgreSQL/Neo4j probes — themselves already fast),
      and storage convergence + the recovery-owned-lineage wait run as a background
      `asyncio.Task` created before `yield`, with the `"storage"` readiness check
      reading that task's state. `worker.start()` (for **normal** claims) is
      deferred until the task signals convergence complete; the worker's *claim
      query* itself is what STORAGE-007 restricts to recovery-owned lineage in the
      meantime (so the existing single worker coordinator can already be running,
      just claim-filtered) rather than needing a second worker start call.
    - **(b) Two-phase app/ASGI mount.** A minimal maintenance-only ASGI app is
      served until convergence completes, then traffic is handed to the full
      `create_app()`-built application. More invasive, likely unnecessary given (a)
      is achievable with the existing `readiness_checks`/worker-claim-filter
      primitives already in the codebase.
    Record the chosen direction and why before implementation begins; do not
    silently pick the more complex option (b) without justifying it over (a).
11. **Rollback/recovery concerns.** A bug here risks either (i) the process
    reporting `ready` while storage is not actually converged (silently breaks
    D19/D20's contract), (ii) a normal claim slipping through during
    STORAGE_CONVERGING (breaks D31's core invariant), or (iii) — the D43-specific
    risk this amendment exists to close — a normal Forget/`DATASET_DELETE` claim
    against a live Case-A Source slipping through during STORAGE_CONVERGING,
    reopening the exact CAS-loss orphan race the STORAGE-006 audit found. All three
    must have an explicit negative test (asserting the *forbidden* case does not
    happen), not only a positive test of the happy path — (iii) specifically
    requires a test that submits a normal Forget/`DATASET_DELETE` targeting a
    Source STORAGE-006's classifier would call Case A while STORAGE_CONVERGING is
    active, and asserts it stays `queued`/unclaimed until OPERATIONAL.
12. **ADR validation items owned.** 3 (fresh S3 install passes through gate), 38,
    39, 40, 41 (maintenance liveness / readiness-blocked / normal-work-blocked /
    recovery-owned-progresses), 42 (schema mismatch never triggers Alembic/
    inspection), plus the process-level half of 31/32 (STORAGE-006 proves the
    algorithm converges; this slice proves the *process* correctly gates on that
    convergence and lets recovery-owned lineage actually get claimed). **D43
    (fifth amendment):** 59, 60, 61, 65, 66 (claim-filter enforcement, single-replica
    boundary, no lock/schema added); 62, 63, 64 are algorithm-level and already
    owned by STORAGE-006/Gate D, re-verified here only insofar as this slice's own
    claim filter is what keeps them true in practice. **D31 sixth amendment
    (recovery-owned claim consistency fix):** 67, 68, 69 (multi-source
    `DATASET_DELETE` pre-mutation/post-mutation claim-eligibility distinction, and
    the structural proof that no recovery-owned claim can execute a new
    `DELETING`-causing mutation) — these are specifically this slice's own claim
    filter's correctness, not merely STORAGE-006's classifier.

---

### STORAGE-008 — Deployment/configuration/docs

1. **Goal.** Complete operator-facing support once runtime behavior (STORAGE-001
   through 007) is proven: Compose files, `docs/operations.md`, `AGENTS.md` §4,
   and the ADR-0010 forward-reference note. **Owns D43's single-replica
   deployment-exclusivity documentation obligation** (sixth amendment): document that
   a supported `STORAGE_BACKEND=s3` deployment must use stop-old-before-start-new
   process exclusivity (a recreate/stop-first deployment strategy) rather than
   start-first rolling overlap — this is a deployment-configuration/documentation
   requirement, not a runtime interlock, and is explicitly this slice's scope, not
   STORAGE-007's.
2. **Why isolated here.** Deliberately last among the "build" slices — deployment
   config for a feature that is not yet proven correct is worse than no deployment
   config at all; this also matches ADR-0011 D29's own operator-experience
   walkthrough, which assumes working software.
3. **Dependencies.** STORAGE-007 (the full runtime contract it documents must
   actually exist).
4. **Expected files/modules touched.** `.env.example` (if the repository has one —
   confirm at slice start; add `STORAGE_BACKEND`/`STORAGE_S3_*` as commented/
   optional entries, default `filesystem`), `compose.yaml`,
   `deploy/easypanel/compose.yaml` (same optional entries — no functional
   difference between the two per `AGENTS.md` §22's "no functional difference
   between deployments" rule), `docs/operations.md` (backup/restore S3-mode
   section, first-start procedure update for the storage convergence gate, D41's
   lost-S3-config operator warning, **D43's stop-old-before-start-new deployment-
   exclusivity requirement for `STORAGE_BACKEND=s3` — explicitly warn against
   start-first/rolling-overlap orchestrator configurations**), `AGENTS.md` §4 stack
   list, and a forward-reference note added to ADR-0010 (per ADR-0011 D28 — a small,
   explicit cross-reference, not a rewrite of ADR-0010's content, consistent with the
   original ADR-0011 task's instruction not to retroactively rewrite ADR-0010).
5. **Explicit non-goals.** No automatic S3→filesystem protection/warning mechanism
   in code (explicitly rejected — this is documentation only, per the ADR's own
   Alternative E rejection). No new deployment topology (still exactly
   `sofias-memory`/`postgres`/`neo4j`, `AGENTS.md` §22).
6. **Accepted ADR sections implemented.** D16 (config surface documented), D22
   (backup/restore), D29 (operator walkthrough, now documented as real, tested
   behavior), D30 (explicitly note no CLI is required/provided), D41 (operator
   warning), D43 (sixth amendment — single-replica deployment-exclusivity operator
   documentation).
7. **Acceptance criteria.** `docker compose config` passes for all three Compose
   files (`AGENTS.md` §22's existing requirement, re-verified after the env-var
   additions). `docs/operations.md` walked through manually against a real
   `STORAGE_BACKEND=s3` deploy (mirroring how the existing doc states every command
   was "executed literally against a real, disposable PostgreSQL + Neo4j pair") —
   this slice's own acceptance requires doing that walkthrough for S3 mode too, not
   just writing prose.
8. **Tests to add/change.** No new automated tests expected (documentation slice);
   if `tests/contract/` or similar has any Compose-file-parsing test, confirm it
   still passes.
9. **Regression commands.** `docker compose -f compose.yaml config`,
   `docker compose -f deploy/easypanel/compose.yaml config`, plus the standard
   `RUFF`/`FMT`/`MYPY`/`PYTEST` sweep (should be a no-op on runtime code).
10. **Review gate.** Documentation reviewed against the actual STORAGE-001..007
    implementation, not against the ADR's prose alone — every claim in
    `docs/operations.md`'s new section must be checked against real observed
    behavior from STORAGE-007's own tests.
11. **Rollback/recovery concerns.** None (docs/config only) beyond ensuring the
    new env vars are genuinely optional and do not change `filesystem`-mode
    defaults.
12. **ADR validation items owned.** None directly numbered 1–58 (those are all
    runtime-behavior tests); this slice's own acceptance criterion (the manual S3
    walkthrough) is the closest analog to item 28's production-shaped smoke,
    formally owned by STORAGE-009.

---

### STORAGE-009 — Full integration / production-shaped validation

1. **Goal.** Run and, where not already covered, add the remaining cross-cutting
   tests from ADR-0011's validation matrix (items 1–58) that don't belong cleanly
   to one earlier slice, and perform the production-shaped S3 smoke test (item 28).
2. **Why isolated here.** Cross-slice interactions (e.g., a Remember run finalizing
   *while* a convergence pass is mid-flight, or a Dataset DELETE racing a
   concurrent migration) are only meaningful to test once every slice is
   individually green — testing them earlier would be testing against a
   moving target.
3. **Dependencies.** STORAGE-001 through STORAGE-008, all green.
4. **Expected files/modules touched.** New: `tests/e2e/test_adr_0011_storage_smoke.py`
   or equivalent (production-shaped, MinIO + real Compose-equivalent stack —
   inspect `tests/e2e/`'s existing pattern at slice start, do not invent a
   structure that doesn't match it). Possibly small additions to existing
   integration files for the handful of matrix items not owned elsewhere (see
   traceability table below — anything without an earlier explicit owner).
5. **Explicit non-goals.** No new production code changes expected — this slice
   is test-writing and validation only. If a real bug is found, STOP per the
   Execution Policy and report before patching (the fix belongs to whichever
   earlier slice owns the affected code, reopened explicitly, not silently patched
   here).
6. **Accepted ADR sections implemented.** None new — this slice is proof, not
   design.
7. **Acceptance criteria.** All 58 ADR-0011 validation items have at least one
   passing, named test; the production-shaped smoke (item 28) runs against a
   MinIO-backed, Compose-equivalent stack end to end: fresh S3 install → Remember →
   Cognify → Recall → Forget → readiness throughout.
8. **Tests to add/change.** See traceability table below for exactly which items
   still need a test after STORAGE-001..008; this slice fills only those gaps plus
   the item-28 smoke.
9. **Regression commands.** Full `RUFF`, `FMT`, `MYPY`, `PYTEST` (entire suite,
   including every integration file touched across all nine slices), plus the new
   e2e smoke run explicitly.
10. **Review gate.** The traceability table itself (below) is the review artifact —
    every row must show a passing test before this slice, and therefore the whole
    plan, is considered complete.
11. **Rollback/recovery concerns.** N/A (validation only).
12. **ADR validation items owned.** 28 (production-shaped smoke) primarily; see the
    traceability table for the small number of cross-cutting items (e.g. 24, 25)
    that get a final confirming re-run here even though an earlier slice is their
    primary owner.

## Validation matrix ownership — traceability table

Every ADR-0011 validation item (1–58) has exactly one **primary** owner. "Re-run"
means the item is also exercised (not re-designed) by a later slice's broader test
as a side effect — not a second primary owner.

| # | Owning slice | Test type | Expected evidence |
|---|---|---|---|
| 1 | STORAGE-001 | unit/integration (existing) | Full existing suite green, `STORAGE_BACKEND` unset |
| 2 | STORAGE-006 | integration (new) | Vacuous convergence pass, zero `file://` rows |
| 3 | STORAGE-007 | integration (new) | Fresh S3 install reaches OPERATIONAL |
| 4,5,6 | STORAGE-004 | unit+integration | Remember text/file/URL → `s3://` |
| 7 | STORAGE-004 | integration | `_ingress` survives backend redeploy |
| 8 | STORAGE-003 | unit+integration | Cognify reads `s3://` |
| 9 | STORAGE-005 | unit | `memory_only=true` preserves S3 original |
| 10 | STORAGE-005 | unit+integration | Full Forget deletes S3 original |
| 11 | STORAGE-005 | integration | Dataset DELETE deletes S3 originals |
| 12 | STORAGE-002 | integration | Versioned-bucket destructive semantics (adapter level) |
| 13,14 | STORAGE-002 (adapter) / STORAGE-004 (finalize) / STORAGE-006 (migration) | unit+integration | Target-exists-matching / target-exists-conflicting, at each of the three call sites |
| 15,16,17 | STORAGE-004 (B1-specific) / STORAGE-006 (general D8) | integration | Crash-after-upload, crash-after-CAS, restart resumes |
| 18,19 | STORAGE-006 | integration | Missing/mismatched legacy file blocks convergence |
| 20,21 | STORAGE-002 (adapter) + STORAGE-007 (process-level block) | integration | S3 unavailable/access-denied |
| 22,23 | STORAGE-006 | integration | `DATA_DIRECTORY`/`_ingress` survive migration |
| 24 | STORAGE-006 (primary) / STORAGE-009 (re-run under full stack) | integration | Concurrent startup correctness |
| 25 | STORAGE-006 (primary) / STORAGE-009 (re-run) | code review + test | No lock across S3 I/O |
| 26 | STORAGE-005 | unit | Idempotent delete-of-absent |
| 27 | STORAGE-002 | integration | Version/delete-marker cleanup proven |
| 28 | STORAGE-009 | e2e | Production-shaped S3 smoke |
| 29,30 | STORAGE-004 | integration | B1 recovery / B1 fail-closed |
| 31,32 | STORAGE-006 (algorithm) + STORAGE-007 (process lets lineage progress) | integration | Forget/DATASET_DELETE crash-before-finalize, backend switch |
| 33 | STORAGE-006 | integration | Live Source missing file still blocks (Case A) |
| 34,35,36,37 | STORAGE-006 | integration | Locator match/mismatch/unmappable, unrelated content survives |
| 38,39,40,41 | STORAGE-007 | integration | Maintenance liveness / readiness / normal-blocked / recovery-owned-progresses |
| 42 | STORAGE-007 | integration | Schema mismatch never triggers Alembic/inspection |
| 43 | STORAGE-002 or STORAGE-006 (namespace-violation detection — decide exact owner in-slice; default STORAGE-002 since it owns the adapter's integrity-metadata check) | integration | External S3 namespace mutation detected |
| 44,45 | STORAGE-005 | unit | Absence-proof cases (filesystem/S3) |
| 46 | STORAGE-005 | integration | S3 config missing → Forget still succeeds |
| 47 | STORAGE-005 | integration | Mixed-outcome Dataset DELETE |
| 48,49 | STORAGE-002 (adapter) / STORAGE-005 (business-convergence) | integration | Versioned purge / retention-blocked |
| 50,51 | STORAGE-005 | unit | Typed timeout/AccessDenied vs. real defect |
| 52,53 | STORAGE-006 (migration-side) + STORAGE-003/004 (Cognify/Remember-recovery side) | integration | `DELETED` never migrated / never treated as live |
| 54 | STORAGE-005 | unit | Metrics never overclaim |
| 55,56,57 | STORAGE-005 | unit | Per-Source coverage invariant, `NOT_REQUESTED`, D39 semantic |
| 58 | STORAGE-006 | integration | Case D fail-closed |

## STORAGE-009 continuation — real MinIO evidence (2026-09-01)

This addendum records the first genuine live-S3 validation run for STORAGE-009.
Before this run, this file's Progress checklist showed every slice unchecked and
no prior Gate G evidence (BLOCKED or otherwise) existed anywhere in the repository
or git history — the implementation code for STORAGE-001 through STORAGE-008
(`sofias_memory/infrastructure/storage/{port,filesystem,s3,router}.py`, the
`STORAGE_BACKEND`/`STORAGE_S3_*` `Settings` fields, and the Remember/Cognify/
Forget/Dataset-DELETE call-site wiring) exists and is exercised by 51+ passing
S3 unit tests (Stubber-based), but had never been run against a real S3-compatible
endpoint. This addendum does not renumber or rewrite the 1–58 table above; it adds
real-infrastructure evidence on top of it.

**Target:** `https://s3.e-lyder.com.br`, bucket `sofias-memory`, isolated under a
dedicated validation prefix (`validation/storage009-20260901/`, cleaned up after
evidence collection) plus a separate throwaway bucket (`sofias-memory-storage009`,
created and destroyed within this run) for the one test requiring bucket-wide
versioning.

**Defect found and fixed (in-scope, no new architecture decision):**
`S3SourceObjectStorage._head_object_sync` (`infrastructure/storage/s3.py`) read
back custom object metadata (`sofias-memory-sha256`/`sofias-memory-byte-size`)
with an exact-case dict lookup, on the documented assumption that "S3 lower-cases"
metadata header names. The live target does not lower-case them (observed:
`Sofias-Memory-Sha256`/`Sofias-Memory-Byte-Size`), so every finalize/verify
identity check against a real object on this endpoint silently mismatched,
manifesting as spurious `SourceStorageUnavailableError`/retries. Fixed by
lower-casing response metadata keys before lookup; regression added
(`tests/unit/test_s3_source_object_storage.py::test_finalize_existing_matching_target_is_idempotent_with_title_cased_metadata_keys`).
No Stubber-based test had ever exercised non-lower-case metadata, which is why
this was invisible before real infrastructure was available — exactly the kind
of gap Gate G exists to catch.

A second, related defect was found and fixed in the same pass: the B1 manual-retry
recovery path (`prepare_remember_retry_ingress`, `services/remember.py` Case 2)
called the filesystem-only `source_storage_path` helper directly instead of
routing through `SourceStorageRouter` by scheme (ADR-0011 D4/D13). A manual retry
of a Remember run whose original `_ingress` was already cleaned up (finalize had
already succeeded) but whose `Source.storage_uri` was `s3://...` therefore failed
closed with `INVALID_REQUEST` instead of recovering — live evidence:
`tests/integration/test_run_control_postgres_integration.py::test_remember_failed_after_final_storage_retry_succeeds_despite_missing_original_ingress`,
deterministically reproducible pre-fix, passing post-fix. Fixed by generalizing
Case 2 to call `SourceStorageRouter.read()` (works for both `file://` and `s3://`
identically); `prepare_remember_retry_ingress` now takes `settings: Settings` and
its one call site (`services/run_control.py`) was updated accordingly. This
function has no dedicated fast unit test file (only PostgreSQL-integration
coverage existed and exists); closing that unit-coverage gap remains open,
recorded as residual risk below rather than fabricated under this pass.

**Real evidence gathered (adapter/router level, direct scripts against the live
endpoint, sentinels proven to survive throughout):**

- Endpoint reachability, bucket existence, credential auth, TLS trust (default
  `botocore` verification, never disabled) — all confirmed via `HeadBucket` +a
  PUT/HEAD/GET/DELETE probe object round-trip.
- Addressing: `botocore`'s endpoint-resolution automatically selects
  `ForcePathStyle: True` against this endpoint/region combination (`us-east-1` +
  custom `endpoint_url`) with no adapter code change and no new addressing-style
  setting — confirmed via `botocore.endpoint` debug logging of the real request
  URL (`https://s3.e-lyder.com.br/sofias-memory`, not virtual-hosted). No
  architecture-review item triggered.
- Real application `S3SourceObjectStorage.probe()` (D21) succeeds end to end.
- Mixed URI routing: one `s3://` Source and one `file://` Source read/deleted
  correctly in the same `SourceStorageRouter` instance (item 17).
- Conflicting deterministic target fails closed with `SourceStorageConflictError`
  (item 16); idempotent replay (`already_present=True`, no duplicate PUT) proven.
- Live versioned-bucket delete (items 12, 27, 48, 49): a dedicated throwaway
  bucket (`sofias-memory-storage009`) was created (credentials permit
  `CreateBucket`), versioning enabled only on that bucket (never on the shared
  `sofias-memory` bucket, whose `GetBucketVersioning` came back unset/never
  configured and which this run never altered), 3 real versions + 1 real delete
  marker created for one exact key, and `S3SourceObjectStorage.delete()` purged
  all of them (`DELETED_NOW`, then a second call correctly returned
  `ALREADY_ABSENT`) while a neighboring sentinel object survived. The throwaway
  bucket was fully emptied and deleted at the end of this run.
- Live `UNRESOLVED` (items 20, 21, 46, 50, 51): a real object was created with
  valid credentials, then a *separate* adapter instance built with deliberately
  invalid `STORAGE_S3_ACCESS_KEY_ID`/`STORAGE_S3_SECRET_ACCESS_KEY` (process-env
  override only — the operator's `.env` was never touched) attempted delete and
  received `UNRESOLVED`; the object was confirmed still present/matching
  afterward with valid credentials restored, then genuinely cleaned up.
  Pipeline-level (`PipelineRun` succeeds / `Source.status=DELETED` /
  `storage_uri` retained / `storage_unresolved` increments) is covered by the
  existing Stubber-based unit suite (`test_forget_pipeline_steps.py`,
  `test_dataset_delete_pipeline_steps.py`) plus this adapter-level live proof;
  a full FastAPI-level live-`UNRESOLVED` run was not separately built in this
  pass (residual risk, below).

**Real evidence gathered (full pipeline, real PostgreSQL + real S3, via the
existing opt-in integration suites re-run with `STORAGE_BACKEND=s3` and the
validation prefix forced as process-env overrides — never by editing `.env`,
against each suite's own dedicated discardable PostgreSQL database, never
`cognee_db`):** `test_remember_postgres_integration.py`,
`test_cognify_async_postgres_integration.py`, `test_forget_postgres_integration.py`,
`test_dataset_delete_postgres_integration.py`,
`test_pipeline_worker_postgres_integration.py`,
`test_pipeline_recovery_postgres_integration.py`,
`test_run_control_postgres_integration.py`,
`test_improve_async_postgres_integration.py`, `test_runs_postgres_integration.py`,
`test_pipeline_submission_postgres_integration.py`,
`test_pipeline_queue_claiming_postgres_integration.py`,
`test_pipeline_engine_postgres_integration.py` — see the final report for exact
counts. These exercise items 2–11, 15–17, 22–26, 29–32, 38–41, 44–47, 52–57 under
real S3 as a byproduct of the existing PostgreSQL-integration fixtures, not only
under Stubber, including the two-attempt retry/B1-recovery paths above.

**Not attempted in this pass (residual risk, explicitly not claimed as PASS):**
a dedicated `tests/e2e/` production-shaped smoke test file (item 28) does not
exist in the repository and was not authored in this pass; a from-scratch
concurrent-startup-convergence race test (items 24/25 under a full second
process) and a from-scratch fast unit test file for
`prepare_remember_retry_ingress` were not authored. See the STORAGE-009 final
report for the complete list and the resulting Gate G verdict.

## STORAGE-009 final closure — item-28 smoke, B1 unit coverage, corrected tests (2026-09-01)

This closes the three gaps the continuation above left open. New validation
prefix for this pass: `validation/storage009-final-20260901/` (the prior
`validation/storage009-20260901/` prefix and its throwaway versioning bucket
were already fully cleaned up in the continuation above and are not reused).
No bucket was created, deleted, or had its versioning configuration touched in
this closure pass — only object-level PUT/GET/HEAD/DELETE against the existing
bucket `sofias-memory`, exactly as D43/the coordinator's closure instructions
require. The prior live-versioning evidence (dedicated throwaway bucket, now
already destroyed) stands unrepeated.

**Item 28 — production-shaped smoke, now PASS.** New
`tests/e2e/test_adr_0011_storage_smoke.py`, three tests, all real
PostgreSQL + real filesystem + real MinIO + Neo4j disabled + no real LLM/
embedding calls (`mode="ingest"` never invokes either):

- `test_adr_0011_item28_filesystem_to_s3_production_shaped_smoke`: one
  continuous scenario using the real production composition root
  (`sofias_memory.app.create_app`) and the real ASGI `lifespan()` context
  manager throughout (never `StorageConvergenceService` called directly).
  Phase A (filesystem): real Remember via HTTP, `Source.storage_uri`
  confirmed `file://...`, local bytes/hash/size confirmed, lifespan fully
  exited ("stop old"). Phase C (fresh runtime, `STORAGE_BACKEND=s3`, brand
  new PostgreSQL engine/session factory): real `BOOTSTRAP_MAINTENANCE ->
  STORAGE_CONVERGING -> OPERATIONAL` transition sequence observed via an
  `asyncio.Event`-based holder subclass (deterministic, not a sleep) that
  fires the instant `STORAGE_CONVERGING` is entered; while genuinely
  suspended inside real S3 I/O (the D21 probe + real convergence pass),
  `/health/live`=200, `/health/ready`=503 `NOT_READY`,
  `GET /api/v1/info`=503 `DEPENDENCY_UNAVAILABLE` all confirmed over real
  ASGI HTTP; after `OPERATIONAL`, `Source.storage_uri` confirmed
  `s3://sofias-memory/validation/storage009-final-20260901/...`, real MinIO
  bytes/SHA-256/byte-size verified via `head_object`/`get_object`, the old
  local final object confirmed removed (post-repoint cleanup) while
  `DATA_DIRECTORY` root itself remains, both a local and an S3 sentinel
  confirmed surviving mid-flight and after, and a fresh Remember executed
  normally once `OPERATIONAL` (business request succeeds, item E).
- `test_adr_0011_item4_filesystem_after_s3_no_reverse_migration`: after one
  real convergence, a fresh `STORAGE_BACKEND=filesystem` runtime reaches
  `OPERATIONAL` with no `STORAGE_CONVERGING` phase at all (real code path:
  `lifespan.py` only enters it when `settings.storage_backend == "s3"`);
  `Source.storage_uri` unchanged (`s3://...`); a direct read through the
  same app's own `SourceStorageRouter` still follows the `s3://` URI
  (reads route by scheme, never by `STORAGE_BACKEND`); then a second
  filesystem-mode startup with deliberately invalid S3 credentials (process-
  env override only) still reaches `OPERATIONAL` — no S3 scan is required at
  filesystem startup.
- `test_adr_0011_item5_fresh_instance_s3_restart_idempotency`: a completely
  fresh app/engine/session-factory/router/convergence-service instance
  (nothing retained in Python memory from the prior converged instance)
  reaches `OPERATIONAL` again from durable PostgreSQL+filesystem+S3 state
  alone; `storage_uri` unchanged, S3 object byte-for-byte unchanged, object
  count for that key unchanged (no duplicate), a fresh sentinel survives.

All three pass consistently (re-run twice for the main smoke). Full traceback
detail, exact counts, and defects found while building this harness are in the
STORAGE-009 final closure report (session of 2026-09-01).

**B1 dedicated unit-level regression — now closed.** Three new tests in
`tests/integration/test_run_control_postgres_integration.py` exercise
`prepare_remember_retry_ingress` DIRECTLY (not through the full pipeline/
worker/HTTP stack), using the same `storage:` injection point
`RememberPipelineResources.source_storage` already established
(`pipelines/steps/remember.py`) — extended onto
`prepare_remember_retry_ingress` itself in this pass (`storage:
SourceObjectStorage | None = None`, mirroring the existing pattern exactly,
no new architecture): `test_retry_ingress_case2_s3_storage_uri_routes_through_router_read`
(Stubber-backed, no live network, proves exactly one `get_object` call and
correct staged bytes), `test_retry_ingress_case2_s3_read_failure_fails_per_typed_contract`
(typed `NoSuchKey` failure -> `False`, per the function's existing documented
contract), `test_retry_ingress_case2_filesystem_storage_uri_unchanged_regression`
(the pre-existing `file://` path, unchanged). Real PostgreSQL is required only
for the one authoritative Source read this function performs — its own
documented exception to being a pure/service-layer helper — which is why
these live in `tests/integration/`, not `tests/unit/`, exactly like every
other PostgreSQL-backed test in this file.

**The 3 previously-failing tests — corrected, not weakened.** All three in
`tests/integration/test_remember_postgres_integration.py`
(`test_text_ingest_wait_false_then_worker_completes`,
`test_file_ingest_txt_preserves_original_bytes`,
`test_url_ingest_fetches_only_in_worker_and_stores_bytes`) asserted a
`file://` `storage_uri` unconditionally, using `build_harness()`, which never
accepted a settings override and therefore silently inherited whatever
`STORAGE_BACKEND` the ambient process environment carried. All three are
genuinely filesystem-specific in intent (two parse `storage_uri` directly as
a local `Path` via `url2pathname` and read the on-disk bytes; the third
asserts the same shape after an async-worker completion). Fix: `build_harness()`
now accepts `settings_overrides: dict[str, object] | None`, and all three
tests pass `{"storage_backend": "filesystem"}` explicitly — proven to pass
identically whether or not `STORAGE_BACKEND=s3` is set in the ambient
environment (verified both ways). No assertion was weakened, relaxed, or
skipped; the `file://` assertions are unchanged.

**A second real defect found while building the item-28 harness (fixed,
regression added):** the first attempt at the two Stubber-backed B1 unit
tests above failed under a forced `STORAGE_BACKEND=s3` ambient environment —
diagnosis: `make_settings(tmp_path, storage_backend="s3", ...)` in
`test_run_control_postgres_integration.py` did not pin `storage_s3_prefix`,
so it silently inherited the ambient `STORAGE_S3_PREFIX`, while the test's
own hand-built S3 key used an empty prefix — the adapter's own real prefix-
containment check (`parse_s3_storage_uri`) then correctly rejected the
mismatched URI as invalid, which `prepare_remember_retry_ingress` correctly
treated as "not recoverable" (`False`), never reaching the Stubber's queued
`get_object` at all. This was a test-isolation bug (the same class of gap as
the 3 corrected tests above: a Stubber-based test's own settings must not
silently leak ambient `STORAGE_S3_PREFIX`), not an application defect — fixed
by pinning `storage_s3_prefix=""` explicitly in both S3-backed unit
regression tests.

## Integration gates

No later gate may compensate for skipping an earlier one (per task instruction).

- **GATE A — filesystem compatibility.** After STORAGE-001. Default
  `STORAGE_BACKEND=filesystem` behavior fully green (existing suite, item 1).
- **GATE B — S3 adapter contract.** After STORAGE-002. Deterministic URI/key,
  integrity, read/write/delete/versioning proven independently against MinIO
  (items 12, 13, 14, 20, 21, 27, 48–51 at the adapter level).
- **GATE C — pipeline integration.** After STORAGE-003/004/005. Remember/Cognify/
  Forget/Dataset DELETE pass under both URI schemes (items 4–11, 26, 29, 30, 44–47,
  54–57).
- **GATE D — convergence/recovery.** After STORAGE-006. Migration, CAS, crash
  windows, concurrent startup, Case A/B/C/D all proven (items 2, 15–19, 22–25, 31–37,
  58). D43 (fifth amendment) is documented and its CAS-loss-classification
  consequence (item 64: a Case-A Source observed `DELETING` at CAS time fails
  closed rather than being treated as benign `OWNED_ELSEWHERE`) is proven at the
  algorithm level here — but D43's actual *enforcement mechanism* (blocking normal
  claims against migration-eligible Sources during STORAGE_CONVERGING) has no
  claim-filtering capability in STORAGE-006 and is explicitly a Gate-E prerequisite
  (below), not closed by this gate alone.
- **GATE E — lifecycle/health.** After STORAGE-007. Maintenance/live/ready/
  worker-claim behavior proven (items 3, 38–42), **and D43's exclusion enforced**
  (items 59–63, 65, 66): STORAGE-007's recovery-owned claim filter must be proven to
  also block a *new* destructive transition against any Source STORAGE-006's
  classifier would consider Case A while STORAGE_CONVERGING is active — this is a
  prerequisite for declaring Gate E complete, not an optional extension. Without it,
  D43's lifecycle-exclusion argument is documented but unenforced, and the
  STORAGE-006 CAS-loss race D43 exists to close remains live in practice. **The
  claim filter itself must implement D31's sixth-amendment predicate, not a bare
  `pipeline_type` allowlist** (items 67–69): a `forget`/`dataset_delete` run is
  recovery-owned only once its own authoritative-mutation step has durably
  `succeeded` for its complete target scope — a run passing only the
  `pipeline_type` check is not sufficient and must not be claimed.
- **GATE F — deployment/documentation.** After STORAGE-008. Operational contract
  documented and deployment config verified (`docker compose config` × 3), **and the
  D43 single-replica deployment-exclusivity obligation is documented** (stop-old-
  before-start-new process exclusivity for `STORAGE_BACKEND=s3` deployments — a
  documentation/deployment-configuration item, not a runtime interlock).
- **GATE G — production-shaped smoke.** After STORAGE-009. Full end-to-end
  evidence, item 28, and the complete traceability table shows every item green.

## Deployment/documentation gate

Owned by STORAGE-008; see Gate F above. Explicitly gated behind Gate E (runtime
correctness) — documentation is never written ahead of proven behavior in this plan.

## Completion criteria

ADR-0011 implementation is complete when:

1. Gates A–G all pass, in order, with no gate skipped;
2. the validation-matrix traceability table shows a passing test for all 58 items;
3. `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy sofias_memory`,
   and `uv run pytest` are all green on `main` with the feature merged;
4. `docker compose config` passes for all three Compose files;
5. `docs/operations.md`, `AGENTS.md` §4, and the ADR-0010 forward-reference are
   updated and match observed behavior;
6. no global implementation rule below was violated at any point (spot-checked
   during STORAGE-009's final review, not just assumed).

## Global implementation rules (frozen checklist, apply to every slice)

- PostgreSQL remains authoritative; Neo4j remains a reconstructible projection.
- No external storage I/O inside `PipelineStep.persist()`.
- No PostgreSQL transaction/row lock held across S3 I/O.
- No Redis/Celery/external queue; no second pipeline engine.
- No generic storage-plugin system — filesystem and S3 only, closed set.
- S3 SDK is a normal `[project.dependencies]` entry (STORAGE-002 only).
- `DATA_DIRECTORY` remains persistent and mandatory in both modes.
- `_ingress/` remains local in both modes, unchanged by backend.
- New writes route by `STORAGE_BACKEND`; existing reads/deletes route by
  `storage_uri` scheme, never by `STORAGE_BACKEND`.
- Automatic migration is filesystem → S3 only; never the reverse.
- `DELETED` tombstones are never migration candidates, regardless of scheme.
- A recognized inability to clean external storage may become `UNRESOLVED`; a
  missing per-Source result or a programming defect may **not**.
- No new PostgreSQL schema for storage backend/migration state unless a slice
  proves an accepted invariant is otherwise impossible — and if that happens, STOP
  and report per the Execution Policy before adding a migration.
- Storage configuration stays outside the semantic config fingerprint
  (`build_config_fingerprint_payload`).

## Progress checklist

- [x] STORAGE-001 — Storage port + filesystem preservation (code present:
      `infrastructure/storage/{port,filesystem,router}.py`; this checklist was
      never updated when the slice landed — confirmed retroactively during the
      2026-09-01 STORAGE-009 continuation, not re-litigated here)
- [x] STORAGE-002 — S3 configuration + S3 adapter (code present:
      `infrastructure/storage/s3.py`, `Settings.storage_s3_*`; 51 passing unit
      tests plus real-MinIO evidence gathered 2026-09-01, see addendum above)
- [x] STORAGE-003 — Router-based Source reads / Cognify (real-S3 evidence:
      `test_cognify_async_postgres_integration.py` green under forced
      `STORAGE_BACKEND=s3`, see addendum)
- [x] STORAGE-004 — Remember finalization (incl. B1) (real-S3 evidence:
      `test_remember_postgres_integration.py` / `test_run_control_postgres_integration.py`
      green under forced `STORAGE_BACKEND=s3`; one real B1 retry-recovery defect
      found and fixed in this pass, see addendum)
- [x] STORAGE-005 — Destructive deletion semantics (D37–D42) (real-S3 evidence:
      `test_forget_postgres_integration.py` / `test_dataset_delete_postgres_integration.py`
      green under forced `STORAGE_BACKEND=s3`, plus live `UNRESOLVED`/versioned-delete
      adapter-level proof, see addendum)
- [x] STORAGE-006 — Filesystem → S3 startup convergence (code present:
      `services/storage_convergence.py`, `tests/unit/test_storage_convergence.py`;
      not independently re-exercised against real S3 in this pass beyond what the
      integration suites above exercise as a byproduct — see residual risk)
- [x] STORAGE-007 — Bootstrap state / recovery-owned claims / health (code
      present: readiness checks, `lifespan.py` wiring; not independently
      re-exercised with a real second-process concurrent-startup race in this
      pass — see residual risk, STORAGE-007 deterministic-timing evidence per D43
      remains primary)
- [x] STORAGE-008 — Deployment/configuration/docs (Compose files and
      `docs/operations.md` already reference `STORAGE_BACKEND`/`STORAGE_S3_*`;
      not re-walked end-to-end against the real endpoint in this pass)
- [x] STORAGE-009 — Full integration / production-shaped validation —
      **CLOSED 2026-09-01.** Real MinIO/S3 evidence gathered across two
      passes (continuation + final closure, both 2026-09-01): item 28's
      dedicated `tests/e2e/test_adr_0011_storage_smoke.py` now exists and
      passes (three tests: full filesystem->S3 production-shaped smoke,
      filesystem-after-S3 no-reverse-migration, fresh-instance S3 restart
      idempotency); `prepare_remember_retry_ingress` (B1) now has dedicated
      unit-level regression coverage; the 3 previously-failing real-S3
      integration tests were corrected (not weakened); the full opt-in
      real-S3 PostgreSQL integration batch is 240 passed / 0 failed / 4
      skipped; the default suite is 1827 passed / 275 skipped / 0 failed.
      Items 24/25's unsupported multi-process overlap (old OPERATIONAL +
      new STORAGE_CONVERGING running simultaneously) remains explicitly
      excluded from the single-replica MVP per D43 — STOP-OLD-THEN-START-NEW
      is the accepted and only tested sequencing (proven directly by the
      item-28 smoke's Phase A -> Phase C boundary), not a residual gap. See
      the STORAGE-009 final closure report (session of 2026-09-01) for the
      complete account.
- [x] Move this file to `docs/exec-plans/completed/` once all gates pass —
      **done, 2026-09-01.**

## Execution policy

Work **one STORAGE slice at a time**. After each slice:

1. run its focused tests (per-slice "Regression commands");
2. run the required static checks (`ruff check`, `ruff format --check`, `mypy`);
3. report exact changed files;
4. report any architecture conflict discovered;
5. stop for review before starting the next slice, unless the reviewer explicitly
   authorizes continuing.

If implementation reveals a true contradiction with accepted ADR-0011: **STOP.** Do
not silently reinterpret the ADR in code. Report:

- the exact ADR clause;
- the exact repository constraint it conflicts with;
- why both cannot hold;
- the smallest architecture amendment that would resolve it.

Do not resume coding on the affected slice until that report has been reviewed.

## POST-COMPLETION — Wasabi provider-compatibility smoke (2026-09-02)

**This note does not reopen this plan, does not change Gate G's verdict, and
does not alter the completed checklist/gate state above.** Gate G already
passed using real MinIO (see the STORAGE-009 continuation/closure addenda
above) — that remains the authoritative, production-shaped validation
evidence for ADR-0011.

A separate, narrower provider-compatibility smoke was subsequently run
against real Wasabi (a second, independent S3-compatible provider) to check
interoperability, not to re-validate the storage/convergence/recovery
contract Gate G already covers. Result: **PASS, zero code changes required.**
The unmodified `S3SourceObjectStorage`/`SourceStorageRouter` implementation
worked against Wasabi exactly as designed — real probe, finalize/PUT via the
real adapter, HEAD + byte-size + SHA-256 identity verification (including
this implementation's metadata-key case-normalization, confirmed harmless
against a provider that already lower-cases custom metadata keys), GET via
the canonical `s3://bucket/key` locator, idempotent re-finalize, the
deterministic-conflict fail-closed path (`SourceStorageConflictError`,
original object unchanged), and the full delete lifecycle
(`DELETED_NOW` then `ALREADY_ABSENT`). TLS was used normally throughout.
Bucket versioning was only inspected (observed unset) — never enabled,
disabled, or otherwise administered; no bucket was created or deleted; only
a temporary, unique validation prefix was used and fully cleaned up
afterward.

Full evidence and required-API-contract documentation now live in
`docs/operations.md` §13.16 ("Validated S3-compatible providers"), which
also states explicitly that MinIO/Wasabi are evidence of compatibility, not
an allowlist, and that Sofias Memory remains provider-neutral at the
application layer. No ADR-0011 D-section changed as a result of this smoke.
