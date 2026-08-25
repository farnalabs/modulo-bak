"""Unit tests for the eval coverage-gap signal engine (FAR-381).

Tests the pure, DB-free heuristics and the ``evaluate_coverage_gap`` decision
logic: variant divergence, eval differentiation, statistical significance
(``min_runs``), and the gap condition (divergence high + differentiation low =>
``recommended_action="improve_evals"``).
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from modulo.core.eval_engine.coverage_gap import (
    DEFAULT_DIVERGENCE_THRESHOLD,
    compute_eval_differentiation,
    compute_variant_divergence,
    evaluate_coverage_gap,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(
    run_id: uuid.UUID,
    *,
    status: str = "complete",
    variant_id: str | None = "v-1",
    variant_name: str = "variant-a",
    outputs: dict[str, Any] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        status=status,
        variant_config_snapshot={"variant_id": variant_id, "variant_name": variant_name},
        outputs_json=outputs,
    )


def _eval(
    run_id: uuid.UUID,
    eval_id: uuid.UUID,
    *,
    score: float | None = None,
    passed: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(run_id=run_id, eval_id=eval_id, score=score, passed=passed)


def _eval_defs(eval_id: uuid.UUID, name: str = "eval-a") -> dict[Any, str]:
    return {eval_id: name}


# ---------------------------------------------------------------------------
# Pure heuristics
# ---------------------------------------------------------------------------


class TestComputeVariantDivergence:
    def test_identical_outputs_have_zero_divergence(self) -> None:
        out = {"answer": {"text": "same", "n": 3}}
        assert compute_variant_divergence([out, out]) == 0.0

    def test_canonical_serialization_ignores_key_order(self) -> None:
        assert compute_variant_divergence([{"a": 1, "b": 2}, {"b": 2, "a": 1}]) == 0.0

    def test_different_outputs_have_positive_divergence(self) -> None:
        d = compute_variant_divergence([{"answer": "alpha"}, {"answer": "completely-different-answer"}])
        assert d > DEFAULT_DIVERGENCE_THRESHOLD

    def test_less_than_two_outputs_is_zero(self) -> None:
        assert compute_variant_divergence([{"answer": "x"}]) == 0.0
        assert compute_variant_divergence([]) == 0.0


class TestComputeEvalDifferentiation:
    def test_identical_values_are_zero(self) -> None:
        assert compute_eval_differentiation([0.9, 0.9, 0.9]) == 0.0

    def test_spread_values_are_positive(self) -> None:
        assert compute_eval_differentiation([0.95, 0.6, 0.3]) > 0.0

    def test_pass_fail_spread_differentiates(self) -> None:
        # All-pass => no differentiation; mixed pass/fail => differentiation.
        assert compute_eval_differentiation([1.0, 1.0, 1.0]) == 0.0
        assert compute_eval_differentiation([1.0, 0.0, 1.0]) > 0.0

    def test_less_than_two_values_is_zero(self) -> None:
        assert compute_eval_differentiation([0.5]) == 0.0
        assert compute_eval_differentiation([]) == 0.0


# ---------------------------------------------------------------------------
# evaluate_coverage_gap — decision logic
# ---------------------------------------------------------------------------


class TestEvaluateCoverageGap:
    def _gap_data(self, *, scores: list[float]) -> tuple[list[Any], list[Any], dict[Any, str]]:
        eval_id = uuid.uuid4()
        runs = [
            _run(uuid.uuid4(), variant_id="v-1", variant_name="variant-a", outputs={"answer": "alpha"}),
            _run(uuid.uuid4(), variant_id="v-2", variant_name="variant-b", outputs={"answer": "bravo"}),
            _run(uuid.uuid4(), variant_id="v-3", variant_name="variant-c", outputs={"answer": "charlie"}),
        ]
        evals = [_eval(r.id, eval_id, score=s) for r, s in zip(runs, scores, strict=True)]
        return runs, evals, _eval_defs(eval_id)

    def test_high_divergence_low_differentiation_gap(self) -> None:
        """Variants diverge but evals score them ~the same => gap => improve_evals."""
        runs, evals, names = self._gap_data(scores=[0.91, 0.92, 0.90])
        summary = evaluate_coverage_gap(runs, evals, eval_names=names, min_runs=3)

        assert summary.status == "complete"
        assert summary.variant_divergence >= DEFAULT_DIVERGENCE_THRESHOLD
        assert len(summary.evals) == 1
        gap = summary.evals[0]
        assert gap.has_gap is True
        assert gap.recommended_action == "improve_evals"
        assert gap.eval_score_spread < 0.05

    def test_high_divergence_high_differentiation_no_gap(self) -> None:
        """Evals clearly separate the variants => no gap => ok."""
        runs, evals, names = self._gap_data(scores=[0.95, 0.6, 0.3])
        summary = evaluate_coverage_gap(runs, evals, eval_names=names, min_runs=3)

        assert summary.status == "complete"
        gap = summary.evals[0]
        assert gap.has_gap is False
        assert gap.recommended_action == "ok"

    def test_pass_flag_used_when_score_absent(self) -> None:
        """When score is null, the passed boolean drives differentiation."""
        eval_id = uuid.uuid4()
        runs = [
            _run(uuid.uuid4(), variant_id="v-1", outputs={"a": 1}),
            _run(uuid.uuid4(), variant_id="v-2", outputs={"b": 2}),
            _run(uuid.uuid4(), variant_id="v-3", outputs={"c": 3}),
        ]
        evals = [
            _eval(runs[0].id, eval_id, passed=True),
            _eval(runs[1].id, eval_id, passed=False),
            _eval(runs[2].id, eval_id, passed=True),
        ]
        summary = evaluate_coverage_gap(runs, evals, eval_names=_eval_defs(eval_id), min_runs=3)
        gap = summary.evals[0]
        # Mixed pass/fail across diverged variants => the eval did discriminate.
        assert gap.recommended_action == "ok"

    def test_all_passed_with_diverged_variants_is_gap(self) -> None:
        """All variants pass with high output divergence => eval can't tell them apart."""
        eval_id = uuid.uuid4()
        runs = [
            _run(uuid.uuid4(), variant_id="v-1", outputs={"answer": "alpha"}),
            _run(uuid.uuid4(), variant_id="v-2", outputs={"answer": "bravo"}),
            _run(uuid.uuid4(), variant_id="v-3", outputs={"answer": "charlie"}),
        ]
        evals = [_eval(r.id, eval_id, passed=True) for r in runs]
        summary = evaluate_coverage_gap(runs, evals, eval_names=_eval_defs(eval_id), min_runs=3)
        assert summary.evals[0].has_gap is True

    def test_below_min_runs_is_insufficient_data(self) -> None:
        eval_id = uuid.uuid4()
        runs = [
            _run(uuid.uuid4(), variant_id="v-1", variant_name="variant-a", outputs={"answer": "alpha"}),
            _run(uuid.uuid4(), variant_id="v-2", variant_name="variant-b", outputs={"answer": "bravo"}),
        ]
        evals = [
            _eval(runs[0].id, eval_id, score=0.9),
            _eval(runs[1].id, eval_id, score=0.9),
        ]
        summary = evaluate_coverage_gap(runs, evals, eval_names=_eval_defs(eval_id), min_runs=3)
        assert summary.status == "insufficient_data"
        assert summary.evals == []
        assert summary.run_count == 2

    def test_only_terminal_runs_count(self) -> None:
        eval_id = uuid.uuid4()
        runs = [
            _run(uuid.uuid4(), variant_id="v-1", outputs={"a": 1}, status="complete"),
            _run(uuid.uuid4(), variant_id="v-2", outputs={"b": 2}, status="complete"),
            _run(uuid.uuid4(), variant_id="v-3", outputs={"c": 3}, status="running"),
        ]
        evals = [
            _eval(runs[0].id, eval_id, score=0.9),
            _eval(runs[1].id, eval_id, score=0.9),
            _eval(runs[2].id, eval_id, score=0.9),
        ]
        summary = evaluate_coverage_gap(runs, evals, eval_names=_eval_defs(eval_id), min_runs=3)
        # Only two terminal runs have eval data => below min_runs => no signal.
        assert summary.status == "insufficient_data"

    def test_prove_the_fix_gap_requires_both_conditions(self) -> None:
        """A gap fires only when variants diverged AND evals did not differentiate.

        Removing the variant-divergence gate would falsely emit a gap for a
        non-divergent batch; removing the differentiation gate would falsely
        clear a real gap. Both assertions pin the two-sided condition.
        """
        eval_id = uuid.uuid4()
        # Non-divergent batch (near-identical outputs) + spread scores: no gap.
        same_runs = [
            _run(uuid.uuid4(), variant_id="v-1", outputs={"answer": "same"}),
            _run(uuid.uuid4(), variant_id="v-2", outputs={"answer": "same"}),
            _run(uuid.uuid4(), variant_id="v-3", outputs={"answer": "same"}),
        ]
        same_evals = [_eval(r.id, eval_id, score=s) for r, s in zip(same_runs, [0.9, 0.6, 0.3], strict=True)]
        no_gap = evaluate_coverage_gap(same_runs, same_evals, eval_names=_eval_defs(eval_id), min_runs=3)
        assert no_gap.status == "complete"
        assert no_gap.variant_divergence == 0.0
        assert no_gap.evals[0].has_gap is False
        assert no_gap.evals[0].recommended_action == "ok"

        # Divergent batch + flat scores: gap.
        diff_runs = [
            _run(uuid.uuid4(), variant_id="v-1", outputs={"answer": "alpha"}),
            _run(uuid.uuid4(), variant_id="v-2", outputs={"answer": "bravo"}),
            _run(uuid.uuid4(), variant_id="v-3", outputs={"answer": "charlie"}),
        ]
        diff_evals = [_eval(r.id, eval_id, score=s) for r, s in zip(diff_runs, [0.9, 0.9, 0.9], strict=True)]
        gap = evaluate_coverage_gap(diff_runs, diff_evals, eval_names=_eval_defs(eval_id), min_runs=3)
        assert gap.status == "complete"
        assert gap.evals[0].has_gap is True
        assert gap.evals[0].recommended_action == "improve_evals"

    def test_invalid_min_runs_rejected(self) -> None:
        with pytest.raises(ValueError):
            evaluate_coverage_gap([], [], min_runs=0)
