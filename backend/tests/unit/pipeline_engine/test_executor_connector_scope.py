"""Tests for FAR-435 / FAR-418 executor run-level connector fetch scope wiring.

Covers the pure run-scope computation (``compute_run_fetch_scope``) and the
``_init_connector_hub`` wiring that passes the scope to
``ConnectorHub.initialise`` so the deny-by-default fetch-time gate applies at
run level.
"""

import uuid
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.capability_scope import compute_run_fetch_scope
from modulo.core.pipeline_engine.executor import PipelineExecutor

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _connector_node(*allowed: str) -> dict[str, Any]:
    scope = {"allowed_connectors": list(allowed)} if allowed else None
    return {"id": "node-conn", "node_type": "connector", "capability_scope": scope}


# ---------------------------------------------------------------------------
# Pure run-scope computation: compute_run_fetch_scope
# ---------------------------------------------------------------------------


def test_fully_scoped_graph_returns_union():
    gh = str(uuid.uuid4())
    sl = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": ["github", gh]}},
            {"capability_scope": {"allowed_connectors": ["slack", sl]}},
        ]
    }
    result = compute_run_fetch_scope(graph)
    assert set(result) == {"github", "slack", gh, sl}


def test_any_unrestricted_node_falls_back_to_none():
    gh = str(uuid.uuid4())
    graph = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": ["github", gh]}},
            {"node_type": "transform"},  # no capability_scope -> unrestricted
        ]
    }
    assert compute_run_fetch_scope(graph) is None


def test_empty_allowed_connectors_falls_back_to_none():
    graph = {"nodes": [{"capability_scope": {"allowed_connectors": []}}]}
    assert compute_run_fetch_scope(graph) is None


def test_empty_graph_returns_none():
    assert compute_run_fetch_scope({"nodes": []}) is None
    assert compute_run_fetch_scope(None) is None


def test_instance_id_and_type_entries_are_deduplicated():
    inst = str(uuid.uuid4())
    graph = {"nodes": [_connector_node("github", inst, "github")]}
    result = compute_run_fetch_scope(graph)
    assert set(result) == {"github", inst}


def test_mixed_union_dedupes_across_nodes():
    shared = str(uuid.uuid4())
    graph = {
        "nodes": [
            _connector_node("github", shared),
            _connector_node("slack", shared),
        ]
    }
    result = compute_run_fetch_scope(graph)
    assert set(result) == {"github", "slack", shared}


def test_malformed_nodes_and_non_dict_graph_are_tolerated():
    assert compute_run_fetch_scope({"nodes": [None, "junk", {"id": "x"}]}) is None
    assert (
        compute_run_fetch_scope(
            [],
        )
        is None
    )


# ---------------------------------------------------------------------------
# Wiring: _init_connector_hub passes the supplied scope to ConnectorHub.initialise
# ---------------------------------------------------------------------------


class _FakeScalar:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeScalar":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _FakeAsyncCM:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None


class _FakeSession:
    def __init__(self, conn_rows: list[Any]) -> None:
        self._conn_rows = conn_rows

    def begin(self) -> _FakeAsyncCM:
        return _FakeAsyncCM()

    async def execute(self, stmt: Any) -> _FakeScalar:
        return _FakeScalar(self._conn_rows)


class _FakeSessionCM:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_init_connector_hub_passes_scope_to_initialise():
    """A run-level fetch scope derived from graph node ``capability_scope`` is
    forwarded as ``allowed_connectors`` so the deny-by-default fetch-time gate
    applies at run level (FAR-435 building on FAR-418)."""
    fake_session = _FakeSession(conn_rows=[MagicMock()])
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSessionCM(fake_session)

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    mock_hub.__aenter__ = AsyncMock(return_value=mock_hub)
    mock_hub.__aexit__ = AsyncMock()
    graph_json = {
        "nodes": [
            {"capability_scope": {"allowed_connectors": ["github"]}},
            {"capability_scope": {"allowed_connectors": ["slack"]}},
        ]
    }
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.connector_hub.ConnectorHub", return_value=mock_hub),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub"),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
    ):
        hub = await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json=graph_json)

    assert hub is mock_hub
    mock_hub.initialise.assert_awaited_once()
    call = mock_hub.initialise.await_args
    assert set(call.kwargs["allowed_connectors"]) == {"github", "slack"}


@pytest.mark.asyncio
async def test_init_connector_hub_none_scope_is_unrestricted():
    """A graph with no connector fetch scope yields ``allowed_connectors=None``
    (the pre-scope, fetch-everything behaviour)."""
    fake_session = _FakeSession(conn_rows=[MagicMock()])
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSessionCM(fake_session)

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    mock_hub.__aenter__ = AsyncMock(return_value=mock_hub)
    mock_hub.__aexit__ = AsyncMock()
    graph_json = {"nodes": [{"node_type": "transform"}]}
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.connector_hub.ConnectorHub", return_value=mock_hub),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub"),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
    ):
        hub = await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json=graph_json)

    assert hub is mock_hub
    call = mock_hub.initialise.await_args
    assert call.kwargs["allowed_connectors"] is None
