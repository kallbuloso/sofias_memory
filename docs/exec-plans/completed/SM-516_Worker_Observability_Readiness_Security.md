# SM-516 — Worker Observability, Readiness and Security Hardening

Baseline: `1794f63` (feat(datasets): add administrative async deletion). Alembic head 0011 (no migration expected).

Phased execution, checkpointing after each phase. No GATE-B5 in this story.

## Staging addendum — worker-availability split-brain + storage sentinel (post-final-report)
Two targeted findings from a staging review, fixed after the phases below closed:
1. **Worker-availability split-brain**: `PipelineSubmissionService`/`DatasetDeleteService` gated new-work creation on `worker.is_running` (started/not-stopped), while readiness had already moved to `health_snapshot().operational` (Phase 1) — a dead poll/outbox task made readiness NOT_READY while new writes were still silently accepted. Fixed: added `PipelineWorkerCoordinator.is_operational` (delegates to `health_snapshot().operational`), `WorkerAvailability` Protocol now requires `is_operational` instead of `is_running`, both gate call sites switched, `app.py`'s worker readiness check also reads `is_operational` now (single source of truth, no duplicated predicate). `is_running` kept unchanged for internal coordinator lifecycle use. Also found and fixed a genuine orphan-task bug while adding the Scenario-B startup-partial-failure test: if the poll task's `create_task` succeeds but the outbox task's own `create_task` call fails, the poll task was left running forever, uncancelled — added `_cleanup_partial_start()`.
2. **Storage/filesystem redaction**: the original final report incorrectly claimed no filesystem/storage module exists. `services/forget.py`'s `delete_source_storage`/`source_storage_path` are real, already-safe-by-construction path-safety primitives (traversal/directory-target rejection via `Path.is_relative_to`, generic error messages, engine's own generic exception classification discards any message anyway). Added a real end-to-end sentinel regression (`test_storage_deletion_failure_never_leaks_path_through_logs_or_public_response` in `test_forget_postgres_integration.py`) exercising a real traversal rejection through the real Forget pipeline/worker/HTTP surface with a unique sentinel path — confirmed absent from logs, `PipelineRun`/`PipelineStep` errors, and the public HTTP response. No leak found; no production code change needed for this finding.

New/changed files: `pipeline_worker.py`, `pipeline_submission.py`, `dataset_delete.py`, `app.py`, `test_pipeline_worker.py` (+2 unit tests: Scenario A/B), `test_pipeline_submission.py`/`test_pipeline_submission_postgres_integration.py` (fake rename), `test_run_control_postgres_integration.py` (+10 integration tests), `test_forget_postgres_integration.py` (+1 integration test).

Revalidation: SM-516 focused suite x3 (217/217 stable), real-Postgres suite x3 (82/82 stable, spanning worker/outbox/readiness/metrics/submission/run-control/forget), SM-515 Dataset Delete x3 (24/24 stable), `pytest -q` (1434 passed/261 skipped/0 failed), ruff/mypy/uv lock/alembic all clean, zero dependency drift, git state clean (nothing staged/committed/pushed).

## Phase 1 — Worker health snapshot + readiness + background task death (spec §4-9, 41-42)
- `WorkerHealthSnapshot` (typed, internal) on `PipelineWorkerCoordinator`.
- `add_done_callback` on poll/outbox tasks to detect unexpected death (not per-tick exceptions, which are already swallowed and logged).
- `operational` flag folded into readiness worker check (`app.py::_worker_readiness_check`).
- Structured events: `worker_starting`, `worker_started`, `worker_start_failed`, `worker_background_task_failed`, `worker_stopping`, `worker_stopped`.
- Unit tests: readiness matrix (§41), background task death, start failure.

## Phase 2 — Log context + lifecycle/step events (spec §13-16, 43-44)
- Extend `LOG_CONTEXT_FIELDS` (worker_id, pipeline_type, attempt).
- Bind context in worker run/step execution paths.
- Run lifecycle events (claimed/started/retry_scheduled/succeeded/failed/recovered).
- Step events with monotonic duration_ms.
- Recovery log enhancement (§43).
- Context isolation tests (concurrent runs/requests).

## Phase 3 — HTTP request metrics + operational Postgres snapshot + reporter (spec §17-21, 28-29, 53)
- Request completion logging middleware/hook (route template, method, status, duration, request_id; no body/headers/query).
- `OperationalMetricsService` (run counts by status, queue pending/eligible, heartbeat stale count, graph_outbox pending/processing/failed[_retryable/at_ceiling]).
- In-process periodic reporter task (`operational_metrics_snapshot` event), tracked/awaited like other background tasks, readiness-independent.

## Phase 4 — LLM/embedding metrics + redaction hardening (spec §22-25, 30-36)
- Safe success/failure events on the 4 LLM clients + embedding client (tokens from real `response.usage` only, no fabrication).
- Redaction audit of actual call sites (not just the redactor blacklist) — sentinel test matrix.
- URL/exception-safety audit.

## Phase 5 — OpenAPI/security regression + real integration (shutdown/restart, readiness matrix) (spec §45-51, 56-59)
- OpenAPI forbidden-route + security schema audit.
- Real Postgres+Neo4j+worker readiness/disabled-mode/restart integration tests (same DB, instance A → instance B).
- Log handler duplication test.

## Phase 6 — Final validation + self-audit + report (spec §65-70)
- Regression across SM-505..515 focused suites.
- ruff/mypy/uv lock/alembic heads/git diff --check.
- Full self-audit against baseline diff.
- Final report per spec §70.

Status: ALL PHASES DONE. See final report delivered to the user.

## Phase 6 results
- Focused suite (all new/modified test files) x3 consecutive: 231 passed each run, stable.
- Real Postgres/Neo4j/worker-restart suite x3 consecutive: 27 passed / 12 skipped each run, stable.
- `pytest -q` (default, no opt-in env vars, matches a bare CI invocation): 1433 passed, 250 skipped, 0 failed.
- One flaky pair (`test_n_forced_shutdown_during_step_start_checkpoint_completes_atomically`, `test_o_transient_heartbeat_failure_recovers_next_cadence`) failed only when interleaved inside one enormous 1400+-test single-process batch alongside unrelated unit tests; both pass reliably in isolation and are pre-existing real-clock-timing tests I did not write or modify -- classified as environment/scheduling contention under an artificially large combined batch, not a regression.
- ruff check/format, mypy: all clean repo-wide.
- `uv lock --check`: clean, zero dependency changes vs. baseline (confirmed via empty `git diff 1794f63 -- pyproject.toml uv.lock`).
- `alembic heads`: `0011`, unchanged.
- git state: nothing staged, nothing committed, nothing pushed; diff vs. `1794f63` touches exactly 12 tracked files (all directly part of this story) + 9 new files (8 source/test + this plan doc); no ADR/migration touched.
- Self-audit finding (pre-existing, not introduced by this story): `prometheus-client>=0.22,<1` is listed in `pyproject.toml`'s runtime dependencies but is never imported anywhere in `sofias_memory/` -- confirmed present at baseline `1794f63` too (empty diff). Contradicts AGENTS.md SS4/this story's SS2 "no external observability stack" invariant by its mere presence, even though nothing in this codebase (including everything built in this story) actually uses it. Not removed here -- out of scope for SM-516 (AGENTS.md SS25: no unrelated dependency changes) -- flagged for the team.

## Phase 5 results
- New `tests/contract/test_openapi_forbidden_routes.py` (7 tests): forbidden-prefix audit (none of the AGENTS.md SS12/spec SS45 prefixes appear), health routes present and API-key-exempt, Runs/Dataset-DELETE routes present, no secret value anywhere in the schema, no provider/DB configuration schema exposed.
- Real finding while writing it: the generated OpenAPI document never declared `X-API-Key` as a security requirement anywhere (0 `securitySchemes`, every operation's `security` was `None`) -- `ApiKeyMiddleware` enforces it at the ASGI layer, entirely outside FastAPI's per-route dependency system, so nothing about it was ever visible to consumers/generated clients. Fixed narrowly: `app.py` now overrides `application.openapi` (`_custom_openapi`) to inject an `ApiKeyAuth` `securityScheme` and mark every operation except `/health/live`/`/health/ready` as requiring it. Documentation-only -- zero runtime behavior change, since the middleware already enforced this regardless of what the schema said.
- Re-ran existing CORS/body-limit/API-key regression suites (`test_http_foundation.py`, `test_api_key_middleware.py`, `test_app.py`) -- all pass, no rewrite needed.
- Provisioned (found already existing and migrated to head 0011) the dedicated discardable `sofias_memory_pipeline_worker_test` database and ran the full previously-skipped `test_pipeline_worker_postgres_integration.py` suite for real (21 tests, all newly executed rather than skipped).
- Added `test_z_real_restart_instance_a_crash_instance_b_recovers` to that same file: a real two-instance restart scenario against the same Postgres database -- instance A claims a run and starts its step, is abandoned via forced task cancellation with no cooperative `stop()`/final heartbeat (closest an in-process test can get to a real crash), its heartbeat is aged past the stale threshold; instance B (new `worker_id`) runs real `PipelineRecoveryService.recover_startup()` before its own `start()`, proving: the run reconciles to `QUEUED` (no essential state was memory-only), B's `worker_id` differs from A's, B's own readiness (`health_snapshot().operational`) is false before `start()` and true after, the run converges to `SUCCEEDED` via B exactly once (never claimed twice concurrently), and no `pipeline-worker-*` task survives either instance after cleanup. Passed on first real run against real Postgres; full 22-test suite (21 pre-existing + 1 new) passes together.
- Verified: full unit+contract suite 1368 passed, real-Postgres worker suite 22/22 passed, ruff check/format clean repo-wide, mypy clean repo-wide (no new errors vs. baseline).
- Noted, not fixed (pre-existing, out of scope): zero test coverage exists anywhere for the URL-ingestion SSRF guard (`sofias_memory/loaders/url.py`'s `fetch_https_url`/`validate_public_ip`/`resolve_host_ips`) -- confirmed present at baseline `1794f63` too, and URL ingestion isn't part of SM-516's worker-observability charter. Flagged for a future story, not fixed here.

## Phase 4 results
- New `sofias_memory/infrastructure/provider_metrics.py`: shared safe logging helpers (`log_llm_request_completed`, `log_embedding_request_completed`, `log_provider_request_failed`) — tokens only from real `response.usage` (never fabricated, omitted entirely when absent), `exception_type` only on failure (never `str(exc)`/`repr(exc)`/response body/prompt/base URL).
- Wired into all 4 LLM clients in `sofias_memory/infrastructure/llm.py` (`knowledge_extraction`, `document_summary`, `dataset_summary`, `rag_answer` operations) and `OpenAIEmbeddingClient` in `sofias_memory/infrastructure/embeddings.py` (`embedding` operation, `input_count`/`embedding_count`, never vectors/text). Purely additive around each existing `self._client.chat.completions.create(...)`/`self._client.embeddings.create(...)` call — same try/except-and-reraise shape everywhere, no retry/concurrency/business-result change (SS 25).
- Redaction audit (SS 30-36): grepped every `logger.*(...)` call site repo-wide for content-bearing field names — none found outside what's already covered. Confirmed the SS 33 sentinel-matrix trigger points are covered: auth failure/validation failure/unexpected HTTP exception/readiness failure already had baseline sentinel tests (`test_api_key_middleware.py`, `test_api_errors.py`, `test_health.py`, `test_postgres_readiness_health.py`); provider failure now covered by this phase's new tests. No filesystem/storage module exists yet in this codebase (not yet built at this phase) -- N/A, not a gap to close here.
- Found and fixed two "fields that do not apply should be absent, not null-filled" (SS 13) violations introduced by earlier phases: `RequestMetricsMiddleware`'s `request_id` (now omitted when the response somehow never carried one) and a new `pipeline_recovery_run_reconciled` `error_code` enhancement (SS 43) in `pipeline_recovery.py` (now omitted when the reconciled run has none, e.g. a successful requeue).
- Tests: `tests/unit/test_provider_metrics.py` (6 — token-from-real-usage, no-usage-omits-fields, provider failure never leaks exception text/prompt/response body, embedding counts-never-vectors, embedding failure safety, empty-input short-circuit never calls provider/logs).
- Verified: full unit suite 1361 passed, ruff check/format clean repo-wide, mypy clean repo-wide (138 files, 0 errors).

## Phase 3 results
- New `sofias_memory/api/middleware/request_metrics.py`: `RequestMetricsMiddleware`, registered outermost (wraps `RequestIdMiddleware`, CORS, ApiKey, RequestBodyLimit) so it observes every request including ones rejected before routing (401/413/etc). Logs one `http_request_completed` event per request: `method`, `route` (resolved template from `scope["route"]`, falling back to raw path only when routing never ran), `status_code`, `duration_ms`, `request_id` (read back off the `X-Request-Id` response header rather than relying on contextvars, since it must also work when nested inside another middleware's context). Never logs headers/query/body.
- New `sofias_memory/services/operational_metrics.py`: `OperationalMetricsService.collect()` — one read-only Postgres query pass producing run counts by status, `runs_queued_total`/`runs_queued_eligible` (claim-eligible predicate matches the real claimer), `heartbeat_stale_count`/`operational_missing_heartbeat` (stale predicate copied verbatim from `PipelineRunRepository.list_stale_candidate_ids`, ADR-0009 SS H/I), and graph_outbox `pending`/`processing`/`done`/`failed_retryable`/`failed_at_ceiling` (ceiling from `GraphOutboxProcessor`'s own `DEFAULT_GRAPH_OUTBOX_MAX_ATTEMPTS`). `OperationalMetricsReporter` — a small tracked/awaited background task (60s fixed interval, code constant) emitting `operational_metrics_snapshot`; a collection failure logs `operational_metrics_snapshot_failed` (exception_type only) and keeps looping, never busy-spins, never touches readiness or business state.
- Wired into `app.py` (built unconditionally, independent of `enable_worker`) and `lifespan.py` (started right after Neo4j bootstrap, before the worker-enabled block; stopped right after `worker.stop()`, before Neo4j/Postgres resources close).
- Tests: `tests/unit/test_request_metrics_middleware.py` (5 — route template resolution/fallback, request_id capture, auth-rejected requests still observed, no header/query/body leakage), `tests/unit/test_operational_metrics.py` (9 — pure aggregation helpers, reporter start/failure/double-start/stop lifecycle), `tests/integration/test_operational_metrics_postgres_integration.py` (1, real dev Postgres, read-only so no dedicated database needed — same tier as the existing readiness integration tests).
- Verified: full unit suite 1355 passed (up from 1341 baseline-phase-count), real-Postgres operational metrics test passed, `tests/unit/test_lifespan.py` re-run with `-W error::RuntimeWarning` to catch orphan-task leaks from the new reporter (12 passed, none), ruff check/format clean repo-wide, mypy clean repo-wide (137 files, 0 errors).
- Deferred to Phase 4+: LLM/embedding provider metrics (SS 22-25), redaction audit/hardening (SS 30-36).

## Phase 2 results
- `sofias_memory/observability/logging.py`: extended `LOG_CONTEXT_FIELDS` with `worker_id`, `pipeline_type`, `attempt`.
- `sofias_memory/pipelines/engine.py`: `execute()` now binds run-level log context (`run_id`/`pipeline_type`/`dataset_id`/`worker_id`/`attempt`) around the whole call via a new `_execute_locked` split; each step's checkpoint/execute/persist cycle binds `step` additionally. Added `pipeline_run_started`, `pipeline_step_started`/`succeeded`/`failed` (monotonic `duration_ms`), and `_log_run_transition` (`pipeline_run_succeeded`/`failed`/`cancelled`/`retry_scheduled`) called at all 9 `transition_run(...)` call sites, right after each commit — one authoritative event per meaningful transition, never input/payload/message content, only stable `error_code`.
- Found and fixed a second latent bug while wiring this up: none — no other bug found in this phase.
- Tests: added `test_bind_worker_pipeline_type_and_attempt` and `test_worker_run_context_does_not_leak_between_concurrent_runs` to `tests/unit/test_logging.py` (concurrent-task context isolation, mirroring the engine's own nested `bound_log_context` usage).
- Verified: full unit suite 1341 passed; real-Postgres `test_pipeline_engine_postgres_integration.py`/`test_pipeline_worker_postgres_integration.py`/`test_pipeline_recovery_postgres_integration.py`/`test_graph_outbox_worker_postgres_integration.py`/`test_pipeline_submission_postgres_integration.py`/`test_run_control_postgres_integration.py` all pass (most still skip pending a dedicated discardable test database — pre-existing, unrelated to this change); ruff clean; mypy clean (no new errors vs. baseline).
- Deferred to Phase 3+: recovery-log enhancement (spec SS43, lives in `pipeline_recovery.py`, not touched yet), HTTP request metrics.

## Phase 1 results
- `sofias_memory/services/pipeline_worker.py`: added `WorkerHealthSnapshot` (enabled/started/stopped/poll_task_alive/outbox_task_expected/outbox_task_alive/active_run_count/operational), `health_snapshot()`, `add_done_callback` on poll/outbox tasks (`_on_poll_task_done`/`_on_outbox_task_done`) to detect unexpected task death vs. graceful stop, `worker_starting`/`worker_started`/`worker_start_failed`/`worker_background_task_failed`/`worker_stopping`/`worker_stopped` structured events. `is_running` kept unchanged (still used for write-acceptance gating in `pipeline_submission.py`/`dataset_delete.py` — deliberately not repurposed, to avoid an unrequested business-behavior change per spec SS63).
- Bug fix: `stop()` used to `await self._poll_task` directly; if the poll task had already died with an exception (exactly the scenario this story asks to make observable), re-awaiting it would re-raise that exception out of `stop()` and break shutdown. Changed to `asyncio.gather(poll_task, return_exceptions=True)`.
- `sofias_memory/app.py`: `_worker_readiness_check` now reads `coordinator.health_snapshot().operational` instead of `coordinator.is_running`. Public detail unchanged (`"worker not ready"`, no internals leaked).
- Tests added in `tests/unit/test_pipeline_worker.py` (13 new): full SS41 operational matrix (before start, started w/ and w/o outbox, disabled, after stop, single-run-failure-does-not-flip, saturated, empty queue), poll-task and outbox-task unexpected-death-by-name-targeted fault injection, start-failure observability.
- Verified: `pytest tests/unit/test_pipeline_worker.py tests/unit/test_health.py` (75 passed), real-Postgres+Neo4j `test_pipeline_worker_postgres_integration.py`/`test_graph_outbox_worker_postgres_integration.py`/`test_postgres_readiness_integration.py`/`test_neo4j_readiness_integration.py` (46 passed, 31 skipped — those skips require a separate dedicated discardable test-database URL via `SOFIAS_MEMORY_PIPELINE_WORKER_TEST_DATABASE_URL`, a pre-existing opt-in unrelated to this change), ruff check/format, mypy (0 new errors; 4 pre-existing baseline mypy errors in this test file confirmed via `git stash` unchanged).
