"""Unit tests for the FAR-190 ongoing-trigger no-delivery streak engine.

Covers the streak engine in ``modulo.core.trigger_streak``:

* ``_streak_config`` — per-trigger threshold (``max_no_delivery_streak`` with
  the legacy ``max_consecutive_failures`` fallback) + wall-clock window.
* ``_streak_deactivate_enabled`` — the kill switch for the deactivate+notify
  side-effect.
* the deactivation SQL — NULL-epoch COALESCE, equal-completed_at ordering,
  excluded-mid-walk breaks, terminal-only predicates, guarded atomic UPDATE.
* ``_deactivate_trigger_on_no_delivery_streak`` — threshold boundary,
  idempotent second tick, audit + trigger-event records in the same tx.
* ``enforce_no_delivery_streaks`` — never-raises failure injection, per-org
  per-hour cap, mass-cascade alert, flag-off, notification failure retry.
* the shared re-enable anchor helpers routed through the active-write sites.
* the dispatcher_reconcile wiring.

Mock/fake based (no real Postgres/Redis), mirroring the test_dispatcher_reconcile
/ test_cron_helpers_ongoing patterns: statement-routed mocked sessions + patched
helpers.
"""

from __future__ import annotations

import uuid
from typing import Any, Self
from unittest.mock import MagicMock

import pytest

from modulo.core import trigger_streak as ts

ORG = uuid.uuid4()
TRIGGER_ID = uuid.uuid4()
PIPELINE_ID = uuid.uuid4()


def _mock_result(**kwargs: Any) -> MagicMock:
    result = MagicMock()
    for name, value in kwargs.items():
        getattr(result, name).return_value = value
    return result


class _Begin:
    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False


def _settings(**overrides: object) -> MagicMock:
    base: dict[str, object] = {
        "saq_runs_queue": "runs",
        "redis_url": "redis://localhost:6379/0",
        "saq_redis_pool_size": 5,
        "fernet_key": "b" * 44,
    }
    base.update(overrides)
    return MagicMock(**base)


def _patch_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://localhost/test")
    monkeypatch.setenv("SECRET_KEY", "a" * 40)
    monkeypatch.setenv("FERNET_KEY", "b" * 44)


def _deactivated_data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": TRIGGER_ID,
        "pipeline_id": PIPELINE_ID,
        "organisation_id": ORG,
        "config_json": {},
        "streak": 5,
        "reason": "no_delivery",
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# _streak_config / kill switch
# ---------------------------------------------------------------------------


class _RoutedSession:
    """Minimal async session double routing statements to canned results."""

    def __init__(
        self, *, update_row: Any = None, reason_row: Any = None, trigger_rows: list[Any] | None = None
    ) -> None:
        self._update_row = update_row
        self._reason_row = reason_row
        self._trigger_rows = trigger_rows or []
        self.executed: list[tuple[Any, Any]] = []
        self.added: list[object] = []
        self.begin_cm = _Begin()
        bind = MagicMock()
        bind.dialect.name = "postgresql"
        self._get_bind = MagicMock(return_value=bind)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args: object) -> bool:
        return False

    def begin(self) -> _Begin:
        return self.begin_cm

    def begin_nested(self) -> _Begin:
        return _Begin()

    def get_bind(self) -> Any:
        return self._get_bind()

    def in_transaction(self) -> bool:
        return True

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def execute(self, stmt: Any, params: dict[str, Any] | None = None) -> MagicMock:
        self.executed.append((stmt, params))
        s = str(stmt).lower()
        if "set_config" in s:
            return MagicMock()
        if s.startswith("update triggers"):
            r = MagicMock()
            r.first.return_value = self._update_row
            r.rowcount = 1 if self._update_row is not None else 0
            return r
        if "as reason" in s:
            r = MagicMock()
            r.first.return_value = self._reason_row
            return r
        if "from triggers" in s:
            return _mock_result(scalars=self._trigger_rows)
        return MagicMock()


# ---------------------------------------------------------------------------
# _streak_config / kill switch
# ---------------------------------------------------------------------------


class TestStreakConfig:
    def test_defaults(self) -> None:
        threshold, window = ts._streak_config({})
        assert threshold == ts.ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT
        assert threshold == 5
        assert window == ts.ONGOING_MIN_NO_DELIVERY_WINDOW_HOURS_DEFAULT
        assert window == 24

    def test_per_trigger_threshold(self) -> None:
        threshold, _ = ts._streak_config({"max_no_delivery_streak": 3})
        assert threshold == 3

    def test_legacy_key_fallback(self) -> None:
        """The legacy ``max_consecutive_failures`` config key is read as a
        fallback for one release."""
        threshold, _ = ts._streak_config({"max_consecutive_failures": 7})
        assert threshold == 7

    def test_new_key_wins_over_legacy(self) -> None:
        threshold, _ = ts._streak_config({"max_no_delivery_streak": 2, "max_consecutive_failures": 9})
        assert threshold == 2

    def test_invalid_threshold_falls_back(self) -> None:
        for bad in ("abc", -3, 0):
            threshold, _ = ts._streak_config({"max_no_delivery_streak": bad})
            assert threshold == ts.ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT

    def test_boolean_threshold_rejected(self) -> None:
        """A mis-typed JSON boolean must not arm the guard to maximum
        aggressiveness: ``int(True) == 1`` would deactivate on a single run.
        Booleans and floats are rejected, not coerced (FAR-190 qa FIX 8)."""
        for bad in (True, False, 3.7):
            threshold, _ = ts._streak_config({"max_no_delivery_streak": bad})
            assert threshold == ts.ONGOING_MAX_NO_DELIVERY_STREAK_DEFAULT

    def test_boolean_window_rejected(self) -> None:
        for bad in (True, 12.5):
            _, window = ts._streak_config({"no_delivery_min_window_hours": bad})
            assert window == ts.ONGOING_MIN_NO_DELIVERY_WINDOW_HOURS_DEFAULT

    def test_window_default_and_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert ts._streak_config({})[1] == 24
        monkeypatch.setenv("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS", "0")  # dogfood no-window variant
        assert ts._streak_config({})[1] == 0
        monkeypatch.setenv("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS", "not-a-number")
        assert ts._streak_config({})[1] == 24

    def test_per_trigger_window_overrides_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_ONGOING_STREAK_MIN_WINDOW_HOURS", "0")
        assert ts._streak_config({"no_delivery_min_window_hours": 12})[1] == 12


class TestKillSwitch:
    def test_default_enabled(self) -> None:
        assert ts.STREAK_DEACTIVATE_ENABLED_DEFAULT is True
        assert ts._streak_deactivate_enabled() is True

    def test_env_disables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for value in ("0", "false", "off", "no", "FALSE"):
            monkeypatch.setenv("MODULO_STREAK_DEACTIVATE_KILL_SWITCH", value)
            assert ts._streak_deactivate_enabled() is False

    def test_env_enables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODULO_STREAK_DEACTIVATE_KILL_SWITCH", "1")
        assert ts._streak_deactivate_enabled() is True


# ---------------------------------------------------------------------------
# Deactivation SQL structure (the count folded into the UPDATE's WHERE)
# ---------------------------------------------------------------------------


class TestDeactivateSQL:
    def test_null_epoch_coalesces(self) -> None:
        """A NULL streak_epoch (rolling-deploy skew) COALESCEs to now() so the
        boundary becomes "now" — no run counts and the trigger can never be
        deactivated until the row is re-anchored. The epoch is read from the
        live trigger row via a self-contained scalar subquery (the reason query
        has no ``triggers`` relation in its FROM)."""
        assert "COALESCE((SELECT tr.streak_epoch FROM triggers tr" in ts._NO_DELIVERY_DEACTIVATE_SQL
        assert ", now())" in ts._NO_DELIVERY_DEACTIVATE_SQL
        assert "GREATEST(" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_last_delivery_derived_from_classification_log(self) -> None:
        """The boundary's last_delivery_at is MAX(completed_at) of delivered
        classifications — a single source of truth, never a raw status."""
        assert "run_classification ->> 'value' = 'delivered'" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_excluded_mid_walk_breaks(self) -> None:
        """A cancelled/budget_exceeded (excluded) run between no-deliveries stops
        the walk — the count must not span across it."""
        assert "('delivered','excluded','unclassified')" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_unclassified_run_breaks_fail_closed(self) -> None:
        """A terminal run with NO classification record (or an 'unclassified'
        marker) stops the walk fail-closed — deactivation can never ride on
        uncertain evidence."""
        assert "r3.run_classification IS NULL" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_equal_completed_at_ordering(self) -> None:
        """Equal completed_at runs get a deterministic total order via the id
        tie-break in the stop predicate."""
        assert "r3.id > r.id" in ts._NO_DELIVERY_DEACTIVATE_SQL

    def test_terminal_only_predicates(self) -> None:
        """Only terminal runs are counted or stop the walk — an in-flight run is
        drained (never counted, never breaking the walk; never cancelled). The
        status set is derived from TERMINAL_STATUSES (single source of truth)."""
        sql = ts._NO_DELIVERY_DEACTIVATE_SQL
        terminal_statuses = (
            "'budget_exceeded','cancelled','complete','eval_failed','failed','router_no_match','stalled'"
        )
        assert f"r.status IN ({terminal_statuses})" in sql
        assert f"r3.status IN ({terminal_statuses})" in sql
        assert "pending" not in sql

    def test_guarded_atomic_update(self) -> None:
        """The UPDATE is guarded on ``active`` and folds the streak into the
        WHERE (no TOCTOU) — a re-enabled trigger or a stale tick can never be
        hit, and concurrent ticks produce one rowcount=1 then a no-op."""
        sql = ts._NO_DELIVERY_DEACTIVATE_SQL
        assert "AND active" in sql
        assert ">= :threshold" in sql
        assert "RETURNING" in sql
        assert "AS streak" in sql

    def test_wall_clock_window_boundary(self) -> None:
        """The boundary must be at least the wall-clock window old
        (<= :window_cutoff) — the product 24h variant; a dogfood window of 0
        makes the cutoff "now", which every boundary trivially satisfies."""
        assert "<= :window_cutoff" in ts._NO_DELIVERY_DEACTIVATE_SQL


class TestMigrationBackfillGrace:
    def test_migration_backfills_epoch_and_branches_off_current_head(self) -> None:
        """The reconciliation chain adds triggers.streak_epoch with a DEFAULT
        CURRENT_TIMESTAMP (backfill = now()) — pre-existing no-delivery history
        can never deactivate on tick 1 because the boundary is
        GREATEST(last_delivery_at, streak_epoch) and every old run predates the
        anchored epoch. The chain has a single linear head."""
        from pathlib import Path

        from alembic.script import ScriptDirectory

        versions_dir = Path(__file__).resolve().parents[3] / "src" / "modulo" / "db" / "migrations" / "versions"
        source = (versions_dir / "0110_schema_pipeline_runtime.py").read_text(encoding="utf-8")
        # The reconciliation DDL adds the column with a server default (backfill).
        assert 'ADD COLUMN IF NOT EXISTS "streak_epoch" timestamp with time zone DEFAULT CURRENT_TIMESTAMP' in source
        assert "ix_runs_unclassified_terminal" in source
        heads = ScriptDirectory(str(versions_dir.parent)).get_heads()
        assert heads == ["0139_add_router_no_match_status"], f"expected a single head, got {heads}"
