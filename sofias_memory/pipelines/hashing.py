"""Deterministic step input-hash derivation (ADR-0009 SS B / SS 11).

The hash must consider both the semantic input a step will act on and a
stable identity of the step's own definition/version, so that a registry
change able to alter behavior for the same input is detectable as drift.
Canonical JSON (sorted keys, stable separators, UTF-8) is used instead of
``repr()``, Python's built-in ``hash()``, or ``pickle`` -- all of which are
either unstable across processes/restarts or not semantically canonical.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256
from typing import Any

_SEPARATORS = (",", ":")


def canonical_json(payload: Any) -> str:
    """Stable JSON encoding: sorted keys, fixed separators, ASCII-safe."""

    return json.dumps(payload, sort_keys=True, separators=_SEPARATORS, ensure_ascii=True)


def canonical_step_input_hash(
    *,
    definition_id: str,
    semantic_input: Mapping[str, Any],
) -> str:
    """SHA-256 hex digest of ``(definition_id, semantic_input)``.

    Semantically equivalent input plus an unchanged ``definition_id`` always
    yields the same digest; a changed ``definition_id`` (the step's own
    definition/version identity, ADR-0009 SS 6) changes the digest even for
    otherwise identical input, which is exactly the drift signal SS B/SS 14
    rely on.
    """

    envelope = {"definition_id": definition_id, "input": semantic_input}
    encoded = canonical_json(envelope)
    return sha256(encoded.encode("utf-8")).hexdigest()


def canonical_work_payload_hash(work_input: Mapping[str, Any]) -> str:
    """SHA-256 hex digest of a durable submission's canonical work identity
    (ADR-0009 SS S). ``work_input`` must already exclude response-shape
    preferences like ``wait`` -- this function has no ``wait`` parameter by
    design, so a caller cannot accidentally fold it into the hash.

    Deliberately does NOT call :func:`canonical_json` (SM-509 audit Finding
    4): that helper is ``ensure_ascii=True`` for the step input hash, but
    B4's existing public ``payload_hash`` (``services.remember.
    stable_payload_hash``) has always used ``ensure_ascii=False``. This
    function reproduces that exact encoding -- sorted keys, the same fixed
    separators, UTF-8, ``ensure_ascii=False`` -- so a B5 submission's
    ``payload_hash`` for a given payload is byte-identical to what B4 would
    have computed for the same payload, without importing from (or editing)
    ``services.remember`` itself.
    """

    encoded = json.dumps(work_input, sort_keys=True, separators=_SEPARATORS, ensure_ascii=False)
    return sha256(encoded.encode("utf-8")).hexdigest()
