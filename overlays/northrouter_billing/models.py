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


def _money(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, Decimal | int | str):
        raise TypeError("Billing amounts must be Decimal, int, or decimal text")
    return Decimal(value).quantize(MONEY_SCALE, rounding=ROUND_HALF_UP)


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
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )

    __table_args__ = (CheckConstraint("status IN ('active', 'suspended')", name="ck_nr_orgs_status"),)


class NrLedgerEntry(BillingBase):
    """Append-only money movement or hold lifecycle record."""

    __tablename__ = "nr_ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False)
    amount: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False)
    ref: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    api_key_id: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(String(500))
    metadata_json: Mapped[str | None] = mapped_column(String)

    __table_args__ = (
        CheckConstraint(
            "type IN ('grant', 'consume', 'hold', 'release', 'refund', 'invoice')", name="ck_nr_ledger_type"
        ),
        UniqueConstraint("type", "ref", name="uq_nr_ledger_type_ref"),
        Index("ix_nr_ledger_org_created", "org_id", "created_at"),
    )


class NrHold(BillingBase):
    """An amount-matched hold that participates in the caller's transaction."""

    __tablename__ = "nr_holds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ref: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint("state IN ('open', 'released')", name="ck_nr_holds_state"),
        Index("ix_nr_holds_open", "org_id", "state", "amount"),
    )


class NrETransfer(BillingBase):
    """Customer-submitted e-transfer awaiting operator review."""

    __tablename__ = "nr_e_transfers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), index=True)
    amount: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False)
    reference: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reviewed_by: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('pending', 'approved', 'rejected')", name="ck_nr_e_transfers_status"),
    )


class NrSubscription(BillingBase):
    """Stripe subscription and the credit value of its active billing period."""

    __tablename__ = "nr_subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("nr_orgs.id", ondelete="CASCADE"), index=True)
    stripe_subscription_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    plan: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    credit_amount: Mapped[Decimal] = mapped_column(UsdAmount(), nullable=False)


class NrWebhookEvent(BillingBase):
    """Stripe event IDs make webhook delivery replay-safe."""

    __tablename__ = "nr_webhook_events"

    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(255), nullable=False)
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(UTC))
    succeeded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
