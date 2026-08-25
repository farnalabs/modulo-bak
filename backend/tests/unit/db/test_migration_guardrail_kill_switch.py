"""Kill-switch flag migration ownership + up/down round-trip (FAR-223 item 13.5).

The ``organisations.guardrails_kill_switch`` (and its ``guardrails_kill_switch_at``
companion) columns are owned by the schema-reconciliation migration
``0108_schema_org_identity``, NOT by a dedicated guardrail migration. Per the
squash convention (AGENTS.md: "reconciliation is not reversible in general"),
0108 uses guarded ``ADD COLUMN IF NOT EXISTS`` DDL so it brings any database to
the current schema state without assuming history, and its DOWNGRADE is a no-op
(a reconciliation migration must not drop columns a pre-squash DB already has).

These tests assert:

* the kill-switch column is ADDED by 0108's upgrade (``ADD COLUMN IF NOT EXISTS
  "guardrails_kill_switch"``),
* the companion ``guardrails_kill_switch_at`` column and the
  ``ck_organisations_guardrails_kill_switch_at`` CHECK constraint are present,
* the DOWNGRADE is a deliberate no-op for the kill-switch column (guarded —
  it does not ``DROP COLUMN`` it), so an up/down round-trip through 0108
  preserves the column rather than destroying it.
"""

from pathlib import Path

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_MIGRATION_0006 = "0108_schema_org_identity"
_MIGRATION_0113 = "0113_guardrail_summary"
_HEAD_MIGRATION = "0139_add_router_no_match_status"
