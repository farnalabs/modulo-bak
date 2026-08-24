"""Unit tests for FAR-402 P1 Router + HITL node ergonomics (FAR-415).

Covers: the shared JMESPath evaluator, ``make_router_node_fn`` (rule
evaluation, default, no-match -> RouterNoMatchError, classifier mode),
Router compile-time default-rule enforcement, HITL-node compile-equivalence
with the legacy edge-gate, and ``loop`` edge authorability.
"""

from unittest.mock import MagicMock, patch

import pytest

from modulo.core.pipeline_engine.errors import RouterNoMatchError
from modulo.core.pipeline_engine.graph_cache import (
    RouterConfigError,
    _validate_router_config,
    build_graph_from_json,
)
from modulo.core.pipeline_engine.jmespath_eval import (
    compile_jmespath,
    evaluate_jmespath_condition,
)
from modulo.core.pipeline_engine.node_runner import make_router_node_fn

# ---------------------------------------------------------------------------
# Shared JMESPath evaluator
# ---------------------------------------------------------------------------


def test_evaluate_jmespath_condition_truthiness():
    state = {"foo": {"bar": 1}, "list": [1, 2]}
    assert evaluate_jmespath_condition(state, "foo.bar == `1`") is True
    assert evaluate_jmespath_condition(state, "foo.bar == `2`") is False
    # Empty/None expr is falsy (no guard).
    assert evaluate_jmespath_condition(state, "") is False
    assert evaluate_jmespath_condition(state, None) is False


def test_evaluate_jmespath_condition_invalid_raises():
    with pytest.raises(ValueError):
        compile_jmespath("foo.++invalid")


def test_evaluate_jmespath_condition_list_bool():
    # bool(...) truthiness (mirrors prior inline sites).
    assert evaluate_jmespath_condition({"x": [1]}, "x") is True
    assert evaluate_jmespath_condition({"x": []}, "x") is False


# ---------------------------------------------------------------------------
# make_router_node_fn
# ---------------------------------------------------------------------------


def _router_fn(rules, mode=None):
    config = {"rules": rules}
    if mode is not None:
        config["mode"] = mode
    return make_router_node_fn(config, node_id="r1")


def test_router_first_match_wins():
    fn = _router_fn(
        [
            {"guard": "state.x == `1`", "target": "a"},
            {"guard": "state.x == `2`", "target": "b"},
            {"default": True, "target": "c"},
        ]
    )
    assert fn({"state": {"x": 2}}) == "b"
    assert fn({"state": {"x": 1}}) == "a"


def test_router_default_used_when_no_match():
    fn = _router_fn(
        [
            {"guard": "state.x == `1`", "target": "a"},
            {"default": True, "target": "c"},
        ]
    )
    assert fn({"state": {"x": 99}}) == "c"


def test_router_no_match_raises():
    # A Router with no default and no matching rule raises at routing time.
    fn = make_router_node_fn({"rules": [{"guard": "state.x == `1`", "target": "a"}]}, node_id="r2")
    with pytest.raises(RouterNoMatchError):
        fn({"state": {"x": 99}})


def test_router_classifier_mode_matches_label():
    fn = _router_fn(
        [
            {"guard": "state.x == `1`", "target": "a"},
            {"label": "go_b", "target": "b"},
            {"default": True, "target": "c"},
        ],
        mode="classifier",
    )
    assert fn({"state": {"x": 0}, "_llm_next_node": "go_b"}) == "b"


def test_router_no_label_key_falls_to_default():
    fn = make_router_node_fn(
        {"rules": [{"label": "go_b", "target": "b"}, {"default": True, "target": "c"}]},
        node_id="rc2",
    )
    assert fn({}) == "c"


# ---------------------------------------------------------------------------
# Compile-time default-rule enforcement
# ---------------------------------------------------------------------------


def test_validate_router_config_requires_default():
    with pytest.raises(RouterConfigError):
        _validate_router_config({"rules": [{"guard": "x", "target": "a"}]}, "n1")
    # classifier mode is exempt (label match or default).
    _validate_router_config({"mode": "classifier", "rules": [{"label": "l", "target": "a"}]}, "n2")
    # default present is fine.
    _validate_router_config({"rules": [{"default": True, "target": "a"}]}, "n3")


# ---------------------------------------------------------------------------
# HITL node compile-equivalence with legacy edge-gate
# ---------------------------------------------------------------------------


def _compiled_structure(graph_json):
    with (
        patch("modulo.core.pipeline_engine.graph_cache.make_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_manual_node_fn", MagicMock()),
        patch("modulo.core.pipeline_engine.graph_cache.make_hitl_gate_fn", MagicMock()),
    ):
        compiled = build_graph_from_json(graph_json)
    g = compiled.get_graph()
    nodes = {n for n in g.nodes if n not in ("__start__", "__end__")}
    edges = {(s, t) for s, t, *_ in g.edges if s not in ("__start__", "__end__") and t not in ("__start__", "__end__")}
    gate_nodes = {n for n in nodes if "hitl_gate" in str(n)}
    return nodes, edges, gate_nodes


def test_hitl_node_compiles_like_edge_gate():
    hitl_config = {"required_team_id": "team-1", "human_only": True}
    # Legacy: an agent node A with an HITL-gated edge to B.
    legacy = {
        "nodes": [{"id": "A", "node_type": "agent"}, {"id": "B", "node_type": "agent"}],
        "edges": [{"source": "A", "target": "B", "hitl_gate_config": dict(hitl_config)}],
    }
    # New: an `hitl` node H (producing output) with a normal edge to B.
    new = {
        "nodes": [
            {"id": "H", "node_type": "hitl", "hitl_config": dict(hitl_config)},
            {"id": "B", "node_type": "agent"},
        ],
        "edges": [{"source": "H", "target": "B"}],
    }
    legacy_nodes, legacy_edges, legacy_gates = _compiled_structure(legacy)
    new_nodes, new_edges, new_gates = _compiled_structure(new)

    # Both insert exactly one synthetic HITL gate node and route
    # source -> gate -> target.
    assert len(legacy_gates) == 1
    assert len(new_gates) == 1
    assert len(legacy_nodes) == 3
    assert len(new_nodes) == 3
    # The gate sits between the source and B in both.
    assert ("B" in legacy_nodes) and ("B" in new_nodes)
    assert all("B" in t for (_, t) in legacy_edges)
    assert all("B" in t for (_, t) in new_edges)
    # Same number of edges (source->gate, gate->target).
    assert len(legacy_edges) == len(new_edges) == 2


# ---------------------------------------------------------------------------
# loop edge authorability
# ---------------------------------------------------------------------------


def test_loop_edge_authorable_in_valid_set():
    from modulo.core.workflow_import_export import VALID_EDGE_TYPES

    assert "loop" in VALID_EDGE_TYPES


def test_loop_edge_compiles():
    graph_json = {
        "nodes": [{"id": "A", "node_type": "agent"}, {"id": "B", "node_type": "agent"}],
        "edges": [
            {
                "source": "A",
                "target": "B",
                "type": "loop",
                "default_target": "B",
                "max_iterations": 3,
            }
        ],
    }
    nodes, _, _ = _compiled_structure(graph_json)
    assert "A" in nodes and "B" in nodes
    # A loop counter synthetic node is inserted.
    assert any("loop_counter" in str(n) for n in nodes)


def test_router_node_compiles():
    graph_json = {
        "nodes": [
            {
                "id": "R",
                "node_type": "router",
                "router_config": {
                    "rules": [
                        {"guard": "state.x == `1`", "target": "A"},
                        {"default": True, "target": "B"},
                    ]
                },
            },
            {"id": "A", "node_type": "agent"},
            {"id": "B", "node_type": "agent"},
        ],
        "edges": [{"source": "R", "target": "A"}, {"source": "R", "target": "B"}],
    }
    nodes, _, _ = _compiled_structure(graph_json)
    assert "R" in nodes and "A" in nodes and "B" in nodes
