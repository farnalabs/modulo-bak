"""Final-state tests for the staging-schema reconciliation surface.

The migration chain was squashed into three idempotent reconciliation
migrations. The old ``0065_reconcile_staging_schema`` migration — which
detected a drifted pre-squash schema and repaired it (creating missing
``mcp_setup_tokens`` / ``lifecycle_maps``, dropping legacy ``scheduled_reports``)
— no longer exists. Its reconcile behaviour is folded into the reconciliation
chain's guarded DDL:

* ``0108_schema_org_identity`` owns ``mcp_setup_tokens`` (columns, indexes,
  tenant trigger, RLS policy),
* ``0110_schema_pipeline_runtime`` owns ``lifecycle_maps`` and
  ``scheduled_reports`` (columns, indexes, tenant triggers, RLS enablement +
  org-isolation policy).

These tests assert the reconciliation chain brings a database to that final
state: every object the old reconcile migration created is present in the new
chain's guarded DDL, and the chain has a single linear head.
"""

from pathlib import Path

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_MIGRATION_0006 = "0108_schema_org_identity"
_MIGRATION_0008 = "0110_schema_pipeline_runtime"
_HEAD_MIGRATION = "0120_org_fk_hardening"
# Current chain head (tracks the latest migration; 0120 is still the
# org-FK hardening migration the OrgFkHardening tests below inspect).
_CHAIN_HEAD_MIGRATION = "0139_add_router_no_match_status"
