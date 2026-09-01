"""Unit tests for migration 0171_runs_list_performance_indexes.

Structural: load the migration module and assert its contract without a
database, and pin model/migration parity for the two new runs indexes:

* the chain is pinned (revision -> 0170_add_residual_foreign_keys) so the
  pre-commit check-migration-heads hook can never be ambushed by a rebase;
* the upgrade creates exactly the two indexes the runs-list fix relies on
  (ix_runs_org_status for the active-run counts, ix_runs_org_created_pipeline
  with a non-key INCLUDE column for the list total COUNT);
* the downgrade drops both;
* the Run model declares both indexes (create_all'd schemas and autogenerate
  stay in sync with the migration).

They run without a database.
"""

import importlib.util
from pathlib import Path
from types import ModuleType

_VERSIONS = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
_MIGRATION_NAME = "0171_runs_list_performance_indexes"
_MIGRATION_PATH = _VERSIONS / f"{_MIGRATION_NAME}.py"


def _load_migration() -> ModuleType:
    assert _MIGRATION_PATH.exists(), f"Migration file missing: {_MIGRATION_PATH}"
    spec = importlib.util.spec_from_file_location(f"migration_{_MIGRATION_NAME}", _MIGRATION_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source_code() -> str:
    """Return the migration's executable code, minus the module docstring."""
    source = _MIGRATION_PATH.read_text(encoding="utf-8")
    parts = source.split('"""', 2)
    return parts[2] if len(parts) >= 3 else source


def test_metadata_pins_chain() -> None:
    module = _load_migration()
    assert module.revision == _MIGRATION_NAME
    assert module.down_revision == "0170_add_residual_foreign_keys"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_upgrade_creates_both_performance_indexes() -> None:
    code = _source_code()
    assert 'op.create_index("ix_runs_org_status", "runs", ["organisation_id", "status"])' in code
    assert '"ix_runs_org_created_pipeline"' in code
    # The covering index must carry pipeline_id as a non-key INCLUDE column —
    # that is what keeps the list total COUNT index-only.
    assert 'postgresql_include=["pipeline_id"]' in code


def test_downgrade_drops_both_indexes() -> None:
    code = _source_code().split("def downgrade", 1)[1]
    assert 'op.drop_index("ix_runs_org_created_pipeline", table_name="runs")' in code
    assert 'op.drop_index("ix_runs_org_status", table_name="runs")' in code


def test_model_declares_both_indexes() -> None:
    from modulo.db.models.run import Run

    index_names = {idx.name for idx in Run.__table__.indexes}
    assert "ix_runs_org_status" in index_names, "model/migration drift: ix_runs_org_status missing from Run"
    assert "ix_runs_org_created_pipeline" in index_names, (
        "model/migration drift: ix_runs_org_created_pipeline missing from Run"
    )
    # Parity on the INCLUDE column too: the model index must carry the same
    # non-key pipeline_id column the migration creates. Included columns live
    # in the postgresql dialect options, not in Index.columns.
    model_index = next(idx for idx in Run.__table__.indexes if idx.name == "ix_runs_org_created_pipeline")
    assert model_index.dialect_options["postgresql"]["include"] == ["pipeline_id"]
    assert [col.name for col in model_index.columns] == ["organisation_id", "created_at"]
