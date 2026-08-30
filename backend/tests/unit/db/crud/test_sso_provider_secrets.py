"""SSO provider secret storage tests."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.auth.secret_storage import DecryptionError, decode_stored_secret
from modulo.db.crud.sso_provider import (
    _slugify_provider_id,
    _unique_provider_id,
    create_provider,
    update_provider,
)
from modulo.db.models.sso_provider import SsoProvider


def test_slugify_symbol_only_or_empty_name_defaults_to_sso() -> None:
    assert _slugify_provider_id("###") == "sso"
    assert _slugify_provider_id("") == "sso"
    assert _slugify_provider_id("  ") == "sso"


def test_slugify_long_name_truncates_within_string_64() -> None:
    long_name = "x" * 200
    slug = _slugify_provider_id(long_name)
    assert len(slug) <= 64
    assert slug == "x" * 58


def test_decode_rejects_encrypted_secret_from_different_key() -> None:
    encrypted = Fernet(Fernet.generate_key()).encrypt(b"secret")

    with pytest.raises(DecryptionError, match="cannot be decrypted"):
        decode_stored_secret(encrypted, Fernet.generate_key().decode())


async def test_create_provider_encrypts_client_secret() -> None:
    key = Fernet.generate_key().decode()
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    session.execute.return_value = result

    with patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock):
        provider = await create_provider(
            session,
            provider_type="oidc",
            name="OIDC",
            client_secret="create-secret",
            fernet_key=key,
            org_id=uuid.uuid4(),
        )

    assert isinstance(provider.client_secret, bytes)
    assert provider.client_secret != b"create-secret"
    assert decode_stored_secret(provider.client_secret, key) == "create-secret"


async def test_update_provider_encrypts_client_secret() -> None:
    key = Fernet.generate_key().decode()
    session = AsyncMock(spec=AsyncSession)
    provider = SsoProvider(
        id=uuid.uuid4(),
        organisation_id=uuid.uuid4(),
        provider_type="oidc",
        name="OIDC",
        client_secret=None,
    )

    with (
        patch("modulo.db.crud.sso_provider.get_provider", new=AsyncMock(return_value=provider)),
        patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock),
    ):
        updated = await update_provider(
            session,
            provider.id,
            org_id=provider.organisation_id,
            fernet_key=key,
            client_secret="update-secret",
        )

    assert updated is provider
    assert isinstance(provider.client_secret, bytes)
    assert decode_stored_secret(provider.client_secret, key) == "update-secret"


# ---------------------------------------------------------------------------
# Globally-unique provider_id (FAR-464 option a)
# ---------------------------------------------------------------------------


async def test_unique_provider_id_dedupes_globally() -> None:
    """The helper scans EVERY provider_id visible to the session and skips to the
    first free ``base``/``base-N`` slug. With existing global slugs ``okta-2`` and
    ``okta-3``, a new ``okta`` base resolves to ``okta-4``."""
    session = AsyncMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.all.return_value = ["okta", "okta-2", "okta-3"]
    session.execute.return_value = result

    assert await _unique_provider_id(session, "okta", uuid.uuid4()) == "okta-4"


async def test_create_provider_uses_system_session_for_global_slug() -> None:
    """create_provider scans ALL orgs via the modulo_system (BYPASSRLS) session.

    A provider named ``Okta`` when another org already owns the global slug
    ``okta`` must get ``okta-2`` — proving the scan is cross-org, not scoped to
    the creating org."""
    key = Fernet.generate_key().decode()
    app_session = AsyncMock(spec=AsyncSession)
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = None
    app_session.execute.return_value = name_result

    system_session = AsyncMock(spec=AsyncSession)
    sys_result = MagicMock()
    sys_result.scalars.return_value.all.return_value = ["okta"]
    system_session.execute.return_value = sys_result

    with (
        patch(
            "modulo.db.crud.sso_provider.get_settings",
            return_value=SimpleNamespace(modulo_system_database_url="postgresql+asyncpg://system/test"),
        ),
        patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock),
    ):
        provider = await create_provider(
            app_session,
            provider_type="oidc",
            name="Okta",
            fernet_key=key,
            org_id=uuid.uuid4(),
            system_session=system_session,
        )

    assert provider.provider_id == "okta-2"


async def test_create_provider_global_dedupe_two_orgs_same_name() -> None:
    """Two orgs with the same provider name get distinct GLOBAL slugs.

    Org A resolves ``okta``; org B (cross-org, system scan now sees ``okta``)
    resolves ``okta-2``. This is the multi-org case the global unique index
    (migration 0151) is designed to satisfy — the pre-fix per-org index would
    have let both rows exist and crashed the system-session read."""
    key = Fernet.generate_key().decode()
    slugs: list[str] = []

    system_session = AsyncMock(spec=AsyncSession)
    sys_result = MagicMock()
    sys_result.scalars.return_value.all.side_effect = lambda: list(slugs)
    system_session.execute.return_value = sys_result

    def _app_session() -> AsyncMock:
        session = AsyncMock(spec=AsyncSession)
        name_result = MagicMock()
        name_result.scalar_one_or_none.return_value = None
        session.execute.return_value = name_result
        return session

    with (
        patch(
            "modulo.db.crud.sso_provider.get_settings",
            return_value=SimpleNamespace(modulo_system_database_url="postgresql+asyncpg://system/test"),
        ),
        patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock),
    ):
        provider_a = await create_provider(
            _app_session(),
            provider_type="oidc",
            name="Okta",
            fernet_key=key,
            org_id=uuid.uuid4(),
            system_session=system_session,
        )
        slugs.append(provider_a.provider_id)

        provider_b = await create_provider(
            _app_session(),
            provider_type="oidc",
            name="Okta",
            fernet_key=key,
            org_id=uuid.uuid4(),
            system_session=system_session,
        )
        slugs.append(provider_b.provider_id)

    assert provider_a.provider_id == "okta"
    assert provider_b.provider_id == "okta-2"
    assert provider_a.provider_id != provider_b.provider_id


async def test_create_provider_falls_back_to_app_session_scan() -> None:
    """When the system role is unprovisioned (URL unset), the app session is used.

    The intra-org dedupe is preserved so a same-org duplicate still gets a
    distinct slug rather than hitting the global unique index."""
    key = Fernet.generate_key().decode()
    app_session = AsyncMock(spec=AsyncSession)
    name_result = MagicMock()
    name_result.scalar_one_or_none.return_value = None
    scan_result = MagicMock()
    scan_result.scalars.return_value.all.return_value = ["okta"]
    app_session.execute.side_effect = [name_result, scan_result]

    system_session = AsyncMock(spec=AsyncSession)

    with (
        patch(
            "modulo.db.crud.sso_provider.get_settings",
            return_value=SimpleNamespace(modulo_system_database_url=""),
        ),
        patch("modulo.db.crud.sso_provider.append_audit_event", new_callable=AsyncMock),
    ):
        provider = await create_provider(
            app_session,
            provider_type="oidc",
            name="Okta",
            fernet_key=key,
            org_id=uuid.uuid4(),
            system_session=system_session,
        )

    assert provider.provider_id == "okta-2"
