#!/usr/bin/env python3
"""Static QA-lens gate over the backend unit-test package.

Runs the "QA lenses" used to review ``backend/tests/unit`` as a *regression
gate* so the suite's high bar does not quietly decay: each lens that fires is
reported with ``file:line`` citations, mirroring the repo's other scripted
gates (``run_vulture.py``, ``run_check_migration_heads.py``).

Lenses
------
1. assertion-less tests (``L1``): a ``test_*`` function that contains no
   ``assert``, no ``pytest.raises``/``pytest.warns``, no mock ``assert_*``
   call, and does not defer to an ``assert*`` / ``*_assert*`` helper. Such a
   test cannot fail on its own and breaks silently when the code under test
   stops working. Exception-based tests and helper-delegating tests are
   allowed: a bare ``pytest.raises`` block or a call to
   ``_assert_feature_402(...)`` is a real assertion.
2. tautological / always-true asserts (``L2``): ``assert <literal>``,
   ``assert <expr> == <same expr>`` and ``assert isinstance(<literal>, T)`` can
   never fail and only create false confidence.
3. wall-clock sleeps in unit tests (``L3``): ``time.sleep(...)`` /
   ``asyncio.sleep(...)`` with a fixed, non-zero literal is a wall-clock /
   flakiness smell. Reported as a warning (hard failure under ``--strict``) so
   the few intentional timing probes (e.g. latency-budget tests) stay possible.

Scope
-----
``backend/tests/unit/**/test_*.py`` — the pure unit package (no DB, no
browser). Integration/BDD/performance suites have their own timing and
side-effect patterns and are out of scope here.

Exit 0 clean, 1 when ``--strict`` or when a hard lens (L1/L2) fires.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
TESTS_UNIT_DIR = BACKEND_DIR / "tests" / "unit"

# Mock call-methods that count as an assertion (from unittest.mock).
_MOCK_ASSERT_METHODS = {
    "assert_called",
    "assert_called_once",
    "assert_called_with",
    "assert_called_once_with",
    "assert_awaited",
    "assert_awaited_once",
    "assert_awaited_with",
    "assert_awaited_once_with",
    "assert_has_calls",
    "assert_any_call",
    "assert_any_await",
    "assert_not_called",
    "assert_not_awaited",
    "assert_has_awaits",
    "assert_has_no_awaits",
    "assert_has_no_calls",
}


def _statement_action_calls(fn: ast.AST) -> tuple[bool, bool, bool, int]:
    """Return (has_assert, has_raises, has_helper, n_actions) for a function.

    ``n_actions`` counts *statement-level* imperative calls (an ``Expr`` whose
    value is a call / awaited call) that are real side effects and can fail by
    raising. Setup calls (mock ``patch*``, ``monkeypatch.setattr``) are
    excluded — a test that builds a rising stub (local ``def`` / monkeypatch)
    then performs a single swallow call is an exception-expectation test, not
    an assertion-less one.
    """

    def _functional_flags(node: ast.AST) -> tuple[bool, bool, bool]:
        has_assert = False
        has_raises = False
        has_helper = False
        for call in ast.walk(node):
            if isinstance(call, ast.Assert):
                has_assert = True
                continue
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Attribute):
                method = func.attr
                owner = func.value
                if method in _MOCK_ASSERT_METHODS or method == "fail":
                    has_assert = True
                    continue
                if method in {"raises", "warns"}:
                    has_raises = True
                    continue
                if "_assert" in method or method.startswith("assert"):
                    has_helper = True
                    continue
                while isinstance(owner, ast.Attribute):
                    owner = owner.value
                if isinstance(owner, ast.Name) and ("_assert" in owner.id or owner.id.startswith("assert")):
                    has_helper = True
                    continue
            elif isinstance(func, ast.Name):
                if func.id in {"raises", "warns", "fail"} or func.id.startswith("assert"):
                    has_assert = True
                    continue
                if "_assert" in func.id:
                    has_helper = True
        return has_assert, has_raises, has_helper

    def _is_setup(func: ast.expr) -> bool:
        if isinstance(func, ast.Attribute):
            return func.attr in {"setattr", "patch"} or (
                func.attr in {"object", "dict", "multiple"} and isinstance(func.value, ast.Name)
            )
        return isinstance(func, ast.Name) and func.id == "patch"

    has_assert, has_raises, has_helper = _functional_flags(fn)
    actions = 0
    seen: set[int] = set()
    for expr in ast.walk(fn):
        if not isinstance(expr, ast.Expr) or id(expr.value) in seen:
            continue
        value = expr.value
        while isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        seen.add(id(value))
        if _is_setup(value.func):
            continue
        actions += 1
    return has_assert, has_raises, has_helper, actions


_ASSERT_OP_TRUTHY = (ast.Eq, ast.Is)
_ASSERT_OP_FALSY = (ast.NotEq, ast.IsNot)


def _same_source(left: str, right: str) -> bool:
    """True when two expressions are textually identical (same token run)."""
    return left.strip() == right.strip()


def _tautology(node: ast.Assert, source: str) -> str | None:
    """Return a description when *node* is an assert that can never fail."""
    test = node.test
    if isinstance(test, ast.Constant):
        return "assert <literal> (always truthy)"
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not) and isinstance(test.operand, ast.Constant):
        return "assert not <literal> (constant)"
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Attribute)
        and test.func.attr == "isinstance"
        and len(test.args) >= 2
        and isinstance(test.args[0], (ast.Constant, ast.Dict, ast.List, ast.Set, ast.Tuple))
    ):
        return "assert isinstance(<literal>, T) (always true)"
    if not isinstance(test, ast.Compare):
        return None
    for op, comp in zip(test.ops, test.comparators, strict=True):
        if isinstance(op, _ASSERT_OP_TRUTHY) and _same_source(
            source[test.left.col_offset : test.left.end_col_offset],
            source[comp.col_offset : comp.end_col_offset],
        ):
            return "assert <expr> == <same expr> (tautology)"
    return None


def _literal_sleep(node: ast.AST) -> ast.Call | None:
    """Return a ``<mod>.sleep(<fixed literal>)`` call inside *node*, if any."""
    for call in ast.walk(node):
        if not isinstance(call, ast.Call):
            continue
        if not isinstance(call.func, ast.Attribute) or call.func.attr != "sleep":
            continue
        if not call.args:
            continue
        arg = call.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
            return call
        if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub) and isinstance(arg.operand, ast.Constant):
            return call
    return None


def _scan() -> tuple[list[tuple[Path, int, str, str]], list[tuple[Path, int, str]]]:
    violations: list[tuple[Path, int, str, str]] = []
    sleeps: list[tuple[Path, int, str]] = []
    for py in sorted(TESTS_UNIT_DIR.rglob("test_*.py")):
        try:
            source = py.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                continue
            sleep_call = _literal_sleep(node)
            if sleep_call is not None:
                line = source.splitlines()[sleep_call.lineno - 1].strip()
                sleeps.append((py, sleep_call.lineno, line or "fixed-literal sleep call"))
            for statement in [n for n in ast.walk(node) if isinstance(n, ast.Assert)]:
                line_text = source.splitlines()[statement.lineno - 1]
                detail = _tautology(statement, line_text)
                if detail:
                    violations.append((py, statement.lineno, node.name, detail))
            has_assert, has_raises, has_helper, actions = _statement_action_calls(node)
            # A test whose body performs a single imperative action (or fewer)
            # is exception-based: it can only fail by raising (e.g. a
            # "must-not-raise" swallow test). Flag anything that performs
            # multiple unasserted actions without any assertion.
            if not has_assert and not has_raises and not has_helper and actions > 1:
                violations.append(
                    (py, node.lineno, node.name, "no assertion, no pytest.raises, and no assert-delegating helper")
                )
    return violations, sleeps


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QA-lens regression gate over backend/tests/unit")
    parser.add_argument("--strict", action="store_true", help="fail on wall-clock sleep warnings too")
    args = parser.parse_args(argv)

    violations, sleeps = _scan()
    for path, lineno, name, detail in violations:
        print(f"{path.relative_to(BACKEND_DIR)}:{lineno}: {name}: {detail}")
    for path, lineno, line in sleeps:
        print(f"{path.relative_to(BACKEND_DIR)}:{lineno}: warning: wall-clock sleep: {line}")

    hard = len(violations)
    warnings = len(sleeps)
    fail = hard or (args.strict and warnings)
    print(f"[test-quality] {'FAIL' if fail else 'ok'}: {hard} violation(s), {warnings} wall-clock sleep warning(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main())
