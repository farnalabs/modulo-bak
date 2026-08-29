"""improve-database: missing CHECK constraints + partial-unique org slug.

Adds data-integrity CHECK constraints identified as genuine remaining gaps in
the SQLAlchemy models (enum/vocabulary columns whose allowed set is knowable
from code/comments, and ledger/count numerics that must be non-negative).
None of these constraints exist yet on the deployed schema (verified against
migrations 0001-0150).

Also converts ``organisations.slug`` from a plain UNIQUE constraint into a
partial UNIQUE index ``WHERE deleted_at IS NULL`` so a soft-deleted
organisation's slug can be reused (the table is soft-deleted manually via
``status='deleted'`` + ``deleted_at``).

Revision ID: 0151_fix_constraints
Revises: 0150_add_router_no_match_status
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0151_fix_constraints"
down_revision: str | None = "0150_add_router_no_match_status"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. run_evidence.evidence_state — EvidenceResult enum (core/pipeline_engine/evidence.py)
    op.create_check_constraint(
        "ck_run_evidence_state",
        "run_evidence",
        "evidence_state IN ('has_work','verified_empty','unverifiable')",
    )
    # 2. pipeline_snapshots.version_kind — 'edit' | 'run' | 'draft'
    op.create_check_constraint(
        "ck_pipeline_snapshots_version_kind",
        "pipeline_snapshots",
        "version_kind IN ('edit','run','draft')",
    )
    # 3. pipeline_snapshots.created_kind — 'initial' | 'edit' | 'rollback' | 'run'
    op.create_check_constraint(
        "ck_pipeline_snapshots_created_kind",
        "pipeline_snapshots",
        "created_kind IN ('initial','edit','rollback','run')",
    )
    # 4. pipeline_snapshots.channel — 'none' | 'stable' | 'canary'
    op.create_check_constraint(
        "ck_pipeline_snapshots_channel",
        "pipeline_snapshots",
        "channel IN ('none','stable','canary')",
    )
    # 5. oauth_authorization_codes.code_challenge_method — S256 only (PKCE)
    op.create_check_constraint(
        "ck_oauth_auth_codes_challenge_method",
        "oauth_authorization_codes",
        "code_challenge_method = 'S256'",
    )
    # 6. organisations.org_cumulative_spend_cents — ledger total, never negative
    op.create_check_constraint(
        "ck_organisations_cum_spend",
        "organisations",
        "org_cumulative_spend_cents >= 0",
    )
    # 7-9. org_daily_run_counts — counts and ledger amounts
    op.create_check_constraint(
        "ck_org_daily_run_counts_run_count",
        "org_daily_run_counts",
        "run_count >= 0",
    )
    op.create_check_constraint(
        "ck_org_daily_run_counts_total_spend",
        "org_daily_run_counts",
        "total_spend_usd >= 0",
    )
    op.create_check_constraint(
        "ck_org_daily_run_counts_refused_spend",
        "org_daily_run_counts",
        "refused_spend_usd >= 0",
    )
    # 10-11. spend_anomalies — amounts/baselines never negative
    op.create_check_constraint(
        "ck_spend_anomalies_amount",
        "spend_anomalies",
        "amount >= 0",
    )
    op.create_check_constraint(
        "ck_spend_anomalies_baseline",
        "spend_anomalies",
        "baseline >= 0",
    )
    # 12-13. library_primitives — nullable counters, never negative when present
    op.create_check_constraint(
        "ck_library_primitives_dl_count",
        "library_primitives",
        "download_count IS NULL OR download_count >= 0",
    )
    op.create_check_constraint(
        "ck_library_primitives_review_count",
        "library_primitives",
        "review_count IS NULL OR review_count >= 0",
    )
    # 14. suite_runs — case counters never negative
    op.create_check_constraint(
        "ck_suite_runs_case_counts",
        "suite_runs",
        "total_cases >= 0 AND passed_cases >= 0 AND failed_cases >= 0 AND excluded_case_count >= 0",
    )

    # organisations.slug: plain UNIQUE -> partial UNIQUE (deleted_at IS NULL)
    op.drop_constraint("organisations_slug_key", "organisations", type_="unique")
    op.create_index(
        "uq_organisations_slug",
        "organisations",
        ["slug"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_organisations_slug",
        table_name="organisations",
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_unique_constraint("organisations_slug_key", "organisations", ["slug"])

    op.drop_constraint("ck_suite_runs_case_counts", "suite_runs", type_="check")
    op.drop_constraint("ck_library_primitives_review_count", "library_primitives", type_="check")
    op.drop_constraint("ck_library_primitives_dl_count", "library_primitives", type_="check")
    op.drop_constraint("ck_spend_anomalies_baseline", "spend_anomalies", type_="check")
    op.drop_constraint("ck_spend_anomalies_amount", "spend_anomalies", type_="check")
    op.drop_constraint("ck_org_daily_run_counts_refused_spend", "org_daily_run_counts", type_="check")
    op.drop_constraint("ck_org_daily_run_counts_total_spend", "org_daily_run_counts", type_="check")
    op.drop_constraint("ck_org_daily_run_counts_run_count", "org_daily_run_counts", type_="check")
    op.drop_constraint("ck_organisations_cum_spend", "organisations", type_="check")
    op.drop_constraint("ck_oauth_auth_codes_challenge_method", "oauth_authorization_codes", type_="check")
    op.drop_constraint("ck_pipeline_snapshots_channel", "pipeline_snapshots", type_="check")
    op.drop_constraint("ck_pipeline_snapshots_created_kind", "pipeline_snapshots", type_="check")
    op.drop_constraint("ck_pipeline_snapshots_version_kind", "pipeline_snapshots", type_="check")
    op.drop_constraint("ck_run_evidence_state", "run_evidence", type_="check")
