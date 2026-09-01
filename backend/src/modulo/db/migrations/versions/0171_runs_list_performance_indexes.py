"""Add runs-list performance indexes for GET /api/v1/runs.

Revision ID: 0171_runs_list_performance_indexes
Revises: 0170_add_residual_foreign_keys
Create Date: 2026-09-01

The Runs page timed out on populated orgs. Three structural costs in the list
request path, two of which these indexes address:

1. ``count_active_runs_for_org`` (every GET /api/v1/runs request AND every
   dispatch admission gate) filters ``organisation_id = ?`` AND
   ``status IN (...)`` — no existing index contains ``status``, so the count
   scanned every org run. ``ix_runs_org_status`` makes it O(active runs).
2. The list's ``total`` COUNT joins pipelines for the soft-delete filter and
   needs ``pipeline_id`` for every org run; the existing
   ``ix_runs_refusal (organisation_id, created_at)`` cannot cover it, forcing a
   heap fetch per run over the whole org. ``ix_runs_org_created_pipeline`` keeps
   the COUNT index-only via a non-key INCLUDE column.
3. The page SELECT loaded every heavy JSONB payload column
   (outputs_json / node_telemetry_json / ...). Fixed in CRUD
   (``crud.run._RUNS_LIST_DEFERRED_COLUMNS``) — no schema change needed.

Deploy-safety: plain ``CREATE INDEX`` (non-concurrent) is used because Alembic
runs inside a transaction and ``CREATE INDEX CONCURRENTLY`` cannot. Expect a
table-size-proportional upgrade with a brief write lock on ``runs``; deploy
outside traffic peaks. The table can be large — schedule accordingly.
"""

from __future__ import annotations

from alembic import op

revision: str = "0171_runs_list_performance_indexes"
down_revision: str | None = "0170_add_residual_foreign_keys"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    # Active-run counts: org + status IN (...). Plain composite — status is
    # low-cardinality but always paired with the org equality prefix.
    op.create_index("ix_runs_org_status", "runs", ["organisation_id", "status"])
    # List total COUNT: (organisation_id, created_at) key (superset of the
    # ordering path) + pipeline_id as a non-key INCLUDE column so the count can
    # be served index-only.
    op.create_index(
        "ix_runs_org_created_pipeline",
        "runs",
        ["organisation_id", "created_at"],
        postgresql_include=["pipeline_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_runs_org_created_pipeline", table_name="runs")
    op.drop_index("ix_runs_org_status", table_name="runs")
