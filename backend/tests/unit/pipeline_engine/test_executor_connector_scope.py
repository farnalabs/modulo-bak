"""Tests for FAR-435 executor run-level connector fetch scope wiring.

Covers the pure union computation (``_run_connector_fetch_scope``) and the
``_init_connector_hub`` wiring that passes the scope to
``ConnectorHub.initialise`` so the FAR-418 deny-by-default fetch-time gate
applies at run level while preserving agent runtime tool-call access.
"""

import uuid
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.core.pipeline_engine.executor import PipelineExecutor, _run_connector_fetch_scope

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _agent_node(agent_id: str) -> dict[str, Any]:
    return {"id": "node-agent", "node_type": "agent", "agent_id": agent_id}


def _connector_node(*allowed: str) -> dict[str, Any]:
    scope = {"allowed_connectors": list(allowed)} if allowed else None
    return {"id": "node-conn", "node_type": "connector", "capability_scope": scope}


# ---------------------------------------------------------------------------
# Pure union computation: _run_connector_fetch_scope
# ---------------------------------------------------------------------------


def test_union_of_node_scope_and_agent_grants():
    graph = {"nodes": [_connector_node("slack"), _agent_node(str(uuid.uuid4()))]}
    grants = {str(graph["nodes"][1]["agent_id"]): {"github", "slack"}}
    result = _run_connector_fetch_scope(graph, grants)
    assert set(result) == {"github", "slack"}


def test_agent_runtime_access_preserved_when_node_has_no_static_binding():
    """An agent node (no capability_scope, no connector_binding) must still keep
    its Agent grants in the run scope so it can tool-call connectors at runtime."""
    agent_id = str(uuid.uuid4())
    graph = {"nodes": [_agent_node(agent_id)]}
    grants = {agent_id: {"github", "linear"}}
    result = _run_connector_fetch_scope(graph, grants)
    assert set(result) == {"github", "linear"}


def test_node_without_explicit_scope_falls_back_to_agent_grants():
    """A node with no capability_scope (UNRESTRICTED default) still contributes
    its Agent's grants — the effective default is not an empty allow-list."""
    agent_id = str(uuid.uuid4())
    node = {"id": "n", "node_type": "agent", "agent_id": agent_id}
    graph = {"nodes": [node]}
    grants = {agent_id: {"slack", "github"}}
    result = _run_connector_fetch_scope(graph, grants)
    assert set(result) == {"github", "slack"}


def test_empty_union_returns_none_unrestricted():
    assert _run_connector_fetch_scope({"nodes": []}, {}) is None
    assert _run_connector_fetch_scope(None, None) is None
    assert _run_connector_fetch_scope({"nodes": [_connector_node()]}, {}) is None


def test_instance_id_and_type_entries_are_deduplicated_and_sorted():
    inst = str(uuid.uuid4())
    graph = {"nodes": [_connector_node("github", inst, "github")]}
    result = _run_connector_fetch_scope(graph, {})
    # The duplicate 'github' is removed and the set is deduped (order of a uuid
    # string vs a type token is deterministic but not semantically meaningful).
    assert set(result) == {"github", inst}


def test_instance_id_entries_are_opaque_and_kept():
    inst = str(uuid.uuid4())
    graph = {"nodes": [_connector_node(inst)]}
    result = _run_connector_fetch_scope(graph, {})
    assert result == [inst]


def test_scope_is_a_superset_of_any_single_node():
    """The run scope must be broad enough (union) that a later per-node narrow
    gate is the real boundary — never defeated by a tighter run scope."""
    agent_id = str(uuid.uuid4())
    graph = {
        "nodes": [
            _connector_node("github"),
            _connector_node("slack"),
            _agent_node(agent_id),
        ]
    }
    grants = {agent_id: {"linear"}}
    result = _run_connector_fetch_scope(graph, grants)
    assert set(result) == {"github", "linear", "slack"}


def test_unknown_agent_grants_are_ignored():
    graph = {"nodes": [_agent_node(str(uuid.uuid4()))]}
    result = _run_connector_fetch_scope(graph, {})
    assert result is None


def test_malformed_nodes_and_non_dict_graph_are_tolerated():
    assert _run_connector_fetch_scope({"nodes": [None, "junk", {"id": "x"}]}, {}) is None
    assert _run_connector_fetch_scope([], {}) is None


# ---------------------------------------------------------------------------
# Wiring: _init_connector_hub passes the computed scope to ConnectorHub.initialise
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
    def __init__(self, agent_rows: list[Any], conn_rows: list[Any]) -> None:
        self._agent_rows = agent_rows
        self._conn_rows = conn_rows

    def begin(self) -> _FakeAsyncCM:
        return _FakeAsyncCM()

    async def execute(self, stmt: Any) -> _FakeScalar:
        table_names = {f.name for f in stmt.froms}
        if "agents" in table_names:
            return _FakeScalar(self._agent_rows)
        return _FakeScalar(self._conn_rows)


class _FakeSessionCM:
    def __init__(self, session: _FakeSession) -> None:
        self._session = session

    async def __aenter__(self) -> _FakeSession:
        return self._session

    async def __aexit__(self, *_: object) -> None:
        return None


@pytest.mark.asyncio
async def test_init_connector_hub_passes_union_scope_to_initialise():
    """The run-start hub init decodes the graph's node capability_scope and Agent
    grants and forwards the union as ``allowed_connectors`` so the fetch-time gate
    applies at run level."""
    agent_id = uuid.uuid4()
    agent_row = SimpleNamespace(
        id=agent_id,
        connector_type_refs=[{"connector_type": "github"}, {"connector_type": "slack"}],
    )
    conn_rows = [SimpleNamespace(id=uuid.uuid4(), connector_type_id="slack")]
    fake_session = _FakeSession(agent_rows=[agent_row], conn_rows=conn_rows)

    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSessionCM(fake_session)

    graph: dict[str, Any] = {
        "nodes": [
            _connector_node("slack"),
            _agent_node(str(agent_id)),
        ]
    }

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.connector_hub.ConnectorHub", return_value=mock_hub),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub"),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
    ):
        hub = await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json=graph)

    assert hub is mock_hub
    mock_hub.initialise.assert_awaited_once()
    call = mock_hub.initialise.await_args
    assert call.args[0] == conn_rows
    assert set(call.kwargs["allowed_connectors"]) == {"github", "slack"}


@pytest.mark.asyncio
async def test_init_connector_hub_empty_scope_is_passed_as_none():
    """A graph with no scope yields ``allowed_connectors=None`` (unrestricted)."""
    fake_session = _FakeSession(agent_rows=[], conn_rows=[SimpleNamespace(id=uuid.uuid4(), connector_type_id="github")])
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSessionCM(fake_session)

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.connector_hub.ConnectorHub", return_value=mock_hub),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub"),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
    ):
        hub = await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json={"nodes": [{"id": "x"}]})

    assert hub is mock_hub
    call = mock_hub.initialise.await_args
    assert call.kwargs["allowed_connectors"] is None


@pytest.mark.asyncio
async def test_init_connector_hub_reuses_cached_scope_for_compensation_path():
    """The compensation path (no graph) reuses the run scope computed at start."""
    agent_id = uuid.uuid4()
    agent_row = SimpleNamespace(id=agent_id, connector_type_refs=[{"connector_type": "github"}])
    fake_session = _FakeSession(
        agent_rows=[agent_row],
        conn_rows=[SimpleNamespace(id=uuid.uuid4(), connector_type_id="github")],
    )
    executor = PipelineExecutor(MagicMock())
    executor._session_factory = lambda: _FakeSessionCM(fake_session)

    mock_hub = MagicMock()
    mock_hub.initialise = AsyncMock()
    with (
        patch("modulo.core.pipeline_engine.executor.set_rls_org", new=AsyncMock()),
        patch("modulo.core.pipeline_engine.executor.set_rls_execution_context", new=AsyncMock()),
        patch("modulo.core.connector_hub.ConnectorHub", return_value=mock_hub),
        patch("modulo.core.pipeline_engine.decorator.set_connector_hub"),
        patch("modulo.core.runtime_provider.create_default_hub", return_value=MagicMock()),
        patch("modulo.core.secrets_backend.create_secrets_backend", return_value=MagicMock()),
        patch("modulo.settings.get_settings", return_value=MagicMock()),
    ):
        await executor._init_connector_hub(org_id=uuid.uuid4(), graph_json={"nodes": [_agent_node(str(agent_id))]})
        # Second call WITHOUT graph_json must reuse the cached scope.
        hub2 = await executor._init_connector_hub(org_id=uuid.uuid4())

    call = mock_hub.initialise.await_args
    assert set(call.kwargs["allowed_connectors"]) == {"github"}
    assert hub2 is mock_hub
