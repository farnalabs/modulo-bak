"""Integration tests: ``write_evidence_row``'s tenant anchor against real Postgres.

``run_evidence`` is org-scoped by migration 0133: ``organisation_id`` is NOT NULL
with an FK to ``organisations(id)``, the table is under ``FORCE ROW LEVEL
SECURITY``, and the ``rls_org_isolation`` policy scopes it to
``app.organisation_id``. ``run_id`` is separately a NOT NULL FK to ``runs(id)``.

The unit tests build the table with ``Base.metadata.create_all`` on SQLite, which
has no ``organisations`` table and no ``PRAGMA foreign_keys=ON``, so neither the
FK nor the RLS policy is exercised there. Only a real Postgres path can pin what
actually happens when the tenant anchor cannot be resolved — which is why an
earlier revision's "derive a deterministic placeholder tenant from the run_id"
fallback passed on SQLite while being unrealizable on the deployment target.

These tests drive ``write_evidence_row`` through a NOBYPASSRLS role (the
production ``modulo_app`` scenario) and read the rows back over a superuser
connection, so the assertions are about whether the row was actually persisted
rather than about read visibility.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from modulo.core.pipeline_engine.evidence import write_evidence_row
from modulo.db.rls import set_rls_org

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def rls_app_session(app_engine: AsyncEngine) -> AsyncSession:
    """Session whose connections run as a NOBYPASSRLS role, so RLS applies.

    Mirrors production, where the app connects as ``modulo_app`` (a non-owner
    role that ``FORCE ROW LEVEL SECURITY`` confines).
    """
    factory = async_sessionmaker(app_engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        await session.close()


async def _insert_run(
    db_engine: AsyncEngine,
    *,
    org_id: uuid.UUID,
    pipeline_id: uuid.UUID,
    snapshot_id: uuid.UUID,
) -> uuid.UUID:
    """Commit a terminal run in *org_id* over a superuser connection."""
    run_id = uuid.uuid4()
    async with db_engine.connect() as conn, conn.begin():
        await conn.execute(
            text(
                "INSERT INTO runs (id, organisation_id, pipeline_id, snapshot_id, "
                "trigger_type, status, run_number, input_hash, langgraph_thread_id) "
                "VALUES (:id, :oid, :pid, :sid, 'manual', 'complete', "
                ":num, :hash, :thread)",
            ),
            {
                "id": str(run_id),
                "oid": str(org_id),
                "pid": str(pipeline_id),
                "sid": str(snapshot_id),
                "num": uuid.uuid4().int % 1_000_000,
                "hash": "e" * 64,
                "thread": f"evidence-tenant-{run_id}",
            },
        )
    return run_id


async def _fetch_evidence(db_engine: AsyncEngine, run_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    """Read run_evidence rows for *run_id* over a superuser connection (RLS bypassed)."""
    async with db_engine.connect() as conn:
        result = await conn.execute(
            text("SELECT organisation_id, evidence_state FROM run_evidence WHERE run_id = :rid"),
            {"rid": str(run_id)},
        )
        return [(row[0], row[1]) for row in result.all()]


async def test_omitted_org_resolves_from_parent_run_and_persists(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """With ``organisation_id`` omitted the anchor is resolved from the parent
    run, which satisfies both the FK and the RLS WITH CHECK on real Postgres."""
    run_id = await _insert_run(db_engine, org_id=test_org, pipeline_id=test_pipeline, snapshot_id=test_snapshot)

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, test_org)
        await write_evidence_row(
            rls_app_session,
            run_id=run_id,
            node_id="node-a",
            evidence_state="verified_empty",
            evidence_detail="probe found no diff",
        )

    rows = await _fetch_evidence(db_engine, run_id)
    assert rows == [(test_org, "verified_empty")]


async def test_missing_parent_run_skips_write_without_raising(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    test_org: uuid.UUID,
) -> None:
    """An unresolvable parent run is skipped, not anchored to a fabricated tenant.

    This is the regression guard for the placeholder-tenant fallback: the write
    must be a deliberate no-op (the caller treats the node as unverifiable)
    rather than an INSERT that Postgres silently rejects.
    """
    orphan_run_id = uuid.uuid4()

    async with rls_app_session.begin():
        await set_rls_org(rls_app_session, test_org)
        await write_evidence_row(
            rls_app_session,
            run_id=orphan_run_id,
            node_id="node-a",
            evidence_state="verified_empty",
            evidence_detail=None,
        )

    assert not await _fetch_evidence(db_engine, orphan_run_id)


async def test_fabricated_tenant_anchor_never_persists(
    db_engine: AsyncEngine,
    rls_app_session: AsyncSession,
    test_org: uuid.UUID,
    test_pipeline: uuid.UUID,
    test_snapshot: uuid.UUID,
) -> None:
    """Pins WHY the placeholder fallback was removed rather than kept.

    Explicitly anchoring a row to an organisation that does not exist can never
    persist: the ``organisation_id`` FK rejects it, and under the 0133
    ``rls_org_isolation`` policy the WITH CHECK rejects it too. Whichever fires
    first, no row survives — so deriving a synthetic tenant could only ever have
    dropped the evidence (silently, when the error was an ``IntegrityError``
    swallowed by the duplicate-key guard) or broken the caller.
    """
    # A real parent run, so the only thing wrong is the tenant anchor itself.
    run_id = await _insert_run(db_engine, org_id=test_org, pipeline_id=test_pipeline, snapshot_id=test_snapshot)
    fabricated_org = uuid.uuid5(uuid.NAMESPACE_OID, f"run-evidence:{run_id}")

    try:
        async with rls_app_session.begin():
            await set_rls_org(rls_app_session, test_org)
            # Passing the anchor explicitly skips resolution, so this is byte-for-byte
            # the INSERT the removed uuid5 fallback would have issued.
            await write_evidence_row(
                rls_app_session,
                run_id=run_id,
                node_id="node-a",
                evidence_state="verified_empty",
                evidence_detail=None,
                organisation_id=fabricated_org,
            )
    except SQLAlchemyError:
        # The RLS WITH CHECK rejection is not an IntegrityError, so it escapes
        # write_evidence_row's duplicate-key guard and reaches the caller.
        pass

    assert not await _fetch_evidence(db_engine, run_id)
