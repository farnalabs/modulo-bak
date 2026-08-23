"""Unit tests for autonomy telemetry emission (fail-open evidence record)."""

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest

from modulo.core.run_context import autonomy_telemetry as at

pytestmark = pytest.mark.asyncio


class _NullSession:
    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _session_factory() -> Any:
    return _NullSession()


async def test_emits_event_with_expected_payload() -> None:
    import modulo.core.audit_logger as al

    captured: dict[str, Any] = {}
    al.append_audit_event = AsyncMock()

    org_id = uuid.uuid4()
    run_id = uuid.uuid4()
    pipeline_id = uuid.uuid4()

    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=org_id,
        run_id=run_id,
        gate_id="g1",
        autonomy_level="fully_autonomous",
        gate_outcome="skipped",
        pipeline_id=pipeline_id,
    )

    assert al.append_audit_event.await_count == 1
    _, kwargs = al.append_audit_event.call_args
    captured = kwargs
    assert captured["event_type"] == at.AUTONOMY_LEVEL_APPLIED
    assert captured["org_id"] == org_id
    assert captured["resource_type"] == "run"
    assert str(captured["resource_id"]) == str(run_id)
    assert captured["payload_json"]["autonomy_level"] == "fully_autonomous"
    assert captured["payload_json"]["gate_outcome"] == "skipped"
    assert captured["payload_json"]["gate_id"] == "g1"
    assert str(captured["payload_json"]["pipeline_id"]) == str(pipeline_id)


async def test_noop_when_session_factory_missing() -> None:
    import modulo.core.audit_logger as al

    al.append_audit_event = AsyncMock()

    await at.emit_autonomy_telemetry(
        None,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="fired",
    )
    assert al.append_audit_event.await_count == 0


async def test_invalid_gate_outcome_is_skipped() -> None:
    import modulo.core.audit_logger as al

    al.append_audit_event = AsyncMock()

    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="manual_approval",
        gate_outcome="bogus",
    )
    assert al.append_audit_event.await_count == 0


async def test_failure_is_fail_open() -> None:
    import modulo.core.audit_logger as al

    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("db down")

    al.append_audit_event = _boom

    # Must not raise — telemetry failures must never break a run.
    await at.emit_autonomy_telemetry(
        _session_factory,
        org_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        gate_id="g1",
        autonomy_level="notify_on_complete",
        gate_outcome="auto_approved",
    )
