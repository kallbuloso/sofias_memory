"""CI-only release consistency guardrail (REL-004, hardened SM-607 SS 6, SM-901
EasyPanel parity hardening).

Verifies two things that must never silently drift, using only the Python
standard library plus `docker compose config` (no new runtime dependency,
no PyYAML):

1. Settings/.env.example/root Compose/EasyPanel Compose parity -- every
   application Settings alias must appear in `.env.example` and in the
   rendered `sofias-memory` service environment of *both* `compose.yaml`
   and `deploy/easypanel/compose.yaml`, and none of those surfaces may
   declare a key that isn't a real Settings alias or a declared
   infrastructure-only variable (this catches a new Setting added to the
   code but forgotten in any of the other places, not just a
   one-directional diff). EasyPanel's Compose file was previously checked
   only for its image/`APP_VERSION` default (item 2 below), not for this
   environment-contract parity -- that gap let `DATABASE_MIGRATION_MODE`
   ship in the root Compose file and `.env.example` while silently missing
   from EasyPanel's own environment contract; this item now closes it.
2. Version consistency -- `pyproject.toml`'s canonical version must match
   the default `APP_VERSION` value in `.env.example`, in the rendered
   `sofias-memory` environment/build args, in the local Compose image tag,
   in the `Dockerfile`'s own `ARG APP_VERSION` default, and in
   `deploy/easypanel/compose.yaml`'s image tag and `APP_VERSION` default.
   `Settings.CANONICAL_APP_VERSION` must also match `pyproject.toml`. SM-607
   SS 6 added the `Dockerfile`/EasyPanel checks specifically because a prior
   bump (v0.2.0 -> did not happen automatically) updated the root Compose
   file and could have silently left those two surfaces behind -- this is a
   plain-text/regex read of each file, deliberately not a generic YAML
   parser, to avoid a PyYAML dependency for two single-line extractions.

Not a runtime feature: this script is never imported by `sofias_memory`,
only invoked directly by `ci.yml` and, optionally, by a developer locally.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parent.parent

# Infrastructure-only interpolation variables used solely to compose other
# values in compose.yaml (DATABASE_URL, NEO4J_AUTH, healthchecks) -- never
# themselves Settings aliases, and never expected in .env.example.
INFRASTRUCTURE_ONLY_VARS = frozenset(
    {
        "DB_PASSWORD",
        "DB_NEO4J_PASSWORD",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "SOFIAS_MEMORY_HTTP_PORT",
        "VCS_REF",
    }
)


def load_pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    return str(data["project"]["version"])


def load_settings_aliases() -> set[str]:
    sys.path.insert(0, str(REPO_ROOT))
    from sofias_memory.config import CANONICAL_APP_VERSION, Settings

    aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
    canonical = load_pyproject_version()
    if canonical != CANONICAL_APP_VERSION:
        raise SystemExit(
            "FAIL: Settings.CANONICAL_APP_VERSION "
            f"({CANONICAL_APP_VERSION!r}) != pyproject.toml version ({canonical!r})"
        )
    return aliases


def load_env_example_keys(settings_aliases: set[str]) -> set[str]:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    keys: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        # Optional settings may intentionally be documented as commented
        # placeholders (for example the STORAGE_S3_* surface). Count only the
        # canonical ``# KEY=...`` form and only when KEY is a real Settings
        # alias, so prose comments containing '=' can never become declarations.
        if stripped.startswith("#"):
            candidate = stripped.removeprefix("#").strip()
            if "=" not in candidate:
                continue
            key = candidate.split("=", 1)[0].strip()
            if key in settings_aliases:
                keys.add(key)
            continue

        if "=" not in stripped:
            continue
        keys.add(stripped.split("=", 1)[0].strip())
    return keys


def load_env_example_app_version() -> str:
    for line in (REPO_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("APP_VERSION="):
            return line.strip().split("=", 1)[1]
    raise SystemExit("FAIL: APP_VERSION not found in .env.example")


def load_dockerfile_arg_version() -> str:
    text = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r"^ARG APP_VERSION=(\S+)$", text, flags=re.MULTILINE)
    if match is None:
        raise SystemExit("FAIL: 'ARG APP_VERSION=' not found in Dockerfile")
    return match.group(1)


def load_easypanel_image_version() -> str:
    text = (REPO_ROOT / "deploy" / "easypanel" / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"image:\s*ghcr\.io/kallbuloso/sofias-memory:(\S+)", text)
    if match is None:
        raise SystemExit(
            "FAIL: 'image: ghcr.io/kallbuloso/sofias-memory:...' not found in "
            "deploy/easypanel/compose.yaml"
        )
    return match.group(1)


def load_easypanel_app_version_default() -> str:
    text = (REPO_ROOT / "deploy" / "easypanel" / "compose.yaml").read_text(encoding="utf-8")
    match = re.search(r"APP_VERSION:\s*\"\$\{APP_VERSION:-([^}]+)\}\"", text)
    if match is None:
        raise SystemExit(
            "FAIL: 'APP_VERSION: \"${APP_VERSION:-...}\"' not found in "
            "deploy/easypanel/compose.yaml"
        )
    return match.group(1)


def render_compose_config(compose_path: Path | None = None) -> dict[str, Any]:
    """Render a Compose file to its resolved JSON config via `docker compose
    config`. Defaults to the root `compose.yaml` (Compose's own default file
    discovery, run from `REPO_ROOT`); pass an explicit `compose_path` (e.g.
    `deploy/easypanel/compose.yaml`) to render a different Compose file with
    the exact same disposable CI environment, so both surfaces are checked
    through one implementation of "what does Compose actually resolve this
    to" rather than two independent regex-based approximations."""

    env = os.environ.copy()
    env.setdefault("API_KEY", "sf-" + "a" * 32)
    env.setdefault("DB_PASSWORD", "ci-disposable-password")
    env.setdefault("DB_NEO4J_PASSWORD", "ci-disposable-password")
    env.setdefault("LLM_API_KEY", "sk-ci-disposable")
    command = ["docker", "compose"]
    if compose_path is not None:
        command += ["-f", str(compose_path)]
    command += ["config", "--format", "json"]
    result = subprocess.run(
        command,
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return cast(dict[str, Any], json.loads(result.stdout))


def check_sofias_memory_environment_parity(
    *,
    label: str,
    compose_env_keys: set[str],
    settings_aliases: set[str],
    failures: list[str],
) -> None:
    """Apply the same alias-parity rule to any rendered Compose file's
    `sofias-memory` service environment: every Settings alias must be
    present, and every present key must be either a Settings alias or a
    declared infrastructure-only variable. `label` names the Compose file in
    the failure message (e.g. `"compose.yaml"` or
    `"deploy/easypanel/compose.yaml"`) so a regression is immediately
    diagnosable."""

    missing = settings_aliases - compose_env_keys
    if missing:
        failures.append(
            f"Settings aliases missing from {label}'s sofias-memory "
            "environment: " + ", ".join(sorted(missing))
        )
    unexpected = compose_env_keys - settings_aliases - INFRASTRUCTURE_ONLY_VARS
    if unexpected:
        failures.append(
            f"{label} environment keys that are neither Settings aliases "
            "nor declared infrastructure-only variables: " + ", ".join(sorted(unexpected))
        )


def main() -> int:
    failures: list[str] = []

    settings_aliases = load_settings_aliases()
    env_example_keys = load_env_example_keys(settings_aliases)

    config = render_compose_config()
    app_service = cast(dict[str, Any], config["services"]["sofias-memory"])
    compose_env = cast(dict[str, str], app_service["environment"])
    compose_env_keys = set(compose_env)

    easypanel_compose_path = REPO_ROOT / "deploy" / "easypanel" / "compose.yaml"
    easypanel_config = render_compose_config(easypanel_compose_path)
    easypanel_app_service = cast(dict[str, Any], easypanel_config["services"]["sofias-memory"])
    easypanel_compose_env_keys = set(cast(dict[str, str], easypanel_app_service["environment"]))

    # --- Settings/.env.example parity ---
    settings_only = settings_aliases - env_example_keys
    if settings_only:
        failures.append(
            "Settings aliases missing from .env.example: " + ", ".join(sorted(settings_only))
        )
    env_example_only = env_example_keys - settings_aliases
    if env_example_only:
        failures.append(
            ".env.example keys that are not Settings aliases: "
            + ", ".join(sorted(env_example_only))
        )

    # --- Settings/root Compose/EasyPanel Compose parity ---
    check_sofias_memory_environment_parity(
        label="compose.yaml",
        compose_env_keys=compose_env_keys,
        settings_aliases=settings_aliases,
        failures=failures,
    )
    check_sofias_memory_environment_parity(
        label="deploy/easypanel/compose.yaml",
        compose_env_keys=easypanel_compose_env_keys,
        settings_aliases=settings_aliases,
        failures=failures,
    )

    # --- Version consistency ---
    canonical = load_pyproject_version()
    env_example_version = load_env_example_app_version()
    if env_example_version != canonical:
        failures.append(
            f".env.example APP_VERSION ({env_example_version!r}) != "
            f"pyproject.toml version ({canonical!r})"
        )
    compose_app_version = compose_env.get("APP_VERSION")
    if compose_app_version != canonical:
        failures.append(
            f"Compose-rendered APP_VERSION ({compose_app_version!r}) != "
            f"pyproject.toml version ({canonical!r})"
        )
    build_args: dict[str, str] = app_service.get("build", {}).get("args", {})
    build_arg_version = build_args.get("APP_VERSION")
    if build_arg_version != canonical:
        failures.append(
            f"Compose build.args.APP_VERSION ({build_arg_version!r}) != "
            f"pyproject.toml version ({canonical!r})"
        )
    image_tag = str(app_service.get("image", ""))
    image_version = image_tag.rsplit(":", 1)[-1] if ":" in image_tag else ""
    if image_version != canonical:
        failures.append(
            f"Compose local image tag version ({image_version!r}) != "
            f"pyproject.toml version ({canonical!r})"
        )

    dockerfile_version = load_dockerfile_arg_version()
    if dockerfile_version != canonical:
        failures.append(
            f"Dockerfile ARG APP_VERSION default ({dockerfile_version!r}) != "
            f"pyproject.toml version ({canonical!r})"
        )
    easypanel_image_version = load_easypanel_image_version()
    if easypanel_image_version != canonical:
        failures.append(
            f"deploy/easypanel/compose.yaml image tag version "
            f"({easypanel_image_version!r}) != pyproject.toml version ({canonical!r})"
        )
    easypanel_app_version = load_easypanel_app_version_default()
    if easypanel_app_version != canonical:
        failures.append(
            f"deploy/easypanel/compose.yaml APP_VERSION default "
            f"({easypanel_app_version!r}) != pyproject.toml version ({canonical!r})"
        )

    if failures:
        print("Release consistency check FAILED:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1

    print(f"Release consistency check OK (canonical version {canonical!r}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
