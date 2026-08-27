---
id: feat-mcp
prd: N/A
adr: []
code:
  - backend/src/modulo/api/mcp_server.py
  - backend/src/modulo/api/mcp_tool_registry.py
  - backend/src/modulo/core/mcp/scope_validator.py
  - backend/src/modulo/api/routes/mcp_oauth.py
  - backend/src/modulo/api/routes/mcp_setup.py
unit-tests:
  - backend/tests/unit/mcp/test_scope_validator.py
  - backend/tests/unit/mcp/test_mcp_config_tools.py
  - backend/tests/unit/mcp/test_mcp_run_tools.py
  - backend/tests/unit/mcp/test_trigger_crud_tools.py
  - backend/tests/unit/mcp/test_team_scope_enforcement.py
  - backend/tests/unit/mcp/test_team_binding_enforcement.py
  - backend/tests/unit/test_mcp_security.py
bdd:
  - backend/tests/bdd/features/mcp/onboarding.feature
  - backend/tests/bdd/features/mcp/mcp_oauth.feature
  - backend/tests/bdd/steps/test_mcp_oauth.py
depends-on:
  - feat-auth
  - feat-pipelines
status: partial
---

# Model Context Protocol (MCP) Tool Configuration

The `/mcp` endpoint (`/settings/mcp` for configuration) exposes Modulo as a Model Context
Protocol server: a FastMCP/StreamableHTTP JSON-RPC server mounted under `/mcp` with an API-key
bearer auth middleware and dual-layer enforcement (middleware key validation + tool-layer
role checks). `core/mcp/scope_validator.py` gates which tools an API key can invoke via
request-scoped allow-lists, and the tool registry (`api/mcp_tool_registry.py`) derives
OpenAI-compatible tool definitions straight from the FastMCP registry so `tools/list`
onboarding stays accurate.

## Behaviours

- [x] MCP server is mounted at `/mcp` and serves `tools/list` (tool definitions with
      description + inputSchema), including `trigger`, `review_hitl`, `library_browse`,
      `human_only`; onboarding without an API key still lists tools but invoking any tool
      is rejected (401); SSE transport is supported (`onboarding.feature`)
- [x] API-key auth: bearer `mk_*` key validated by `McpAuthMiddleware`; org_id and role
      carried in request context; per-event org validation for streaming connections
      (`api/mcp_server.py`, `test_mcp_security.py`)
- [x] Tool-level scope enforcement: `check_tool_scope` and the request allowed-tools
      allow-list restrict a key to its granted scopes (e.g. `trigger:run` without
      `hitl:review` is forbidden) (`core/mcp/scope_validator.py`,
      `test_scope_validator.py`)
- [x] MCP OAuth onboarding handoff is exercised (`mcp_oauth.feature`,
      `test_mcp_oauth.py`, `api/routes/mcp_oauth.py`)
- [x] The scoped tool surface (run tools, config tools, trigger CRUD, schema tools,
      library tools, team scope/binding enforcement) is unit-covered
      (`tests/unit/mcp/test_*tools*.py`, `test_team_*_enforcement.py`)

## Known Gaps

- **`mcp/trigger.feature`, `mcp/review_hitl.feature`, `mcp/human_only.feature`,
  `mcp/library_browse.feature` are `@awaiting-implementation`** — their scenarios drive the
  removed legacy `/mcp/tools/call` HTTP surface. The shipped server speaks JSON-RPC over
  StreamableHTTP (POST `/mcp`), so those BDD suites need re-authoring against the current
  protocol; the underlying behaviour is unit-tested (trigger/run/schema/library tools).
- **HITL approval via MCP (`review_hitl`, human-only gates)** depends on the re-staged
  tool surface above; until then the MCP assistant cannot approve gates.
- BDD onboarding is the only MCP feature file bound to an implemented step suite; the
  rest are tracked here as partial rather than covered.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-mcp`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/mcp_server.py`,
  `api/mcp_tool_registry.py`, `core/mcp/scope_validator.py` and the mcp unit suites; the
  stale legacy-tool-call BDD features are recorded as the known gap rather than claimed
  covered. Status: partial.