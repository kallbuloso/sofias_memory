"""Deterministic PostgreSQL advisory-lock key derivation (ADR-0009 SS D/SS 10).

Pure and side-effect free: no database session, no I/O. Keys must be stable
across processes and restarts, so this deliberately does not use Python's
built-in ``hash()`` (salted per-process, unstable across runs) -- it uses
``hashlib.sha256``, a stdlib deterministic digest.

PostgreSQL advisory lock functions taking a single ``bigint`` key accept the
signed 64-bit range ``[-2**63, 2**63 - 1]``. ``GLOBAL_BARRIER_KEY`` and
``dataset_lock_key()`` partition that range by construction, not by hashing
into two "different" namespaces before reducing to the same 64-bit space
(distinct hash prefixes alone do not create disjoint numeric ranges -- two
independent SHA-256 digests can still collide on their low 64 bits):

- ``GLOBAL_BARRIER_KEY`` is a fixed, reserved **negative** sentinel -- never
  derived from a hash.
- ``dataset_lock_key()`` is masked to 63 bits and is therefore always
  **non-negative** (``0 <= key <= 2**63 - 1``).

A negative value and a non-negative value can never be equal, so a
global-vs-dataset collision is impossible by construction, not merely
improbable. A dataset-vs-dataset collision between two different
``dataset_id`` values remains an accepted, extremely low-probability MVP
trade-off (~63 bits of hash space, single-replica worker, ADR-0009 SS D) --
that risk is probabilistic, not eliminated, and is not claimed to be.
"""

from __future__ import annotations

import hashlib
from typing import Final
from uuid import UUID

_KEY_BYTES = 8  # 64 bits of digest before masking.
_DATASET_KEY_MASK = (1 << 63) - 1  # low 63 bits -> always non-negative.

_DATASET_NAMESPACE_PREFIX = b"sofias_memory:adr-0009:pipeline_runs:dataset:"

GLOBAL_BARRIER_KEY: Final[int] = -1
"""Fixed, reserved negative sentinel for the global barrier lock.

Deliberately not hash-derived: staying negative by construction is what
makes disjunction from every ``dataset_lock_key()`` output (always
non-negative) a property of the two ranges themselves, not a sampled or
probabilistic guarantee.
"""


def dataset_lock_key(dataset_id: UUID) -> int:
    """Deterministic, namespaced advisory-lock key for one dataset.

    Same ``dataset_id`` always yields the same key, on any process, any
    restart. Always non-negative (``0 <= key <= 2**63 - 1``, 63 bits of
    digest space), so it can never equal :data:`GLOBAL_BARRIER_KEY`.

    A collision between two different ``dataset_id`` values is an accepted,
    extremely low-probability MVP trade-off (single ~63-bit hash space,
    single-replica worker) per ADR-0009 SS D -- not treated as an open
    correctness gap here, and not what this function's disjunction guarantee
    is about.
    """

    digest = hashlib.sha256(_DATASET_NAMESPACE_PREFIX + dataset_id.bytes).digest()[:_KEY_BYTES]
    value = int.from_bytes(digest, byteorder="big", signed=False)
    return value & _DATASET_KEY_MASK
