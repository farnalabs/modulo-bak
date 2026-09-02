"""Unit tests for the FAR-535 demo auto-login seed (modulo.db.seed_demo).

Hermetic in-memory SQLite (mirrors tests/unit/db/test_seed.py — no Postgres
required). Locks the seed contract: env-gated no-op when the demo trio is not
fully configured, idempotent entity creation (org, account, viewer membership,
2 published schemas, 1 pipeline + snapshot, deterministic terminal runs 1-2),
password re-stamping on MODULO_DEMO_PASSWORD rotation (including a corrupt
stored hash re-stamping instead of crashing the seed), role/system-admin/
must_change_password drift reset, the drift warning reporting the role captured
BEFORE the overwrite, demo-org scoping of every seeded entity even with a
second (non-demo) organisation present, and the seed_demo_runtime wrapper
running the seed in its own transaction on a caller-provided session factory.
"""

import logging
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import modulo.db.seed_demo as seed_demo_module
from modulo.auth.passwords import hash_password, verify_password
from modulo.core.demo import DEMO_ORG_SLUG
from modulo.db.models.account import Account
from modulo.db.models.base import Base
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation
from modulo.db.models.pipeline import Pipeline
from modulo.db.models.pipeline_snapshot import PipelineSnapshot
from modulo.db.models.run import Run
from modulo.db.models.schema import Schema, SchemaVersion
from modulo.db.seed_demo import seed_demo, seed_demo_runtime
from modulo.settings import Settings

_VALID_32 = "a" * 32
_DEMO_EMAIL = "demo@modulo.run"
_DEMO_PASSWORD = "demo-passphrase-123"

# Tables required by the seed incl. FK dependencies. Scoped create_all because
# unrelated models use Postgres-only column types SQLite cannot render.
_SEED_TABLES = {
    "organisations",
    "accounts",
    "org_memberships",
    "schemas",
    "schema_versions",
    "pipelines",
    "pipeline_snapshots",
    "runs",
}


def _demo_settings(*, enabled: bool = True, user: str = _DEMO_EMAIL, password: str = _DEMO_PASSWORD) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_demo_enabled=enabled,
        modulo_demo_user=user,
        modulo_demo_password=password,
    )


@pytest.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with eng.begin() as conn:
        wanted = [t for t in Base.metadata.sorted_tables if t.name in _SEED_TABLES]
        await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=wanted))
    yield eng
    await eng.dispose()


@pytest.fixture
async def session(engine) -> AsyncGenerator[AsyncSession, None]:
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


async def _run_seed(session: AsyncSession, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> str | None:
    monkeypatch.setattr(seed_demo_module, "get_settings", lambda: settings)
    # Reads between seeds autobegin an implicit transaction on the shared
    # session; end it so the explicit seed transaction can start cleanly.
    if session.in_transaction():
        await session.rollback()
    async with session.begin():
        return await seed_demo(session)


async def _count(session: AsyncSession, model: type) -> int:
    return (await session.execute(select(func.count()).select_from(model))).scalar_one()


async def _orgs(session: AsyncSession) -> list[Organisation]:
    return list((await session.execute(select(Organisation))).scalars())


@pytest.mark.parametrize(
    ("enabled", "user", "password"),
    [
        pytest.param(False, _DEMO_EMAIL, _DEMO_PASSWORD, id="disabled"),
        pytest.param(True, "", _DEMO_PASSWORD, id="user-empty"),
        pytest.param(True, _DEMO_EMAIL, "", id="password-empty"),
    ],
)
async def test_seed_disabled_is_a_noop(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, enabled: bool, user: str, password: str
) -> None:
    summary = await _run_seed(session, monkeypatch, _demo_settings(enabled=enabled, user=user, password=password))
    assert summary is None
    assert await _count(session, Organisation) == 0
    assert await _count(session, Account) == 0


async def test_seed_creates_demo_entities(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    summary = await _run_seed(session, monkeypatch, _demo_settings())
    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"

    assert await _count(session, Organisation) == 1
    assert await _count(session, Account) == 1
    assert await _count(session, OrgMembership) == 1
    assert await _count(session, Schema) == 2
    assert await _count(session, SchemaVersion) == 2
    assert await _count(session, Pipeline) == 1
    assert await _count(session, PipelineSnapshot) == 1
    assert await _count(session, Run) == 2

    org = (await _orgs(session))[0]
    assert org.slug == DEMO_ORG_SLUG

    account = (await session.execute(select(Account))).scalar_one()
    assert account.email == _DEMO_EMAIL
    assert account.is_system_admin is False
    assert account.active is True
    assert account.must_change_password is False

    membership = (await session.execute(select(OrgMembership))).scalar_one()
    assert membership.role == "viewer"
    assert membership.account_id == account.id
    assert membership.organisation_id == org.id

    published_versions = list(
        (await session.execute(select(SchemaVersion).where(SchemaVersion.published.is_(True)))).scalars()
    )
    assert len(published_versions) == 2

    runs = list((await session.execute(select(Run))).scalars())
    assert {run.run_number for run in runs} == {1, 2}
    assert {run.status for run in runs} == {"complete", "failed"}


async def test_seed_is_idempotent(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _run_seed(session, monkeypatch, _demo_settings())
    counts_first = {
        "orgs": await _count(session, Organisation),
        "accounts": await _count(session, Account),
        "memberships": await _count(session, OrgMembership),
        "schemas": await _count(session, Schema),
        "schema_versions": await _count(session, SchemaVersion),
        "pipelines": await _count(session, Pipeline),
        "snapshots": await _count(session, PipelineSnapshot),
        "runs": await _count(session, Run),
    }

    await _run_seed(session, monkeypatch, _demo_settings())

    counts_second = {
        "orgs": await _count(session, Organisation),
        "accounts": await _count(session, Account),
        "memberships": await _count(session, OrgMembership),
        "schemas": await _count(session, Schema),
        "schema_versions": await _count(session, SchemaVersion),
        "pipelines": await _count(session, Pipeline),
        "snapshots": await _count(session, PipelineSnapshot),
        "runs": await _count(session, Run),
    }
    assert counts_second == counts_first
    assert counts_second["runs"] == 2


async def test_seed_restamps_password_on_rotation(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _run_seed(session, monkeypatch, _demo_settings())
    rotated = "rotated-demo-passphrase"
    await _run_seed(session, monkeypatch, _demo_settings(password=rotated))

    account = (await session.execute(select(Account))).scalar_one()
    assert verify_password(rotated, account.password_hash) is True
    assert verify_password(_DEMO_PASSWORD, account.password_hash) is False


async def test_seed_resets_role_and_system_admin_drift(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    await _run_seed(session, monkeypatch, _demo_settings())

    account = (await session.execute(select(Account))).scalar_one()
    membership = (await session.execute(select(OrgMembership))).scalar_one()
    membership.role = "operator"
    account.is_system_admin = True
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    refreshed_account = (await session.execute(select(Account))).scalar_one()
    refreshed_membership = (await session.execute(select(OrgMembership))).scalar_one()
    assert refreshed_membership.role == "viewer"
    assert refreshed_account.is_system_admin is False


async def test_seed_scopes_entities_to_demo_org_with_second_org_present(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_account = Account(
        email="other@example.com",
        display_name="Other",
        password_hash=hash_password("other-passphrase-123"),
        auth_provider="local",
        active=True,
    )
    other_org = Organisation(name="Other", slug="other", settings_json={})
    session.add(other_account)
    session.add(other_org)
    await session.flush()
    other_pipeline = Pipeline(
        organisation_id=other_org.id,
        name="Other Org Pipeline",
        account_id=other_account.id,
        visibility="org",
    )
    session.add(other_pipeline)
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    orgs = await _orgs(session)
    orgs_by_slug = {org.slug: org for org in orgs}
    demo_org = orgs_by_slug[DEMO_ORG_SLUG]
    other = orgs_by_slug["other"]

    demo_pipeline = (
        await session.execute(select(Pipeline).where(Pipeline.name == "Demo Governance Pipeline"))
    ).scalar_one()
    assert demo_pipeline.organisation_id == demo_org.id
    assert demo_pipeline.organisation_id != other.id

    runs = list((await session.execute(select(Run))).scalars())
    assert {run.organisation_id for run in runs} == {demo_org.id}

    schemas = list((await session.execute(select(Schema))).scalars())
    assert {schema.organisation_id for schema in schemas} == {demo_org.id}

    other_pipeline = (await session.execute(select(Pipeline).where(Pipeline.name == "Other Org Pipeline"))).scalar_one()
    assert other_pipeline.organisation_id == other.id

    demo_account = (await session.execute(select(Account).where(Account.email == _DEMO_EMAIL))).scalar_one()
    demo_memberships = list(
        (await session.execute(select(OrgMembership).where(OrgMembership.account_id == demo_account.id))).scalars()
    )
    assert {membership.organisation_id for membership in demo_memberships} == {demo_org.id}


async def test_seed_logs_previous_role_on_drift_reset(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The role-drift warning must report the role captured BEFORE the overwrite."""
    await _run_seed(session, monkeypatch, _demo_settings())
    membership = (await session.execute(select(OrgMembership))).scalar_one()
    membership.role = "operator"
    await session.commit()

    with caplog.at_level(logging.WARNING, logger="modulo.db.seed_demo"):
        await _run_seed(session, monkeypatch, _demo_settings())

    reset_records = [record for record in caplog.records if record.getMessage() == "demo_seed.membership_role_reset"]
    assert len(reset_records) == 1
    assert reset_records[0].previous_role == "operator"
    assert reset_records[0].role == "viewer"
    assert reset_records[0].email == _DEMO_EMAIL

    refreshed = (await session.execute(select(OrgMembership))).scalar_one()
    assert refreshed.role == "viewer"


async def test_seed_restamps_corrupt_password_hash(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A malformed stored hash re-stamps from env instead of crashing the seed."""
    await _run_seed(session, monkeypatch, _demo_settings())
    account = (await session.execute(select(Account))).scalar_one()
    account.password_hash = "not-a-valid-bcrypt-hash"
    await session.commit()

    summary = await _run_seed(session, monkeypatch, _demo_settings())

    assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
    repaired = (await session.execute(select(Account))).scalar_one()
    assert verify_password(_DEMO_PASSWORD, repaired.password_hash) is True


async def test_seed_resets_must_change_password_drift(session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pre-existing account flagged must_change_password is reset to False.

    Otherwise the demo viewer would be trapped in ForceChangePasswordView,
    whose mutation is viewer-denied — an unusable demo session.
    """
    await _run_seed(session, monkeypatch, _demo_settings())
    account = (await session.execute(select(Account))).scalar_one()
    account.must_change_password = True
    await session.commit()

    await _run_seed(session, monkeypatch, _demo_settings())

    refreshed = (await session.execute(select(Account))).scalar_one()
    assert refreshed.must_change_password is False


async def test_seed_demo_runtime_uses_provided_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    """seed_demo_runtime runs the seed in its own transaction on the given factory.

    main.py's boot lifespan passes its DI engine-backed factory; the wrapper
    must own the transaction so both callers share one engine path each.
    """
    eng = create_async_engine("sqlite+aiosqlite://", echo=False)
    try:
        async with eng.begin() as conn:
            wanted = [t for t in Base.metadata.sorted_tables if t.name in _SEED_TABLES]
            await conn.run_sync(lambda sync_conn: Base.metadata.create_all(sync_conn, tables=wanted))
        maker = async_sessionmaker(eng, expire_on_commit=False)
        monkeypatch.setattr(seed_demo_module, "get_settings", lambda: _demo_settings())

        summary = await seed_demo_runtime(session_factory=maker)

        assert summary == f"org={DEMO_ORG_SLUG} user={_DEMO_EMAIL}"
        async with maker() as check:
            assert await _count(check, Organisation) == 1
            assert await _count(check, Account) == 1
            assert await _count(check, Run) == 2

        summary_again = await seed_demo_runtime(session_factory=maker)

        assert summary_again == summary
        async with maker() as check:
            assert await _count(check, Organisation) == 1
            assert await _count(check, Account) == 1
            assert await _count(check, Run) == 2
    finally:
        await eng.dispose()
