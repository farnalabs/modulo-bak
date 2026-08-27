---
id: feat-triggers
prd: 6.1
adr: []
code:
  - backend/src/modulo/api/routes/triggers.py
  - backend/src/modulo/api/routes/webhooks.py
  - backend/src/modulo/core/trigger_engine
  - backend/src/modulo/api/routes/slack.py
unit-tests:
  - backend/tests/unit/trigger_engine/test_trigger_engine.py
  - backend/tests/unit/trigger_engine/test_agent_signal.py
  - backend/tests/unit/trigger_engine/test_polling.py
  - backend/tests/unit/trigger_engine/test_polling_connector_drift.py
  - backend/tests/unit/trigger_engine/test_polling_shared_redis.py
  - backend/tests/unit/trigger_engine/test_slack_app_mention.py
bdd:
  - backend/tests/bdd/features/triggers/manual.feature
  - backend/tests/bdd/features/triggers/cron.feature
  - backend/tests/bdd/features/triggers/webhook_hmac.feature
  - backend/tests/bdd/features/triggers/webhook_payload_mapping.feature
  - backend/tests/bdd/features/triggers/polling.feature
  - backend/tests/bdd/features/triggers/agent_signal.feature
  - backend/tests/bdd/features/triggers/pause.feature
  - backend/tests/bdd/features/triggers/flood_protection.feature
depends-on:
  - feat-runs
  - feat-pipelines
status: covered
---

# Triggers

Manual, webhook (HMAC-authenticated), scheduled/cron, polling, Slack app-mention and
agent-signal triggers that start pipeline runs on `/settings/triggers`. Each trigger
maps an incoming event into a run input payload; a pause switch and flood protection
guard the ingestion edge.

## Behaviours

- [x] Manual triggers start a run directly from the UI
      (`tests/bdd/features/triggers/manual.feature`)
- [x] Scheduled/cron triggers fire on a cadence via the trigger engine and the system
      cron watchdog (`tests/bdd/features/triggers/cron.feature`)
- [x] Webhook triggers authenticate callers by HMAC signature and map the incoming
      JSON payload to run input (`webhook_hmac.feature`, `webhook_payload_mapping.feature`)
- [x] Polling triggers converge a declared endpoint into run input, handling connector
      drift and optional shared-Redis polling (`core/trigger_engine/polling.py`,
      `tests/unit/trigger_engine/test_polling*.py`)
- [x] Agent-signal triggers start runs from agent signals
      (`core/trigger_engine/agent_signal.py`, `agent_signal.feature`)
- [x] Slack app-mention triggers respond to mentions (`core/trigger_engine/slack_app_mention.py`,
      `test_slack_app_mention.py`)
- [x] A trigger pause switch stops fire-due delivery without deleting the trigger
      (`tests/bdd/features/triggers/pause.feature`)
- [x] Webhook flood protection caps requests per window before runs are dispatched
      (`tests/bdd/features/triggers/flood_protection.feature`,
      `backend/tests/integration/test_webhook_flood_protection.py`)

## Known Gaps

- Trigger event-log surface and polling pre-guardrail interception are covered by
  separate suites; no single consolidated "trigger catalogue" UI aggregates every
  trigger kind.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-triggers`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/triggers.py`,
  `core/trigger_engine/*` and the trigger unit/BDD suites. Status: covered.
