"""Add missing foreign-key constraints on internal reference columns.

Revision ID: 0153_add_missing_foreign_keys
Revises: 0152_dismissed_by_user_id_index
Create Date: 2026-08-29

These columns are clearly foreign keys (named ``*_id`` and referencing another
table's primary key) but were never given a DB-level foreign key. Without the
constraint, orphaned rows can be written and referential integrity is not
enforced. All six columns are already ``Uuid`` typed in the models, so only the
constraint is added (no type change). ON DELETE semantics follow the existing
convention in the schema: ``SET NULL`` for nullable refs, ``RESTRICT`` for the
mandatory ``pipeline_edges`` endpoints.
"""

from __future__ import annotations

from alembic import op

revision: str = "0153_add_missing_foreign_keys"
down_revision: str | None = "0152_dismissed_by_user_id_index"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    op.create_foreign_key(
        "org_api_keys_run_id_fkey",
        "org_api_keys",
        "runs",
        ["run_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "eval_definitions_node_id_fkey",
        "eval_definitions",
        "nodes",
        ["node_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "eval_results_node_id_fkey",
        "eval_results",
        "nodes",
        ["node_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "pipeline_edges_source_node_id_fkey",
        "pipeline_edges",
        "nodes",
        ["source_node_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "pipeline_edges_target_node_id_fkey",
        "pipeline_edges",
        "nodes",
        ["target_node_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "runs_variant_group_id_fkey",
        "runs",
        "variant_groups",
        ["variant_group_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("runs_variant_group_id_fkey", "runs", type_="foreignkey")
    op.drop_constraint("pipeline_edges_target_node_id_fkey", "pipeline_edges", type_="foreignkey")
    op.drop_constraint("pipeline_edges_source_node_id_fkey", "pipeline_edges", type_="foreignkey")
    op.drop_constraint("eval_results_node_id_fkey", "eval_results", type_="foreignkey")
    op.drop_constraint("eval_definitions_node_id_fkey", "eval_definitions", type_="foreignkey")
    op.drop_constraint("org_api_keys_run_id_fkey", "org_api_keys", type_="foreignkey")
