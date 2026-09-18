"""Provider sovereignty attestations.

NorthRouter's residency routing needs a per-provider (and optionally
per-model) statement of *where* an inference offering lives and *who*
operates it, so a request carrying a residency bar can be filtered to the
candidates that clear it. This table is that statement: a small,
operator-written record keyed on the provider instance name used in
canonical ``instance:model`` keys.

Rows are additive evidence, not credentials: nothing here is secret, and
the routing decision reads only ``sovereignty_level``. The supporting
fields (``hosted_in``, ``operator_jurisdiction``, ``model_origin``,
``vetted``, ``evidence``) exist so an operator can explain, and a portal
can show, *why* a provider sits at its level.

A ``model`` of ``None`` means the row covers every model of that provider
instance; a model-specific row overrides the provider-level row for that
model. A ``organization_id`` of ``None`` makes the row deployment-wide;
an organization-scoped row overrides the deployment-wide row for that
organization. Config-file ``providers.<instance>.sovereignty`` is the
fallback beneath both (see ``services/sovereignty_access.py`` for the
precedence rule).

Style follows ``models/provider_keys.py``: SQLModel with the shared
tenancy mixins, no ``relationship()`` (lazy loading raises on an
``AsyncSession``), and CASCADE on the organization FK because a
sovereignty attestation has no meaning once its organization is gone.
"""

import uuid
from typing import Any

from sqlalchemy import JSON, Column, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from gateway.models.tenancy import CreatedAtMixin, PrimaryKeyMixin, UpdatedAtMixin

# The four sovereignty levels (authoritative for the whole product).
# Kept as plain ints rather than a DB enum so the wire mapping in
# ``services/sovereignty_access.py`` stays the single place the vocabulary
# is translated, and a new level is a constant, not a migration.
SOVEREIGNTY_LEVEL_MIN = 1
SOVEREIGNTY_LEVEL_MAX = 4


class ProviderSovereignty(SQLModel, PrimaryKeyMixin, CreatedAtMixin, UpdatedAtMixin, table=True):
    """One sovereignty attestation for a provider instance (or one of its models)."""

    __tablename__ = "provider_sovereignty"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "provider",
            "model",
            name="uq_provider_sovereignty_org_provider_model",
        ),
        Index("ix_provider_sovereignty_provider", "provider"),
        Index("ix_provider_sovereignty_organization", "organization_id"),
    )

    # None = deployment-wide attestation; set = this organization's own view.
    organization_id: uuid.UUID | None = Field(
        default=None, foreign_key="organization.id", ondelete="CASCADE", index=True
    )
    # The provider *instance* name as used in canonical model keys
    # (e.g. ``lan_gemma``), not the any-llm implementation name.
    provider: str = Field(max_length=255)
    # None = every model of the provider; set = this model only.
    model: str | None = Field(default=None, max_length=255)
    sovereignty_level: int = Field(ge=SOVEREIGNTY_LEVEL_MIN, le=SOVEREIGNTY_LEVEL_MAX)
    hosted_in: str = Field(max_length=8)
    operator_jurisdiction: str = Field(max_length=64)
    model_origin: str | None = Field(default=None, max_length=64)
    vetted: bool = Field(default=False)
    # Free-form attestation: links to an audit report, a contract reference,
    # the date and by-whom of the vetting. Never read by the routing path.
    evidence: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))


class ProviderSovereigntyPublic(SQLModel):
    """The API-facing shape of one attestation."""

    id: uuid.UUID
    organization_id: uuid.UUID | None = None
    provider: str
    model: str | None = None
    sovereignty_level: int
    hosted_in: str
    operator_jurisdiction: str
    model_origin: str | None = None
    vetted: bool
    evidence: dict[str, Any] | None = None
    created_at: Any
    updated_at: Any | None = None


__all__ = [
    "SOVEREIGNTY_LEVEL_MAX",
    "SOVEREIGNTY_LEVEL_MIN",
    "ProviderSovereignty",
    "ProviderSovereigntyPublic",
]
