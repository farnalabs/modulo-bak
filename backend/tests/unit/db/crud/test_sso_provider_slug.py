"""Tests for SSO provider slug derivation and org-uniqueness dedup (FAR-457).

These exercise the *real* DB path in ``create_provider`` (and the login
resolver ``_resolve_oidc_provider``) so the feature core — deriving a URL-safe
``provider_id`` slug from the provider name and de-duplicating collisions with
``-2`` suffixes — is covered by a test that fails without the slug code.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from modulo.auth.sso import _resolve_oidc_provider
from modulo.db.crud.sso_provider import create_provider
from modulo.db.models.base import Base
from modulo.db.models.sso_provider import SsoProvider
from modulo.settings import Settings


@pytest.fixture
async def session() -> AsyncSession:
    engine: AsyncEngine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=[SsoProvider.__table__]))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    with patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock):
        async with maker() as s:
            yield s
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        secret_key="a" * 32,
        fernet_key="a" * 32,
        modulo_license_key="test-license",
        modulo_oidc_providers="[]",
    )


async def test_create_provider_derives_slug_from_name(session: AsyncSession) -> None:
    org_id = uuid.uuid4()
    provider = await create_provider(
        session,
        provider_type="oidc",
        name="Acme Corp OIDC!",
        fernet_key="a" * 32,
        org_id=org_id,
    )
    # URL-safe slug: lowercased, non-alnum collapsed to '-', punctuation dropped.
    assert provider.provider_id == "acme-corp-oidc"


async def test_create_provider_uses_explicit_provider_id(session: AsyncSession) -> None:
    org_id = uuid.uuid4()
    provider = await create_provider(
        session,
        provider_type="oidc",
        name="Display Name",
        provider_id="custom-slug",
        fernet_key="a" * 32,
        org_id=org_id,
    )
    assert provider.provider_id == "custom-slug"


async def test_create_provider_dedups_colliding_slug(session: AsyncSession) -> None:
    org_id = uuid.uuid4()
    # Distinct names that slugify to the same provider_id exercise the dedup loop
    # (the name check rejects same-name inserts, so collisions are cross-name).
    first = await create_provider(session, provider_type="oidc", name="Acme Corp!", fernet_key="a" * 32, org_id=org_id)
    assert first.provider_id == "acme-corp"
    second = await create_provider(session, provider_type="oidc", name="Acme Corp?", fernet_key="a" * 32, org_id=org_id)
    assert second.provider_id == "acme-corp-2"
    third = await create_provider(session, provider_type="oidc", name="Acme Corp@", fernet_key="a" * 32, org_id=org_id)
    assert third.provider_id == "acme-corp-3"


async def test_create_provider_slug_unique_per_org_not_global(session: AsyncSession) -> None:
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    a_provider = await create_provider(session, provider_type="oidc", name="Acme", fernet_key="a" * 32, org_id=org_a)
    # Same name in a different org reuses the slug (uniqueness is per-org).
    b_provider = await create_provider(session, provider_type="oidc", name="Acme", fernet_key="a" * 32, org_id=org_b)
    assert a_provider.provider_id == "acme"
    assert b_provider.provider_id == "acme"


async def test_admin_created_provider_round_trips_into_oidc_login_resolver(session: AsyncSession) -> None:
    org_id = uuid.uuid4()
    provider = await create_provider(
        session,
        provider_type="oidc",
        name="Acme Login",
        client_id="acme-client",
        scopes=["openid", "email"],
        discovery_url=None,
        fernet_key="a" * 32,
        org_id=org_id,
    )
    # The login route resolves IdP config by the slug via get_provider_by_provider_id.
    client_id, _secret, discovery_url, scopes, db_provider = await _resolve_oidc_provider(
        "acme-login", session, _settings()
    )
    assert db_provider is provider
    assert client_id == "acme-client"
    assert scopes == ["openid", "email"]
    assert discovery_url is None

    # Resolving by the raw display name (not the slug) must NOT find the provider.
    miss_client, _s, _d, _sc, miss_provider = await _resolve_oidc_provider("Acme Login", session, _settings())
    assert miss_provider is None
    assert miss_client is None
