"""Production smoke test (REL-006).

Exercises an already-deployed Sofias Memory instance over HTTP only: it does
not start Compose, run migrations, touch Docker, or provision anything. It
proves a running deployment is actually functional by creating one isolated
smoke dataset, remembering one small piece of content through the real
provider-backed pipeline, recalling it back, and cleaning up after itself.

Usage:
    SOFIAS_MEMORY_API_KEY=sf-... uv run python scripts/production_smoke.py \\
        --base-url http://127.0.0.1:8000

The API key is read from the SOFIAS_MEMORY_API_KEY environment variable
(falling back to API_KEY for consistency with the rest of the project's
configuration) -- never from a CLI argument, so it never appears in a
process listing. It is never printed or logged.
"""

from __future__ import annotations

import argparse
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

API_KEY_ENV_VARS = ("SOFIAS_MEMORY_API_KEY", "API_KEY")
API_KEY_HEADER = "X-API-Key"

SMOKE_DATASET_PREFIX = "production-smoke-"
MAIN_DATASET_SLUG = "main"

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_POLL_INTERVAL_SECONDS = 2.0
HTTP_REQUEST_TIMEOUT_SECONDS = 30.0

TERMINAL_FAILURE_STATUSES = frozenset({"failed", "cancelled"})
TERMINAL_SUCCESS_STATUS = "succeeded"


class SmokeError(Exception):
    """Base for every error this script raises deliberately."""


class SmokeHTTPError(SmokeError):
    def __init__(self, *, stage: str, status_code: int, code: str | None, message: str | None):
        self.stage = stage
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(f"{stage}: HTTP {status_code} {code or ''} {message or ''}".strip())


class RunFailedError(SmokeError):
    def __init__(
        self, *, run_id: str, status: str, error_code: str | None, error_message: str | None
    ):
        self.run_id = run_id
        self.status = status
        self.error_code = error_code
        self.error_message = error_message
        super().__init__(
            f"run {run_id} ended in terminal non-success status={status} "
            f"error_code={error_code} error_message={error_message}"
        )


class RunTimeoutError(SmokeError):
    def __init__(self, *, run_id: str, status: str, timeout_seconds: float):
        self.run_id = run_id
        self.status = status
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"run {run_id} did not reach a terminal state within {timeout_seconds}s "
            f"(last status={status})"
        )


class SmokeGuardError(SmokeError):
    """Raised when a dataset fails the pre-delete safety guard."""


@dataclass(frozen=True, slots=True)
class CreatedDataset:
    """A dataset this script itself created, and is therefore allowed to
    consider for deletion. Never construct one for a dataset resolved by any
    other means (e.g. looked up by name)."""

    dataset_id: str
    slug: str
    created_by_this_run: bool = True


@dataclass
class SmokeReport:
    lines: list[str] = field(default_factory=list)

    def passed(self, message: str) -> None:
        line = f"[PASS] {message}"
        self.lines.append(line)
        print(line)

    def failed(self, message: str) -> None:
        line = f"[FAIL] {message}"
        self.lines.append(line)
        print(line)


def resolve_api_key() -> str:
    for name in API_KEY_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    raise SmokeError(
        f"No API key found. Set one of: {', '.join(API_KEY_ENV_VARS)} in the environment "
        "(never as a CLI argument)."
    )


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def guard_dataset_deletable(dataset: CreatedDataset) -> None:
    """Refuse deletion unless every safety condition holds. Called
    immediately before the cleanup DELETE call -- never bypassed."""

    if not dataset.created_by_this_run:
        raise SmokeGuardError(
            f"refusing to delete dataset {dataset.dataset_id}: not created by this run"
        )
    if dataset.slug == MAIN_DATASET_SLUG:
        raise SmokeGuardError("refusing to delete the 'main' dataset")
    if not dataset.slug.startswith(SMOKE_DATASET_PREFIX):
        raise SmokeGuardError(
            f"refusing to delete dataset {dataset.dataset_id}: slug '{dataset.slug}' "
            f"does not start with the smoke prefix '{SMOKE_DATASET_PREFIX}'"
        )


class SmokeClient:
    """Thin HTTP wrapper: unwraps the SuccessEnvelope, raises SmokeHTTPError
    with only safe/stable fields on failure. Talks to an already-deployed
    instance; never touches infrastructure directly."""

    def __init__(self, base_url: str, api_key: str, *, timeout_seconds: float) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={API_KEY_HEADER: api_key},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SmokeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def live(self) -> dict[str, Any]:
        response = self._client.get("/health/live")
        return _unwrap_plain(response, stage="live")

    def ready(self) -> dict[str, Any]:
        response = self._client.get("/health/ready")
        return _unwrap_plain(response, stage="ready", allow_status_codes=(200, 503))

    def info(self) -> dict[str, Any]:
        response = self._client.get("/api/v1/info")
        return _unwrap_envelope(response, stage="info")

    def create_dataset(self, name: str) -> dict[str, Any]:
        response = self._client.post("/api/v1/datasets", json={"name": name})
        return _unwrap_envelope(response, stage="dataset create", allow_status_codes=(201,))

    def remember_full(self, *, dataset_slug: str, content: str) -> dict[str, Any]:
        response = self._client.post(
            "/api/v1/remember",
            json={
                "dataset": dataset_slug,
                "content": content,
                "mode": "full",
                "wait": False,
            },
        )
        return _unwrap_envelope(response, stage="remember", allow_status_codes=(200, 202))

    def get_run(self, run_id: str) -> dict[str, Any]:
        response = self._client.get(f"/api/v1/runs/{run_id}")
        return _unwrap_envelope(response, stage="get run")

    def recall_chunks(self, *, query: str, dataset_slug: str) -> dict[str, Any]:
        response = self._client.post(
            "/api/v1/recall",
            json={"query": query, "datasets": [dataset_slug], "mode": "chunks"},
        )
        return _unwrap_envelope(response, stage="recall")

    def delete_dataset(self, dataset_id: str) -> tuple[int, dict[str, Any]]:
        response = self._client.delete(f"/api/v1/datasets/{dataset_id}")
        body = _unwrap_envelope(response, stage="dataset delete", allow_status_codes=(200, 202))
        return response.status_code, body


def _unwrap_envelope(
    response: httpx.Response,
    *,
    stage: str,
    allow_status_codes: tuple[int, ...] = (200,),
) -> dict[str, Any]:
    if response.status_code not in allow_status_codes:
        _raise_from_response(response, stage=stage)
    body = response.json()
    data = body.get("data")
    if not isinstance(data, dict):
        raise SmokeHTTPError(
            stage=stage, status_code=response.status_code, code=None, message="malformed envelope"
        )
    return data


def _unwrap_plain(
    response: httpx.Response,
    *,
    stage: str,
    allow_status_codes: tuple[int, ...] = (200,),
) -> dict[str, Any]:
    if response.status_code not in allow_status_codes:
        _raise_from_response(response, stage=stage)
    body = response.json()
    if not isinstance(body, dict):
        raise SmokeHTTPError(
            stage=stage, status_code=response.status_code, code=None, message="malformed response"
        )
    return body


def _raise_from_response(response: httpx.Response, *, stage: str) -> None:
    code: str | None = None
    message: str | None = None
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message")
    except ValueError:
        pass
    raise SmokeHTTPError(stage=stage, status_code=response.status_code, code=code, message=message)


def poll_run_terminal(
    client: SmokeClient,
    run_id: str,
    *,
    timeout_seconds: float,
    poll_interval_seconds: float,
    clock: Any = time.monotonic,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    """Poll GET /api/v1/runs/{run_id} until a terminal status. Never polls
    forever -- timeout_seconds bounds the whole wait."""

    deadline = clock() + timeout_seconds
    run = client.get_run(run_id)
    while True:
        status = run.get("status")
        if status == TERMINAL_SUCCESS_STATUS:
            return run
        if status in TERMINAL_FAILURE_STATUSES:
            raise RunFailedError(
                run_id=run_id,
                status=str(status),
                error_code=run.get("error_code"),
                error_message=run.get("error_message"),
            )
        if clock() >= deadline:
            raise RunTimeoutError(
                run_id=run_id, status=str(status), timeout_seconds=timeout_seconds
            )
        sleep(poll_interval_seconds)
        run = client.get_run(run_id)


def assert_marker_recalled(recall_result: dict[str, Any], *, marker: str) -> None:
    context = recall_result.get("context")
    if not isinstance(context, list) or not context:
        raise SmokeError("recall returned empty context -- memory was not found")
    references = recall_result.get("references")
    if not isinstance(references, list) or not references:
        raise SmokeError("recall returned no references -- provenance is missing")
    if not any(marker in str(item.get("text", "")) for item in context):
        raise SmokeError("recall context did not contain the verification marker")


def run_smoke(
    args: argparse.Namespace,
    *,
    client_factory: Any = None,
) -> int:
    report = SmokeReport()
    base_url = normalize_base_url(args.base_url)

    try:
        api_key = resolve_api_key()
    except SmokeError as exc:
        report.failed(f"configuration: {exc}")
        return 1

    created_dataset: CreatedDataset | None = None
    exit_code = 0

    make_client = client_factory or (
        lambda: SmokeClient(base_url, api_key, timeout_seconds=HTTP_REQUEST_TIMEOUT_SECONDS)
    )
    with make_client() as client:
        try:
            client.live()
            report.passed("live")

            ready_body = client.ready()
            if ready_body.get("status") != "ready":
                raise SmokeError(f"not ready: {ready_body.get('status')}")
            report.passed("ready")

            info_body = client.info()
            report.passed(f"info version={info_body.get('version')}")

            unique = uuid.uuid4()
            dataset_name = f"{SMOKE_DATASET_PREFIX}{unique}"
            dataset_body = client.create_dataset(dataset_name)
            dataset_id = str(dataset_body["dataset_id"])
            dataset_slug = str(dataset_body["slug"])
            created_dataset = CreatedDataset(dataset_id=dataset_id, slug=dataset_slug)
            report.passed(f"dataset created id={dataset_id} slug={dataset_slug}")

            marker = f"SOFIAS_SMOKE_{unique}"
            content = f"Project Aurora uses component Nimbus.\nVerification marker: {marker}."
            remember_body = client.remember_full(dataset_slug=dataset_slug, content=content)
            remember_run_id = str(remember_body["run_id"])

            poll_run_terminal(
                client,
                remember_run_id,
                timeout_seconds=args.timeout,
                poll_interval_seconds=args.poll_interval,
            )
            report.passed(f"remember run {remember_run_id} succeeded")

            recall_body = client.recall_chunks(query=marker, dataset_slug=dataset_slug)
            assert_marker_recalled(recall_body, marker=marker)
            report.passed("recall marker found")

        except SmokeError as exc:
            report.failed(f"{type(exc).__name__}: {exc}")
            exit_code = 1
        except httpx.HTTPError as exc:
            report.failed(f"HTTP transport error: {exc}")
            exit_code = 1
        finally:
            if created_dataset is not None:
                try:
                    guard_dataset_deletable(created_dataset)
                    status_code, delete_body = client.delete_dataset(created_dataset.dataset_id)
                    delete_status = str(delete_body.get("status"))
                    if status_code == 202 and delete_status != TERMINAL_SUCCESS_STATUS:
                        delete_run_id = str(delete_body["run_id"])
                        poll_run_terminal(
                            client,
                            delete_run_id,
                            timeout_seconds=args.timeout,
                            poll_interval_seconds=args.poll_interval,
                        )
                        report.passed(f"dataset delete run {delete_run_id} succeeded")
                    else:
                        report.passed(f"dataset delete run {delete_body.get('run_id')} succeeded")
                except SmokeError as exc:
                    report.failed(f"cleanup {type(exc).__name__}: {exc}")
                    exit_code = 1
                except httpx.HTTPError as exc:
                    report.failed(f"cleanup HTTP transport error: {exc}")
                    exit_code = 1

    if exit_code == 0:
        print("PRODUCTION SMOKE PASS")
    return exit_code


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Deployed instance base URL.")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Seconds to wait for a PipelineRun to reach a terminal state.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Seconds between PipelineRun status polls.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_smoke(args)


if __name__ == "__main__":
    raise SystemExit(main())
