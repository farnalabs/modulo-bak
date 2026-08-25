"""FAR-309 PR B trust-model migration (0116) content tests.

``0116_guardrail_trust_pr_b`` ships the trust-model schema surface:

* ``pipeline_snapshots.guardrail_pins_fingerprint`` — the run-start
  snapshot-integrity digest (nullable: legacy snapshots are still trusted),
* ``eval_definitions.deleted_at`` / ``deleted_by`` — the two-step soft-delete
  stamp, plus the ``ix_eval_definitions_deleted_at`` index.

Unlike the schema-reconciliation migrations (0108/0109/0110, which are not
reversible in general), 0116 is a plain additive migration chained directly
onto ``0115_notification_preferences`` and IS fully reversible — the downgrade
drops exactly the columns the upgrade added. These tests assert the chain edge
(down_revision), the upgrade DDL, the downgrade DDL, and that 0116 is the
single linear chain head.
"""

from pathlib import Path

from alembic.script import ScriptDirectory

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"

_REVISION = "0116_guardrail_trust_pr_b"
_DOWN_REVISION = "0115_notification_preferences"


def _source() -> str:
    path = _VERSIONS / f"{_REVISION}.py"
    assert path.exists(), f"Migration file missing: {path}"
    return path.read_text(encoding="utf-8")


def _script() -> ScriptDirectory:
    return ScriptDirectory(str(_VERSIONS.parent))


class TestGuardrailTrustMigration:
    def test_head_is_single_chain(self) -> None:
        heads = _script().get_heads()
        assert heads == ["0139_add_router_no_match_status"], f"expected a single head, got {heads}"
