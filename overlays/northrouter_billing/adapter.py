"""Session-scoped implementation of Otari's deployment-paid billing port."""

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.ports.billing_port import BilledModel, BillingPort, InsufficientFundsError, ToolUse
from northrouter_billing.models import NrHold, NrLedgerEntry, NrOrg, NrSubscription, _money

_PAID_IN_TYPES = ("grant", "refund", "invoice")
# Passthrough commits the hold before provider dispatch, so a crash before
# release must not strand funds forever.
_HOLD_TTL = timedelta(hours=1)
_SUSPENDED_MESSAGE = "This organization is suspended; contact billing@northrouter.ca to restore service."


async def _insert_if_absent(
    session: AsyncSession, model: type, values: dict[str, object], conflict_columns: list[str]
) -> None:
    """Insert a row unless a concurrent writer already claimed the conflict key.

    ``on_conflict_do_nothing`` is a dialect-specific construct, so the sqlite
    and postgresql insert variants are chosen from the bound dialect; other
    dialects fall back to a plain ORM insert, which is safe under the
    single-writer guarantees those deployments make.
    """
    dialect = session.get_bind().dialect.name
    if dialect in ("sqlite", "postgresql"):
        insert = sqlite_insert if dialect == "sqlite" else pg_insert
        statement = insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
        await session.execute(statement)
    else:
        session.add(model(**values))
        await session.flush()


class NorthRouterBillingAdapter(BillingPort):
    """Billing adapter whose writes remain part of Otari's request transaction."""

    def __init__(self, session: AsyncSession | None) -> None:
        self.session = session

    def _session(self) -> AsyncSession:
        if self.session is None:
            raise RuntimeError("NorthRouterBillingAdapter requires a request database session")
        return self.session

    async def _org(self, organization_id: uuid.UUID) -> NrOrg:
        session = self._session()
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is not None:
            return org

        await _insert_if_absent(
            session,
            NrOrg,
            {"id": uuid.uuid4(), "otari_org_id": organization_id, "status": "active"},
            ["otari_org_id"],
        )
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is None:
            raise RuntimeError("Organization insert did not produce a billing row")
        return org

    async def _amounts(self, org_id: uuid.UUID) -> tuple[Decimal, Decimal, Decimal]:
        session = self._session()
        paid_in = await session.scalar(
            select(func.coalesce(func.sum(NrLedgerEntry.amount), 0)).where(
                NrLedgerEntry.org_id == org_id, NrLedgerEntry.type.in_(_PAID_IN_TYPES)
            )
        )
        consumed = await session.scalar(
            select(func.coalesce(func.sum(NrLedgerEntry.amount), 0)).where(
                NrLedgerEntry.org_id == org_id, NrLedgerEntry.type == "consume"
            )
        )
        held = await session.scalar(
            select(func.coalesce(func.sum(NrHold.amount), 0)).where(
                NrHold.org_id == org_id,
                NrHold.state == "open",
                or_(NrHold.expires_at.is_(None), NrHold.expires_at > datetime.now(UTC)),
            )
        )
        return _money(paid_in or 0), _money(consumed or 0), _money(held or 0)

    async def _check_org(self, organization_id: uuid.UUID) -> tuple[NrOrg, Decimal]:
        org = await self._org(organization_id)
        paid_in, consumed, _ = await self._amounts(org.id)
        available = _money(paid_in - consumed)
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if available <= 0:
            raise InsufficientFundsError(available)
        return org, available

    async def apply_due_credit(self, *, organization_id: uuid.UUID) -> None:
        org = await self._org(organization_id)
        session = self._session()
        subscriptions = await session.scalars(
            select(NrSubscription).where(
                NrSubscription.org_id == org.id,
                NrSubscription.status.in_(("active", "trialing")),
            )
        )
        for subscription in subscriptions:
            period_end = _as_utc(subscription.current_period_end)
            ref = f"subscription:{subscription.stripe_subscription_id}:{period_end.isoformat()}"
            exists = await session.scalar(
                select(NrLedgerEntry.id).where(NrLedgerEntry.type == "grant", NrLedgerEntry.ref == ref)
            )
            if exists is None:
                await _insert_if_absent(
                    session,
                    NrLedgerEntry,
                    {
                        "org_id": org.id,
                        "type": "grant",
                        "amount": subscription.credit_amount,
                        "ref": ref,
                        "description": f"Subscription credit for {subscription.plan}",
                    },
                    ["type", "ref"],
                )
        await session.flush()

    async def require_funds_on_deposit(
        self,
        *,
        organization_id: uuid.UUID,
        billed: BilledModel | None,
        tools: frozenset[str],
        actor_user_id: uuid.UUID | None,
    ) -> None:
        del billed, tools, actor_user_id
        await self._check_org(organization_id)

    async def has_funds_on_deposit(
        self,
        *,
        organization_id: uuid.UUID,
        billed: BilledModel | None,
        tools: frozenset[str],
        actor_user_id: uuid.UUID | None,
    ) -> bool:
        del billed, tools, actor_user_id
        session = self._session()
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is None or org.status != "active":
            return False
        paid_in, consumed, _ = await self._amounts(org.id)
        owed = Decimal(0)
        subscriptions = await session.scalars(
            select(NrSubscription).where(
                NrSubscription.org_id == org.id,
                NrSubscription.status.in_(("active", "trialing")),
            )
        )
        for subscription in subscriptions:
            period_end = _as_utc(subscription.current_period_end)
            ref = f"subscription:{subscription.stripe_subscription_id}:{period_end.isoformat()}"
            exists = await session.scalar(
                select(NrLedgerEntry.id).where(NrLedgerEntry.type == "grant", NrLedgerEntry.ref == ref)
            )
            if exists is None:
                owed += subscription.credit_amount
        return paid_in - consumed + owed > 0

    async def concurrency_limit(
        self, *, organization_id: uuid.UUID, billed: BilledModel, actor_user_id: uuid.UUID | None
    ) -> int | None:
        del organization_id, billed, actor_user_id
        return None

    async def require_unheld_funds(self, *, organization_id: uuid.UUID) -> None:
        org = await self._org(organization_id)
        paid_in, consumed, held = await self._amounts(org.id)
        available = _money(paid_in - consumed - held)
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if available <= 0:
            raise InsufficientFundsError(available)

    async def hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        amount = _money(amount)
        if amount <= 0:
            return
        org = await self._org(organization_id)
        # The no-op timestamp update obtains the organization row lock on
        # PostgreSQL and the SQLite write lock before the balance is checked.
        session = self._session()
        await session.execute(update(NrOrg).where(NrOrg.id == org.id).values(updated_at=datetime.now(UTC)))
        paid_in, consumed, held = await self._amounts(org.id)
        available = _money(paid_in - consumed - held)
        if org.status != "active" or available < amount:
            raise InsufficientFundsError(available)
        hold = NrHold(
            org_id=org.id,
            amount=amount,
            state="open",
            expires_at=datetime.now(UTC) + _HOLD_TTL,
        )
        session.add(hold)
        await session.flush()
        hold.ref = f"hold:{hold.id}"
        session.add(NrLedgerEntry(org_id=org.id, type="hold", amount=amount, ref=f"hold:{hold.id}"))

    async def release_hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        amount = _money(amount)
        org = await self._org(organization_id)
        session = self._session()
        hold = await session.scalar(
            select(NrHold)
            .where(
                NrHold.org_id == org.id,
                NrHold.amount == amount,
                NrHold.state == "open",
                or_(NrHold.expires_at.is_(None), NrHold.expires_at > datetime.now(UTC)),
            )
            .order_by(NrHold.created_at, NrHold.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if hold is None:
            raise ValueError("No open hold matches this amount")
        hold.state = "released"
        hold.released_at = datetime.now(UTC)
        session.add(NrLedgerEntry(org_id=org.id, type="release", amount=amount, ref=f"release:{hold.id}"))
        await session.flush()

    async def charge(
        self,
        *,
        organization_id: uuid.UUID,
        amount: Decimal,
        billed: BilledModel | None,
        tools: Mapping[str, ToolUse],
        description: str | None = None,
        actor_user_id: uuid.UUID | None = None,
        api_key_id: str | None = None,
    ) -> None:
        if amount < 0:
            raise ValueError("A charge amount cannot be negative")
        org = await self._org(organization_id)
        audit = {
            "billed": (
                {
                    "provider": billed.provider,
                    "model": billed.model,
                    "open_weights": billed.open_weights,
                }
                if billed is not None
                else None
            ),
            "tools": {
                name: {"calls": usage.calls, "amount": str(_money(usage.amount))} for name, usage in tools.items()
            },
        }
        self._session().add(
            NrLedgerEntry(
                org_id=org.id,
                type="consume",
                amount=_money(amount),
                ref=f"charge:{uuid.uuid4()}",
                description=description,
                actor_user_id=actor_user_id,
                api_key_id=api_key_id,
                metadata_json=json.dumps(audit, sort_keys=True, separators=(",", ":")),
            )
        )
        await self._session().flush()


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
