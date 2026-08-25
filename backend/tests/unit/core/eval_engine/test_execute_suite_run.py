"""Scheduled/event-driven eval execution (FAR-377) — runner + guards.

Covers, without Docker (in-memory SQLite async engine + EvalEngine):

* the end-to-end runner: build a SuiteRun from a dataset + definitions, execute
  it (regex/human_set deterministic evals), persist per-case EvalResults with
  the FAR-382 ``eval_definition_version`` stamp, reconcile counts, and reach the
  terminal state;
* the artifact contract: empty dataset refuses loudly; a suite with no active
  definitions never silently passes;
* the separate spend pool (``suite_runs`` ledger, never ``runs``);
* the loop guard: eval/family exclusion + no Run/TriggerEvent write;
* the failure sink: an orchestration failure transitions the run to ``failed``
  with ``error_detail`` populated and escalates to the Error Dashboard.
"""

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from modulo.core.eval_engine.execute_suite_run import (
    EVAL_WATCH_EVENT_FAMILIES,
    SuiteRunEmptyDatasetError,
    SuiteRunExecutionError,
    _suite_run_transition,
    build_suite_run,
    build_suite_run_tuple,
    exclude_eval_families,
    execute_suite_run,
    is_eval_trigger,
    suite_run_daily_spend_exceeded,
)
from modulo.db.models import Base
from modulo.db.models.eval_dataset import EvalCase, EvalDataset
from modulo.db.models.eval_definition import EvalDefinition
from modulo.db.models.eval_result import EvalResult
from modulo.db.models.eval_suite import EvalSuite
from modulo.db.models.eval_suite_run import SuiteRun, SuiteRunState
from modulo.db.models.model_backend import ModelBackend


def _tables() -> list[Any]:
    return [
        EvalSuite.__table__,
        EvalDataset.__table__,
        EvalCase.__table__,
        EvalDefinition.__table__,
        SuiteRun.__table__,
        EvalResult.__table__,
        ModelBackend.__table__,
    ]


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=_tables())
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _org() -> uuid.UUID:
    return uuid.uuid4()


async def seed_suite(session, org_id: uuid.UUID) -> tuple[EvalSuite, EvalDataset, list[EvalDefinition], ModelBackend]:
    suite = EvalSuite(organisation_id=org_id, name="suite-a", eval_definition_ids=[])
    dataset = EvalDataset(organisation_id=org_id, name="ds-a", version=1)
    backend = ModelBackend(
        organisation_id=org_id,
        name="backend-a",
        display_name="Backend A",
        provider="openai",
        model_id="gpt-4o",
        credentials_ciphertext=b"x",
        account_id=org_id,
    )
    session.add_all([suite, dataset, backend])
    await session.flush()

    regex_def = EvalDefinition(
        organisation_id=org_id,
        pipeline_id=uuid.uuid4(),
        name="category-regex",
        eval_type="regex",
        config_json={"pattern": "^billing$", "field": "output.category"},
        failure_behaviour="warn",
        eval_suite_id=suite.id,
        account_id=org_id,
    )
    session.add(regex_def)
    await session.flush()
    suite.eval_definition_ids = [str(regex_def.id)]
    return suite, dataset, [regex_def], backend


async def seed_cases(
    session, org_id: uuid.UUID, dataset: EvalDataset, payloads: list[dict[str, Any]]
) -> list[EvalCase]:
    from modulo.db.models.eval_dataset import compute_input_hash

    cases = []
    for payload in payloads:
        case = EvalCase(
            organisation_id=org_id,
            dataset_id=dataset.id,
            input_payload=payload,
            input_hash=compute_input_hash(payload),
        )
        cases.append(case)
    session.add_all(cases)
    await session.flush()
    return cases


# --------------------------------------------------------------------------- #
# Runner end-to-end                                                           #
# --------------------------------------------------------------------------- #
async def test_execute_suite_run_completes_and_persists_results(session) -> None:
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}, {"category": "sales"}])

    run = await build_suite_run(
        session,
        org_id=org,
        suite_id=suite.id,
        dataset_id=dataset.id,
        model_backend_id=backend.id,
        scenario_inputs=None,
    )
    assert run.state == SuiteRunState.PENDING.value

    from modulo.core.eval_engine.execute_suite_run import _suite_run_transition

    await _suite_run_transition(session, run, SuiteRunState.RUNNING)
    await session.flush()

    stats = await execute_suite_run(session, run, eval_definition_version=7)
    await session.flush()

    assert stats["state"] == SuiteRunState.COMPLETED.value
    assert stats["total_cases"] == 2
    assert stats["passed_cases"] == 1
    assert stats["failed_cases"] == 1
    assert stats["excluded_case_count"] == 0

    rows = list(
        (
            await session.execute(__import__("sqlalchemy").select(EvalResult).where(EvalResult.suite_run_id == run.id))
        ).scalars()
    )
    assert len(rows) == 2
    for row in rows:
        assert row.suite_run_id == run.id
        assert row.eval_definition_version == 7
        assert row.organisation_id == org


async def test_execute_suite_run_rejects_empty_dataset(session) -> None:
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    # A dataset with zero active cases is refused LOUDLY at construction — never
    # a silent pass (artifact contract).
    with pytest.raises(SuiteRunEmptyDatasetError, match="no active cases"):
        await build_suite_run(
            session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
        )


async def test_failure_sink_transitions_run_to_failed(session) -> None:
    """An orchestration failure sinks the run to ``failed`` + surfaces detail."""
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}])
    # Add an llm_judge definition with NO judge callable -> execute refuses.
    llm_def = EvalDefinition(
        organisation_id=org,
        pipeline_id=uuid.uuid4(),
        name="judge",
        eval_type="llm_judge",
        config_json={"field": "output", "prompt": "grade"},
        failure_behaviour="warn",
        eval_suite_id=suite.id,
        account_id=org,
    )
    session.add(llm_def)
    await session.flush()

    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )
    await _suite_run_transition(session, run, SuiteRunState.RUNNING)
    with pytest.raises(SuiteRunExecutionError, match="llm_judge"):
        await execute_suite_run(session, run)
    assert run.state == SuiteRunState.FAILED.value
    assert "llm_judge" in (run.error_detail or "")


async def test_execute_suite_run_rejects_no_definitions(session) -> None:
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    case = EvalCase(organisation_id=org, dataset_id=dataset.id, input_payload={"a": 1}, input_hash="h" * 64)
    session.add(case)
    await session.flush()
    # Remove all definitions -> no active definitions.
    for d in (await session.execute(__import__("sqlalchemy").select(EvalDefinition))).scalars():
        await session.delete(d)
    await session.flush()

    # build_suite_run refuses a suite with no active definitions (never silent).
    with pytest.raises(SuiteRunExecutionError):
        await build_suite_run(
            session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
        )


# --------------------------------------------------------------------------- #
# Loop guard                                                                  #
# --------------------------------------------------------------------------- #
def test_is_eval_trigger_discriminator() -> None:
    class _Trigger:
        run_kind = "suite_run"
        eval_suite_id = uuid.uuid4()

    class _RunTrigger:
        run_kind = "run"
        eval_suite_id = None

    assert is_eval_trigger(_Trigger()) is True
    assert is_eval_trigger(_RunTrigger()) is False
    # A plain object without the columns (pre-migration) is a 'run' trigger.
    g = type("G", (), {"run_kind": "run", "eval_suite_id": None})()
    assert is_eval_trigger(g) is False
    s = type("S", (), {"run_kind": "suite_run", "eval_suite_id": None})()
    assert is_eval_trigger(s) is True


def test_exclude_eval_families_drops_eval_events() -> None:
    # Production watch set filtered through exclude_eval_families can never
    # re-fire a trigger from an eval/feedback write.
    watch = {"run_failed", "run_stalled", "eval_regression", "feedback", "suite_run"}
    assert exclude_eval_families(watch) == {"run_failed", "run_stalled"}
    assert {"eval_regression", "eval_blocked", "suite_run", "eval_result", "feedback"} <= EVAL_WATCH_EVENT_FAMILIES
    assert exclude_eval_families(None) == set()


async def test_suite_run_completion_writes_only_eval_results(session) -> None:
    """A SuiteRun execution persists EvalResult rows and nothing else.

    Only the eval tables are created here — there is no ``runs`` or
    ``trigger_events`` table. If the runner attempted to write a ``Run`` or a
    ``TriggerEvent``, the flush would fail and the test would error. Reaching
    ``completed`` therefore proves the loop-guard: a finished eval writes ONLY
    into the eval surface, never into the webhook/trigger-watch source.
    """
    org = await _org()
    suite, dataset, _, backend = await seed_suite(session, org)
    await seed_cases(session, org, dataset, [{"category": "billing"}])
    run = await build_suite_run(
        session, org_id=org, suite_id=suite.id, dataset_id=dataset.id, model_backend_id=backend.id
    )
    from modulo.core.eval_engine.execute_suite_run import _suite_run_transition

    await _suite_run_transition(session, run, SuiteRunState.RUNNING)
    stats = await execute_suite_run(session, run, eval_definition_version=1)
    await session.flush()
    assert stats["state"] == SuiteRunState.COMPLETED.value


# --------------------------------------------------------------------------- #
# Separate spend pool                                                         #
# --------------------------------------------------------------------------- #
def test_suite_run_spend_is_independent_of_production_pool() -> None:
    # The suite-run daily limit is enforced over suite_runs cost; a production
    # run at its limit does NOT gate a suite-run trigger and vice versa.
    assert suite_run_daily_spend_exceeded(Decimal("10.0"), Decimal("10.0")) is True
    assert suite_run_daily_spend_exceeded(Decimal("9.99"), Decimal("10.0")) is False
    assert suite_run_daily_spend_exceeded(Decimal(5), None) is False


# --------------------------------------------------------------------------- #
# Baseline tuple snapshot                                                     #
# --------------------------------------------------------------------------- #
def test_build_suite_run_tuple_snapshots_version() -> None:
    tup, checksum, sig = build_suite_run_tuple(
        suite_id=uuid.uuid4(),
        dataset_id=uuid.uuid4(),
        dataset_version=3,
        definition_snapshots=[{"id": str(uuid.uuid4()), "eval_type": "regex", "config_json": {"pattern": "x"}}],
        model_backend_id=uuid.uuid4(),
        scenario_inputs=None,
    )
    assert tup["dataset_version"] == 3
    assert sig is None
    assert len(checksum) == 64


# --------------------------------------------------------------------------- #
# Trigger dispatch (prove-the-fix)                                            #
# --------------------------------------------------------------------------- #
def _suite_run_trigger() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        pipeline_id=uuid.uuid4(),
        run_kind="suite_run",
        eval_suite_id=uuid.uuid4(),
        active=True,
        max_concurrent_runs=1,
        daily_spend_limit=None,
        config_json={
            "dataset_id": uuid.uuid4(),
            "model_backend_id": uuid.uuid4(),
            "scenario_inputs": {"lang": "en"},
            "entity_thresholds": {"absolute_drop": 0.15},
            "suite_ceiling": "5.00",
            "eval_definition_version": 1,
        },
    )


async def test_suite_run_execution_jobs_are_registered_on_runs_worker() -> None:
    """The dispatch wiring exists: the runs worker registers both suite jobs.

    Prove-the-fix: without the FAR-377 wiring, a ``suite_run`` trigger would
    enqueue ``execute_run`` (a pipeline Run). The runs worker must register the
    SuiteRun execution + per-item fire jobs so a dispatched SuiteRun actually
    runs.
    """
    from modulo.core.saq_worker import _runs_functions

    names = {n for n, _ in _runs_functions()}
    assert "modulo.core.saq_worker.execute_suite_run" in names
    assert "modulo.core.saq_worker.fire_suite_run_trigger" in names
