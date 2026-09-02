"""Shared test-only composition-root helper (ADR-0011 D31/D43, STORAGE-007
fail-closed audit).

``sofias_memory.app.create_app`` now defaults to
``ProcessState.BOOTSTRAP_MAINTENANCE`` -- the correct fail-closed production
default (a real process must never accept business requests merely because
nobody has proven bootstrap/convergence finished). Most existing route/unit
tests build an app via ``create_app()`` and issue requests directly through
``httpx.ASGITransport`` *without* ever running the ASGI lifespan protocol at
all, so every one of those routes would otherwise observe a permanent 503
from ``OperationalGateMiddleware``.

This module is the single shared seam such a test deliberately opts into: an
already-``OPERATIONAL`` holder, exactly the compromise ``app.py`` used to
make unconditionally. Import ``create_app`` from here (instead of
``sofias_memory.app``) in any test that exercises an ordinary business/
readiness route without running lifespan.

Tests that instead need to prove the *real* fail-closed default, or the real
lifespan-driven transition out of it, must import ``create_app`` directly
from ``sofias_memory.app`` (see ``test_operational_gate.py``) -- a real
process boot (``lifespan()``) always force-resets the holder to
``BOOTSTRAP_MAINTENANCE`` at its own start regardless of what this helper (or
any other caller) constructed the app with, so using this helper's
already-``OPERATIONAL`` default is also safe for a ``TestClient(app)``-based
test that *does* run lifespan.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI

from sofias_memory.app import create_app as _create_app
from sofias_memory.services.process_state import ProcessState, ProcessStateHolder

__all__ = ["create_app"]


def create_app(*args: Any, **kwargs: Any) -> FastAPI:
    kwargs.setdefault("process_state_holder", ProcessStateHolder(state=ProcessState.OPERATIONAL))
    return _create_app(*args, **kwargs)
