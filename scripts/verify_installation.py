from __future__ import annotations

import importlib.abc
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

MIN_PYTHON = (3, 12)
MAX_PYTHON = (3, 13)


def check_python_version() -> tuple[bool, str]:
    version = sys.version_info
    supported = MIN_PYTHON <= (version.major, version.minor) < MAX_PYTHON
    version_text = f"{version.major}.{version.minor}.{version.micro}"
    if supported:
        return True, f"Python version OK: {version_text}"

    return False, f"Python version unsupported: {version_text}"


def check_package_importable() -> tuple[bool, str]:
    repo_root = Path(__file__).resolve().parents[1]
    spec = importlib.machinery.PathFinder.find_spec("sofias_memory", [str(repo_root)])
    if spec is None or spec.origin is None:
        return False, "Package import failed: sofias_memory"

    if spec.loader is None or not isinstance(spec.loader, importlib.abc.Loader):
        return False, "Package loader unavailable: sofias_memory"

    try:
        package = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(package)
    except ModuleNotFoundError:
        return False, "Package import failed: sofias_memory"

    package_file = getattr(package, "__file__", None)
    if not package_file:
        return False, "Package location unavailable: sofias_memory"

    return True, f"Package import OK: {Path(package_file)}"


def check_pyproject_available() -> tuple[bool, str]:
    pyproject = Path.cwd() / "pyproject.toml"
    if pyproject.is_file():
        return True, f"pyproject OK: {pyproject}"

    return False, f"pyproject missing: {pyproject}"


def run_checks() -> list[tuple[bool, str]]:
    return [
        check_python_version(),
        check_package_importable(),
        check_pyproject_available(),
    ]


def main() -> int:
    results = run_checks()
    for ok, message in results:
        status = "OK" if ok else "FAIL"
        print(f"{status}: {message}")

    return 0 if all(ok for ok, _message in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
