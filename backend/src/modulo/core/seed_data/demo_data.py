"""Demo-org seed framework (FAR-450 foundation ticket).

GATED behind ``MODULO_SEED_DEMO_ORGS`` (see ``modulo.settings``). Each demo
organisation carries its OWN signed ``license_key`` in
``Organisation.settings_json`` so it resolves the intended tier (community or
team) via ``core.feature_flags.resolve_plan_context`` — a bare ``plan_id``
without a valid signed license silently downgrades to community, so the
per-org license is mandatory.

This is the FOUNDATION ticket: it builds the framework only. The module-level
``DEMO_ORGS`` list is empty by default; follow-up tickets populate it with
concrete organisations and seed their entities (the ``full`` flag gates which
entities get seeded — ignored for now).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from modulo.core.license_signing import (
    LicenseSigningError,
    encode_license_key,
    generate_team_license,
)
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation

_log = logging.getLogger(__name__)

# Demo organisations to seed at boot. Empty by default (FAR-450 foundation) —
# follow-up tickets append dicts of the form:
#   {"slug": str, "tier": "community" | "team", "full": bool,
#    "email": str, "password": str}
DEMO_ORGS: list[dict[str, Any]] = []

# Far-future expiry (~10y) for community demo licenses so they never expire
# mid-eval.
_COMMUNITY_LICENSE_YEARS = 10


def _community_license_key(slug: str, private_key_hex: str) -> str:
    """Sign a community-tier license key for *slug*."""
    org_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"modulo:{slug}"))
    expires_at = (datetime.now(UTC) + timedelta(days=365 * _COMMUNITY_LICENSE_YEARS)).isoformat()
    payload: dict[str, Any] = {
        "tier": "community",
        "features": [],
        "expires_at": expires_at,
        "org_id": org_id,
    }
    return encode_license_key(payload, private_key_hex)


async def seed_demo_org(
    session: Any,
    settings: Any,
    *,
    slug: str,
    tier: str,
    full: bool,
    admin_email: str,
    admin_password: str,
) -> None:
    """Idempotently create a demo organisation + admin account + membership.

    All three entities are created only if absent (slug / email / account+org
    existence guards), so the function is safe to call on every boot. The org is
    stamped with a per-org signed ``license_key`` (community or team) so the
    tier resolves correctly through ``resolve_plan_context``.

    ``full`` is accepted but IGNORED for now — demo entities are seeded in
    follow-up tickets. It is persisted under ``settings_json["demo"]["full"]``
    for future use.
    """
    private_key = getattr(settings, "modulo_license_private_key", "") or ""
    if not private_key:
        raise LicenseSigningError(
            f"No license signing private key configured for demo org {slug!r} (set MODULO_LICENSE_PRIVATE_KEY)."
        )

    # 1. Organisation (idempotent by slug)
    result = await session.execute(select(Organisation).where(Organisation.slug == slug))
    org = result.scalar_one_or_none()
    if org is None:
        org = Organisation(name=slug, slug=slug, settings_json={})
        session.add(org)
        await session.flush()
        _log.info("demo_org.created", extra={"slug": slug})
    else:
        _log.info("demo_org.exists", extra={"slug": slug})

    # 2. Admin account (idempotent by email)
    result = await session.execute(select(Account).where(Account.email == admin_email))
    account = result.scalar_one_or_none()
    if account is None:
        from modulo.auth.passwords import hash_password

        account = Account(
            email=admin_email,
            display_name=admin_email.split("@", 1)[0],
            password_hash=hash_password(admin_password),
            auth_provider="local",
        )
        session.add(account)
        await session.flush()
        _log.info("demo_account.created", extra={"email": admin_email})
    else:
        _log.info("demo_account.exists", extra={"email": admin_email})

    # 3. Membership (idempotent by account + org)
    result = await session.execute(
        select(OrgMembership).where(
            OrgMembership.account_id == account.id,
            OrgMembership.organisation_id == org.id,
        )
    )
    membership = result.scalar_one_or_none()
    if membership is None:
        membership = OrgMembership(
            account_id=account.id,
            organisation_id=org.id,
            role="admin",
        )
        session.add(membership)
        _log.info("demo_membership.created", extra={"email": admin_email, "slug": slug})
    else:
        _log.info("demo_membership.exists", extra={"email": admin_email, "slug": slug})

    # 4. Per-org signed license (community | team)
    if tier == "team":
        license_key = generate_team_license(org_name=slug, private_key_hex=private_key)
    else:
        license_key = _community_license_key(slug, private_key)

    settings_json: dict[str, Any] = dict(org.settings_json or {})
    settings_json["license_key"] = license_key
    settings_json.setdefault("demo", {})["full"] = bool(full)
    org.settings_json = settings_json
    await session.flush()


async def seed_demo_orgs(settings: Any) -> None:
    """Seed every demo org listed in ``DEMO_ORGS`` (gated by the caller).

    The boot lifespan gates this with ``settings.modulo_seed_demo_orgs``; this
    function simply iterates ``DEMO_ORGS``. Each org is seeded in its own
    transaction for isolation. Idempotent — re-running never duplicates.
    """
    if not DEMO_ORGS:
        _log.info("demo_orgs.empty")
        return

    from modulo.api.dependencies import get_or_create_engine, get_or_create_session_factory

    engine = get_or_create_engine(settings)
    factory = get_or_create_session_factory(engine)

    for spec in DEMO_ORGS:
        async with factory() as session, session.begin():
            await seed_demo_org(
                session,
                settings,
                slug=spec["slug"],
                tier=spec["tier"],
                full=spec.get("full", False),
                admin_email=spec["email"],
                admin_password=spec["password"],
            )
