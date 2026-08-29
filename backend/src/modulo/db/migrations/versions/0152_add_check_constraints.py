"""Add CHECK constraints for non-negative counters and enum vocabularies.

Revision ID: 0152_add_check_constraints
Revises: 0151_add_missing_foreign_keys
Create Date: 2026-08-29

Several columns are counters / bounded enums that should be enforced at the DB
level but currently are not:

* Non-negative counters (``>= 0``) — drift or a bug could write a negative
  count, which would corrupt rate-limit / billing math.
* ``error_notification_rule`` thresholds — ``condition_min_count`` and
  ``condition_window_seconds`` must be strictly positive; ``cooldown_seconds``
  must be ``>= 0``.
* ``library_primitive.average_rating`` must be within ``0..5``.
* ``eval_suite_run`` case tallies must be non-negative and the passed/failed
  tallies cannot exceed ``total_cases``.
* ``run_daily_facts`` ``trigger_type`` / ``status`` mirror the existing
  ``ck_runs_*`` vocabularies so the analytics fact table cannot hold a value
  the runs table rejects.

Nullable columns are safe: a NULL row satisfies every CHECK below.
"""

from __future__ import annotations

from alembic import op

revision: str = "0152_add_check_constraints"
down_revision: str | None = "0151_add_missing_foreign_keys"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_RUN_TRIGGER_TYPE = (
    "trigger_type IN ('manual','webhook','cron','polling','agent_signal','ongoing','correction','slack_app_mention')"
)
_RUN_STATUS = (
    "status IN ('pending','running','awaiting_human','claimed','complete',"
    "'failed','cancelled','eval_failed','stalled','budget_exceeded',"
    "'router_no_match','cost_ceiling_exceeded')"
)


def upgrade() -> None:
    op.create_check_constraint("ck_run_claim_count", "runs", "claim_count >= 0")
    op.create_check_constraint("ck_run_node_attempt_count", "runs", "node_attempt_count >= 0")
    op.create_check_constraint(
        "ck_notification_delivery_attempt_count",
        "notification_delivery_log",
        "attempt_count >= 0",
    )
    op.create_check_constraint(
        "ck_notification_endpoint_dead_letter",
        "notification_endpoints",
        "consecutive_dead_letter_count >= 0",
    )
    op.create_check_constraint(
        "ck_organisation_cumulative_spend",
        "organisations",
        "org_cumulative_spend_cents >= 0",
    )
    op.create_check_constraint(
        "ck_library_primitive_download_count",
        "library_primitives",
        "download_count >= 0",
    )
    op.create_check_constraint(
        "ck_library_primitive_review_count",
        "library_primitives",
        "review_count >= 0",
    )
    op.create_check_constraint(
        "ck_library_primitive_average_rating",
        "library_primitives",
        "average_rating >= 0 AND average_rating <= 5",
    )
    op.create_check_constraint(
        "ck_error_notification_rule_min_count",
        "error_notification_rules",
        "condition_min_count > 0",
    )
    op.create_check_constraint(
        "ck_error_notification_rule_window",
        "error_notification_rules",
        "condition_window_seconds > 0",
    )
    op.create_check_constraint(
        "ck_error_notification_rule_cooldown",
        "error_notification_rules",
        "cooldown_seconds >= 0",
    )
    op.create_check_constraint("ck_error_group_count", "error_groups", "count >= 0")
    op.create_check_constraint("ck_daily_run_count_run_count", "org_daily_run_counts", "run_count >= 0")
    op.create_check_constraint("ck_audit_event_event_count", "audit_events", "event_count >= 0")
    op.create_check_constraint("ck_journey_run_count", "journeys", "run_count >= 0")
    op.create_check_constraint("ck_cost_component_sort_order", "cost_components", "sort_order >= 0")
    op.create_check_constraint(
        "ck_connector_profile_response_max_bytes",
        "connector_profiles",
        "response_max_bytes >= 0",
    )
    op.create_check_constraint("ck_eval_suite_run_total_cases", "suite_runs", "total_cases >= 0")
    op.create_check_constraint("ck_eval_suite_run_passed_cases", "suite_runs", "passed_cases >= 0")
    op.create_check_constraint("ck_eval_suite_run_failed_cases", "suite_runs", "failed_cases >= 0")
    op.create_check_constraint(
        "ck_eval_suite_run_excluded_cases",
        "suite_runs",
        "excluded_case_count >= 0",
    )
    op.create_check_constraint(
        "ck_eval_suite_run_passed_le_total",
        "suite_runs",
        "passed_cases <= total_cases",
    )
    op.create_check_constraint(
        "ck_eval_suite_run_failed_le_total",
        "suite_runs",
        "failed_cases <= total_cases",
    )
    op.create_check_constraint("ck_run_daily_facts_trigger_type", "run_daily_facts", _RUN_TRIGGER_TYPE)
    op.create_check_constraint("ck_run_daily_facts_status", "run_daily_facts", _RUN_STATUS)


def downgrade() -> None:
    op.drop_constraint("ck_run_daily_facts_status", "run_daily_facts", type_="check")
    op.drop_constraint("ck_run_daily_facts_trigger_type", "run_daily_facts", type_="check")
    op.drop_constraint("ck_eval_suite_run_failed_le_total", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_passed_le_total", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_excluded_cases", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_failed_cases", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_passed_cases", "suite_runs", type_="check")
    op.drop_constraint("ck_eval_suite_run_total_cases", "suite_runs", type_="check")
    op.drop_constraint(
        "ck_connector_profile_response_max_bytes",
        "connector_profiles",
        type_="check",
    )
    op.drop_constraint("ck_cost_component_sort_order", "cost_components", type_="check")
    op.drop_constraint("ck_journey_run_count", "journeys", type_="check")
    op.drop_constraint("ck_audit_event_event_count", "audit_events", type_="check")
    op.drop_constraint("ck_daily_run_count_run_count", "org_daily_run_counts", type_="check")
    op.drop_constraint("ck_error_group_count", "error_groups", type_="check")
    op.drop_constraint(
        "ck_error_notification_rule_cooldown",
        "error_notification_rules",
        type_="check",
    )
    op.drop_constraint(
        "ck_error_notification_rule_window",
        "error_notification_rules",
        type_="check",
    )
    op.drop_constraint(
        "ck_error_notification_rule_min_count",
        "error_notification_rules",
        type_="check",
    )
    op.drop_constraint("ck_library_primitive_average_rating", "library_primitives", type_="check")
    op.drop_constraint("ck_library_primitive_review_count", "library_primitives", type_="check")
    op.drop_constraint("ck_library_primitive_download_count", "library_primitives", type_="check")
    op.drop_constraint("ck_organisation_cumulative_spend", "organisations", type_="check")
    op.drop_constraint(
        "ck_notification_endpoint_dead_letter",
        "notification_endpoints",
        type_="check",
    )
    op.drop_constraint(
        "ck_notification_delivery_attempt_count",
        "notification_delivery_log",
        type_="check",
    )
    op.drop_constraint("ck_run_node_attempt_count", "runs", type_="check")
    op.drop_constraint("ck_run_claim_count", "runs", type_="check")
