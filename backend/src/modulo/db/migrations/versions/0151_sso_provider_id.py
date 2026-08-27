"""Add provider_id slug column to sso_providers (FAR-457).

Revision ID: 0151_sso_provider_id
Revises: 0150_add_router_no_match_status
Create Date: 2026-08-26

The admin SSO UI writes providers to the sso_providers table, but the runtime
OIDC/SAML flows only read IdP config from env vars. This adds a `provider_id`
slug column (the URL key used at /api/v1/auth/oidc/{provider_id}/login) and a
partial unique index (organisation_id, provider_id) so the runtime can resolve
IdP config from the DB first, falling back to env vars for backward compat.

Existing rows are backfilled: the slug is derived from `name` (postgres uses
regexp_replace; sqlite lowercases the name). The column is nullable so the
unique index can stay partial.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0151_sso_provider_id"
down_revision: str | None = "0150_add_router_no_match_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.add_column("sso_providers", sa.Column("provider_id", sa.String(length=64), nullable=True))

    if is_pg:
        op.execute("SET search_path TO public")
        op.execute(
            """
        DO $$
        DECLARE r RECORD; base text; cand text; n int;
        BEGIN
          FOR r IN SELECT id, organisation_id, name FROM sso_providers WHERE provider_id IS NULL LOOP
            base := lower(regexp_replace(r.name, '[^a-zA-Z0-9]+', '-', 'g'));
            base := trim(both '-' from base);
            IF base = '' THEN base := 'sso'; END IF;
            base := left(base, 58);
            cand := base; n := 2;
            WHILE EXISTS (SELECT 1 FROM sso_providers WHERE provider_id = cand AND id <> r.id) LOOP
              cand := base || '-' || n; n := n + 1;
            END LOOP;
            UPDATE sso_providers SET provider_id = cand WHERE id = r.id;
          END LOOP;
        END $$;
        """
        )
    else:
        op.execute("UPDATE sso_providers SET provider_id = COALESCE(lower(name), 'sso') WHERE provider_id IS NULL")

    if is_pg:
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sso_providers_org_provider_id "
            "ON sso_providers (organisation_id, provider_id) WHERE provider_id IS NOT NULL"
        )
    else:
        op.create_index(
            "uq_sso_providers_org_provider_id",
            "sso_providers",
            ["organisation_id", "provider_id"],
            unique=True,
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.drop_index("uq_sso_providers_org_provider_id", table_name="sso_providers")
    op.drop_column("sso_providers", "provider_id")
