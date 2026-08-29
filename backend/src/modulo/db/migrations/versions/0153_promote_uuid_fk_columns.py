"""Promote ``String(255)`` FK columns that hold UUIDs to native ``Uuid`` type.

Revision ID: 0153_promote_uuid_fk_columns
Revises: 0152_add_check_constraints
Create Date: 2026-08-29

Four columns store UUID primary keys of other tables as ``String(255)``:

* ``node_observations.node_id``  -> nodes.id
* ``run_evidence.node_id``       -> nodes.id  (part of the (run_id, node_id) PK)
* ``feedback_records.producing_node_id`` -> nodes.id
* ``agents.template_id``         -> composite_templates.id

Storing UUIDs as text loses type safety and index efficiency and prevents a real
FK. The conversion uses ``USING col::uuid`` so existing well-formed UUID values
are preserved. The two PK/unique-involved columns are dropped and recreated
around the type change.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0153_promote_uuid_fk_columns"
down_revision: str | None = "0152_add_check_constraints"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.drop_constraint("pk_run_evidence_run_node", "run_evidence", type_="primary")
    op.alter_column(
        "run_evidence",
        "node_id",
        type_=sa.Uuid(),
        postgresql_using="node_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_primary_key("pk_run_evidence_run_node", "run_evidence", ["run_id", "node_id"])

    op.drop_constraint("uq_node_observations_run_node", "node_observations", type_="unique")
    op.alter_column(
        "node_observations",
        "node_id",
        type_=sa.Uuid(),
        postgresql_using="node_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.create_unique_constraint("uq_node_observations_run_node", "node_observations", ["run_id", "node_id"])

    op.alter_column(
        "feedback_records",
        "producing_node_id",
        type_=sa.Uuid(),
        postgresql_using="producing_node_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=False,
    )
    op.alter_column(
        "agents",
        "template_id",
        type_=sa.Uuid(),
        postgresql_using="template_id::uuid",
        existing_type=sa.String(length=255),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.drop_constraint("pk_run_evidence_run_node", "run_evidence", type_="primary")
    op.alter_column(
        "run_evidence",
        "node_id",
        type_=sa.String(length=255),
        existing_type=sa.Uuid(),
        existing_nullable=False,
    )
    op.create_primary_key("pk_run_evidence_run_node", "run_evidence", ["run_id", "node_id"])

    op.drop_constraint("uq_node_observations_run_node", "node_observations", type_="unique")
    op.alter_column(
        "node_observations",
        "node_id",
        type_=sa.String(length=255),
        existing_type=sa.Uuid(),
        existing_nullable=False,
    )
    op.create_unique_constraint("uq_node_observations_run_node", "node_observations", ["run_id", "node_id"])

    op.alter_column(
        "feedback_records",
        "producing_node_id",
        type_=sa.String(length=255),
        existing_type=sa.Uuid(),
        existing_nullable=False,
    )
    op.alter_column(
        "agents",
        "template_id",
        type_=sa.String(length=255),
        existing_type=sa.Uuid(),
        existing_nullable=True,
    )
