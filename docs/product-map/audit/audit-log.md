---
id: feat-audit
prd: 9.3
adr: []
code:
  - backend/src/modulo/api/routes/audit.py
  - backend/src/modulo/core/audit_logger/append_only.py
unit-tests:
  - backend/tests/unit/audit_logger/test_audit_logger.py
  - backend/tests/unit/audit_logger/test_append_only.py
bdd:
  - backend/tests/bdd/features/audit/audit_viewer.feature
  - backend/tests/bdd/features/audit/event_recording.feature
depends-on:
  - feat-teams
  - feat-auth
status: covered
---

# Audit Trail

Append-only audit trail over security-relevant and system events, surfaced to
organisation admins on `/admin/audit`. Events are recorded with actor, action,
resource and tenant context and are replayable as an immutable, append-only log.

## Behaviours

- [x] Audit events are recorded for security-relevant operations with actor, action,
      resource and tenant/organisation context
- [x] The log is append-only — existing records can never be mutated or deleted
      (`core/audit_logger/append_only.py`, immutability asserted by
      `tests/integration/test_audit_immutability.py`)
- [x] The `/admin/audit` viewer lists, filters and paginates recorded events for the
      calling organisation (`tests/bdd/features/audit/audit_viewer.feature`)
- [x] Event recording covers the audited operations on the shipped routes
      (`tests/bdd/features/audit/event_recording.feature`)
- [x] Row-level security keeps audit records isolated per organisation; cross-tenant
      reads of another organisation's audit history are rejected
      (`tests/integration/test_audit_append_only.py`, `test_api_key_audit_rls.py`)

## Known Gaps

- The audit viewer is scoped to the current organisation; cross-organisation/system
  audit aggregation is not a shipped surface.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-audit`, which previously had no
  `docs/product-map/` entry (invisible to the feature graph and to Remy's indexer).
  Behaviours verified against `api/routes/audit.py`, `core/audit_logger/append_only.py`,
  the audit unit/BDD suites and the immutable/RSL integration tests. Status: covered.
