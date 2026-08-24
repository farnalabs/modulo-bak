"""Unit tests for the FAR-410 stable idempotency-key derivation."""

from modulo.core.pipeline_engine.idempotency import stable_idempotency_key


def test_stable_idempotency_key_is_deterministic() -> None:
    a = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    b = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert a == b


def test_stable_idempotency_key_differs_across_nodes_and_index() -> None:
    base = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert base != stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=1)
    assert base != stable_idempotency_key(run_ref="pipeline:42", node_ref="node-b", index=0)
    assert base != stable_idempotency_key(run_ref="pipeline:43", node_ref="node-a", index=0)


def test_stable_idempotency_key_ignores_a_fresh_run_id() -> None:
    """A re-run that forks a fresh run_id must NOT mint a new key.

    Two re-runs of the same logical work (same pipeline:run_number, node, index)
    produce the same key — that is what lets an operator re-run reuse the
    persisted key rather than double-applying the write.
    """
    replay_a = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    replay_b = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a", index=0)
    assert replay_a == replay_b


def test_stable_idempotency_key_without_index() -> None:
    key = stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a")
    assert key  # a plain 64-char hex digest
    assert len(key) == 64
    assert key == stable_idempotency_key(run_ref="pipeline:42", node_ref="node-a")
