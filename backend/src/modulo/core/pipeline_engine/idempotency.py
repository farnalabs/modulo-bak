"""Stable idempotency-key derivation for connector writes (FAR-410).

An operator re-run of an UNKNOWN-terminated connector node must reuse the SAME
persisted idempotency key so a write that may (or may not) have reached the
upstream is not re-applied as a fresh, distinct operation. A key minted from a
fresh random (or from a fresh per-replay ``run_id``) would break that contract.

The key is derived deterministically from a STABLE LOGICAL identity a re-run can
recompute (typically ``<pipeline_id>:<run_number>`` plus the node id and a
cardinality/fanout index). It is never a fresh random per run, and an in-run
retry reuses the identical key.

NOTE (persistence): the derived key is meant to be stored on the run record so
an operator re-run can READ it back. The runs-column migration is DEFERRED —
origin/main's migration chain is currently broken (``0136_rename_remy_*``
references the absent ``0135_status_check_constraints``, and migrations
0132-0134 are missing), so adding a new runs column migration here would
compound that breakage rather than fix it. The derivation primitive is the
delivered contract; the column lands once the chain is repaired.
"""

from __future__ import annotations

import hashlib

_IDEMPOTENCY_NAMESPACE = "modulo"


def stable_idempotency_key(
    *,
    run_ref: str,
    node_ref: str,
    index: int | str | None = None,
) -> str:
    """Derive a deterministic idempotency key for a single node execution.

    ``run_ref`` is the stable logical run identity a re-run can recompute
    (e.g. ``"<pipeline_id>:<run_number>"``), NOT the per-replay ``run_id`` —
    re-running the pipeline forks a fresh ``run_id``, so keying on it would mint
    a new key for the same logical work. ``node_ref`` is the node id/name;
    ``index`` is the item / fanout-cardinality position (``None`` for a
    single-execution node). The same inputs always produce the same key, which
    is what makes an operator re-run (and an in-run retry) reuse the identical
    persisted key.
    """
    raw = f"{_IDEMPOTENCY_NAMESPACE}:{run_ref}:{node_ref}"
    if index is not None:
        raw = f"{raw}:{index}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
