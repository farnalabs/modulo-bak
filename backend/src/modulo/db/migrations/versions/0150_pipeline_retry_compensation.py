"""Register ``compensation_failed``/``unknown`` run statuses + P5 columns (FAR-402 P5).

Revision ID: 0150_pipeline_retry_compensation
Revises: 0149_suite_run_trigger_kind
Create Date: 2026-08-26

Implements the FAR-402 P5 (FAR-419) failure/retry + compensation data-model
deltas:

1. **``runs.idempotency_key``** (nullable String(128)) — the run-level
   idempotency identity of an operator re-run, derived deterministically from
   ``<pipeline_id>:<run_number>`` (FAR-410 primitive, column previously
   deferred). An ``UNKNOWN``-terminated run re-run by an operator reuses the
   SAME persisted key so a write that may have reached the upstream is not
   re-applied as a fresh operation.

2. **``pipeline_edges.retry`` / ``pipeline_edges.on_failure_target``** — a
   transition-edge retry block (re-executes the source node) and a compensation
   node id. Mutually exclusive per failure (design §4F); the graph validator
   enforces the exclusion at compile time.

3. **``ck_runs_status``** — extend the CHECK constraint to admit the two new
   statuses. ``compensation_failed`` is a NEW TERMINAL status (a watched node AND
   its compensation path both failed); ``unknown`` is adopted from FAR-410 as a
   NON-TERMINAL recovery status. The drop-if-differs / add-if-absent pattern
   (introduced in 0146, mirroring 0110) keeps the migration idempotent and
   rolling-deploy safe — the constraint is rebuilt with a backward-compatible
   SUPERSET so the overlap window where new app code emits a new status while the
   constraint is still the OLD list stays WRITE-SAFE (design §10 O1).
"""

import sqlalchemy as sa
from alembic import op

revision = "0150_pipeline_retry_compensation"
down_revision = "0149_suite_run_trigger_kind"
branch_labels = None
depends_on = None

# DROP (idempotent): only drop the existing constraint if its definition is NOT
# already the target list (whitespace-stripped comparison against the def that
# Postgres would emit for the NEW status set). String literals are hardcoded
# (no f-string interpolation) so the raw-SQL injection detector does not fire —
# these are immutable constants, mirroring migration 0146.
_DROP_NEW = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''unknown''::charactervarying,"
    "''complete''::charactervarying,''failed''::charactervarying,''cancelled''::charactervarying,"
    "''eval_failed''::charactervarying,''stalled''::charactervarying,''budget_exceeded''::charactervarying,"
    "''cost_ceiling_exceeded''::charactervarying,''compensation_failed''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
# ADD (idempotent): re-add the constraint with the NEW list if it is now absent.
_ADD_NEW = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'unknown'::character varying, 'complete'::character varying, "
    "'failed'::character varying, 'cancelled'::character varying, 'eval_failed'::character varying, "
    "'stalled'::character varying, 'budget_exceeded'::character varying, "
    "'cost_ceiling_exceeded'::character varying, 'compensation_failed'::character varying])::text[]))); "
    "END IF; END $$;"
)
# Revert: DROP if the def is not already the pre-0150 (post-0146) list ...
_DROP_OLD = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' AND "
    "regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') <> "
    "'CHECK(((status)::text=ANY((ARRAY[''pending''::charactervarying,''running''::charactervarying,"
    "''awaiting_human''::charactervarying,''claimed''::charactervarying,''complete''::charactervarying,"
    "''failed''::charactervarying,''cancelled''::charactervarying,''eval_failed''::charactervarying,"
    "''stalled''::charactervarying,''budget_exceeded''::charactervarying,"
    "''cost_ceiling_exceeded''::charactervarying])::text[])))') "
    "THEN ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; END IF; END $$;"
)
# ... and ADD the pre-0150 list if absent.
_ADD_OLD = (
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') "
    "THEN ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status CHECK (((status)::text = ANY "
    "((ARRAY['pending'::character varying, 'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, 'failed'::character varying, "
    "'cancelled'::character varying, 'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying, 'cost_ceiling_exceeded'::character varying])::text[]))); "
    "END IF; END $$;"
)


def upgrade() -> None:
    # 1. run-level idempotency key (FAR-410 deferred column, landed by P5).
    op.add_column("runs", sa.Column("idempotency_key", sa.String(length=128), nullable=True))
    # 2. edge retry + compensation columns (design §5).
    op.add_column("pipeline_edges", sa.Column("retry", sa.JSON(), nullable=True))
    op.add_column("pipeline_edges", sa.Column("on_failure_target", sa.String(length=64), nullable=True))
    # 3. extend ck_runs_status to the new superset.
    op.execute(_DROP_NEW)
    op.execute(_ADD_NEW)


def downgrade() -> None:
    op.drop_column("pipeline_edges", "on_failure_target")
    op.drop_column("pipeline_edges", "retry")
    op.drop_column("runs", "idempotency_key")
    op.execute(_DROP_OLD)
    op.execute(_ADD_OLD)
