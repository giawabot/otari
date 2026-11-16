"""Session-scoped implementation of Otari's deployment-paid billing port."""

import json
import logging
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from gateway.exceptions.organizations_exceptions import OrganizationNotFoundError
from gateway.ports.billing_port import BilledModel, BillingPort, InsufficientFundsError, ToolUse
from gateway.services.tenancy import OrganizationService
from northrouter_billing.models import NrHold, NrLedgerEntry, NrOrg, _money

_PAID_IN_TYPES = ("grant", "refund", "invoice", "dispute")
# Passthrough commits the hold before provider dispatch, so a crash before
# release must not strand funds forever.
_HOLD_TTL = timedelta(hours=1)
_SUSPENDED_MESSAGE = "This organization is suspended; sign in at northrouter.ca to review your account details."
logger = logging.getLogger(__name__)


def _expired_condition(now: datetime) -> ColumnElement[bool]:
    """Predicate matching open holds whose expiration window has elapsed."""
    return or_(
        and_(NrHold.expires_at.is_(None), NrHold.created_at <= now - _HOLD_TTL),
        NrHold.expires_at <= now,
    )


def get_org_concurrency_limit() -> int | None:
    """Read and validate NORTHROUTER_ORG_CONCURRENCY_LIMIT from the environment."""
    raw = os.getenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "").strip()
    if not raw:
        return None
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("NORTHROUTER_ORG_CONCURRENCY_LIMIT must be a positive integer") from exc
    if limit < 1:
        raise ValueError("NORTHROUTER_ORG_CONCURRENCY_LIMIT must be a positive integer")
    return limit


def get_min_available_usd() -> Decimal:
    """Read and validate NORTHROUTER_MIN_AVAILABLE_USD from the environment."""
    raw = os.getenv("NORTHROUTER_MIN_AVAILABLE_USD", "0").strip()
    if not raw:
        raw = "0"
    try:
        minimum = _money(Decimal(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("NORTHROUTER_MIN_AVAILABLE_USD must be a non-negative finite decimal") from exc
    if minimum < Decimal("0"):
        raise ValueError("NORTHROUTER_MIN_AVAILABLE_USD must be a non-negative finite decimal")
    return minimum


def validate_billing_env() -> None:
    """Validate overlay configuration variables during startup registration."""
    service_token = os.getenv("NORTHROUTER_BILLING_SERVICE_TOKEN", "").strip()
    if not service_token:
        raise ValueError("NORTHROUTER_BILLING_SERVICE_TOKEN must be set and non-blank")
    get_org_concurrency_limit()
    get_min_available_usd()


def _has_minimum_available(available: Decimal) -> bool:
    minimum = get_min_available_usd()
    return available > Decimal("0") and available >= minimum


async def _insert_if_absent(
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


async def _post_ledger(
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
    now = datetime.now(UTC)
    await session.execute(update(NrOrg).where(NrOrg.id == org_id).values(updated_at=now))
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
    inserted = await _insert_if_absent(session, NrLedgerEntry, values, ["type", "ref"])
    if not inserted:
        return False

    if entry_type in _PAID_IN_TYPES:
        delta = normalized_amount
    elif entry_type == "consume":
        delta = _money(-normalized_amount)
    else:
        delta = Decimal("0.000000")

    if delta != Decimal("0.000000"):
        await session.execute(
            update(NrOrg).where(NrOrg.id == org_id).values(balance=NrOrg.balance + delta, updated_at=now)
        )
        org_in_session = await session.get(NrOrg, org_id)
        if org_in_session is not None:
            await session.refresh(org_in_session)
    return True


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

        if not await OrganizationService(session, membership_listener=None).has_organization(organization_id):
            raise OrganizationNotFoundError(organization_id)

        await _insert_if_absent(
            session,
            NrOrg,
            {
                "id": uuid.uuid4(),
                "otari_org_id": organization_id,
                "status": "active",
                "balance": Decimal("0.000000"),
            },
            ["otari_org_id"],
        )
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is None:
            raise RuntimeError("Organization insert did not produce a billing row")
        return org

    async def _amounts(self, org_id: uuid.UUID) -> tuple[Decimal, Decimal]:
        session = self._session()
        balance = await session.scalar(select(NrOrg.balance).where(NrOrg.id == org_id))
        now = datetime.now(UTC)
        held = await session.scalar(
            select(func.coalesce(func.sum(NrHold.amount), 0)).where(
                NrHold.org_id == org_id,
                NrHold.state == "open",
                ~_expired_condition(now),
            )
        )
        return _money(balance or 0), _money(held or 0)

    async def _check_org(self, organization_id: uuid.UUID) -> tuple[NrOrg, Decimal]:
        session = self._session()
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is None:
            raise InsufficientFundsError(_money(0))
        balance, _ = await self._amounts(org.id)
        available = _money(balance)
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if not _has_minimum_available(available):
            raise InsufficientFundsError(available)
        return org, available

    async def apply_due_credit(self, *, organization_id: uuid.UUID) -> None:
        """No-op: subscription credits are granted strictly after paid invoices."""
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
        balance, _ = await self._amounts(org.id)
        return _has_minimum_available(balance)

    async def concurrency_limit(
        self, *, organization_id: uuid.UUID, billed: BilledModel, actor_user_id: uuid.UUID | None
    ) -> int | None:
        del organization_id, billed, actor_user_id
        return get_org_concurrency_limit()

    async def require_unheld_funds(self, *, organization_id: uuid.UUID) -> None:
        session = self._session()
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is None:
            raise InsufficientFundsError(_money(0))
        balance, held = await self._amounts(org.id)
        available = _money(balance - held)
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if not _has_minimum_available(available):
            raise InsufficientFundsError(available)

    async def hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        amount = _money(amount)
        if amount <= 0:
            return
        session = self._session()
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is None:
            raise InsufficientFundsError(_money(0))
        # The timestamp update obtains the organization row lock on PostgreSQL
        # and the SQLite write lock before the balance is checked.
        await session.execute(update(NrOrg).where(NrOrg.id == org.id).values(updated_at=datetime.now(UTC)))
        await session.refresh(org)
        balance, held = await self._amounts(org.id)
        available = _money(balance - held)
        if org.status != "active":
            raise InsufficientFundsError(available, _SUSPENDED_MESSAGE)
        if available < amount:
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
        await _post_ledger(
            session,
            org_id=org.id,
            entry_type="hold",
            amount=amount,
            ref=f"hold:{hold.id}",
        )

    async def release_hold(self, *, organization_id: uuid.UUID, amount: Decimal) -> None:
        amount = _money(amount)
        if amount <= 0:
            return
        session = self._session()
        org = await session.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
        if org is None:
            logger.warning("No billing organization found for %s when releasing hold of %s", organization_id, amount)
            return
        now = datetime.now(UTC)
        hold = await session.scalar(
            select(NrHold)
            .where(
                NrHold.org_id == org.id,
                NrHold.amount == amount,
                NrHold.state == "open",
            )
            .order_by(case((_expired_condition(now), 1), else_=0), NrHold.created_at, NrHold.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if hold is None:
            logger.warning("No open hold matches amount %s for organization %s", amount, organization_id)
            return
        hold.state = "released"
        hold.released_at = datetime.now(UTC)
        await _post_ledger(
            session,
            org_id=org.id,
            entry_type="release",
            amount=amount,
            ref=f"release:{hold.id}",
        )
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
        await _post_ledger(
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
