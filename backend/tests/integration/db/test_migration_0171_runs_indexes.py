"""Integration test for migration 0171_runs_list_performance_indexes.

Runs against the fully-migrated testcontainer Postgres (the ``migrated_db_url``
session fixture applies the whole Alembic chain, including 0171) and verifies
the two runs-list performance indexes actually exist on the live schema:

* ``ix_runs_org_status (organisation_id, status)`` — serves the per-request
  active-run counts (runs page + every dispatch admission gate);
* ``ix_runs_org_created_pipeline (organisation_id, created_at) INCLUDE
  (pipeline_id)`` — serves the runs-page ``total`` COUNT index-only. The INCLUDE
  column must be NON-KEY: pg_index.indnkeyatts == 2 (key columns) while
  indnatts == 3 (key + included).

No schema mutation — read-only against the shared migrated database.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

_RUNS_INDEX_SQL = (
    "SELECT i.relname AS index_name, ix.indnatts AS total_attrs, ix.indnkeyatts AS key_attrs "
    "FROM pg_index ix "
    "JOIN pg_class i ON i.oid = ix.indexrelid "
    "JOIN pg_class t ON t.oid = ix.indrelid "
    "WHERE t.relname = 'runs'"
)


async def test_0171_runs_list_indexes_exist(migrated_db_url: str) -> None:
    engine = create_async_engine(migrated_db_url, poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            rows = (await conn.execute(text(_RUNS_INDEX_SQL))).mappings().all()
    finally:
        await engine.dispose()

    by_name = {row["index_name"]: (row["total_attrs"], row["key_attrs"]) for row in rows}

    assert "ix_runs_org_status" in by_name, f"ix_runs_org_status missing; got {sorted(by_name)}"
    status_total, status_key = by_name["ix_runs_org_status"]
    assert (status_total, status_key) == (2, 2), "ix_runs_org_status must be a plain two-column composite"

    assert "ix_runs_org_created_pipeline" in by_name, f"ix_runs_org_created_pipeline missing; got {sorted(by_name)}"
    cover_total, cover_key = by_name["ix_runs_org_created_pipeline"]
    # (organisation_id, created_at) as KEY columns + pipeline_id INCLUDEd — the
    # non-key column is what lets the runs-page total COUNT scan index-only.
    assert (cover_total, cover_key) == (3, 2), (
        f"expected 3 total attrs / 2 key attrs (INCLUDE pipeline_id), got {(cover_total, cover_key)}"
    )
