from pathlib import Path

import pytest

from scripts import verify_installation


def test_check_python_version_accepts_current_runtime() -> None:
    ok, message = verify_installation.check_python_version()

    assert ok is True
    assert "Python version OK" in message


def test_check_package_importable_returns_location() -> None:
    ok, message = verify_installation.check_package_importable()

    assert ok is True
    assert "sofias_memory" in message


def test_check_pyproject_available_from_repo_root() -> None:
    ok, message = verify_installation.check_pyproject_available()

    assert ok is True
    assert "pyproject" in message


def test_check_pyproject_available_fails_outside_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    ok, message = verify_installation.check_pyproject_available()

    assert ok is False
    assert "pyproject missing" in message


def test_main_returns_zero_when_checks_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verify_installation,
        "run_checks",
        lambda: [(True, "first ok"), (True, "second ok")],
    )

    assert verify_installation.main() == 0


def test_main_returns_nonzero_when_a_check_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        verify_installation,
        "run_checks",
        lambda: [(True, "first ok"), (False, "second failed")],
    )

    assert verify_installation.main() == 1
