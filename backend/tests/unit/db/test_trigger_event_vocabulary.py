"""Vocabulary/constraint tests for the ``auto_deactivated`` widening.

The migration chain was squashed into three idempotent reconciliation
migrations (``0108_schema_org_identity`` / ``0109_schema_teams_library`` /
``0110_schema_pipeline_runtime``). The per-feature migrations that used to carry
this surface (``0104_trigger_event_auto_deactivated`` head ``0105``, plus main's
``0106`` adding ``guardrail_blocked``) no longer exist;
``0110_schema_pipeline_runtime`` now owns the ``ck_trigger_events_validation_result``
constraint with the FULL 21-value vocabulary. This file asserts:

* the model vocabulary (``VALIDATION_RESULT_VALUES``) contains
  ``auto_deactivated`` and the ORM CHECK constraint reflects it,
* the reconciliation migration's hardcoded vocabulary stays in sync with the
  model (the single source of truth) — a value added to one side and not the
  other breaks the constraint/model contract,
* the chain has a single linear head ``0110_schema_pipeline_runtime`` (the
  FAR-213 ``0111_run_blocked_partial_summary``, FAR-210
  ``0112_feedback_correction_state``, FAR-223
  ``0113_guardrail_summary``, FAR-296 ``0114_org_api_keys_run_id``,
  FAR-247 ``0115_notification_preferences``, FAR-309
  ``0116_guardrail_trust_pr_b``, TOCTOU ``0117_toctou_hardening``,
  batch-scoped variants ``0118_batch_scoped_variants``,
  ``0119_analytics_batch_id``, and org-FK hardening ``0120_org_fk_hardening``
  migrations chain on top of it).

The old SQLite round-trip (which ran the migration's upgrade/downgrade against
a mock ``op``) is obsolete: the reconciliation migration expresses the
constraint as guarded raw DDL (``ADD CONSTRAINT ... IF NOT EXISTS`` with the
full vocabulary) rather than a reversible drop/add pair, and its downgrade is a
no-op. The drift-guard tests below are the meaningful contract.
"""

from __future__ import annotations

from pathlib import Path

_MIGRATION_NAME = "0110_schema_pipeline_runtime"
_MIGRATION_PATH = (
    Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions" / f"{_MIGRATION_NAME}.py"
)

# The chain head after the FAR-210 feedback correction_state migration (0112),
# now topped by the FAR-309 PR B trust-model migration (0116), the TOCTOU
# hardening migration (0117), and the batch-scoped variants migration (0118),
# the metrics_staging migration (0121), and the FAR-363 library_sync_state
# (0122) + relax_registry_signature_check (0123) migrations.
_CHAIN_HEAD_MIGRATION_NAME = "0139_add_router_no_match_status"
