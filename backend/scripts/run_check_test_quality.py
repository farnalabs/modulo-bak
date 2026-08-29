#!/usr/bin/env python3
"""Static QA-lens gate over the backend unit-test package.

Runs the "QA lenses" used to review ``backend/tests/unit`` as a *regression
gate* so the suite's high bar does not quietly decay: each lens that fires is
reported with ``file:line`` citations.

Lens
----
1. assertion-less tests (``L1``): a ``test_*`` function that performs more than
   one imperative action (a non-setup ``Expr`` call / awaited call) yet contains
   no ``assert``, no ``pytest.raises``/``pytest.warns``, no mock ``assert_*``
   call, and does not defer to an ``assert*`` / ``*_assert*`` helper. Such a test
   cannot fail on its own and breaks silently when the code under test stops
   working. Exception-based tests are allowed: a test that builds a rising stub
   then performs a single swallow call can only fail by raising, so a
   one-action test is not flagged. A bare ``pytest.raises`` block or a call to
   ``_assert_feature_402(...)`` counts as a real assertion.

Scope
-----
``backend/tests/unit/**/test_*.py`` — the pure unit package (no DB, no
browser). Integration/BDD/performance suites have their own timing and
side-effect patterns and are out of scope here.

Sleep and tautology checks are intentionally *not* duplicated here: those
concerns are already owned by the CI-enforced architecture lens in
``backend/tests/architecture/test_test_suite_quality.py`` (which runs on every
merge via ``ci.yml`` ``pytest tests/architecture/``). That lens deliberately
leaves literal sleeps (``asyncio.sleep(0)`` event-loop yields, ``sleep(999)``
hang simulation) alone and flags computed-duration sleeps; re-implementing a
sleep lens here would contradict it and risk hard-failing intentional sleeps
under ``--strict``.

Exit 0 clean, 1 when a hard lens (L1) fires.
"""

from __future__ import annotations

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


def _scan() -> list[tuple[Path, int, str, str]]:
    violations: list[tuple[Path, int, str, str]] = []
    for py in sorted(TESTS_UNIT_DIR.rglob("test_*.py")):
        try:
            source = py.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not node.name.startswith("test_"):
                continue
            has_assert, has_raises, has_helper, actions = _statement_action_calls(node)
            # A test whose body performs a single imperative action (or fewer)
            # is exception-based: it can only fail by raising (e.g. a
            # "must-not-raise" swallow test). Flag anything that performs
            # multiple unasserted actions without any assertion.
            if not has_assert and not has_raises and not has_helper and actions > 1:
                violations.append(
                    (py, node.lineno, node.name, "no assertion, no pytest.raises, and no assert-delegating helper")
                )
    return violations


def _main(argv: list[str] | None = None) -> int:
    violations = _scan()
    for path, lineno, name, detail in violations:
        print(f"{path.relative_to(BACKEND_DIR)}:{lineno}: {name}: {detail}")

    hard = len(violations)
    fail = hard > 0
    print(f"[test-quality] {'FAIL' if fail else 'ok'}: {hard} assertion-less test(s)")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(_main())
