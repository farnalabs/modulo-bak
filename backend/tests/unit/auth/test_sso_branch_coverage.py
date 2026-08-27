"""Additional branch coverage for modulo.auth.sso (FAR-457).

These tests exercise defensive / error branches and new DB-backed provider
resolution paths introduced by the SSO source-of-truth migration so the
``modulo.auth`` per-module coverage gate stays at >= 90%.
"""

import base64
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from modulo.auth.sso import (
    _lookup_provider_by_client_id,
    _lookup_provider_by_entity_id,
    _parse_oidc_providers,
    _require_json_object,
    _resolve_oidc_provider,
    _saml_parse_idp_metadata,
    _validate_saml_response_destination,
    apply_group_mappings,
    oidc_get_authorize_url,
    oidc_process_callback,
    saml_get_auth_url,
    saml_process_response,
    sign_state,
)
from modulo.settings import Settings

_VALID_32 = "a" * 32


def _override(**kwargs: str | bool) -> Settings:
    base: dict[str, str | bool] = {
        "database_url": "postgresql+asyncpg://localhost/test",
        "secret_key": _VALID_32,
        "fernet_key": _VALID_32,
        "modulo_license_key": "test-license",
        "modulo_oidc_providers": json.dumps(
            [
                {
                    "provider_id": "google",
                    "client_id": "google-client-id",
                    "client_secret": "google-client-secret",
                    "discovery_url": "https://accounts.google.com/.well-known/openid-configuration",
                }
            ]
        ),
    }
    base.update(kwargs)
    return Settings(**base)  # type: ignore[arg-type]


def _mock_session(scalar: object = None) -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = scalar
    result.scalars.return_value.all.return_value = []
    session.execute.return_value = result
    return session


def _decoded_id_token(email: str = "user@example.com") -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"RS256"}').rstrip(b"=").decode()
    payload = (
        base64.urlsafe_b64encode(json.dumps({"email": email, "name": "Test", "sub": "abc"}).encode())
        .rstrip(b"=")
        .decode()
    )
    return f"{header}.{payload}.sig"


# ---------------------------------------------------------------------------
# apply_group_mappings
# ---------------------------------------------------------------------------


class TestApplyGroupMappings:
    async def test_skips_non_dict_mapping(self) -> None:
        session = _mock_session()
        account = SimpleNamespace(id=uuid.uuid4())
        await apply_group_mappings(session, account, uuid.uuid4(), ["team-a"], ["not-a-dict"])  # type: ignore[list-item]
        session.execute.assert_not_awaited()

    async def test_skips_invalid_team_uuid(self) -> None:
        session = _mock_session()
        account = SimpleNamespace(id=uuid.uuid4())
        with patch("modulo.auth.sso.get_membership_by_team_and_account", new_callable=AsyncMock) as m:
            m.return_value = None
            await apply_group_mappings(
                session,
                account,
                uuid.uuid4(),
                ["team-a"],
                [{"idp_group": "team-a", "team_id": "not-a-uuid", "team_role": "viewer"}],
            )
            m.assert_not_awaited()

    async def test_adds_new_team_member(self) -> None:
        session = _mock_session()
        account = SimpleNamespace(id=uuid.uuid4())
        org_id = uuid.uuid4()
        team_id = uuid.uuid4()
        with (
            patch("modulo.auth.sso.get_membership_by_team_and_account", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.add_team_member", new_callable=AsyncMock) as add,
            patch("modulo.auth.sso.update_member_role", new_callable=AsyncMock) as upd,
        ):
            m.return_value = None
            await apply_group_mappings(
                session,
                account,
                org_id,
                ["team-a"],
                [{"idp_group": "team-a", "team_id": str(team_id), "team_role": "editor"}],
            )
            add.assert_awaited_once()
            upd.assert_not_awaited()

    async def test_updates_existing_team_member_role(self) -> None:
        session = _mock_session()
        account = SimpleNamespace(id=uuid.uuid4())
        org_id = uuid.uuid4()
        team_id = uuid.uuid4()
        existing = SimpleNamespace(id=uuid.uuid4(), role="viewer")
        with (
            patch("modulo.auth.sso.get_membership_by_team_and_account", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.add_team_member", new_callable=AsyncMock) as add,
            patch("modulo.auth.sso.update_member_role", new_callable=AsyncMock) as upd,
        ):
            m.return_value = existing
            await apply_group_mappings(
                session,
                account,
                org_id,
                ["team-a"],
                [{"idp_group": "team-a", "team_id": str(team_id), "team_role": "editor"}],
            )
            add.assert_not_awaited()
            upd.assert_awaited_once_with(session, existing.id, "editor")


# ---------------------------------------------------------------------------
# DB provider lookup helpers
# ---------------------------------------------------------------------------


class TestLookupProviderHelpers:
    async def test_lookup_by_client_id_returns_provider(self) -> None:
        session = _mock_session()
        provider = SimpleNamespace(id="p1")
        session.execute.return_value.scalar_one_or_none.return_value = provider
        result = await _lookup_provider_by_client_id(session, "client-1", uuid.uuid4())
        assert result is provider

    async def test_lookup_by_entity_id_returns_provider(self) -> None:
        session = _mock_session()
        provider = SimpleNamespace(id="p2")
        session.execute.return_value.scalar_one_or_none.return_value = provider
        result = await _lookup_provider_by_entity_id(session, "entity-1", uuid.uuid4())
        assert result is provider


# ---------------------------------------------------------------------------
# OIDC provider parsing edge cases
# ---------------------------------------------------------------------------


class TestOidcProviderParsingEdges:
    def test_empty_string_returns_empty(self) -> None:
        assert _parse_oidc_providers(_override(modulo_oidc_providers="")) == []

    def test_non_array_logs_and_returns_empty(self) -> None:
        providers = _parse_oidc_providers(_override(modulo_oidc_providers=json.dumps({"provider_id": "x"})))
        assert providers == []

    def test_require_json_object_rejects_non_string_key(self) -> None:
        with pytest.raises(ValueError, match="non-string key"):
            _require_json_object({1: "value"}, "ctx")


# ---------------------------------------------------------------------------
# OIDC provider resolution (SSRF guard)
# ---------------------------------------------------------------------------


class TestOidcResolveSsrf:
    async def test_rejects_blocked_discovery_url(self) -> None:
        settings = _override()
        session = _mock_session()
        db_provider = SimpleNamespace(
            provider_type="oidc",
            enabled=True,
            discovery_url="http://169.254.169.254/.well-known/openid-configuration",
        )
        with (
            patch("modulo.auth.sso.get_provider_by_provider_id", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.validate_outbound_url_async", new_callable=AsyncMock) as v,
        ):
            m.return_value = db_provider
            v.side_effect = ValueError("blocked by SSRF guard")
            with pytest.raises(ValueError, match="Rejected OIDC discovery_url"):
                await _resolve_oidc_provider("auth0", session, settings)


# ---------------------------------------------------------------------------
# OIDC authorize url error branches
# ---------------------------------------------------------------------------


class TestOidcAuthorizeErrors:
    async def test_raises_when_discovery_url_missing(self) -> None:
        settings = _override()
        session = _mock_session()
        db_provider = SimpleNamespace(
            provider_type="oidc",
            enabled=True,
            client_id="db-client-id",
            client_secret=None,
            discovery_url=None,
            scopes=None,
        )
        with patch("modulo.auth.sso.get_provider_by_provider_id", new_callable=AsyncMock) as m:
            m.return_value = db_provider
            with pytest.raises(ValueError, match="missing client_id or discovery_url"):
                await oidc_get_authorize_url("auth0", settings, "http://localhost/cb", session)

    async def test_raises_when_discovery_fetch_fails(self) -> None:
        settings = _override()
        session = _mock_session()
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as m:
            import httpx

            m.side_effect = httpx.HTTPError("boom")
            with pytest.raises(ValueError, match="Failed to fetch discovery document"):
                await oidc_get_authorize_url("google", settings, "http://localhost/cb", session)


# ---------------------------------------------------------------------------
# OIDC callback error branches
# ---------------------------------------------------------------------------


class TestOidcCallbackErrors:
    async def test_raises_when_client_secret_missing(self) -> None:
        settings = _override()
        session = _mock_session()
        db_provider = SimpleNamespace(
            provider_type="oidc",
            enabled=True,
            client_id="db-client-id",
            client_secret=None,
            discovery_url="https://example.auth0.com/.well-known/openid-configuration",
            scopes=None,
        )
        signed = sign_state("auth0:raw", settings.secret_key)
        with (
            patch("modulo.auth.sso.get_provider_by_provider_id", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.validate_outbound_url_async", new_callable=AsyncMock) as v,
        ):
            m.return_value = db_provider
            v.return_value = None
            with pytest.raises(ValueError, match="missing required configuration"):
                await oidc_process_callback("code", signed, settings, session, "http://localhost/cb")

    async def test_raises_when_no_token_endpoint(self) -> None:
        settings = _override()
        session = _mock_session()
        signed = sign_state("google:raw", settings.secret_key)
        with patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as m:
            m.return_value = {"jwks_uri": "https://x/certs", "issuer": "https://x"}
            with pytest.raises(ValueError, match="No token_endpoint"):
                await oidc_process_callback("code", signed, settings, session, "http://localhost/cb")

    async def test_raises_when_id_token_missing(self) -> None:
        settings = _override()
        session = _mock_session()
        signed = sign_state("google:raw", settings.secret_key)
        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as d,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as e,
        ):
            d.return_value = {"token_endpoint": "https://x/token", "jwks_uri": "https://x/certs", "issuer": "https://x"}
            e.return_value = {}
            with pytest.raises(ValueError, match="missing a valid id_token"):
                await oidc_process_callback("code", signed, settings, session, "http://localhost/cb")

    async def test_raises_when_jwks_or_issuer_missing(self) -> None:
        settings = _override()
        session = _mock_session()
        signed = sign_state("google:raw", settings.secret_key)
        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as d,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as e,
        ):
            d.return_value = {"token_endpoint": "https://x/token"}
            e.return_value = {"id_token": _decoded_id_token()}
            with pytest.raises(ValueError, match="jwks_uri or issuer"):
                await oidc_process_callback("code", signed, settings, session, "http://localhost/cb")

    async def test_raises_when_email_missing(self) -> None:
        settings = _override()
        session = _mock_session()
        signed = sign_state("google:raw", settings.secret_key)
        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as d,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as e,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as v,
        ):
            d.return_value = {"token_endpoint": "https://x/token", "jwks_uri": "https://x/certs", "issuer": "https://x"}
            e.return_value = {"id_token": _decoded_id_token()}
            v.return_value = {}
            with pytest.raises(ValueError, match="email or sub claim"):
                await oidc_process_callback("code", signed, settings, session, "http://localhost/cb")

    async def test_groups_not_list_is_ignored(self) -> None:
        settings = _override()
        session = _mock_session()
        signed = sign_state("google:raw", settings.secret_key)
        with (
            patch("modulo.auth.sso._fetch_discovery", new_callable=AsyncMock) as d,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as e,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as v,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as tok,
        ):
            d.return_value = {"token_endpoint": "https://x/token", "jwks_uri": "https://x/certs", "issuer": "https://x"}
            e.return_value = {"id_token": _decoded_id_token()}
            v.return_value = {"email": "user@example.com", "name": "Test", "sub": "abc", "groups": "not-a-list"}
            jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}
            result = await oidc_process_callback("code", signed, settings, session, "http://localhost/cb")
            assert result["access_token"] == "at"

    async def test_applies_group_mappings_from_db_provider(self) -> None:
        settings = _override()
        org_id = uuid.uuid4()
        team_id = uuid.uuid4()
        db_provider = SimpleNamespace(
            provider_type="oidc",
            enabled=True,
            client_id="db-client-id",
            client_secret="secret",
            discovery_url="https://example.auth0.com/.well-known/openid-configuration",
            scopes=None,
            organisation_id=org_id,
            group_mappings=[{"idp_group": "team-a", "team_id": str(team_id), "team_role": "viewer"}],
        )
        session = _mock_session(scalar=db_provider)
        signed = sign_state("auth0:raw", settings.secret_key)
        with (
            patch("modulo.auth.sso._fetch_discovery_pinned", new_callable=AsyncMock) as d,
            patch("modulo.auth.sso._exchange_code", new_callable=AsyncMock) as e,
            patch("modulo.auth.sso.verify_id_token", new_callable=AsyncMock) as v,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as tok,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as gm,
            patch("modulo.auth.sso.decode_stored_secret", return_value="secret") as dec,
            patch("modulo.auth.sso.validate_outbound_url_async", new_callable=AsyncMock),
        ):
            d.return_value = {
                "token_endpoint": "https://x/token",
                "jwks_uri": "https://x/certs",
                "issuer": "https://auth0",
            }
            e.return_value = {"id_token": _decoded_id_token()}
            v.return_value = {"email": "user@example.com", "name": "Test", "sub": "abc", "groups": ["team-a"]}
            jit.return_value = (MagicMock(), org_id, "runner")
            tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}
            await oidc_process_callback("code", signed, settings, session, "http://localhost/cb")
            gm.assert_awaited_once()
            dec.assert_called()


# ---------------------------------------------------------------------------
# Pinned discovery / exchange json errors + exchange json decode
# ---------------------------------------------------------------------------


class TestDiscoveryAndExchangeErrors:
    async def test_fetch_discovery_pinned_json_error(self) -> None:
        from modulo.auth.sso import _fetch_discovery_pinned

        response = MagicMock()
        response.json.side_effect = json.JSONDecodeError("bad", "x", 0)
        client = AsyncMock()
        client.get.return_value = response
        with patch("modulo.auth.sso.pinned_async_client") as pc:
            pc.return_value.__aenter__.return_value = client
            with pytest.raises(ValueError, match="Invalid JSON in discovery document"):
                await _fetch_discovery_pinned("https://issuer.example/discovery")

    async def test_exchange_code_json_error(self) -> None:
        from modulo.auth.sso import _exchange_code

        response = MagicMock()
        response.json.side_effect = json.JSONDecodeError("bad", "x", 0)
        client = AsyncMock()
        client.post.return_value = response
        with patch("modulo.auth.sso.httpx.AsyncClient") as pc:
            pc.return_value.__aenter__.return_value = client
            with pytest.raises(ValueError, match="Invalid JSON in OIDC token response"):
                await _exchange_code("https://x/token", "c", "s", "code", "cb")


# ---------------------------------------------------------------------------
# SAML metadata_url fetch + error branches
# ---------------------------------------------------------------------------

SAMPLE_SAML_METADATA = """<?xml version="1.0"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                     entityID="https://idp.example.com">
  <md:IDPSSODescriptor
   protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
    <md:SingleSignOnService
     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
     Location="https://idp.example.com/sso"/>
  </md:IDPSSODescriptor>
</md:EntityDescriptor>"""


class TestSamlMetadataUrlFetch:
    async def test_fetches_metadata_from_url(self) -> None:
        from modulo.auth.sso import _resolve_saml_config

        settings = _override()
        provider = SimpleNamespace(
            metadata_xml=None,
            metadata_url="https://idp.example.com/metadata",
            entity_id="https://idp.example.com",
        )
        session = _mock_session()
        response = MagicMock()
        response.text = SAMPLE_SAML_METADATA
        client = AsyncMock()
        client.get.return_value = response
        with (
            patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.validate_outbound_url_async", new_callable=AsyncMock) as v,
            patch("modulo.auth.sso.pinned_async_client") as pc,
        ):
            m.return_value = provider
            v.return_value = None
            pc.return_value.__aenter__.return_value = client
            meta, entity_id, _sp_key, _sp_cert, db = await _resolve_saml_config(session, settings)
            assert db is provider
            assert "SingleSignOnService" in meta
            assert entity_id == "https://idp.example.com"

    async def test_raises_when_metadata_url_blocked(self) -> None:
        from modulo.auth.sso import _resolve_saml_config

        settings = _override()
        provider = SimpleNamespace(
            metadata_xml=None,
            metadata_url="http://169.254.169.254/metadata",
            entity_id="https://idp.example.com",
        )
        session = _mock_session()
        with (
            patch("modulo.auth.sso.get_enabled_saml_provider", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.validate_outbound_url_async", new_callable=AsyncMock) as v,
        ):
            m.return_value = provider
            v.side_effect = ValueError("blocked")
            with pytest.raises(ValueError, match="Rejected SAML metadata_url"):
                await _resolve_saml_config(session, settings)


class TestSamlAuthUrlError:
    async def test_raises_when_handler_fails(self) -> None:
        settings = _override(modulo_saml_enabled=False, modulo_license_key="")
        provider = SimpleNamespace(
            metadata_xml=SAMPLE_SAML_METADATA,
            metadata_url=None,
            entity_id="https://idp.example.com",
        )
        session = _mock_session(scalar=provider)
        with patch("modulo.auth.sso.ModuloSamlAuth") as handler_cls:
            handler_cls.return_value.get_auth_url.side_effect = RuntimeError("bad xml")
            with pytest.raises(ValueError, match="Failed to generate SAML AuthnRequest"):
                await saml_get_auth_url(settings, "https://modulo.example.com/acs", session)


class TestSamlDestinationParseError:
    def test_skips_garbled_xml(self) -> None:
        assert _validate_saml_response_destination("!!not-base64!!", "https://app.example.com/acs") is None


class TestSamlProcessResponseErrors:
    SAML_RESPONSE_XML = """<?xml version="1.0" encoding="UTF-8"?>
    <samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol"
     xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion">
      <saml:Assertion ID="_abc123" IssueInstant="2024-01-01T00:00:00Z">
        <saml:Subject>
          <saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">
            user@example.com
          </saml:NameID>
        </saml:Subject>
        <saml:AttributeStatement>
          <saml:Attribute Name="email">
            <saml:AttributeValue>user@example.com</saml:AttributeValue>
          </saml:Attribute>
        </saml:AttributeStatement>
      </saml:Assertion>
    </samlp:Response>"""

    async def test_raises_when_idp_metadata_unparseable(self) -> None:
        settings = _override(modulo_license_key="lic-123", modulo_saml_enabled=True)
        session = _mock_session()
        with patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m:
            m.return_value = "<root>not-metadata</root>"
            encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
            with pytest.raises(ValueError, match="Failed to parse IdP metadata"):
                await saml_process_response(encoded, settings, session)

    async def test_raises_when_email_missing(self) -> None:
        from modulo.auth.saml_handler import ModuloSamlAuth

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=SAMPLE_SAML_METADATA,
        )
        session = _mock_session()
        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as jit,
            patch.object(
                ModuloSamlAuth,
                "process_response",
                return_value={"name_id": "", "attributes": {}},
            ),
        ):
            m.return_value = SAMPLE_SAML_METADATA
            jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            with pytest.raises(ValueError, match="did not return an email"):
                await saml_process_response(encoded, settings, session)

    async def test_propagates_jit_runtime_error(self) -> None:
        from modulo.auth.saml_handler import ModuloSamlAuth

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=SAMPLE_SAML_METADATA,
        )
        session = _mock_session()
        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as jit,
            patch.object(
                ModuloSamlAuth,
                "process_response",
                return_value={
                    "name_id": "user@example.com",
                    "attributes": {"email": ["user@example.com"]},
                },
            ),
        ):
            m.return_value = SAMPLE_SAML_METADATA
            jit.side_effect = RuntimeError("No organisation exists")
            with pytest.raises(ValueError, match="No organisation exists"):
                await saml_process_response(encoded, settings, session)

    async def test_applies_saml_group_mappings(self) -> None:
        from modulo.auth.saml_handler import ModuloSamlAuth

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=SAMPLE_SAML_METADATA,
        )
        org_id = uuid.uuid4()
        team_id = uuid.uuid4()
        db_saml = SimpleNamespace(
            metadata_xml=SAMPLE_SAML_METADATA,
            metadata_url=None,
            entity_id="https://idp.example.com",
            organisation_id=org_id,
            group_mappings=[{"idp_group": "team-a", "team_id": str(team_id), "team_role": "viewer"}],
        )
        session = _mock_session(scalar=db_saml)
        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as tok,
            patch("modulo.auth.sso._lookup_provider_by_entity_id", new_callable=AsyncMock) as lp,
            patch("modulo.auth.sso.apply_group_mappings", new_callable=AsyncMock) as gm,
            patch.object(
                ModuloSamlAuth,
                "process_response",
                return_value={
                    "name_id": "user@example.com",
                    "attributes": {"email": ["user@example.com"], "groups": ["team-a"]},
                },
            ),
        ):
            m.return_value = SAMPLE_SAML_METADATA
            jit.return_value = (MagicMock(), org_id, "runner")
            tok.return_value = {"access_token": "at", "refresh_token": "rt", "token_type": "bearer"}
            lp.return_value = db_saml
            await saml_process_response(encoded, settings, session)
            gm.assert_awaited_once()

    async def test_propagates_issue_tokens_runtime_error(self) -> None:
        from modulo.auth.saml_handler import ModuloSamlAuth

        settings = _override(
            modulo_license_key="lic-123",
            modulo_saml_enabled=True,
            modulo_saml_idp_metadata_xml=SAMPLE_SAML_METADATA,
        )
        session = _mock_session()
        encoded = base64.b64encode(self.SAML_RESPONSE_XML.encode()).decode()
        with (
            patch("modulo.auth.sso._saml_fetch_idp_metadata", new_callable=AsyncMock) as m,
            patch("modulo.auth.sso.jit_provision_user", new_callable=AsyncMock) as jit,
            patch("modulo.auth.sso.issue_sso_tokens", new_callable=AsyncMock) as tok,
            patch.object(
                ModuloSamlAuth,
                "process_response",
                return_value={
                    "name_id": "user@example.com",
                    "attributes": {"email": ["user@example.com"]},
                },
            ),
        ):
            m.return_value = SAMPLE_SAML_METADATA
            jit.return_value = (MagicMock(), uuid.uuid4(), "runner")
            tok.side_effect = RuntimeError("No organisation exists")
            with pytest.raises(ValueError, match="No organisation exists"):
                await saml_process_response(encoded, settings, session)


class TestSamlParseNoSsoUrl:
    def test_raises_when_no_sso_location(self) -> None:
        xml = """<?xml version="1.0"?>
        <md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"
                             entityID="https://idp.example.com">
          <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
          </md:IDPSSODescriptor>
        </md:EntityDescriptor>"""
        with pytest.raises(ValueError, match="No SAML SingleSignOnService"):
            _saml_parse_idp_metadata(xml)
