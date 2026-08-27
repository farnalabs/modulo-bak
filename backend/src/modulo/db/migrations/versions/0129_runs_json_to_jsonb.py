"""Convert ``runs`` JSON columns to JSONB non-blockingly (dist db-runs-jsonb).

Revision ID: 0129_runs_json_to_jsonb
Revises: 0128_add_fk_lookup_indexes
Create Date: 2026-08-23

Why this migration is NON-BLOCKING and RESUMABLE
-----------------------------------------------
The ``runs`` table is the hottest table in the system — the pipeline runner
writes to it continuously. The original migration used a direct
``ALTER TABLE public."runs" ALTER COLUMN "col" TYPE jsonb USING "col"::jsonb``
for each of the seven columns. Every ``ALTER COLUMN ... TYPE`` takes an
ACCESS EXCLUSIVE lock **and** performs a full-table rewrite. On a live,
constantly-written ``runs`` table the lock is never acquired (the writer keeps
owning it), so the migration hangs forever. That hangs the deploy: the worker
fail-closes once it sees the DB is "not at head", and a migrated-out-of-line
idle-in-transaction session has held the ALTER lock for 14+ minutes (requiring
a manual ``pg_terminate_backend``).

This rewrite avoids the blocking rewrite entirely. The ``json`` -> ``jsonb``
cast is lossless for every existing row (NULL stays NULL; well-formed ``json``
re-parses identically as ``jsonb``), so the data can be converted in place
using this additive, per-column algorithm:

1. **Add a temp ``jsonb`` column** ``{col}_jsonb`` (fast: brief ACCESS
   EXCLUSIVE, metadata-only — no data rewrite).
2. **Batch backfill** ``{col}_jsonb = {col}::jsonb`` for rows where
   ``{col}_jsonb IS NULL`` in chunks of 1000, committing after every batch.
   Row-level only: no table lock, safe under concurrent writes, and resumable
   because only still-pending rows are touched.
3. **Swap** by renaming: ``{col}`` -> ``{col}_old``, then ``{col}_jsonb`` ->
   ``{col}``, then drop ``{col}_old``. Each rename/drop is a brief
   metadata-only lock; there is no data rewrite.

Because every phase is idempotent (information_schema column-existence checks
gate ADD/RENAME/DROP and ``WHERE {col}_jsonb IS NULL`` gates the backfill), a
migration that fails midway can simply be re-run: already-converted columns are
skipped and only the remaining rows are backfilled.

The ORM model keeps these columns mapped as generic ``sqlalchemy.JSON`` (NOT
``JSONB``) for SQLite/MariaDB parity — the same convention the four
pre-existing jsonb columns follow. Migrations run against Postgres only; the
SQLite unit-test backend uses the ORM model, so the type change lives entirely
in the migration, never in the model.

Downgrade reverses each column ``jsonb`` -> ``json`` using the same
non-blocking, resumable pattern (add ``{col}_json``, batch ``::json`` backfill,
swap, drop).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import column, select, table, text, update
from sqlalchemy.dialects.postgresql import JSON as PG_JSON
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0129_runs_json_to_jsonb"
down_revision: str | None = "0128_add_fk_lookup_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Runs columns currently typed ``json`` that this migration promotes to ``jsonb``.
_JSON_COLUMNS: tuple[str, ...] = (
    "cost_breakdown",
    "node_token_usage",
    "input_payload",
    "outputs_json",
    "node_telemetry_json",
    "guardrail_summary_json",
    "variant_config_snapshot",
)

_TABLE = "runs"
_SCHEMA = "public"
_BATCH_SIZE = 1000
_OLD_SUFFIX = "_old"


def _column_meta(bind, table: str, column: str) -> dict | None:
    """Return ``{is_nullable, column_default}`` for ``table.column`` or None.

    Uses ``information_schema.columns`` so the existence check doubles as a read
    of the original column's nullability/default before any rename invalidates
    it. Postgres-only; the caller gates on the dialect.
    """
    row = bind.execute(
        text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = :table AND column_name = :col"
        ),
        {"table": table, "col": column},
    ).fetchone()
    if row is None:
        return None
    return {"is_nullable": row[0], "column_default": row[1]}


def _cast_type(cast: str):
    """Return the Postgres SQLAlchemy type for a ``::cast`` target."""
    return JSONB() if cast == "jsonb" else PG_JSON()


def _backfill(bind, col: str, tmp: str, cast: str) -> None:
    """Backfill ``tmp`` from ``col`` in bounded batches, committing each batch.

    Uses a SQLAlchemy Core ``update`` (not a raw string) so the row predicate is
    parameterised and no f-string SQL reaches the executor. ``{tmp} IS NULL``
    makes it resumable; ``ctid IN (SELECT ... LIMIT 1000)`` caps each UPDATE so
    it holds no long transaction and never blocks writers (row-level locks
    only). Loops until a batch touches zero rows.
    """
    runs_tbl = table(_TABLE, column("ctid"), column(col), column(tmp), schema=_SCHEMA)
    pending = runs_tbl.c[tmp].is_(None) & runs_tbl.c[col].isnot(None)
    ctid_subq = select(runs_tbl.c.ctid).where(pending).limit(_BATCH_SIZE)
    stmt = (
        update(runs_tbl)
        .where(pending)
        .where(runs_tbl.c.ctid.in_(ctid_subq))
        .values({tmp: runs_tbl.c[col].cast(_cast_type(cast))})
    )
    while True:
        result = bind.execute(stmt)
        rowcount = result.rowcount
        bind.commit()
        if rowcount == 0:
            break


def _add_temp_column(bind, tmp: str, cast: str) -> None:
    """Add ``tmp`` (typed ``cast``) if absent — brief metadata-only lock."""
    if _column_meta(bind, _TABLE, tmp) is not None:
        return
    op.execute(text(f'ALTER TABLE public."{_TABLE}" ADD COLUMN "{tmp}" {cast}'))
    bind.commit()


def _swap(bind, col: str, tmp: str, old: str) -> None:
    """Rename ``col`` -> ``old``, ``tmp`` -> ``col``, drop ``old``.

    Skips entirely if the ``old`` column already exists (re-run safety). Each
    step is a brief metadata-only lock; no data is rewritten.
    """
    if _column_meta(bind, _TABLE, old) is not None:
        return
    op.execute(text(f'ALTER TABLE public."{_TABLE}" RENAME COLUMN "{col}" TO "{old}"'))
    bind.commit()
    op.execute(text(f'ALTER TABLE public."{_TABLE}" RENAME COLUMN "{tmp}" TO "{col}"'))
    bind.commit()
    op.execute(text(f'ALTER TABLE public."{_TABLE}" DROP COLUMN "{old}"'))
    bind.commit()


def _finalize(bind, col: str, orig: dict) -> None:
    """Mirror the original column's nullability/default onto the swapped column.

    The temp column is added nullable without a default, so after the swap the
    new ``col`` must be re-constrained to match the original. The cast is
    lossless, so the backfilled data is identical and ``SET NOT NULL`` is safe.
    """
    if orig["is_nullable"] == "NO":
        op.execute(text(f'ALTER TABLE public."{_TABLE}" ALTER COLUMN "{col}" SET NOT NULL'))
        bind.commit()
    if orig["column_default"] is not None:
        op.execute(text(f'ALTER TABLE public."{_TABLE}" ALTER COLUMN "{col}" SET DEFAULT {orig["column_default"]}'))
        bind.commit()


def _convert(bind, *, tmp_suffix: str, cast: str) -> None:
    """Non-blockingly convert each ``_JSON_COLUMNS`` column via a temp column.

    ``tmp_suffix`` names the temporary column (``jsonb`` upgrading ``json`` ->
    ``jsonb``, ``json`` downgrading ``jsonb`` -> ``json``); the held-back column
    is always ``{col}_old``. Additive and idempotent, so a failed run resumes
    cleanly on re-execution.
    """
    for col in _JSON_COLUMNS:
        orig = _column_meta(bind, _TABLE, col)
        if orig is None:
            # Column absent (non-standard DB) — nothing to convert.
            continue
        tmp = f"{col}_{tmp_suffix}"
        _add_temp_column(bind, tmp, cast)
        _backfill(bind, col, tmp, cast)
        _swap(bind, col, tmp, f"{col}{_OLD_SUFFIX}")
        _finalize(bind, col, orig)


def upgrade() -> None:
    # Postgres-only: plain ``json`` -> ``jsonb`` cast. SQLite uses the ORM model
    # (generic JSON), so skip on non-Postgres dialects.
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _convert(bind, tmp_suffix="jsonb", cast="jsonb")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _convert(bind, tmp_suffix="json", cast="json")
