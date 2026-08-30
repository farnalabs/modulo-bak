"""Org-gate the ``rls_team_isolation`` policy on ``lifecycle_map_stages`` (cross-org leak).

Revision ID: 0155_stages_team_rls_org_gated
Revises: 0154_add_web_vital_events_time_index
Create Date: 2026-08-26

The original v2 ``stages`` table (created in 0003 and protected by an
org-gated ``rls_team_isolation`` team policy alongside an org-only
``rls_org_isolation`` policy) was dropped and replaced by
``lifecycle_map_stages`` in migrations 0108/0110. ``lifecycle_map_stages``
kept the ``rls_org_isolation`` policy (from 0110) but — unlike its five
sibling team-scoped tables (``pipelines``, ``connector_instances``,
``model_backends``, ``environment_profiles``, ``library_primitives``) which
received an org-gated ``rls_team_isolation`` in 0124 — never received a team
policy of its own.

Without a team policy, background machinery that sets only
``app.execution_context='true'`` (org scope, no user) cannot rely on the
org-gated team-visibility clause that the sibling tables have, and the row
is governed solely by ``rls_org_isolation`` (which is correct but leaves the
account-ownership dimension unenforced at the database layer). This migration
adds the org-gated ``rls_team_isolation`` policy to ``lifecycle_map_stages``
to match the sibling tables.

``lifecycle_map_stages`` has no ``visibility``/``owner_team_id`` columns (it
is owned per-account via ``account_id``), so the team-visibility clause is
expressed in terms of account ownership instead of team membership:
``organisation_id = current_org AND (account_id IS NULL OR account_id =
current_user OR org_role='admin' OR execution_context='true')``. The
``organisation_id`` AND gate means the execution-context escape hatch can only
widen the account-visibility clause WITHIN the organisation — it can never
leak rows across organisations.

The downgrade drops the team policy, leaving ``lifecycle_map_stages`` with its
original ``rls_org_isolation`` policy (untouched by this migration).

Postgres-only (RLS policies do not exist on the deprecated MariaDB / SQLite
backends).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0155_stages_team_rls_org_gated"
down_revision: str | None = "0154_add_web_vital_events_time_index"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "lifecycle_map_stages"

# lifecycle_map_stages has no `visibility`/`owner_team_id` columns (it is the
# renamed successor of the old `stages` table, dropped in 0108). Rows are scoped
# per-account via `account_id`, so the team-visibility clause is expressed in
# terms of account ownership instead of team membership.
# org check AND (account_id IS NULL OR account_id = current_user
#                OR org_role='admin').
_TEAM_POLICY_ORIGINAL = (
    "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)"
    " AND ("
    "(account_id IS NULL)"
    " OR (account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)"
    " OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text)"
    ")"
    ")"
)

_EXEC_CONTEXT_CLAUSE = "(NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text)"
_TEAM_POLICY_EXEC_CONTEXT = _TEAM_POLICY_ORIGINAL.replace(
    "= 'admin'::text)",
    "= 'admin'::text) OR " + _EXEC_CONTEXT_CLAUSE,
)


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    # The org-gated rls_org_isolation policy already exists (from 0110); add the
    # org-gated team policy to match the sibling team-scoped tables.
    op.execute(f"DROP POLICY IF EXISTS rls_team_isolation ON public.{_TABLE}")
    op.execute(f"CREATE POLICY rls_team_isolation ON public.{_TABLE} USING ({_TEAM_POLICY_EXEC_CONTEXT})")


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    # Restore the pre-0155 state: lifecycle_map_stages keeps only its
    # rls_org_isolation policy (this migration never touched it).
    op.execute(f"DROP POLICY IF EXISTS rls_team_isolation ON public.{_TABLE}")
