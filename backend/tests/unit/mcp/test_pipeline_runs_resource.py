"""Unit tests for the modulo://pipelines/{id}/runs MCP resource."""

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from modulo.api.mcp_server import resource_pipeline_runs

_PLACEHOLDER_ORG_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
_API_KEY = "mk_testprefix_testsecretkey1234567890abc"
_PIPELINE_ID = uuid.uuid4()


def _make_mock_pipeline(*, name: str = "Test Pipeline") -> MagicMock:
    p = MagicMock()
    p.id = _PIPELINE_ID
    p.name = name
    return p


def _make_mock_run(
    *,
    run_id: uuid.UUID | None = None,
    status: str = "complete",
    trigger_type: str = "manual",
    total_tokens: int | None = 1500,
    total_cost_usd: float | None = 0.075,
    cost_breakdown: list[dict[str, Any]] | None = None,
) -> MagicMock:
    r = MagicMock()
    r.id = run_id or uuid.uuid4()
    r.status = status
    r.trigger_type = trigger_type
    r.created_at = MagicMock()
    r.created_at.isoformat.return_value = "2026-06-20T14:30:00+00:00"
    r.total_tokens = total_tokens
    r.total_cost_usd = total_cost_usd
    r.cost_breakdown = cost_breakdown
    return r


class _DetachedDeferredRun:
    """Run stand-in that mimics a detached deferred-column ORM instance.

    The runs-list query defers ``cost_breakdown`` (crud/run.py
    ``_RUNS_LIST_DEFERRED_COLUMNS``), so a read on the detached instance after
    the session closes would raise instead of lazy-loading. This stand-in
    reproduces that contract: reading ``cost_breakdown`` is only allowed while
    ``session_state["open"]`` is True.
    """

    def __init__(self, session_state: dict[str, bool]) -> None:
        self.id = uuid.uuid4()
        self.status = "complete"
        self.trigger_type = "manual"
        self.created_at = MagicMock()
        self.created_at.isoformat.return_value = "2026-06-20T14:30:00+00:00"
        self.total_tokens = 1500
        self.total_cost_usd = 0.075
        self._session_state = session_state
        self._breakdown: list[dict[str, Any]] = [
            {"component": "llm", "display_name": "LLM tokens", "amount_usd": 0.075, "source": "ledger"}
        ]

    @property
    def cost_breakdown(self) -> list[dict[str, Any]]:
        if not self._session_state["open"]:
            raise AssertionError(
                "cost_breakdown read outside the session — the MCP pipeline-runs "
                "resource must capture the deferred value in-session"
            )
        return self._breakdown


def _make_mock_session() -> AsyncMock:
    session = AsyncMock()
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


def _make_page_result(items: list, total: int) -> MagicMock:
    pr = MagicMock()
    pr.items = items
    pr.total = total
    pr.page = 1
    pr.page_size = 50
    return pr


# ---------------------------------------------------------------------------
# Auth error cases
# ---------------------------------------------------------------------------


class TestResourcePipelineRunsAuth:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=False)
    async def test_returns_auth_error_on_revoked_token(self, mock_validate_auth: AsyncMock) -> None:
        result = await resource_pipeline_runs(pipeline_id=str(uuid.uuid4()))
        assert "revoked" in result.lower() or "expired" in result.lower()


# ---------------------------------------------------------------------------
# Successful responses
# ---------------------------------------------------------------------------


class TestResourcePipelineRunsSuccess:
    def setup_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(_PLACEHOLDER_ORG_ID)
        _ctx_role.set("runner")
        _ctx_auth_token.set(_API_KEY)
        _ctx_auth_type.set("api_key")

    def teardown_method(self) -> None:
        from modulo.api.mcp_server import _ctx_auth_token, _ctx_auth_type, _ctx_org_id, _ctx_role

        _ctx_org_id.set(None)
        _ctx_role.set(None)
        _ctx_auth_token.set(None)
        _ctx_auth_type.set(None)

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.db.crud.run.list_runs")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.api.mcp_server._session")
    async def test_returns_runs_for_pipeline(
        self,
        mock_session: AsyncMock,
        mock_get_child_run_rollup: AsyncMock,
        mock_list_runs: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        pipeline = _make_mock_pipeline()
        mock_get_pipeline.return_value = pipeline

        run1 = _make_mock_run(
            run_id=uuid.uuid4(),
            status="complete",
            trigger_type="manual",
            total_tokens=1500,
            total_cost_usd=0.075,
        )
        run2 = _make_mock_run(
            run_id=uuid.uuid4(),
            status="failed",
            trigger_type="webhook",
            total_tokens=None,
            total_cost_usd=None,
        )
        mock_list_runs.return_value = _make_page_result(items=[run1, run2], total=2)
        # run1 has two child runs worth 0.125000 in total; run2 has none.
        mock_get_child_run_rollup.return_value = {run1.id: (Decimal("0.125000"), 2)}

        result = await resource_pipeline_runs(pipeline_id=str(_PIPELINE_ID))

        assert "Test Pipeline" in result
        assert str(run1.id) in result
        assert "complete" in result
        assert "manual" in result
        assert str(run2.id) in result
        assert "failed" in result
        assert "webhook" in result
        assert "1500" in result
        assert "0.075" in result or "$0.075" in result
        assert "2026-06-20T14:30:00+00:00" in result
        # Rollup fields are rendered per run, matching the REST API semantics.
        assert "child_count=2" in result
        assert "child_cost=$0.125000" in result
        assert "aggregate_cost=$0.200000" in result
        assert "child_count=0" in result
        assert "child_cost=$0.000000" in result
        assert "aggregate_cost=$0.000000" in result
        # The query must be scoped to the requested pipeline.
        mock_list_runs.assert_awaited_once()
        assert mock_list_runs.await_args.kwargs["pipeline_id"] == _PIPELINE_ID
        # ONE GROUP BY query for the whole page — never a per-run aggregate.
        mock_get_child_run_rollup.assert_awaited_once()
        assert set(mock_get_child_run_rollup.await_args.args[1]) == {run1.id, run2.id}

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.db.crud.run.list_runs")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.api.mcp_server._session")
    async def test_run_without_children_shows_zero_rollup(
        self,
        mock_session: AsyncMock,
        mock_get_child_run_rollup: AsyncMock,
        mock_list_runs: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        mock_get_pipeline.return_value = _make_mock_pipeline()

        run1 = _make_mock_run(run_id=uuid.uuid4(), total_cost_usd=0.075)
        mock_list_runs.return_value = _make_page_result(items=[run1], total=1)
        mock_get_child_run_rollup.return_value = {}

        result = await resource_pipeline_runs(pipeline_id=str(_PIPELINE_ID))

        assert "child_count=0" in result
        assert "child_cost=$0.000000" in result
        assert "aggregate_cost=$0.075000" in result
        mock_get_child_run_rollup.assert_awaited_once()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.db.crud.run.list_runs")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.api.mcp_server._session")
    async def test_run_with_cost_breakdown_renders_breakdown(
        self,
        mock_session: AsyncMock,
        mock_get_child_run_rollup: AsyncMock,
        mock_list_runs: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        mock_get_pipeline.return_value = _make_mock_pipeline()

        breakdown = [{"component": "llm", "display_name": "LLM tokens", "amount_usd": 0.075, "source": "ledger"}]
        run1 = _make_mock_run(run_id=uuid.uuid4(), total_cost_usd=0.075, cost_breakdown=breakdown)
        mock_list_runs.return_value = _make_page_result(items=[run1], total=1)
        mock_get_child_run_rollup.return_value = {}

        result = await resource_pipeline_runs(pipeline_id=str(_PIPELINE_ID))

        # The resource output still carries per-component cost data.
        assert "breakdown={" in result
        assert "- LLM tokens (llm): $0.075 ledger" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.db.crud.run.list_runs")
    @patch("modulo.db.crud.run.get_child_run_rollup")
    @patch("modulo.api.mcp_server._session")
    async def test_cost_breakdown_captured_in_session_not_detached(
        self,
        mock_session: AsyncMock,
        mock_get_child_run_rollup: AsyncMock,
        mock_list_runs: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        state = {"open": False}
        mock_session.return_value.__aenter__.side_effect = lambda: state.update(open=True)
        mock_session.return_value.__aexit__.side_effect = lambda *a: state.update(open=False)

        mock_get_pipeline.return_value = _make_mock_pipeline()

        run1 = _DetachedDeferredRun(state)
        mock_list_runs.return_value = _make_page_result(items=[run1], total=1)
        mock_get_child_run_rollup.return_value = {}

        result = await resource_pipeline_runs(pipeline_id=str(_PIPELINE_ID))

        # Rendering after the session closed still shows the breakdown captured
        # in-session; a detached read of the deferred column would have raised.
        assert "breakdown={" in result
        assert "- LLM tokens (llm): $0.075 ledger" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.db.crud.run.list_runs")
    @patch("modulo.api.mcp_server._session")
    async def test_empty_runs(
        self,
        mock_session: AsyncMock,
        mock_list_runs: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        pipeline = _make_mock_pipeline()
        mock_get_pipeline.return_value = pipeline

        mock_list_runs.return_value = _make_page_result(items=[], total=0)

        result = await resource_pipeline_runs(pipeline_id=str(_PIPELINE_ID))

        assert "no runs" in result.lower()

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server.get_pipeline")
    @patch("modulo.api.mcp_server._session")
    async def test_pipeline_not_found(
        self,
        mock_session: AsyncMock,
        mock_get_pipeline: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        session = _make_mock_session()
        mock_session.return_value.__aenter__.return_value = session

        mock_get_pipeline.return_value = None

        result = await resource_pipeline_runs(pipeline_id=str(_PIPELINE_ID))

        assert "not found" in result

    @patch("modulo.api.mcp_server.validate_current_auth", return_value=True)
    @patch("modulo.api.mcp_server._session")
    async def test_invalid_pipeline_id_returns_error(
        self,
        mock_session: AsyncMock,
        mock_validate_auth: AsyncMock,
    ) -> None:
        mock_session.return_value.__aenter__ = AsyncMock()

        result = await resource_pipeline_runs(pipeline_id="not-a-uuid")

        assert "Invalid UUID" in result
        mock_session.return_value.__aenter__.assert_not_called()
