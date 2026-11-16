"""Session-scoped implementation of Otari's deployment-paid billing port."""

import json
import logging
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from gateway.exceptions.organizations_exceptions import OrganizationNotFoundError
from gateway.ports.billing_port import BilledModel, BillingPort, InsufficientFundsError, ToolUse
from gateway.services.tenancy import OrganizationService
from northrouter_billing.models import PAID_IN_TYPES, NrHold, NrLedgerEntry, NrOrg, _money
from northrouter_billing.settings import BillingSettings, current_settings

# Passthrough commits the hold before provider dispatch, so a crash before
# release must not strand funds forever.
HOLD_TTL = timedelta(hours=1)
_SUSPENDED_MESSAGE = "This organization is suspended; sign in at northrouter.ca to review your account details."
_ZERO = Decimal("0.000000")
logger = logging.getLogger(__name__)


def expired_condition(now: datetime) -> ColumnElement[bool]:
    """Predicate matching holds whose expiration time has passed."""
    return NrHold.expires_at <= now


def _has_minimum_available(available: Decimal, settings: BillingSettings) -> bool:
    return available > 0 and available >= settings.min_available_usd


async def insert_if_absent(
    session: AsyncSession, model: Any, values: dict[str, Any], conflict_columns: list[str]
) -> bool:
    """Insert a row unless a concurrent writer already claimed the conflict key.

    ``on_conflict_do_nothing`` is dialect-specific, so the SQLite and PostgreSQL
    insert constructs are chosen from the bound dialect; other dialects check
    before inserting. Returns True only when this call inserted the row.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "sqlite":
        sqlite_stmt = sqlite_insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
        result = await session.execute(sqlite_stmt)
        return bool(getattr(result, "rowcount", 0) == 1)
    if dialect == "postgresql":
        pg_stmt = pg_insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
        result = await session.execute(pg_stmt)
        return bool(getattr(result, "rowcount", 0) == 1)
    existing = await session.scalar(
        select(model).where(*(getattr(model, name) == values[name] for name in conflict_columns))
    )
    if existing is not None:
        return False
    session.add(model(**values))
    await session.flush()
    return True


async def lock_org(session: AsyncSession, org_id: uuid.UUID) -> None:
    """Take the organization's row lock (PostgreSQL) or the write lock (SQLite) until the transaction ends."""
    await session.execute(update(NrOrg).where(NrOrg.id == org_id).values(updated_at=datetime.now(UTC)))


async def post_ledger(
    session: AsyncSession,
    *,
    org_id: uuid.UUID,
    entry_type: str,
    amount: Decimal,
    ref: str,
    description: str | None = None,
    actor_user_id: uuid.UUID | None = None,
    api_key_id: str | None = None,
    metadata_json: str | None = None,
) -> bool:
    """Insert a ledger entry and update ``nr_orgs.balance`` atomically under the org row lock.

    The balance is updated only when the ledger insert actually lands
    (``rowcount == 1``), so idempotent replays never double-apply a credit or
    debit.
    """
    await session.flush()
    await lock_org(session, org_id)
    now = datetime.now(UTC)
    normalized_amount = _money(amount)
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "org_id": org_id,
        "type": entry_type,
        "amount": normalized_amount,
        "ref": ref,
        "created_at": now,
        "actor_user_id": actor_user_id,
        "api_key_id": api_key_id,
        "description": description,
        "metadata_json": metadata_json,
    }
    if not await insert_if_absent(session, NrLedgerEntry, values, ["type", "ref"]):
        return False

    if entry_type in PAID_IN_TYPES:
        delta = normalized_amount
    elif entry_type == "consume":
        delta = -normalized_amount
    else:
        delta = _ZERO

    if delta != _ZERO:
        await session.execute(
            update(NrOrg).where(NrOrg.id == org_id).values(balance=NrOrg.balance + delta, updated_at=now)
        )
        org_in_session = await session.get(NrOrg, org_id)
        if org_in_session is not None:
            await session.refresh(org_in_session)
    return True


async def held_amount(session: AsyncSession, org_id: uuid.UUID, now: datetime) -> Decimal:
    """What the organization's unexpired open holds claim."""
    held = await session.scalar(
        select(func.coalesce(func.sum(NrHold.amount), 0)).where(
            NrHold.org_id == org_id,
            NrHold.state == "open",
            ~expired_condition(now),
        )
    )
    return _money(held or 0)


class NorthRouterBillingAdapter(BillingPort):
    """Billing adapter whose writes remain part of Otari's request transaction."""

    def __init__(self, session: AsyncSession | None, settings: BillingSettings | None = None) -> None:
        self.session = session
        self._settings = settings

    @property
    def settings(self) -> BillingSettings:
        return self._settings if self._settings is not None else current_settings()

    def _session(self) -> AsyncSession:
        if self.session is None:
            raise RuntimeError("NorthRouterBillingAdapter requires a request database session")
        return self.session

    async def _find(self, organization_id: uuid.UUID) -> NrOrg | None:
        # Re-read the row even when the session holds it, so a gate never decides on a stale balance or status.
        return await self._session().scalar(
            select(NrOrg).where(NrOrg.otari_org_id == organization_id).execution_options(populate_existing=True)
        )

    async def ensure_org(self, organization_id: uuid.UUID) -> NrOrg:
        """The organization's billing row, created on first use for an Otari organization that exists."""
        session = self._session()
        org = await self._find(organization_id)
        if org is not None:
            return org

        if not await OrganizationService(session, membership_listener=None).has_organization(organization_id):
            raise OrganizationNotFoundError(organization_id)

        await insert_if_absent(
            session,
            NrOrg,
            {
                "id": uuid.uuid4(),
                "otari_org_id": organization_id,
                "status": "active",
                "balance": _ZERO,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            },
            ["otari_org_id"],
        )
        org = await self._find(organization_id)
        if org is None:
            raise RuntimeError("Organization insert did not produce a billing row")
        return org

    async def apply_due_credit(self, *, organization_id: uuid.UUID) -> None:
        """No-op: credit is granted by the billing service, never accrued here."""
        del organization_id

    async def require_funds_on_deposit(
        self,
        *,
        organization_id: uuid.UUID,
        billed: BilledModel | None,
        tools: frozenset[str],
        actor_user_id: uuid.UUID | None,
    ) -> None:
        del billed, tools, actor_user_id
        org = await self._find(organization_id)
        if org is None:
            raise InsufficientFundsError(_ZERO)
        available = _money(org.balance)
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if not _has_minimum_available(available, self.settings):
            raise InsufficientFundsError(available)

    async def has_funds_on_deposit(
        self,
        *,
        organization_id: uuid.UUID,
        billed: BilledModel | None,
        tools: frozenset[str],
        actor_user_id: uuid.UUID | None,
    ) -> bool:
        del billed, tools, actor_user_id
        org = await self._find(organization_id)
        if org is None or org.status != "active":
            return False
        return _has_minimum_available(_money(org.balance), self.settings)

    async def concurrency_limit(
        self, *, organization_id: uuid.UUID, billed: BilledModel, actor_user_id: uuid.UUID | None
    ) -> int | None:
        del organization_id, billed, actor_user_id
        return self.settings.concurrency_limit

    async def require_unheld_funds(self, *, organization_id: uuid.UUID) -> None:
        session = self._session()
        org = await self._find(organization_id)
        if org is None:
            raise InsufficientFundsError(_ZERO)
        available = _money(org.balance - await held_amount(session, org.id, datetime.now(UTC)))
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if not _has_minimum_available(available, self.settings):
            raise InsufficientFundsError(available)

    async def hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        amount = _money(amount)
        if amount <= 0:
            return
        session = self._session()
        org = await self._find(organization_id)
        if org is None:
            raise InsufficientFundsError(_ZERO)
        # Lock before reading the balance, so overlapping holds decide one at a time.
        await lock_org(session, org.id)
        await session.refresh(org)
        now = datetime.now(UTC)
        available = _money(org.balance - await held_amount(session, org.id, now))
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if available < amount:
            raise InsufficientFundsError(available)
        hold = NrHold(org_id=org.id, amount=amount, state="open", created_at=now, expires_at=now + HOLD_TTL)
        session.add(hold)
        await session.flush()
        hold.ref = f"hold:{hold.id}"
        await post_ledger(session, org_id=org.id, entry_type="hold", amount=amount, ref=hold.ref)

    async def release_hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        """Release one open hold of ``amount``, preferring one that has not expired.

        The port names a hold by its amount alone. If the sweep already released
        this caller's expired hold, the release lands on another open hold of the
        same amount, which ends that hold early; totals stay right, and the TTL is
        far longer than any call that takes a hold.
        """
        amount = _money(amount)
        if amount <= 0:
            return
        session = self._session()
        org = await self._find(organization_id)
        if org is None:
            logger.warning("No billing organization found for %s when releasing hold of %s", organization_id, amount)
            return
        now = datetime.now(UTC)
        hold = await session.scalar(
            select(NrHold)
            .where(NrHold.org_id == org.id, NrHold.amount == amount, NrHold.state == "open")
            .order_by(case((expired_condition(now), 1), else_=0), NrHold.created_at, NrHold.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if hold is None:
            logger.warning("No open hold matches amount %s for organization %s", amount, organization_id)
            return
        hold.state = "released"
        hold.released_at = now
        await post_ledger(session, org_id=org.id, entry_type="release", amount=amount, ref=f"release:{hold.id}")
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
        amount = _money(amount)
        if amount < 0:
            raise ValueError("A charge amount cannot be negative")
        org = await self.ensure_org(organization_id)
        audit = {
            "billed": (
                {"provider": billed.provider, "model": billed.model, "open_weights": billed.open_weights}
                if billed is not None
                else None
            ),
            "tools": {
                name: {"calls": usage.calls, "amount": str(_money(usage.amount))} for name, usage in tools.items()
            },
        }
        await post_ledger(
            self._session(),
            org_id=org.id,
            entry_type="consume",
            amount=amount,
            ref=f"charge:{uuid.uuid4()}",
            description=description,
            actor_user_id=actor_user_id,
            api_key_id=api_key_id,
            metadata_json=json.dumps(audit, sort_keys=True, separators=(",", ":")),
        )
        await self._session().flush()
