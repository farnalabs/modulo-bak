---
id: feat-auth-sso-provider-ui
prd: 9.4
delivery-tasks: []
code:
  - frontend/src/views/SettingsSsoView.vue
  - frontend/src/components/SsoProviderForm.vue
  - frontend/src/views/LoginView.vue
  - frontend/src/router/index.ts
  - frontend/src/config/navigation.ts
  - backend/src/modulo/api/routes/admin_sso.py
  - backend/src/modulo/api/routes/sso.py
  - backend/src/modulo/auth/sso.py
  - backend/src/modulo/db/crud/sso_provider.py
  - backend/src/modulo/db/models/sso_provider.py
unit-tests:
  - backend/tests/unit/api/test_admin_sso.py
  - backend/tests/unit/api/test_sso_gating.py
  - backend/tests/unit/api/test_error_handling.py
  - backend/tests/unit/auth/test_sso.py
  - frontend/src/__tests__/LoginView.spec.ts
bdd:
  - backend/tests/bdd/features/auth/sso_oidc.feature
  - backend/tests/bdd/features/auth/sso_saml.feature
  - backend/tests/bdd/features/auth/sso_team_mapping.feature
depends-on:
  - feat-core-oidc-integration
  - feat-core-saml-integration
  - feat-teams-org-entity
status: covered
---

# SSO Provider UI

Admin settings page (`/settings/sso`, `settings-sso`) for configuring OIDC and SAML 2.0
identity providers. Team-gated (§9.4): the route is `required_tier: team` +
`required_roles: [admin]` in `frontend/src/manifest.yaml`. Referenced by the
incident-response playbook as the prevention control for IdP-initiated SSO validation
(`docs/security/incident-response-playbook.md`).

## Behaviours

- [x] Admin can view the list of all configured SSO providers with type badges (O / S)
- [x] Admin can add an OIDC provider (client ID, client secret, discovery URL, scopes)
- [x] Admin can add a SAML 2.0 provider (metadata URL, metadata XML, entity ID)
- [x] Provider form shows conditional fields based on selected type (OIDC vs SAML)
- [x] Common fields per provider: name, auto-provision toggle, default role
      (operator/runner), group-to-team mappings
- [x] Admin can edit, enable/disable, and delete an SSO provider (confirmation dialog)
- [x] Admin can test an SSO provider connection — OIDC resolves the discovery URL,
      SAML parses the metadata XML
- [x] Adds/edits/deletes/toggles raise audit events
- [x] SSO provider management is admin-only (403 for non-admin)
- [x] Duplicate provider name → 409 (with FOR UPDATE lock); empty update body → 400;
      invalid provider type/default role → 422; provider not found → 404
- [x] ProgrammingError → 501 and SQLAlchemyError → 503 on the admin CRUD routes
- [x] OIDC callback exchanges the code, verifies HMAC-signed state (CSRF), and issues a
      JWT pair; SAML ACS parses `SAMLResponse`, validates the assertion, and issues JWTs
- [x] JIT provisioning creates the user with the provider's default role; group
      mappings apply at provisioning time
- [x] Configured OIDC providers appear on the login page as buttons that redirect to
      `/api/v1/auth/oidc/{provider}/login`; a SAML button appears when SAML is enabled;
      the section hides when SSO is feature-gated or no provider is configured

## Known Gaps

- **Sidebar entry tier-gated but not SSO-skill-gated** — the nav entry hides for
  community (team tier required) but does not re-check the `sso` license key; the page
  renders a locked prompt via `FeatureGate show-disabled`.
- **No BDD scenarios for admin provider CRUD** — BDD covers the auth flows only; admin CRUD
  is unit-tested.
- **Delete-provider confirmation does not warn about active SSO sessions** — the dialog
  states only "This action cannot be undone".

## QA History

- 2026-08-26: **improve-architecture (product-map walk)** — closed the login-page provider
  gap: `LoginView.vue` now fetches `/api/v1/auth/sso/providers` on mount and renders one
  OIDC button per configured provider plus a SAML button when SAML is enabled, each
  redirecting to the existing SSO login endpoints. Covered by `LoginView.spec.ts`
  (per-provider buttons, SAML button, feature-gated/empty hide, OIDC + SAML redirects).
  Status: covered.
- 2026-08-25: **improve-architecture (product-map walk)** — restored this entry as part of
  rebuilding the `docs/product-map/` feature graph. The file is explicitly referenced by
  `docs/security/incident-response-playbook.md` as the SSO validation prevention control.
  Re-verified behaviours and file paths against the current tree (admin_sso.py model
  fields, sso.py endpoints, manifest route gating). Status was partial — login-page provider
  buttons were the acknowledged gap.