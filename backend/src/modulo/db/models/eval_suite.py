import uuid
from typing import Any

from sqlalchemy import JSON, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from modulo.db.models.base import OrgScoped

# Reserved ``legacy_suite_id`` for eval definitions that had a NULL legacy
# ``suite_id`` string. The backfill migration (0126) creates ONE such sentinel
# EvalSuite per organisation (enforced by the UNIQUE(organisation_id,
# legacy_suite_id) constraint). A real user-named suite colliding with this
# exact string is astronomically unlikely; documented as reserved.
SENTINEL_LEGACY_SUITE_ID = "__NO_SUITE__"


class EvalSuite(OrgScoped):
    """First-class grouping entity for eval definitions (FAR-374 Phase 1).

    Promotes the free-text ``EvalDefinition.suite_id`` tag into a real entity.
    ``legacy_suite_id`` preserves the original string value for the backfill and
    for read-back compatibility; new suites created through the UI leave it
    NULL. ``eval_definition_ids`` is a denormalised JSON list of the eval
    definitions that currently belong to the suite (kept in sync by the
    service layer in later phases).

    ``created_at`` / ``updated_at`` are provided by the ``TimestampMixin`` base.
    Not versioned in Phase 1 (versioning is a later phase).
    """

    __tablename__ = "eval_suites"
    __table_args__ = (
        CheckConstraint(
            "visibility IN ('org', 'team')",
            name="ck_eval_suites_visibility",
        ),
        # A team-scoped suite MUST name its owning team (mirrors model_backends).
        CheckConstraint(
            "visibility = 'org' OR owner_team_id IS NOT NULL",
            name="ck_eval_suites_team_owner",
        ),
        # One backfilled suite per (organisation, legacy string tag). Postgres
        # treats NULL legacy_suite_id as distinct per row, so new UI-created
        # suites (legacy_suite_id NULL) are not constrained by this.
        UniqueConstraint(
            "organisation_id",
            "legacy_suite_id",
            name="uq_eval_suites_org_legacy_suite_id",
        ),
    )

    owner_team_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("teams.id", ondelete="RESTRICT"), nullable=True
    )
    visibility: Mapped[str] = mapped_column(String(10), nullable=False, server_default="org")
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    # Denormalised list of eval_definitions.id belonging to this suite.
    eval_definition_ids: Mapped[list[uuid.UUID]] = mapped_column(JSON, nullable=False, default=list)
    # Optional reference to an input set / dataset (Phase 2 concept). NULL in
    # Phase 1 — reserved column so later phases don't need a migration.
    input_set_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Original free-text suite tag (FAR-374 backfill). NULL for suites created
    # through the new UI.
    legacy_suite_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Eval-suite versioning (FAR-382): mirrors EvalDefinition.version — an
    # integer starting at 1, bumped on edit so a suite-level change is an
    # explicitly version-scoped event. ``pre_version_raw`` snapshots the suite
    # definition as it existed before the current version was stamped (nullable
    # for suites that have never been edited since versioning).
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1", default=1)
    pre_version_raw: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
