---
id: feat-guardrails
prd: 7.4
adr: []
code:
  - backend/src/modulo/core/guardrails
  - backend/src/modulo/api/routes/guardrail_config.py
unit-tests:
  - backend/tests/unit/test_guardrail_compensation.py
  - backend/tests/unit/test_guardrail_config.py
  - backend/tests/unit/test_guardrail_correction.py
  - backend/tests/unit/test_guardrail_correction_e2e.py
  - backend/tests/unit/test_guardrail_loop_intercept.py
  - backend/tests/unit/test_guardrail_policy_pack.py
  - backend/tests/unit/test_guardrail_soc2_pack.py
  - backend/tests/unit/test_guardrails_kill_switch.py
bdd:
  - backend/tests/bdd/features/evals/guardrails.feature
  - backend/tests/bdd/features/evals/guardrail_config.feature
depends-on:
  - feat-runs
  - feat-pipelines
status: covered
---

# Guardrails

Guardrail policies that constrain and correct agentic pipeline behaviour on
`/settings/guardrails`. Guardrails wrap the output surface with conformance checks,
compensation on failure, mid-run correction, and a loop-interceptor that aborts
runaway agent loops; policies bundle into policy packs (including a SOC2 pack).

## Behaviours

- [x] Guardrail config is creatable/editable and stored as a policy on
      `/settings/guardrails` (`api/routes/guardrail_config.py`,
      `tests/unit/test_guardrail_config.py`)
- [x] Guardrail policies enforce output conformance and can compensate/finalize a run
      when a constraint is breached (`core/guardrails/compensation.py`,
      `tests/unit/test_guardrail_compensation.py`)
- [x] Mid-run correction can steer an off-policy output back to compliance
      (`core/guardrails/correction.py`, `test_guardrail_correction*.py`)
- [x] A loop-intercept guardrail terminates runaway agent loops (`core/guardrails/loop_intercept.py`,
      `tests/unit/test_guardrail_loop_intercept.py`)
- [x] Policies compose into policy packs, including a SOC2 pack
      (`core/guardrails/policy_pack.py`, `packs/`, `test_guardrail_policy_pack.py`,
      `test_guardrail_soc2_pack.py`)
- [x] A kill switch disables guardrail enforcement without deleting configuration
      (`core/guardrails/config.py`, `tests/unit/test_guardrails_kill_switch.py`)

## Known Gaps

- Guardrail policy packs ship with built-in (SOC2) packs; a user-facing pack builder
  is not a shipped surface.

## QA History

- 2026-08-27: **improve-architecture (product-map walk)** — added this behaviour-tracker
  for the registered manifest feature `feat-guardrails`, which previously had no
  `docs/product-map/` entry. Behaviours verified against `core/guardrails/*`,
  `api/routes/guardrail_config.py`, the guardrail unit suite and the
  `guardrails(.config)`.feature BDD files. Status: covered.
