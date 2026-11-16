"""Overlay-owned billing tables, kept outside Otari's migration metadata."""

import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

MONEY_SCALE = Decimal("0.000001")
# The largest magnitude ``Numeric(18, 6)`` holds.
MONEY_MAX = Decimal("999999999999.999999")
LEDGER_TYPES = ("grant", "consume", "hold", "release", "refund")
PAID_IN_TYPES = ("grant", "refund")
ORG_STATUSES = ("active", "suspended")


def _money(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError("Billing amounts must be Decimal, int, or decimal text")
    amount = Decimal(value)
    if not amount.is_finite():
        raise ValueError("Billing amounts must be finite")
    return amount.quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class UsdAmount(TypeDecorator[Decimal]):
    """Store USD as a fixed-scale NUMERIC and reject binary floating-point input."""

    impl = Numeric(18, 6)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Decimal | None:
        return None if value is None else _money(value)


class BillingBase(DeclarativeBase):
    """Declarative registry owned by this overlay's Alembic chain."""


class NrOrg(BillingBase):
    """Billing state linked to an Otari organization UUID."""

    __tablename__ = "nr_orgs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    otari_org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    suspend_reason: Mapped[str | None] = mapped_column(String(255))
    # When the billing service decided the current status; an older decision is ignored.
    status_effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    balance: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False, default=lambda: Decimal("0.000000"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (CheckConstraint(_in_list("status", ORG_STATUSES), name="ck_nr_orgs_status"),)


class NrLedgerEntry(BillingBase):
    """Append-only money movement or hold lifecycle record."""

    __tablename__ = "nr_ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    api_key_id: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(_in_list("type", LEDGER_TYPES), name="ck_nr_ledger_type"),
        UniqueConstraint("type", "ref", name="uq_nr_ledger_type_ref"),
        Index("ix_nr_ledger_org_created", "org_id", "created_at", "id"),
    )


class NrHold(BillingBase):
    """An amount-matched hold that participates in the caller's transaction."""

    __tablename__ = "nr_holds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ref: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint("state IN ('open', 'released')", name="ck_nr_holds_state"),
        Index("ix_nr_holds_open", "org_id", "state", "amount"),
        Index("ix_nr_holds_state_expires", "state", "expires_at"),
    )


class NrGrantEvent(BillingBase):
    """Idempotency record and original result for a machine-issued grant."""

    __tablename__ = "nr_grant_events"

    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(String(255))
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    # Which credential authorized the grant: ``service``, ``service_previous`` or ``master``.
    principal: Mapped[str] = mapped_column(String(32), nullable=False)
    ledger_entry_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    balance_after: Mapped[Decimal | None] = mapped_column(UsdAmount())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)


class NrStatusEvent(BillingBase):
    """Audit record of every status decision received, whether or not it was applied."""

    __tablename__ = "nr_status_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), nullable=False)
    from_status: Mapped[str] = mapped_column(String(16), nullable=False)
    to_status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied: Mapped[bool] = mapped_column(Boolean, nullable=False)
    principal: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utcnow)

    __table_args__ = (Index("ix_nr_status_events_org_created", "org_id", "created_at"),)
