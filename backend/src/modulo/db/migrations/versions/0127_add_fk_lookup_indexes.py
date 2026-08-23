"""Add missing foreign-key lookup indexes on high-traffic join columns.

Revision ID: 0127_add_fk_lookup_indexes
Revises: 0126_soft_delete_partial_unique
Create Date: 2026-08-23

Several foreign-key columns that are heavily used in JOINs and filter
predicates (runs-for-pipeline, runs-for-account, nodes-for-pipeline,
eval/feedback/trigger/lease-for-run) had no secondary index, forcing
sequential scans on what are core read paths. The ``OrgScoped`` mixin only
indexes ``organisation_id``; these indexes cover the join/lookup keys.

Indexes are created ``CONCURRENTLY`` so they do not take a prolonged
``SHARE`` lock that blocks writes on production data. ``CREATE INDEX
CONCURRENTLY`` cannot run inside a transaction, so each statement is issued
on a separate autocommit connection. ``IF NOT EXISTS`` keeps the migration
idempotent and re-runnable.

Postgres-only: the deprecated SQLite / MariaDB backends ignore
``CONCURRENTLY`` (they run it as a normal blocking index build, which is
harmless for those environments).
"""

from alembic import op
from sqlalchemy import text

revision = "0127_add_fk_lookup_indexes"
down_revision = "0126_soft_delete_partial_unique"
branch_labels = None
depends_on = None

_INDEXES = (
    ("ix_nodes_pipeline_id", "nodes", ["pipeline_id"]),
    ("ix_eval_results_run_id", "eval_results", ["run_id"]),
    ("ix_feedback_records_run_id", "feedback_records", ["run_id"]),
    ("ix_trigger_events_run_id", "trigger_events", ["run_id"]),
    ("ix_workspace_leases_run_id", "workspace_leases", ["run_id"]),
    ("ix_pipelines_account_id", "pipelines", ["account_id"]),
    ("ix_pipelines_owner_team_id", "pipelines", ["owner_team_id"]),
    ("ix_runs_owner_team_id", "runs", ["owner_team_id"]),
    ("ix_runs_account_id", "runs", ["account_id"]),
)


def upgrade():
    bind = op.get_bind()
    for index_name, table, columns in _INDEXES:
        cols = ", ".join(columns)
        with bind.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} ON {table} ({cols})"))


def downgrade():
    bind = op.get_bind()
    for index_name, _table, _columns in _INDEXES:
        with bind.connect() as conn:
            conn = conn.execution_options(isolation_level="AUTOCOMMIT")
            conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {index_name}"))
