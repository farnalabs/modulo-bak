"""Trigger run_kind discriminator + eval_suite_id binding (FAR-377).

Revision ID: 0138_trigger_run_kind_suite
Revises: 0137_eval_suite_run
Create Date: 2026-08-25

Scheduled / event-driven eval execution (FAR-377). A ``trigger`` already owns a
``pipeline_id`` (the suite's owning/placeholder pipeline, satisfying the existing
FK + constraints) but has no way to say "this scheduled/event-driven trigger
fires a *SuiteRun* rather than a *Run*". This migration adds:

* ``triggers.run_kind`` — a run-kind discriminator: ``'run'`` (DEFAULT, the
  existing behaviour) or ``'suite_run'``. When ``run_kind = 'suite_run'`` the
  cron/ongoing/event dispatch path enqueues a **SuiteRun** execution instead of a
  pipeline ``Run``.
* ``triggers.eval_suite_id`` — nullable FK to ``eval_suites``. The eval
  dataset + schedule config + model backend + scenario inputs live in the
  trigger's ``config_json``; this column pins the suite the trigger runs.
  ``pipeline_id`` remains NOT NULL (it is the suite's owning/placeholder
  pipeline).

Both columns are ADDITIVE and nullable-or-defaulted: an existing trigger row is
a ``run_kind = 'run'`` trigger with ``eval_suite_id = NULL`` and behaves exactly
as before. No shipped migration is modified.

Reversible: downgrade drops the index, FK + both columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0138_trigger_run_kind_suite"
down_revision: str | None = "0137_eval_suite_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.add_column("triggers", sa.Column("run_kind", sa.String(length=20), nullable=False, server_default="run"))
    op.add_column("triggers", sa.Column("eval_suite_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_triggers_eval_suite_id",
        "triggers",
        "eval_suites",
        ["eval_suite_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_triggers_eval_suite_id", "triggers", ["eval_suite_id"])
    op.create_check_constraint(
        "ck_triggers_run_kind",
        "triggers",
        "run_kind IN ('run', 'suite_run')",
    )

    # FAR-382 version stamp on the per-case outcome (nullable — legacy
    # pipeline-path rows keep it NULL). ``eval_results`` is owned by
    # ``modulo_app`` (migration 0003), so this ALTER runs as the migration
    # CALLER (never under SET ROLE modulo_migrate).
    op.add_column("eval_results", sa.Column("eval_definition_version", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.drop_column("eval_results", "eval_definition_version")
    op.drop_constraint("ck_triggers_run_kind", "triggers", type_="check")
    op.drop_index("ix_triggers_eval_suite_id", table_name="triggers")
    op.drop_constraint("fk_triggers_eval_suite_id", "triggers", type_="foreignkey")
    op.drop_column("triggers", "eval_suite_id")
    op.drop_column("triggers", "run_kind")
