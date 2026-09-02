"""Idempotent demo-org seed for the /demo auto-login experience (FAR-535).

GATED: runs only when ``MODULO_DEMO_ENABLED`` is truthy AND both
``MODULO_DEMO_USER`` (email) and ``MODULO_DEMO_PASSWORD`` are non-empty
(see ``modulo.settings``). Default off — the seed is a no-op otherwise, so the
default release path behaviour is unchanged.

Creates (idempotently, NO Alembic migrations):

* the demo organisation (slug ``demo``),
* the demo user account (email/password from env; the password hash is
  re-stamped to match the env on every run so rotating the secret works),
* a ``viewer``-role org membership (read-only permission set — the org-role
  hierarchy viewer < runner < operator < admin is the enforcement boundary for
  every route via ``require_permission``; the seed also forces the role BACK to
  viewer if it drifted, and forces ``is_system_admin`` off),
* minimal benign sample data, all clearly prefixed "Demo": two published
  schemas, one two-node pipeline (+ snapshot v1), and two synthetic terminal
  runs (one complete, one failed).

Safety:
* No migrations — pure runtime ORM inserts.
* Org-scoped writes run under ``set_rls_org`` + ``set_rls_execution_context``
  (the documented boot/execution context) so Postgres RLS admits them.
* Account/email writes follow the same pattern as the boot-time
  ``_seed_modulo_users`` seed (system-context transaction, no RLS org).
* Idempotent by natural keys (org slug, account email, schema name, pipeline
  name, per-org run_number) — re-running never duplicates.

Runnable standalone (entrypoint-style, mirrors ``modulo.db.bootstrap_role``):

    python -m modulo.db.seed_demo
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import sys
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.rls import set_rls_execution_context, set_rls_org
from modulo.settings import Settings, get_settings

_log = logging.getLogger(__name__)

DEMO_ORG_SLUG = "demo"
DEMO_ORG_NAME = "Demo"
# Read-only: viewer is the bottom of the org-role hierarchy (ADR 017) and every
# mutating route requires runner/operator/admin through require_permission.
DEMO_ORG_ROLE = "viewer"


def demo_login_config(settings: Settings) -> tuple[str, str] | None:
    """Return ``(email, password)`` when the demo experience is fully configured.

    ``None`` when the kill switch is off or either credential env var is empty —
    both the demo endpoint and this seed treat that identically (feature absent).
    """
    if not settings.modulo_demo_enabled:
        return None
    email = settings.modulo_demo_user.strip()
    password = settings.modulo_demo_password
    if not email or not password:
        return None
    return (email, password)


def _demo_pipeline_graph() -> list[dict[str, object]]:
    """Two-node demo pipeline graph (the shape PipelinesView renders)."""
    return [
        {
            "id": "demo-intake",
            "node_type": "agent",
            "label": "Demo: Intake",
            "position": {"x": 100, "y": 100},
            "config": {"agent_prompt": "Summarise the demo input payload."},
        },
        {
            "id": "demo-report",
            "node_type": "agent",
            "label": "Demo: Report",
            "position": {"x": 400, "y": 100},
            "config": {"agent_prompt": "Write a short demo report from the summary."},
        },
    ]


def _demo_graph_json(nodes: list[dict[str, object]]) -> dict[str, object]:
    return {"nodes": nodes, "edges": [{"id": "demo-edge-1", "source": "demo-intake", "target": "demo-report"}]}


async def _get_or_create_demo_org(session: AsyncSession) -> Organisation:
    """Idempotently create the demo organisation (slug-unique, race-safe)."""
    result = await session.execute(select(Organisation).where(Organisation.slug == DEMO_ORG_SLUG))
    org = result.scalar_one_or_none()
    if org is not None:
        if org.deleted_at is not None:
            org.deleted_at = None
            _log.info("demo_seed.org_undeleted", extra={"slug": DEMO_ORG_SLUG})
        return org
    org = Organisation(name=DEMO_ORG_NAME, slug=DEMO_ORG_SLUG, settings_json={})
    try:
        # Savepoint so a concurrent boot that already committed the slug only
        # rolls back this insert, not the surrounding seed transaction.
        async with session.begin_nested():
            session.add(org)
            await session.flush()
    except IntegrityError:
        result = await session.execute(select(Organisation).where(Organisation.slug == DEMO_ORG_SLUG))
        org = result.scalar_one_or_none()
        if org is None:
            raise
        _log.info("demo_seed.org_recovered_after_conflict", extra={"slug": DEMO_ORG_SLUG})
    else:
        _log.info("demo_seed.org_created", extra={"slug": DEMO_ORG_SLUG})
    return org


async def _seed_demo_account(session: AsyncSession, email: str, password: str) -> Account:
    """Idempotently create/update the demo user (password re-stamped from env)."""
    from modulo.auth.passwords import hash_password, verify_password

    result = await session.execute(select(Account).where(Account.email == email))
    account = result.scalar_one_or_none()
    if account is None:
        account = Account(
            email=email,
            display_name="Demo",
            password_hash=hash_password(password),
            auth_provider="local",
            active=True,
            is_system_admin=False,
        )
        session.add(account)
        await session.flush()
        _log.info("demo_seed.account_created", extra={"email": email})
        return account

    changed = False
    # Re-stamp the hash every run when the stored hash no longer verifies
    # against the env password, so rotating MODULO_DEMO_PASSWORD takes effect
    # on the next boot without manual DB surgery. bcrypt hashes are salted, so
    # the comparison must go through verify_password (never hash-to-hash).
    if not account.password_hash or not verify_password(password, account.password_hash):
        account.password_hash = hash_password(password)
        changed = True
    if account.active is not True:
        account.active = True
        changed = True
    # Defense: the demo account must never be a system admin.
    if account.is_system_admin is True:
        account.is_system_admin = False
        changed = True
    if changed:
        _log.info("demo_seed.account_updated", extra={"email": email})
    return account


async def _seed_demo_membership(session: AsyncSession, account: Account, org: Organisation) -> None:
    """Idempotent viewer-role membership; forces a drifted role back to viewer."""
    result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.account_id == account.id,
            OrgMembership.organisation_id == org.id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        session.add(
            OrgMembership(
                account_id=account.id,
                organisation_id=org.id,
                role=DEMO_ORG_ROLE,
            )
        )
        _log.info("demo_seed.membership_created", extra={"email": account.email, "role": DEMO_ORG_ROLE})
    elif membership.role != DEMO_ORG_ROLE:
        membership.role = DEMO_ORG_ROLE
        _log.warning(
            "demo_seed.membership_role_reset",
            extra={"email": account.email, "previous_role": membership.role, "role": DEMO_ORG_ROLE},
        )


async def _seed_demo_schemas(session: AsyncSession, org: Organisation, account: Account) -> None:
    """Two published demo schemas (idempotent by (organisation, name))."""
    specs = [
        {
            "name": "Demo Intake",
            "description": "Demo sample: intake payload",
            "definition": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
                "required": ["title"],
            },
        },
        {
            "name": "Demo Report",
            "description": "Demo sample: report payload",
            "definition": {
                "type": "object",
                "properties": {"report": {"type": "string"}},
                "required": ["report"],
            },
        },
    ]
    for spec in specs:
        result = await session.execute(
            select(Schema).where(Schema.organisation_id == org.id, Schema.name == spec["name"])
        )
        schema = result.scalar_one_or_none()
        if schema is None:
            schema = Schema(
                organisation_id=org.id,
                name=spec["name"],
                account_id=account.id,
                description=spec["description"],
            )
            session.add(schema)
            await session.flush()
            _log.info("demo_seed.schema_created", extra={"schema_name": spec["name"]})
        version_result = await session.execute(
            select(SchemaVersion).where(
                SchemaVersion.schema_id == schema.id,
                SchemaVersion.version == "v1",
                SchemaVersion.organisation_id == org.id,
            )
        )
        if version_result.scalar_one_or_none() is not None:
            continue
        session.add(
            SchemaVersion(
                organisation_id=org.id,
                schema_id=schema.id,
                version="v1",
                version_number=1,
                definition_json=spec["definition"],
                published=True,
                account_id=account.id,
            )
        )
        _log.info("demo_seed.schema_version_created", extra={"schema_name": spec["name"], "version": "v1"})


async def _seed_demo_pipeline_and_runs(session: AsyncSession, org: Organisation, account: Account) -> None:
    """One demo pipeline (+ snapshot v1) and two terminal demo runs."""
    pipeline_result = await session.execute(
        select(Pipeline).where(Pipeline.organisation_id == org.id, Pipeline.name == "Demo Governance Pipeline")
    )
    pipeline = pipeline_result.scalar_one_or_none()
    if pipeline is None:
        nodes = _demo_pipeline_graph()
        pipeline = Pipeline(
            organisation_id=org.id,
            name="Demo Governance Pipeline",
            description="Demo sample pipeline — read-only demo data (FAR-535).",
            account_id=account.id,
            visibility="org",
            graph_nodes_json=nodes,
            default_autonomy_level="manual_approval",
        )
        session.add(pipeline)
        await session.flush()
        _log.info("demo_seed.pipeline_created", extra={"pipeline_id": str(pipeline.id)})

    snapshot_result = await session.execute(
        select(PipelineSnapshot).where(
            PipelineSnapshot.pipeline_id == pipeline.id,
            PipelineSnapshot.snapshot_version == 1,
        )
    )
    snapshot = snapshot_result.scalar_one_or_none()
    if snapshot is None:
        snapshot = PipelineSnapshot(
            organisation_id=org.id,
            pipeline_id=pipeline.id,
            snapshot_version=1,
            account_id=account.id,
            graph_json=_demo_graph_json(pipeline.graph_nodes_json),
            connector_bindings_json=[],
            schema_pins_json=[],
            prompt_pins_json=[],
            model_backend_pins_json=[],
        )
        session.add(snapshot)
        await session.flush()
        _log.info("demo_seed.snapshot_created", extra={"snapshot_id": str(snapshot.id)})

    # Deterministic, idempotent runs: fixed run_numbers with per-number
    # existence checks. The demo org is seed-owned (the viewer demo user cannot
    # trigger runs), so a missing number can only mean a partial/absent seed.
    run_specs = [
        {"run_number": 1, "status": "complete", "total_tokens": 1840, "total_cost_usd": 0.0042},
        {"run_number": 2, "status": "failed", "total_tokens": 210, "total_cost_usd": 0.0005},
    ]
    for spec in run_specs:
        existing_result = await session.execute(
            select(Run.id).where(Run.organisation_id == org.id, Run.run_number == spec["run_number"])
        )
        if existing_result.scalar_one_or_none() is not None:
            continue
        thread_id = f"demo-seed-{org.id}-{spec['run_number']}"
        run = Run(
            organisation_id=org.id,
            pipeline_id=pipeline.id,
            snapshot_id=snapshot.id,
            account_id=account.id,
            trigger_type="manual",
            status=spec["status"],
            run_number=spec["run_number"],
            input_hash=hashlib.sha256(thread_id.encode()).hexdigest(),
            langgraph_thread_id=thread_id,
            started_at=datetime.now(UTC) - timedelta(hours=2 * spec["run_number"]),
            completed_at=datetime.now(UTC) - timedelta(hours=2 * spec["run_number"] - 1),
            total_tokens=spec["total_tokens"],
            total_cost_usd=spec["total_cost_usd"],
            error_detail="Demo sample failure — no real work was performed." if spec["status"] == "failed" else None,
            error_code="DEMO_SAMPLE" if spec["status"] == "failed" else None,
            outputs_json={"demo": "Sample demo output — synthetic, no agent execution."},
        )
        session.add(run)
        _log.info("demo_seed.run_created", extra={"run_number": spec["run_number"], "status": spec["status"]})


async def seed_demo(session: AsyncSession) -> str | None:
    """Seed the demo org, demo user, and sample data. Idempotent.

    Returns a summary string when the demo feature is configured (something may
    have been created or already existed), or ``None`` when the feature is not
    configured (a deliberate no-op — the caller logs its own outcome).
    """
    settings = get_settings()
    config = demo_login_config(settings)
    if config is None:
        _log.info("demo_seed.disabled")
        return None
    email, password = config

    # Org/account/membership writes follow the boot-seed pattern (system
    # context, no org RLS) used by _seed_modulo_users / seed_demo_org.
    org = await _get_or_create_demo_org(session)
    account = await _seed_demo_account(session, email, password)
    await _seed_demo_membership(session, account, org)

    # Org-scoped entity writes run under the documented execution context so
    # Postgres RLS admits them (mirrors seed_cost_components_for_org).
    await set_rls_org(session, org.id)
    await set_rls_execution_context(session)
    await _seed_demo_schemas(session, org, account)
    await _seed_demo_pipeline_and_runs(session, org, account)

    return f"org={DEMO_ORG_SLUG} user={email}"


async def seed_demo_runtime() -> str | None:
    """Run ``seed_demo`` in its own transaction on the shared session factory.

    Used by the FastAPI boot lifespan (main.py) and the ``python -m modulo.db.seed_demo``
    entry point below.
    """
    from modulo.db.session import AsyncSessionLocal

    async with AsyncSessionLocal() as session, session.begin():
        return await seed_demo(session)


def main() -> None:
    """Standalone entry: seed the demo org/user when configured, then exit."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    if demo_login_config(get_settings()) is None:
        print("[demo-seed] disabled — set MODULO_DEMO_ENABLED, MODULO_DEMO_USER, MODULO_DEMO_PASSWORD")  # noqa: T201
        return
    try:
        summary = asyncio.run(seed_demo_runtime())
        print(f"[demo-seed] ok ({summary})", flush=True)  # noqa: T201
    except Exception as exc:
        _log.exception("demo_seed.failed")
        print(f"[demo-seed] FAILED ({exc!r})", flush=True)  # noqa: T201
        sys.exit(1)


if __name__ == "__main__":
    main()
