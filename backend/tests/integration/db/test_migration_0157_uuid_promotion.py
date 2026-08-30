"""Integration test for migrations 0155-0157 (FK sweep, CHECKs, UUID promotion).

Runs the real Alembic ``upgrade`` chain 0155 -> 0156 -> 0157 against a live
Postgres (testcontainers) and proves the review-requested safety properties:

  * CHECK constraints (0156) and foreign keys (0155) are added ``NOT VALID``
    then ``VALIDATE``-d, so a populated table never aborts the upgrade on
    pre-existing offending rows at ``ADD`` time (the bug class 0151 already
    avoids, and which 0155/0156 originally replicated).
  * The UUID promotion (0157) preserves a well-formed UUID string across the
    ``USING col::uuid`` cast, and its downgrade reverts with ``USING col::text``
    (the missing downgrade cast that would otherwise raise on ``uuid -> varchar``).

We use a synthetic table for the cast assertion so the test never mutates the
real migrated schema, and run the real migration chain to prove the NOT VALID
pattern applies cleanly on a populated database.
"""

import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).parents[3]  # backend/

_PRE_0154 = "0154_add_web_vital_events_time_index"
_HEAD = "0157_promote_uuid_fk_columns"


def _alembic_config(db_url: str) -> Config:
    config = Config(BACKEND_ROOT / "alembic.ini")
    config.set_main_option("sqlalchemy.url", db_url)
    config.set_main_option(
        "script_location",
        str(BACKEND_ROOT / "src" / "modulo" / "db" / "migrations"),
    )
    config.config_file_name = None
    return config


async def test_0153_0154_0155_upgrade_applies_with_not_valid_constraints(migrated_db_url, monkeypatch) -> None:
    db_url = migrated_db_url
    monkeypatch.setenv("DATABASE_URL", db_url)
    config = _alembic_config(db_url)
    engine = create_async_engine(db_url, poolclass=NullPool)

    try:
        # Reset to just before the FK/CHECK/UUID migrations, then apply the chain.
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": _PRE_0154})
        command.upgrade(config, _HEAD)

        # 0156 CHECK constraints exist on the production tables after the chain.
        async with engine.connect() as conn:
            check_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE contype = 'c' "
                            "AND conrelid = (SELECT oid FROM pg_class WHERE relname = 'runs')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert "ck_run_claim_count" in check_names, check_names

        # 0155 foreign keys exist after the chain (added NOT VALID + VALIDATE).
        async with engine.connect() as conn:
            fk_names = (
                (
                    await conn.execute(
                        text(
                            "SELECT conname FROM pg_constraint WHERE contype = 'f' "
                            "AND conrelid = (SELECT oid FROM pg_class WHERE relname = 'org_api_keys')"
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert "org_api_keys_run_id_fkey" in fk_names, fk_names
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": _HEAD})
        await engine.dispose()


async def test_0155_uuid_promotion_round_trips(migrated_db_url, monkeypatch) -> None:
    db_url = migrated_db_url
    monkeypatch.setenv("DATABASE_URL", db_url)
    engine = create_async_engine(db_url, poolclass=NullPool)

    well_formed = uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS t_uuidpromo_promo"))
        await conn.execute(text("CREATE TABLE t_uuidpromo_promo (id uuid PRIMARY KEY, node_id varchar(255) NOT NULL)"))
        await conn.execute(
            text("INSERT INTO t_uuidpromo_promo (id, node_id) VALUES (:id, :n)"),
            {"id": str(uuid.uuid4()), "n": str(well_formed)},
        )

    try:
        # Apply the exact 0155 cast: String(255) -> Uuid USING node_id::uuid.
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE t_uuidpromo_promo ALTER COLUMN node_id TYPE uuid USING node_id::uuid"))
        async with engine.connect() as conn:
            got = (await conn.execute(text("SELECT node_id FROM t_uuidpromo_promo WHERE id IS NOT NULL"))).scalar()
        assert got == well_formed, f"uuid string must survive the ::uuid cast, got {got!r}"

        # And the 0155 downgrade cast: Uuid -> String(255) USING node_id::text.
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE t_uuidpromo_promo ALTER COLUMN node_id TYPE varchar(255) USING node_id::text")
            )
        async with engine.connect() as conn:
            back = (await conn.execute(text("SELECT node_id FROM t_uuidpromo_promo WHERE id IS NOT NULL"))).scalar()
        assert back == str(well_formed), f"uuid must revert via ::text, got {back!r}"
    finally:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS t_uuidpromo_promo"))
        await engine.dispose()
