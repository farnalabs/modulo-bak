"""Demo auto-login endpoint tests (FAR-535) via FastAPI TestClient.

Locks the POST /api/v1/auth/demo contract: the kill switch answers a plain 404
when any of MODULO_DEMO_ENABLED / MODULO_DEMO_USER / MODULO_DEMO_PASSWORD is
unset or empty, a successful call mints an access token carrying the SHORT
demo TTL (modulo_demo_token_minutes, NOT modulo_access_token_minutes) with NO
refresh token and viewer-role demo-org claims, misconfigured env credentials
are denied byte-identically to /login, the auth.demo_login audit event is
emitted, and the per-IP rate-limit rule (10/hour) is registered.
"""

import logging
import uuid
from collections.abc import AsyncGenerator, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from modulo.api.dependencies import _get_engine, get_db_session
from modulo.api.middleware.rate_limiter import RateLimitMiddleware
from modulo.api.routes.auth import router as auth_router
from modulo.auth.passwords import hash_password
from modulo.settings import Settings, get_settings

_VALID_32 = "a" * 32
_DEMO_ORG_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
_DEMO_USER_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
_DEMO_EMAIL = "demo@modulo.run"
_DEMO_PASSWORD = "demo-passphrase-123"
# Deliberately DIFFERENT from the demo TTL so the exp claim distinguishes which
# setting minted the token (normal login would produce 60-minute expiry here).
_NORMAL_TOKEN_MINUTES = 60
_DEMO_TOKEN_MINUTES = 30


def _settings(
    *,
    demo_enabled: bool = True,
    demo_user: str = _DEMO_EMAIL,
    demo_password: str = _DEMO_PASSWORD,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://localhost/test",
        secret_key=_VALID_32,
        fernet_key=_VALID_32,
        modulo_auth_rate_limit_enabled=False,
        redis_url="",
        modulo_access_token_minutes=_NORMAL_TOKEN_MINUTES,
        modulo_demo_enabled=demo_enabled,
        modulo_demo_user=demo_user,
        modulo_demo_password=demo_password,
        modulo_demo_token_minutes=_DEMO_TOKEN_MINUTES,
    )


def _demo_account(password_hash: str | None = None) -> MagicMock:
    account = MagicMock()
    account.id = _DEMO_USER_ID
    account.email = _DEMO_EMAIL
    account.display_name = "Demo"
    account.active = True
    account.is_system_admin = False
    account.must_change_password = False
    account.password_hash = password_hash if password_hash is not None else hash_password(_DEMO_PASSWORD)
    return account


def _viewer_membership() -> MagicMock:
    membership = MagicMock()
    membership.organisation_id = _DEMO_ORG_ID
    membership.role = "viewer"
    return membership


@pytest.fixture(autouse=True)
def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", _VALID_32)
    monkeypatch.setenv("FERNET_KEY", _VALID_32)
    get_settings.cache_clear()


@pytest.fixture
def mock_session() -> AsyncMock:
    session = AsyncMock(spec=AsyncSession)
    begin_cm = AsyncMock()
    begin_cm.__aenter__ = AsyncMock(return_value=None)
    begin_cm.__aexit__ = AsyncMock(return_value=False)
    session.begin = MagicMock(return_value=begin_cm)
    return session


@pytest.fixture
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(auth_router)
    return _app


def _override_settings(app: FastAPI, **kwargs: bool | str) -> None:
    app.dependency_overrides[get_settings] = lambda: _settings(**kwargs)  # type: ignore[arg-type]


@pytest.fixture
def client(mock_session: AsyncMock, app: FastAPI) -> Generator[TestClient, None, None]:
    async def override_session() -> AsyncGenerator[AsyncMock, None]:
        yield mock_session

    _override_settings(app)
    app.dependency_overrides[get_db_session] = override_session
    app.dependency_overrides[_get_engine] = lambda: MagicMock()
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    ("demo_enabled", "demo_user", "demo_password"),
    [
        pytest.param(False, _DEMO_EMAIL, _DEMO_PASSWORD, id="disabled"),
        pytest.param(True, "", _DEMO_PASSWORD, id="user-empty"),
        pytest.param(True, "   ", _DEMO_PASSWORD, id="user-whitespace"),
        pytest.param(True, _DEMO_EMAIL, "", id="password-empty"),
        pytest.param(False, "", "", id="all-unset"),
    ],
)
def test_demo_login_kill_switch_answers_plain_404(
    app: FastAPI, client: TestClient, demo_enabled: bool, demo_user: str, demo_password: str
) -> None:
    """Any missing/falsy piece of the demo env trio must 404 without DB access."""
    _override_settings(app, demo_enabled=demo_enabled, demo_user=demo_user, demo_password=demo_password)
    with patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=None)) as account_lookup:
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 404
    account_lookup.assert_not_called()


def test_demo_login_success_mints_short_lived_demo_token(client: TestClient) -> None:
    account = _demo_account()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth.list_memberships_for_account",
            new=AsyncMock(return_value=[_viewer_membership()]),
        ),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert "refresh_token" not in body
    claims = jwt.decode(body["access_token"], _VALID_32, algorithms=["HS256"])
    assert claims["sub"] == _DEMO_EMAIL
    assert claims["account_id"] == str(_DEMO_USER_ID)
    assert claims["org_id"] == str(_DEMO_ORG_ID)
    assert claims["org_role"] == "viewer"
    assert claims["is_system_admin"] is False
    # exp-iat must equal the SHORT demo TTL (30 min here), not the normal
    # modulo_access_token_minutes (60 min here) a /login token would carry.
    assert claims["exp"] - claims["iat"] == _DEMO_TOKEN_MINUTES * 60


def test_demo_login_sets_session_cookie_with_demo_ttl(client: TestClient) -> None:
    account = _demo_account()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth.list_memberships_for_account",
            new=AsyncMock(return_value=[_viewer_membership()]),
        ),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 200
    assert "modulo_session" in resp.cookies
    session_cookie = next(
        cookie for cookie in resp.headers.get_list("set-cookie") if cookie.startswith("modulo_session=")
    )
    assert "Max-Age=1800" in session_cookie


def test_demo_login_unknown_env_user_denied_like_login(app: FastAPI, client: TestClient) -> None:
    """Env user matching no real account yields the login-identical 401 shape."""
    _override_settings(app, demo_user="ghost@modulo.run")
    with patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=None)):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


def test_demo_login_env_password_mismatch_denied_like_login(app: FastAPI, client: TestClient) -> None:
    """A real demo account whose stored hash does not match the env password 401s."""
    _override_settings(app, demo_password="rotated-env-password")
    stale_account = _demo_account(password_hash=hash_password("old-stored-password"))
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=stale_account)),
        patch("modulo.api.routes.auth.get_auth_rate_limiter", return_value=None),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


def test_demo_login_success_logs_audit_event(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    account = _demo_account()
    with (
        patch("modulo.api.routes.auth.get_account_by_email", new=AsyncMock(return_value=account)),
        patch("modulo.api.routes.auth.update_last_login", new=AsyncMock()),
        patch(
            "modulo.api.routes.auth.list_memberships_for_account",
            new=AsyncMock(return_value=[_viewer_membership()]),
        ),
        caplog.at_level(logging.INFO, logger="modulo.api.routes.auth"),
    ):
        resp = client.post("/api/v1/auth/demo")
    assert resp.status_code == 200
    demo_records = [record for record in caplog.records if record.getMessage() == "auth.demo_login"]
    assert demo_records
    assert demo_records[0].account_id == str(_DEMO_USER_ID)
    assert demo_records[0].org_id == str(_DEMO_ORG_ID)


def test_demo_rate_limit_rule_registered() -> None:
    """The RULES table must carry the 10/hour per-IP demo rule."""
    demo_rules = [rule for rule in RateLimitMiddleware.RULES if rule.path_prefix == "/api/v1/auth/demo"]
    assert len(demo_rules) == 1
    assert demo_rules[0].max_requests == 10
    assert demo_rules[0].window_s == 3600
