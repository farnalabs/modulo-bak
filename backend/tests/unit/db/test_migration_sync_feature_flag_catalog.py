"""Name-sync test for the feature-flag catalog surface (final state).

The migration chain was squashed into three idempotent reconciliation
migrations. The old ``0072_sync_feature_flag_catalog`` migration (which
upserted flags into ``feature_flag_catalog`` that the seed catalog missed) and
the ``0105`` head no longer exist. The feature-flag catalog is now created by
the reconciliation chain (``0109_schema_teams_library`` adds the table
columns) and populated at application startup from
``modulo.core.seed_data.catalog.FLAGS``. These tests assert:

* the reconciliation chain creates the ``feature_flag_catalog`` columns the
  seed path writes to,
* the seed catalog covers the full ``_KNOWN_FLAGS`` set — a flag added to
  ``_KNOWN_FLAGS`` without a matching ``catalog.FLAGS`` entry (and a startup
  seed that upserts it) never appears for existing deployments,
* every expected flag is present in the seed catalog.
"""

_HEAD_MIGRATION_NAME = "0139_add_router_no_match_status"
