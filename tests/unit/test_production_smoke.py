"""Unit coverage for scripts/production_smoke.py (REL-006).

No real HTTP or provider calls: `SmokeClient` is faked entirely so these
tests cover the script's own logic -- polling, guards, cleanup ordering,
marker assertion, and secret hygiene -- not the deployed API itself (that is
covered by the real REL-006 production smoke run, not by unit tests).
"""

from __future__ import annotations

import argparse
from typing import Any
from uuid import uuid4

import pytest

from scripts import production_smoke as smoke


def _args(**overrides: Any) -> argparse.Namespace:
    defaults = {"base_url": "http://127.0.0.1:8000", "timeout": 5.0, "poll_interval": 0.0}
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, amount: float) -> None:
        self.value += amount


class FakeSmokeClient:
    """Duck-typed stand-in for smoke.SmokeClient, driven entirely by
    pre-scripted responses -- no network I/O."""

    def __init__(
        self,
        *,
        run_sequence: list[dict[str, Any]] | None = None,
        recall_result: dict[str, Any] | None = None,
        dataset_id: str = "dataset-1",
        dataset_slug: str = "production-smoke-abc",
        delete_status_code: int = 200,
        delete_body: dict[str, Any] | None = None,
        second_run_sequence: list[dict[str, Any]] | None = None,
    ) -> None:
        self._run_sequence = list(run_sequence or [{"status": "succeeded"}])
        self._recall_result = recall_result
        self._dataset_id = dataset_id
        self._dataset_slug = dataset_slug
        self._delete_status_code = delete_status_code
        self._delete_body = delete_body or {
            "run_id": "delete-run-1",
            "dataset_id": dataset_id,
            "status": "succeeded",
        }
        self._second_run_sequence = list(second_run_sequence or [{"status": "succeeded"}])
        self.recall_called = False
        self.delete_called_with: str | None = None
        self.get_run_calls: list[str] = []

    def __enter__(self) -> FakeSmokeClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        return None

    def live(self) -> dict[str, Any]:
        return {"status": "ok"}

    def ready(self) -> dict[str, Any]:
        return {"status": "ready"}

    def info(self) -> dict[str, Any]:
        return {"version": "0.1.0-rc.1"}

    def create_dataset(self, name: str) -> dict[str, Any]:
        return {"dataset_id": self._dataset_id, "slug": self._dataset_slug}

    def remember_full(self, *, dataset_slug: str, content: str) -> dict[str, Any]:
        return {"run_id": "remember-run-1"}

    def get_run(self, run_id: str) -> dict[str, Any]:
        self.get_run_calls.append(run_id)
        sequence = self._run_sequence if run_id == "remember-run-1" else self._second_run_sequence
        if len(sequence) > 1:
            return sequence.pop(0)
        return sequence[0]

    def recall_chunks(self, *, query: str, dataset_slug: str) -> dict[str, Any]:
        self.recall_called = True
        if self._recall_result is not None:
            return self._recall_result
        return {
            "context": [{"text": f"marker present: {query}"}],
            "references": [{"source_id": "s1"}],
        }

    def delete_dataset(self, dataset_id: str) -> tuple[int, dict[str, Any]]:
        self.delete_called_with = dataset_id
        return self._delete_status_code, self._delete_body


# --- polling ---------------------------------------------------------------


def test_poll_run_terminal_succeeded() -> None:
    client = FakeSmokeClient(run_sequence=[{"status": "running"}, {"status": "succeeded"}])
    result = smoke.poll_run_terminal(
        client, "remember-run-1", timeout_seconds=10, poll_interval_seconds=0, sleep=lambda _s: None
    )
    assert result["status"] == "succeeded"


def test_poll_run_terminal_failed_raises() -> None:
    client = FakeSmokeClient(
        run_sequence=[
            {"status": "running"},
            {"status": "failed", "error_code": "X", "error_message": "boom"},
        ]
    )
    with pytest.raises(smoke.RunFailedError) as exc_info:
        smoke.poll_run_terminal(
            client,
            "remember-run-1",
            timeout_seconds=10,
            poll_interval_seconds=0,
            sleep=lambda _s: None,
        )
    assert exc_info.value.status == "failed"
    assert exc_info.value.error_code == "X"


def test_poll_run_terminal_timeout_raises() -> None:
    client = FakeSmokeClient(run_sequence=[{"status": "running"}])
    clock = FakeClock()

    def fake_sleep(_seconds: float) -> None:
        clock.advance(5.0)

    with pytest.raises(smoke.RunTimeoutError):
        smoke.poll_run_terminal(
            client,
            "remember-run-1",
            timeout_seconds=1.0,
            poll_interval_seconds=1.0,
            clock=clock,
            sleep=fake_sleep,
        )


# --- marker assertion --------------------------------------------------------


def test_marker_not_found_raises() -> None:
    with pytest.raises(smoke.SmokeError, match="marker"):
        smoke.assert_marker_recalled(
            {"context": [{"text": "unrelated content"}], "references": [{"source_id": "s1"}]},
            marker="SOFIAS_SMOKE_abc",
        )


def test_marker_found_passes() -> None:
    smoke.assert_marker_recalled(
        {
            "context": [{"text": "contains SOFIAS_SMOKE_abc here"}],
            "references": [{"source_id": "s1"}],
        },
        marker="SOFIAS_SMOKE_abc",
    )


# --- dataset delete guard -----------------------------------------------------


def test_guard_refuses_main_dataset() -> None:
    dataset = smoke.CreatedDataset(dataset_id="d1", slug="main")
    with pytest.raises(smoke.SmokeGuardError, match="main"):
        smoke.guard_dataset_deletable(dataset)


def test_guard_refuses_dataset_not_created_by_this_run() -> None:
    dataset = smoke.CreatedDataset(
        dataset_id="d1", slug="production-smoke-abc", created_by_this_run=False
    )
    with pytest.raises(smoke.SmokeGuardError, match="not created"):
        smoke.guard_dataset_deletable(dataset)


def test_guard_refuses_dataset_without_smoke_prefix() -> None:
    dataset = smoke.CreatedDataset(dataset_id="d1", slug="something-else")
    with pytest.raises(smoke.SmokeGuardError, match="prefix"):
        smoke.guard_dataset_deletable(dataset)


def test_guard_allows_valid_smoke_dataset() -> None:
    dataset = smoke.CreatedDataset(dataset_id="d1", slug="production-smoke-abc")
    smoke.guard_dataset_deletable(dataset)  # must not raise


# --- end-to-end run_smoke() orchestration ------------------------------------


def test_cleanup_attempted_when_recall_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOFIAS_MEMORY_API_KEY", "sf-test-key")
    client = FakeSmokeClient(recall_result={"context": [], "references": []})
    exit_code = smoke.run_smoke(_args(), client_factory=lambda: client)
    assert exit_code == 1
    assert client.recall_called
    assert client.delete_called_with == "dataset-1"


def test_async_dataset_delete_is_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOFIAS_MEMORY_API_KEY", "sf-test-key")
    client = FakeSmokeClient(
        delete_status_code=202,
        delete_body={"run_id": "delete-run-1", "dataset_id": "dataset-1", "status": "queued"},
        second_run_sequence=[{"status": "running"}, {"status": "succeeded"}],
    )
    exit_code = smoke.run_smoke(_args(), client_factory=lambda: client)
    assert exit_code == 0
    assert "delete-run-1" in client.get_run_calls


def test_happy_path_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOFIAS_MEMORY_API_KEY", "sf-test-key")
    client = FakeSmokeClient()
    exit_code = smoke.run_smoke(_args(), client_factory=lambda: client)
    assert exit_code == 0


# --- secret hygiene -----------------------------------------------------------


def test_api_key_never_appears_in_report_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = f"sf-{uuid4()}-super-secret"
    monkeypatch.setenv("SOFIAS_MEMORY_API_KEY", secret)
    client = FakeSmokeClient()
    smoke.run_smoke(_args(), client_factory=lambda: client)
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_missing_api_key_reported_without_leaking_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOFIAS_MEMORY_API_KEY", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    client = FakeSmokeClient()
    exit_code = smoke.run_smoke(_args(), client_factory=lambda: client)
    assert exit_code == 1


# --- base URL normalization ---------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://127.0.0.1:8000/", "http://127.0.0.1:8000"),
        ("http://127.0.0.1:8000///", "http://127.0.0.1:8000"),
    ],
)
def test_normalize_base_url(raw: str, expected: str) -> None:
    assert smoke.normalize_base_url(raw) == expected
