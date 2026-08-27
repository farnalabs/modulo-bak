---
id: feat-observability
prd: N/A
adr: []
code:
  - backend/src/modulo/api/routes/observability.py
  - backend/src/modulo/api/routes/errors.py
  - backend/src/modulo/core/error_tracking
  - backend/src/modulo/otel_bridge
unit-tests:
  - backend/tests/unit/error_tracking/test_error_ingestion.py
  - backend/tests/unit/error_tracking/test_error_dashboard.py
  - backend/tests/unit/error_tracking/test_error_alerting.py
  - backend/tests/unit/error_tracking/test_error_metrics.py
  - backend/tests/unit/error_tracking/test_forwarders.py
  - backend/tests/unit/error_tracking/test_saq_hooks.py
  - backend/tests/unit/error_tracking/test_alert_dispatcher.py
bdd:
  - backend/tests/bdd/features/observability/metrics.feature
  - backend/tests/bdd/features/observability/error_forwarders.feature
  - backend/tests/bdd/features/observability/otel_traces.feature
  - backend/tests/bdd/features/observability/monitor_config.feature
  - backend/tests/bdd/features/errors/failed_state.feature
  - backend/tests/bdd/features/errors/retry.feature
depends-on:
  - feat-runs
status: covered
---

# Observability

Error dashboard, monitoring and observability exports on `/settings/observability`,
`/settings/monitoring`, `/settings/error-forwarders`, and the `/admin/errors` surface.
Runs ingest structured errors, breadcrumbs and trace ids; alerts dispatch via
cooldown/fingerprint dedup; error metrics and forwarders export the stream; and SAQ
worker hooks keep background-job failures visible. OTel settings (endpoint/token) are
configured per organisation and wired into the LangGraph→OTel bridge.

## Behaviours

- [x] Structured run errors are ingested with breadcrumbs, trace ids and masking
      (`core/error_tracking/__init__.py`, `tests/unit/error_tracking/test_error_ingestion.py`)
- [x] The error dashboard lists/filters errors with status transitions (failed state,
      retry/recovery) (`api/routes/errors.py`, `tests/bdd/features/errors/*`)
- [x] Error alerts deduplicate by fingerprint with a cooldown and dispatch to
      channels (Slack/webhook) (`core/error_tracking/alerting.py`, `alert_dispatcher.py`,
      `test_error_alerting.py`, `test_alert_dispatcher.py`)
- [x] Error forwarders export the error stream to external sinks
      (`core/error_tracking/forwarders/`, `test_forwarders.py`,
      `tests/bdd/features/observability/error_forwarders.feature`)
- [x] Per-organisation OTel observability settings (endpoint, token, sampling) are
      configured on `/settings/observability` (`api/routes/observability.py`)
- [x] SAQ worker hooks record backend failures/metrics into the error tracking surface
      (`core/error_tracking/saq_hooks.py`, `test_saq_hooks.py`)
- [x] OTel metrics and LangGraph→OTel trace bridging emit observability telemetry
      (`core/error_tracking/metrics.py`, `otel_bridge/`,
      `tests/bdd/features/observability/metrics.feature`, `otel_traces.feature`)

## Known Gaps

- The observability surface spans several settings/admin routes; no single consolidated
  "observability home" aggregates them beyond the sidebar monitor group.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-observability`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/observability.py`,
  `api/routes/errors.py`, `core/error_tracking/*`, `otel_bridge/` and the
  error-tracking/observability unit+BDD suites. Status: covered.