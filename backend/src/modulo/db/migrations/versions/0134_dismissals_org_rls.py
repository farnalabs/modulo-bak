"""dismissals — add organisation_id + RLS (org isolation) to the dismissals table.

Revision ID: 0134_dismissals_org_rls
Revises: 0126_human_set_eval_type
Create Date: 2026-08-23

``dismissals`` records that a user (or, for ``dismiss_scope = 'scope'``, an org
admin on behalf of the org) has dismissed a notification. It was created in the
0110 schema migration WITHOUT an ``organisation_id`` and WITHOUT RLS, so a
reader in one org could (in principle) observe dismissals belonging to another
org. This migration closes that gap by giving ``dismissals`` the same
org-isolation RLS every other org-scoped table has.

ROLE WIRING (the 0066 ceremony, adapted from 0115): the migration connects via
``DATABASE_ADMIN_URL`` (env.py — the superuser/owner URL). ``modulo_migrate`` is
a NOLOGIN role (bootstrap_role.py), so it cannot be CONNECTED to. Unlike 0115
(which ``CREATE TABLE``s a brand-new table owned by ``modulo_migrate`` via
``SET ROLE``), this migration ``ALTER``s the pre-existing ``dismissals`` table,
which is owned by the migration caller (superuser). A non-owner role cannot
``ALTER`` a table, so we do NOT ``SET ROLE modulo_migrate`` around the DDL.
Instead the migration caller adds the column + FK, then explicitly
``ALTER TABLE dismissals OWNER TO modulo_migrate`` so ownership stays consistent
with the rest of the org-scoped schema. The post-add ownership assertion verifies
the table's owner is ``modulo_migrate`` — the owner-bypasses-RLS precondition for
``dismissals`` RLS confinement.

The ceremony is conditional on the roles existing (checked via ``pg_roles``):
on a fresh DB where ``alembic upgrade heads`` runs BEFORE the app bootstraps
roles (e.g. the BDD suite), the GRANT / owner-reassign / owner assertion are
skipped and the column is added by the migration caller. When the roles exist
(production, where bootstrap runs before alembic), the full ``modulo_migrate``
ownership ceremony runs as described above.

BACKFILL: every ``dismissal`` references a ``notification`` via ``notification_id``,
and ``notifications`` is org-scoped (``Notification`` extends ``OrgScoped``), so
the parent's ``organisation_id`` is a safe, deterministic backfill source. Existing
rows are populated by a JOIN before the column is made NOT NULL.

RLS: ENABLE + FORCE ROW LEVEL SECURITY (the owner is ``modulo_migrate`` and must
NOT bypass RLS). A single ``rls_org_isolation`` policy (FOR ALL) confines reads
AND writes to the current org context (``app.organisation_id``).

``modulo_app`` (the runtime role) is granted full DML. Direct grant, not
default-privileges: the pre-alembic ``bootstrap_role`` run already created the
role, and default privileges only cover tables created afterwards.

Postgres-only: SQLite (used by unit tests via ``Base.metadata.create_all``) has no
RLS / role / policy machinery, so those steps are skipped and the column is left
nullable there to match the model declaration and keep SQLite tests green.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0134_dismissals_org_rls"
down_revision: str | None = "0133_run_evidence_rls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIGRATE_ROLE = "modulo_migrate"
_APP_ROLE = "modulo_app"

_ORG_SCOPE = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"
_TABLE = "dismissals"


def _is_postgres(bind) -> bool:
    return bind.dialect.name == "postgresql"


def _role_exists(bind, role: str) -> bool:
    """Return True when the Postgres role exists (fresh dev/BDD DBs have none)."""
    return (
        bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}).scalar_one_or_none()
        is not None
    )


def _assert_owner_is_migrate(bind) -> None:
    """POST-ADD ownership assertion — the 0066 ceremony (after column/FK add)."""
    owner = bind.execute(
        sa.text("SELECT relowner::regrole::text FROM pg_class WHERE oid = to_regclass('public.dismissals')")
    ).scalar_one_or_none()
    if owner != _MIGRATE_ROLE:
        raise RuntimeError(
            f"{_TABLE} owner is {owner!r}, expected '{_MIGRATE_ROLE}' "
            "(the app role must NOT own dismissals — owner bypasses RLS)"
        )


def upgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("SET search_path TO public")
        migrate_role = _role_exists(bind, _MIGRATE_ROLE)
        app_role = _role_exists(bind, _APP_ROLE)
        if migrate_role:
            # The migration caller (superuser/owner) performs the DDL below. We
            # deliberately do NOT ``SET ROLE modulo_migrate`` here: ``dismissals``
            # is owned by the migration caller, and a non-owner role cannot
            # ``ALTER`` it. After the column/FK are added we re-own the table to
            # ``modulo_migrate`` (the RLS-confined owner) below.
            op.execute(f"GRANT REFERENCES ON TABLE public.organisations TO {_MIGRATE_ROLE}")
    else:
        migrate_role = False
        app_role = False

    # Add the column nullable first so existing rows can be backfilled.
    op.add_column(
        _TABLE,
        sa.Column("organisation_id", sa.Uuid(), nullable=True),
    )
    op.create_index(f"ix_{_TABLE}_organisation_id", _TABLE, ["organisation_id"])
    op.create_foreign_key(
        f"{_TABLE}_organisation_id_fkey",
        _TABLE,
        "organisations",
        ["organisation_id"],
        ["id"],
        ondelete="CASCADE",
        use_alter=True,
    )

    if pg:
        # Re-own the table to modulo_migrate so the RLS-confined owner is
        # consistent with the rest of the org-scoped schema. ``modulo_migrate``
        # is NOLOGIN, so it never connects at runtime and the owner-bypasses-RLS
        # precondition is satisfied (the runtime role ``modulo_app`` is not the
        # owner). This replaces the original ``SET ROLE`` ceremony, which failed
        # because ``modulo_migrate`` did not own the pre-existing ``dismissals``
        # table and therefore could not ``ALTER`` it.
        if migrate_role:
            op.execute(f"ALTER TABLE {_TABLE} OWNER TO {_MIGRATE_ROLE}")
            _assert_owner_is_migrate(bind)

        # Backfill from the parent notification's organisation_id.
        op.execute(
            sa.text(
                "UPDATE dismissals d "
                "SET organisation_id = n.organisation_id "
                "FROM notifications n "
                "WHERE d.notification_id = n.id AND d.organisation_id IS NULL"
            )
        )
        # Make the column NOT NULL now that every row has an org.
        op.execute(sa.text("ALTER TABLE dismissals ALTER COLUMN organisation_id SET NOT NULL"))

        op.execute("ALTER TABLE dismissals ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE dismissals FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY rls_org_isolation ON dismissals FOR ALL USING ({_ORG_SCOPE})")
        if app_role:
            op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON dismissals TO modulo_app")


def downgrade() -> None:
    bind = op.get_bind()
    pg = _is_postgres(bind)

    if pg:
        op.execute("DROP POLICY IF EXISTS rls_org_isolation ON dismissals")
        op.execute("ALTER TABLE dismissals DISABLE ROW LEVEL SECURITY")

    op.drop_constraint(f"{_TABLE}_organisation_id_fkey", _TABLE, type_="foreignkey")
    op.drop_index(f"ix_{_TABLE}_organisation_id", table_name=_TABLE)
    op.drop_column(_TABLE, "organisation_id")
