"""Org-gate the ``rls_team_isolation`` policy on ``stages`` (cross-org leak).

Revision ID: 0151_stages_team_rls_org_gated
Revises: 0150_add_router_no_match_status
Create Date: 2026-08-26

Migration 0124 re-created ``rls_team_isolation`` with an org-gated,
execution-context-widened expression on the five team-scoped tables
(``pipelines``, ``connector_instances``, ``model_backends``,
``environment_profiles``, ``library_primitives``) and dropped the redundant
org-only ``rls_org_isolation`` policy there. It OMITTED ``stages`` — a sibling
team-scoped table created in 0003 with the SAME two policies.

``stages`` therefore still carries the ORIGINAL un-org-gated ``rls_team_isolation``
policy (``visibility='org' OR owner_team_id IS NULL OR ... membership OR
org_role='admin'`` with NO ``organisation_id`` AND gate). Because Postgres ORs
permissive policies, the org-only ``rls_org_isolation`` policy AND the
un-org-gated team policy together meant: when ``app.organisation_id`` context is
empty (or the user is an admin/``owner_team_id IS NULL``), a reader could see
``stages`` rows belonging to another organisation. 0124 fixed this leak on the
five sibling tables; this migration applies the same org-gated fix to ``stages``.

The corrected ``rls_team_isolation`` ANDs ``organisation_id`` with the
team-visibility clause, so the escape hatch (``app.execution_context='true'``)
widens the team-visibility clause WITHIN the organisation only — it can never
leak rows across organisations.

The downgrade restores the ORIGINAL un-org-gated team policy and re-adds the
org-only policy, exactly mirroring the 0124 downgrade.

Postgres-only (RLS policies do not exist on the deprecated MariaDB / SQLite
backends).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0151_stages_team_rls_org_gated"
down_revision: str | None = "0150_add_router_no_match_status"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "stages"

_ORG_ONLY_STRICT = "organisation_id = nullif(current_setting('app.organisation_id', true), '')::uuid"

# Original rls_team_isolation USING expression (verbatim from 0003/0109/0110):
# org check AND (visibility='org' OR visibility IS NULL OR owner_team_id IS NULL
# OR owner_team_id IN (my team_memberships) OR org_role='admin').
_TEAM_POLICY_ORIGINAL = (
    "((organisation_id = (NULLIF(current_setting('app.organisation_id'::text, true), ''::text))::uuid)"
    " AND (((visibility)::text = 'org'::text)"
    " OR (visibility IS NULL)"
    " OR (owner_team_id IS NULL)"
    " OR (owner_team_id IN ( SELECT team_memberships.team_id FROM public.team_memberships"
    " WHERE (team_memberships.account_id = (NULLIF(current_setting('app.user_id'::text, true), ''::text))::uuid)))"
    " OR (NULLIF(current_setting('app.org_role'::text, true), ''::text) = 'admin'::text)))"
)

_EXEC_CONTEXT_CLAUSE = "(NULLIF(current_setting('app.execution_context'::text, true), ''::text) = 'true'::text)"
_TEAM_POLICY_EXEC_CONTEXT = _TEAM_POLICY_ORIGINAL.replace(
    "= 'admin'::text)))",
    "= 'admin'::text) OR " + _EXEC_CONTEXT_CLAUSE + "))",
)


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    # The org check remains an AND gate, so the execution-context hatch can only
    # widen the team-visibility clause within the organisation — never across orgs.
    op.execute(f"DROP POLICY IF EXISTS rls_org_isolation ON public.{_TABLE}")
    op.execute(f"DROP POLICY IF EXISTS rls_team_isolation ON public.{_TABLE}")
    op.execute(f"CREATE POLICY rls_team_isolation ON public.{_TABLE} USING ({_TEAM_POLICY_EXEC_CONTEXT})")


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    # Restore the ORIGINAL un-org-gated team policy + the org-only policy, exactly
    # mirroring the 0124 downgrade.
    op.execute(f"DROP POLICY IF EXISTS rls_team_isolation ON public.{_TABLE}")
    op.execute(f"CREATE POLICY rls_team_isolation ON public.{_TABLE} USING ({_TEAM_POLICY_ORIGINAL})")
    op.execute(f"CREATE POLICY rls_org_isolation ON public.{_TABLE} USING ({_ORG_ONLY_STRICT})")
