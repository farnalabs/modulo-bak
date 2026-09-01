"""Unit tests pinning the runs-list query column deferral (Runs-page timeout fix).

``db_list_runs`` backs GET /api/v1/runs, the MCP list_runs tool, the MCP
``modulo://pipelines/{id}/runs`` resource, and the viewmodel RunSummary. The
page SELECT must NOT load the heavy per-run payload columns
(``crud.run._RUNS_LIST_DEFERRED_COLUMNS``) — on sandbox-heavy orgs each can
carry megabytes of outputs/telemetry, and no list consumer reads them.

Pinned here:

* every deferred column is absent from the compiled list SELECT (and from the
  count SELECT — which selects no run columns at all);
* the contract columns the responses DO read stay selected: ``input_payload``
  (masked into every REST list item) and ``cost_breakdown`` (read by the MCP
  pipeline-runs resource on DETACHED instances outside the session — a deferred
  access there would raise, not lazy-load);
* the deferral tuple contains only heavy JSON/Text payload columns, so a future
  edit cannot quietly defer a light column a consumer needs.

They run without a database (mock session, statement capture).
"""

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import JSON, Text

from modulo.db.crud.run import _RUNS_LIST_DEFERRED_COLUMNS
from modulo.db.crud.run import list_runs as db_list_runs
from modulo.db.models.run import Run

_MUST_STAY_LOADED = ("input_payload", "cost_breakdown")


def _compile(stmt: Any) -> str:
    from sqlalchemy.dialects import postgresql

    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


def _make_capturing_session() -> tuple[AsyncMock, list[Any]]:
    """A mock session whose execute() captures every statement it is given."""
    session = AsyncMock()
    captured: list[Any] = []
    count_result = MagicMock()
    count_result.scalar_one_or_none.return_value = 0
    rows_result = MagicMock()
    rows_result.scalars.return_value = []

    async def fake_execute(stmt: Any, *a: Any, **k: Any) -> Any:
        captured.append(stmt)
        if "count(" in str(stmt).lower():
            return count_result
        return rows_result

    session.execute = fake_execute
    return session, captured


def _items_statement(captured: list[Any]) -> Any:
    return next(stmt for stmt in captured if "count(" not in str(stmt).lower())


@pytest.mark.parametrize("column", _RUNS_LIST_DEFERRED_COLUMNS)
async def test_list_runs_select_omits_heavy_payload_column(column: str) -> None:
    session, captured = _make_capturing_session()

    page = await db_list_runs(session, page=1, page_size=20)

    assert page.total == 0
    assert captured, "list_runs must execute its queries through the session"
    items_sql = _compile(_items_statement(captured)).lower()
    assert f"runs.{column}" not in items_sql, (
        f"the runs-list SELECT must not load heavy payload column {column!r} — "
        "no list consumer reads it and detoasting it dominates the page latency"
    )


async def test_list_runs_select_keeps_contract_columns() -> None:
    session, captured = _make_capturing_session()

    await db_list_runs(session, page=1, page_size=20)

    items_sql = _compile(_items_statement(captured)).lower()
    for column in _MUST_STAY_LOADED:
        assert f"runs.{column}" in items_sql, (
            f"the runs-list SELECT must keep loading {column!r} — list responses read it"
        )


def test_deferred_columns_are_heavy_payload_columns_only() -> None:
    """The deferral tuple must only ever name JSON/Text payload columns."""
    for name in _RUNS_LIST_DEFERRED_COLUMNS:
        column = Run.__table__.columns[name]
        assert isinstance(column.type, (JSON, Text)), (
            f"{name!r} is not a heavy payload column (type {column.type!r}) — "
            "only megabyte-scale JSON/Text columns belong in _RUNS_LIST_DEFERRED_COLUMNS"
        )


def test_deferral_excludes_consumer_read_columns() -> None:
    """input_payload and cost_breakdown must never land in the deferral tuple."""
    assert not set(_MUST_STAY_LOADED) & set(_RUNS_LIST_DEFERRED_COLUMNS)
