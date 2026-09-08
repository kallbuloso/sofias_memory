"""SM-705 structural audit: no hidden Skill integration exists in Recall,
Remember, Cognify, Session Context, PipelineRun/Worker, or Improve.

Skills are a standalone API surface (ADR-0013): none of these flows may
resolve a Skill automatically, fetch ``procedure``, append a SessionEntry
on a Skill's behalf, or execute ``declared_tools``. The cheapest, most
durable proof of "never wired in" is that these modules' source text never
even mentions "skill" -- a positive integration test would necessarily also
require Skill-aware code to exist somewhere in these files first."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

AUDITED_MODULES = (
    "sofias_memory/api/routes/recall.py",
    "sofias_memory/api/routes/remember.py",
    "sofias_memory/api/routes/cognify.py",
    "sofias_memory/api/routes/improve.py",
    "sofias_memory/api/routes/runs.py",
    "sofias_memory/services/recall.py",
    "sofias_memory/services/improve.py",
    "sofias_memory/services/pipeline_worker.py",
    "sofias_memory/services/pipeline_submission.py",
    "sofias_memory/services/pipeline_waiter.py",
    "sofias_memory/services/pipeline_recovery.py",
    "sofias_memory/services/pipeline_queue_claimer.py",
    "sofias_memory/domain/session_context.py",
)


@pytest.mark.parametrize("relative_path", AUDITED_MODULES)
def test_module_never_mentions_skill(relative_path: str) -> None:
    source = (REPO_ROOT / relative_path).read_text(encoding="utf-8")
    assert "skill" not in source.lower(), (
        f"{relative_path} mentions 'skill' -- Skills must remain a standalone "
        "surface with no automatic integration into Recall/Remember/Cognify/"
        "Session Context/PipelineRun/Worker/Improve (ADR-0013)."
    )
