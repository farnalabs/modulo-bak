---
id: feat-hitl
prd: 7.3
adr: []
code:
  - backend/src/modulo/api/routes/hitl.py
  - backend/src/modulo/core/hitl_manager
unit-tests:
  - backend/tests/unit/hitl_manager/test_hitl_manager.py
  - backend/tests/unit/hitl_manager/test_claim_expiry_job.py
  - backend/tests/unit/hitl_manager/test_overdue_warning.py
  - backend/tests/unit/hitl_manager/test_output_delivery_audit.py
bdd:
  - backend/tests/bdd/features/hitl/approve.feature
  - backend/tests/bdd/features/hitl/claim.feature
  - backend/tests/bdd/features/hitl/reject.feature
  - backend/tests/bdd/features/hitl/approval_gate.feature
  - backend/tests/bdd/features/hitl/overdue_warning.feature
  - backend/tests/bdd/features/hitl/modify_then_approve.feature
depends-on:
  - feat-core-run-context
  - feat-runs
status: covered
---

# Human-in-the-loop (HITL)

Human-in-the-loop approval gates and review on `/settings/hitl-review`. Runs that reach
an approval gate pause for a human reviewer to claim, review, approve/reject (optionally
after editing output). Overdue HITL items escalate via an overdue-warning job, and manual
approval can be delegated to a designated human-only node.

## Behaviours

- [x] A run pausing at an approval gate blocks until a reviewer claims it
      (`tests/bdd/features/hitl/claim.feature`)
- [x] A reviewer approves or rejects the gated output, driving the run forward or to
      rejection (`approve.feature`, `reject.feature`)
- [x] A reviewer may modify output before approving (`modify_then_approve.feature`),
      and approval gate semantics gate the transition (`approval_gate.feature`)
- [x] Overdue/unclaimed HITL items raise an overdue warning; claimed items expire back
      to the pool via the claim-expiry job (`core/hitl_manager/overdue_warning.py`,
      `expiry_job.py`, `tests/unit/hitl_manager/*`)
- [x] Manual-delivery nodes and output-delivery of approved content is audited
      (`core/hitl_manager`, `tests/unit/hitl_manager/test_output_delivery_audit.py`)
- [x] The review surface is exposed on `/settings/hitl-review` (`api/routes/hitl.py`)

## Known Gaps

- HITL gate behaviour is enforced at the pipeline/gate layer; a storage-level hard
  prohibition on mutating approved output is not separately modelled.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-hitl`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `api/routes/hitl.py`,
  `core/hitl_manager/*` and the hitl unit/BDD suites. Status: covered.
