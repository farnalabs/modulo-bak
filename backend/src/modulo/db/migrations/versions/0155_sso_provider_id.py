"""Add provider_id slug column to sso_providers (FAR-457).

Revision ID: 0155_sso_provider_id
Revises: 0154_add_web_vital_events_time_index
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

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import text

revision: str = "0155_sso_provider_id"
down_revision: str | None = "0154_add_web_vital_events_time_index"
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
        # Mirror the Postgres DO-block: slugify to a URL-safe, 58-char base and
        # de-duplicate collisions per org with ``-N`` suffixes so the unique
        # index (below) never fails on a name > 64 chars or two same-named
        # providers. The Postgres path truncates + dedups; sqlite must match or
        # dev/test migrations blow up on valid pre-existing data.
        rows = bind.execute(
            text("SELECT id, organisation_id, name FROM sso_providers WHERE provider_id IS NULL")
        ).fetchall()
        # Seed the used-set with any pre-existing (non-null) slugs so newly
        # derived slugs don't collide with what's already there.
        existing = bind.execute(
            text("SELECT organisation_id, provider_id FROM sso_providers WHERE provider_id IS NOT NULL")
        ).fetchall()
        used: dict[object, set[str]] = {}
        for org_id, pid in existing:
            used.setdefault(org_id, set()).add(pid)
        for rid, org_id, name in rows:
            base = re.sub(r"[^a-zA-Z0-9]+", "-", name or "").strip("-").lower() or "sso"
            base = base[:58]
            cand = base
            n = 2
            while cand in used.get(org_id, set()):
                cand = f"{base}-{n}"
                n += 1
            used.setdefault(org_id, set()).add(cand)
            bind.execute(text("UPDATE sso_providers SET provider_id = :pid WHERE id = :rid"), {"pid": cand, "rid": rid})

    if is_pg:
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sso_providers_org_provider_id "
            "ON sso_providers (organisation_id, provider_id) WHERE provider_id IS NOT NULL"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_sso_providers_org_provider_id "
            "ON sso_providers (organisation_id, provider_id) WHERE provider_id IS NOT NULL"
        )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    if is_pg:
        op.execute("SET search_path TO public")

    op.drop_index("uq_sso_providers_org_provider_id", table_name="sso_providers")
    op.drop_column("sso_providers", "provider_id")
