"""Final-state tests for the ``ongoing`` / ``slack_app_mention`` trigger types.

The migration chain was squashed into three idempotent reconciliation
migrations (``0108_schema_org_identity`` / ``0109_schema_teams_library`` /
``0110_schema_pipeline_runtime``). The per-feature migrations that used to
carry this surface (``0094_ongoing_trigger_type`` through
``0101_guardrails``) no longer exist, so the DDL source assertions here run
against ``0110_schema_pipeline_runtime`` — the reconciliation migration that
owns the trigger/run CHECK vocabulary, the streak-engine partial indexes, the
guardrail eval vocabulary, and the raw-output markers column:


* the wide ``ck_triggers_type`` / ``ck_runs_trigger_type`` vocabularies include
  ``ongoing`` and ``slack_app_mention``,
* the partial ``ck_triggers_ongoing_spend_limit`` /
  ``ck_triggers_ongoing_target_range`` checks exist,
* the ``ix_runs_trigger_id_status`` / ``ix_runs_trigger_id_created_at`` and
  streak-engine partial indexes exist,
* ``runs.raw_output_markers`` / ``run_classification`` / ``work_intact`` and
  ``eval_results.observed`` columns exist,
* ``ck_eval_definitions_type`` includes ``guardrail``,
* the ORM models' CHECK constraints carry the same vocabulary (drift guard).

"""

from __future__ import annotations

_MIGRATION_0008 = "0110_schema_pipeline_runtime"
_MIGRATION_0113 = "0113_guardrail_summary"
_HEAD_MIGRATION = "0139_add_router_no_match_status"
