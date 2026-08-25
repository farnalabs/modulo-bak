"""runs — add ``router_no_match`` to the ``ck_runs_status`` CHECK constraint.

Revision ID: 0138_add_router_no_match_status
Revises: 0137_eval_suite_run
Create Date: 2026-08-24

FAR-402 P1 (FAR-415) introduces the ``router_no_match`` terminal run status
emitted when a Router node (F2-A) has no matching rule and no ``default``. The
``ck_runs_status`` CHECK constraint enumerates the allowed status values, so
the new status must be added to it on existing databases. A rolling-deploy
mitigation (design doc Appendix B, O1): the constraint change is additive and
backward-compatible — pre-existing rows never carry the new status, and once
this migration lands the backend may emit it.

The constraint is recreated idempotently: only drop + recreate when the
current definition does not already include ``router_no_match`` (mirrors the
guard pattern used throughout 0110).
"""

from __future__ import annotations

from alembic import op

revision: str = "0138_add_router_no_match_status"
down_revision: str | None = "0137_eval_suite_run"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_UPGRADE_SQL = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status' "
    "AND regexp_replace(pg_get_constraintdef(oid), '\\s+', '', 'g') "
    "NOT LIKE '%router_no_match%') THEN "
    "ALTER TABLE public.runs DROP CONSTRAINT IF EXISTS ck_runs_status; "
    "END IF; "
    "IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') THEN "
    "ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status "
    "CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, "
    "'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, "
    "'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying, 'router_no_match'::character varying])::text[]))); "
    "END IF; "
    "END $$;"
)

_DOWNGRADE_SQL = (
    "DO $$ BEGIN "
    "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname='ck_runs_status') THEN "
    "ALTER TABLE public.runs DROP CONSTRAINT ck_runs_status; "
    "END IF; "
    "ALTER TABLE public.runs ADD CONSTRAINT ck_runs_status "
    "CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, "
    "'running'::character varying, 'awaiting_human'::character varying, "
    "'claimed'::character varying, 'complete'::character varying, "
    "'failed'::character varying, 'cancelled'::character varying, "
    "'eval_failed'::character varying, 'stalled'::character varying, "
    "'budget_exceeded'::character varying])::text[]))); "
    "END $$;"
)


def upgrade() -> None:
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    op.execute(_DOWNGRADE_SQL)
