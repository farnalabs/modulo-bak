"""Unit tests for the demo-org seed framework (FAR-450).

These exercise the seeding logic and per-org signed-license wiring without a
live database: a tiny fake async session records added entities and answers the
existence queries seed_demo_org issues, while the real license signing/verification
helpers (encode_license_key / generate_team_license / parse_and_verify) run
end-to-end with a generated Ed25519 test keypair.

resolve_plan_context is exercised against the real license on the org by
stubbing the DB-backed tier-catalog lookup (FeatureFlagRegistry.from_db) — the
org license path is exactly what FAR-450 is about.
"""

from __future__ import annotations

import operator
import types

import pytest

from modulo.core.license import parse_and_verify, set_public_key
from modulo.core.license_signing import LicenseSigningError
from modulo.core.registry.crypto import generate_keypair
from modulo.core.seed_data.demo_data import DEMO_ORGS, seed_demo_org
from modulo.db.models.account import Account
from modulo.db.models.org_membership import OrgMembership
from modulo.db.models.organisation import Organisation

_KP = generate_keypair()
_TEST_PRIV = _KP["private_key"]
_TEST_PUB = _KP["public_key"]
set_public_key(_TEST_PUB)


# ---------------------------------------------------------------------------
# Fake async session
# ---------------------------------------------------------------------------
class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


def _col_name(left) -> str | None:
    for attr in ("key", "name"):
        val = getattr(left, attr, None)
        if val:
            return val
    return None


def _extract_predicates(clause) -> list[tuple[str, object]]:
    preds: list[tuple[str, object]] = []
    if clause is None:
        return preds
    if hasattr(clause, "clauses"):  # and_ / BooleanClauseList
        for sub in clause.clauses:
            preds.extend(_extract_predicates(sub))
        return preds
    if getattr(clause, "operator", None) is operator.eq:
        name = _col_name(clause.left)
        val = getattr(clause.right, "value", None)
        if name is not None:
            preds.append((name, val))
    return preds


class FakeAsyncSession:
    """Minimal in-memory async session for seeding logic."""

    def __init__(self) -> None:
        self._store: dict[type, list] = {}
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)
        self._store.setdefault(type(obj), []).append(obj)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        preds = _extract_predicates(stmt.whereclause)
        matches = [
            obj for obj in self._store.get(entity, []) if all(getattr(obj, name, None) == val for (name, val) in preds)
        ]
        return _FakeResult(matches)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


def _settings(private_key: str = _TEST_PRIV):
    return types.SimpleNamespace(
        modulo_license_private_key=private_key,
        modulo_license_key="",
    )


def _count(store: dict, cls: type) -> int:
    return len(store.get(cls, []))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------
async def test_seed_demo_org_community_creates_entities_and_license() -> None:
    session = FakeAsyncSession()
    await seed_demo_org(
        session,
        _settings(),
        slug="demo-community",
        tier="community",
        full=False,
        admin_email="admin@demo.example",
        admin_password="secret123",
    )

    assert _count(session._store, Organisation) == 1
    assert _count(session._store, Account) == 1
    assert _count(session._store, OrgMembership) == 1

    org = session._store[Organisation][0]
    account = session._store[Account][0]
    membership = session._store[OrgMembership][0]

    assert org.slug == "demo-community"
    assert account.email == "admin@demo.example"
    assert account.password_hash.startswith("$2")  # bcrypt hash, not plaintext
    assert account.auth_provider == "local"
    assert membership.account_id == account.id
    assert membership.organisation_id == org.id
    assert membership.role == "admin"

    license_key = org.settings_json["license_key"]
    validation = parse_and_verify(license_key)
    assert validation.valid
    assert validation.license_data.tier == "community"
    assert org.settings_json["demo"]["full"] is False


async def test_seed_demo_org_team_license() -> None:
    session = FakeAsyncSession()
    await seed_demo_org(
        session,
        _settings(),
        slug="demo-team",
        tier="team",
        full=True,
        admin_email="admin@team.example",
        admin_password="secret123",
    )

    org = session._store[Organisation][0]
    license_key = org.settings_json["license_key"]
    validation = parse_and_verify(license_key)
    assert validation.valid
    assert validation.license_data.tier == "team"
    # Team license grants the team feature set.
    assert "team_rbac" in validation.license_data.features
    assert org.settings_json["demo"]["full"] is True


async def test_seed_demo_org_idempotent() -> None:
    session = FakeAsyncSession()
    kwargs = {
        "slug": "demo-idem",
        "tier": "community",
        "full": False,
        "admin_email": "idem@demo.example",
        "admin_password": "secret123",
    }
    await seed_demo_org(session, _settings(), **kwargs)
    org_id_after_first = session._store[Organisation][0].id
    await seed_demo_org(session, _settings(), **kwargs)

    # No duplicates after a second call.
    assert _count(session._store, Organisation) == 1
    assert _count(session._store, Account) == 1
    assert _count(session._store, OrgMembership) == 1
    # The same org object is reused (settings update path, not a new row).
    assert session._store[Organisation][0].id == org_id_after_first


async def test_seed_demo_org_requires_private_key_for_team() -> None:
    session = FakeAsyncSession()
    with pytest.raises(LicenseSigningError):
        await seed_demo_org(
            session,
            _settings(private_key=""),
            slug="demo-np",
            tier="team",
            full=False,
            admin_email="np@demo.example",
            admin_password="secret123",
        )


async def test_resolve_plan_context_returns_per_org_tier(monkeypatch) -> None:
    from modulo.core.feature_flags import DbPlanContext, FeatureFlagRegistry, resolve_plan_context

    # Stub the DB tier-catalog lookup — we only care that the ORG license's
    # tier flows through resolve_plan_context unchanged.
    async def _fake_from_db(cls, session, plan_id, has_license_key=False, license_features=None):
        return DbPlanContext(FeatureFlagRegistry(current_tier=plan_id, has_license_key=has_license_key))

    monkeypatch.setattr(
        "modulo.core.feature_flags.DbPlanContext.from_db",
        classmethod(_fake_from_db),
    )

    for tier in ("community", "team"):
        session = FakeAsyncSession()
        await seed_demo_org(
            session,
            _settings(),
            slug=f"rpc-{tier}",
            tier=tier,
            full=False,
            admin_email=f"rpc-{tier}@demo.example",
            admin_password="secret123",
        )
        org = session._store[Organisation][0]
        pc = await resolve_plan_context(_settings(), session, org=org)
        assert pc.tier() == tier
        assert pc.has_license_key() is True


def test_seed_demo_orgs_empty_by_default() -> None:
    # FAR-450 foundation: DEMO_ORGS ships empty so nothing seeds until a
    # follow-up ticket populates it.
    assert DEMO_ORGS == []
