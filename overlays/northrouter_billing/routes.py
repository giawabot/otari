"""Billing service, customer read, and hold-maintenance routes."""

import base64
import binascii
import hashlib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.api.deps import (
    get_config,
    get_db,
    verify_api_key_or_master_key,
    verify_master_key,
)
from gateway.core.config import GatewayConfig
from gateway.exceptions.organizations_exceptions import NotAuthorizedError, OrganizationNotFoundError
from gateway.models.api_keys import APIKey
from gateway.models.tenancy import User as TenancyUser
from gateway.services.tenancy import OrganizationService
from northrouter_billing.adapter import (
    NorthRouterBillingAdapter,
    expired_condition,
    held_amount,
    insert_if_absent,
    lock_org,
    post_ledger,
)
from northrouter_billing.models import NrGrantEvent, NrHold, NrLedgerEntry, NrOrg, NrStatusEvent, _money
from northrouter_billing.settings import MASTER, BillingSettings, current_settings

billing_router = APIRouter(prefix="/nr/billing", tags=["NorthRouter billing"])
logger = logging.getLogger(__name__)

_DECIMAL_AMOUNT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_INVALID_AMOUNT = "amount must be a finite non-zero decimal USD amount"
# How far ahead of this server's clock a status decision may claim to take effect.
_STATUS_CLOCK_SKEW = timedelta(minutes=5)
SWEEP_BATCH = 500
_HIDDEN_LEDGER_TYPES = ("hold", "release")

SettingsDep = Annotated[BillingSettings, Depends(current_settings)]


class GrantRequest(BaseModel):
    """A signed USD grant or reversal and its stable idempotency key."""

    model_config = ConfigDict(extra="forbid")

    amount: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=500, description="Shown to the organization in its ledger.")
    source_ref: str | None = Field(
        default=None, max_length=255, description="The billing service's own reference, such as a payment id."
    )


class StatusRequest(BaseModel):
    """An organization status decision from the billing service."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "suspended"]
    reason: str = Field(min_length=1, max_length=255)
    effective_at: AwareDatetime = Field(
        description="When the billing service made the decision. A decision older than the current one is ignored."
    )


def _person_uuid(user_id: str | None) -> uuid.UUID | None:
    if user_id is None:
        return None
    try:
        person = uuid.UUID(user_id)
    except ValueError:
        return None
    return person if str(person) == user_id else None


def _bearer(request: Request) -> str | None:
    scheme, separator, token = request.headers.get("Authorization", "").partition(" ")
    token = token.strip()
    return token if separator and scheme == "Bearer" and token else None


def _service_principal(request: Request, settings: BillingSettings) -> str | None:
    """The principal a billing service token authenticates as, or ``None`` when no token matches."""
    presented = _bearer(request)
    if presented is None:
        return None
    matched: str | None = None
    for principal, token in settings.service_tokens:
        # Compare against every token, so the time taken does not say which one matched.
        if secrets.compare_digest(presented.encode(), token.encode()) and matched is None:
            matched = principal
    return matched


async def require_billing_service(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    config: Annotated[GatewayConfig, Depends(get_config)],
    settings: SettingsDep,
) -> str:
    """Accept the billing service token, or Otari's master key in a header (never a dashboard session).

    Returns the principal: ``service``, ``service_previous`` or ``master``.
    """
    principal = _service_principal(request, settings)
    if principal is not None:
        return principal
    await verify_master_key(request, db, config, None)
    return MASTER


async def _resolve_oauth_billing_caller(
    api_key: APIKey | None,
    organizations: OrganizationService,
    settings: BillingSettings,
) -> tuple[TenancyUser, uuid.UUID | None]:
    """Verify that an API key was issued to the billing OAuth app for an active user.

    Trusts ``api_keys.oauth_client_id``, which only the app grant exchange
    writes, never ``metadata``, which the key's owner can edit.
    """
    if api_key is None or not api_key.self_service or settings.oauth_client_id is None:
        raise NotAuthorizedError
    if api_key.oauth_client_id != settings.oauth_client_id:
        raise NotAuthorizedError
    person_id = _person_uuid(api_key.user_id)
    if person_id is None:
        raise NotAuthorizedError
    user = await organizations.users.get(person_id)
    if user is None or not user.is_active:
        raise NotAuthorizedError
    key_org_id = await organizations.get_organization_id_for_workspace(api_key.workspace_id)
    return user, key_org_id


async def require_billing_manager(
    organization_id: uuid.UUID,
    auth: tuple[APIKey | None, bool],
    db: AsyncSession,
    settings: BillingSettings,
) -> uuid.UUID:
    """Authorize an organization owner/admin (via a billing OAuth app key) or the master key.

    Creates no ``NrOrg`` row, so unauthorized requests and reads leave no state behind.
    """
    organizations = OrganizationService(db, membership_listener=None)
    api_key, is_master = auth
    if is_master:
        if not await organizations.has_organization(organization_id):
            raise OrganizationNotFoundError(organization_id)
        return organization_id

    user, key_org_id = await _resolve_oauth_billing_caller(api_key, organizations, settings)
    if key_org_id != organization_id or not await organizations.user_has_active_membership(
        organization_id=organization_id, user_id=user.id
    ):
        raise OrganizationNotFoundError(organization_id)
    organization = await organizations.organizations.get(organization_id)
    if organization is None:
        raise OrganizationNotFoundError(organization_id)
    await organizations.require_active_organization_management_access(user=user, organization=organization)
    return organization_id


@dataclass(frozen=True)
class BillingReader:
    """Who is reading an organization's billing, and whether they see the service's internal fields."""

    organization_id: uuid.UUID
    internal: bool


async def require_billing_reader(
    organization_id: uuid.UUID,
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    config: Annotated[GatewayConfig, Depends(get_config)],
    settings: SettingsDep,
) -> BillingReader:
    """Accept the billing service token, the master key, or an organization owner/admin's billing app key."""
    if _service_principal(request, settings) is not None:
        if not await OrganizationService(db, membership_listener=None).has_organization(organization_id):
            raise OrganizationNotFoundError(organization_id)
        return BillingReader(organization_id=organization_id, internal=True)
    auth = await verify_api_key_or_master_key(request, db, config)
    await require_billing_manager(organization_id, auth, db, settings)
    return BillingReader(organization_id=organization_id, internal=auth[1])


def _grant_ledger_ref(idempotency_key: str) -> str:
    return f"grant:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"


def _key_digest(idempotency_key: str) -> str:
    """A short, non-reversible handle on an idempotency key, for logs."""
    return hashlib.sha256(idempotency_key.encode()).hexdigest()[:12]


def _grant_fingerprint(*, organization_id: uuid.UUID, amount: Decimal, reason: str, source_ref: str | None) -> str:
    payload = json.dumps(
        {
            "amount": str(amount),
            "organization_id": str(organization_id),
            "reason": reason,
            "source_ref": source_ref,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _grant_result(event: NrGrantEvent) -> dict[str, Any]:
    return {
        "granted": False,
        "replayed": True,
        "amount": str(event.amount),
        "ledger_entry_id": str(event.ledger_entry_id) if event.ledger_entry_id is not None else None,
        "balance_after": str(event.balance_after) if event.balance_after is not None else None,
    }


def _reject_fingerprint_mismatch(event: NrGrantEvent, fingerprint: str) -> None:
    if event.payload_fingerprint != fingerprint:
        raise HTTPException(status_code=409, detail="idempotency_key was already used with a different payload")


def _parse_amount(raw: str, settings: BillingSettings) -> Decimal:
    raw_amount = raw.strip()
    if not _DECIMAL_AMOUNT.fullmatch(raw_amount):
        raise HTTPException(status_code=422, detail=_INVALID_AMOUNT)
    try:
        amount = _money(Decimal(raw_amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=_INVALID_AMOUNT) from exc
    if amount == 0:
        raise HTTPException(status_code=422, detail=_INVALID_AMOUNT)
    if abs(amount) > settings.max_grant_usd:
        raise HTTPException(status_code=422, detail=f"amount must be at most {settings.max_grant_usd} USD either way")
    return amount


@billing_router.post("/orgs/{organization_id}/grants")
async def grant_funds(
    organization_id: uuid.UUID,
    body: GrantRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[str, Depends(require_billing_service)],
    settings: SettingsDep,
) -> dict[str, Any]:
    """Apply one signed credit or reversal, replay-safe by idempotency key."""
    amount = _parse_amount(body.amount, settings)
    if not body.idempotency_key.strip():
        raise HTTPException(status_code=422, detail="idempotency_key must not be blank")
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason must not be blank")
    source_ref = body.source_ref.strip() if body.source_ref is not None and body.source_ref.strip() else None

    fingerprint = _grant_fingerprint(
        organization_id=organization_id, amount=amount, reason=reason, source_ref=source_ref
    )
    existing = await db.get(NrGrantEvent, body.idempotency_key)
    if existing is not None:
        _reject_fingerprint_mismatch(existing, fingerprint)
        return _grant_result(existing)

    org = await NorthRouterBillingAdapter(db, settings).ensure_org(organization_id)
    event_values: dict[str, Any] = {
        "idempotency_key": body.idempotency_key,
        "payload_fingerprint": fingerprint,
        "source_ref": source_ref,
        "org_id": org.id,
        "amount": amount,
        "reason": reason,
        "principal": principal,
        "created_at": datetime.now(UTC),
    }
    if not await insert_if_absent(db, NrGrantEvent, event_values, ["idempotency_key"]):
        existing = await db.get(NrGrantEvent, body.idempotency_key, populate_existing=True)
        if existing is None:
            raise RuntimeError("Grant idempotency record was not visible after a key collision")
        _reject_fingerprint_mismatch(existing, fingerprint)
        return _grant_result(existing)

    ledger_type = "grant" if amount > 0 else "refund"
    ledger_ref = _grant_ledger_ref(body.idempotency_key)
    await post_ledger(db, org_id=org.id, entry_type=ledger_type, amount=amount, ref=ledger_ref, description=reason)
    ledger_entry = await db.scalar(
        select(NrLedgerEntry).where(NrLedgerEntry.type == ledger_type, NrLedgerEntry.ref == ledger_ref)
    )
    if ledger_entry is None:
        raise RuntimeError("Grant ledger entry was not created")
    await db.refresh(org)
    event = await db.get(NrGrantEvent, body.idempotency_key)
    if event is None:
        raise RuntimeError("Grant idempotency record disappeared during processing")
    event.ledger_entry_id = ledger_entry.id
    event.balance_after = org.balance
    balance_after = org.balance
    await db.commit()
    logger.info(
        "Billing %s of %s USD applied to organization %s by %s (key %s)",
        ledger_type,
        amount,
        organization_id,
        principal,
        _key_digest(body.idempotency_key),
    )
    return {
        "granted": True,
        "replayed": False,
        "amount": str(amount),
        "ledger_entry_id": str(ledger_entry.id),
        "balance_after": str(balance_after),
    }


def _utc(value: datetime) -> datetime:
    """``value`` as UTC; SQLite hands a timezone-aware column back without its zone."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _aware(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


@billing_router.post("/orgs/{organization_id}/status")
async def set_organization_status(
    organization_id: uuid.UUID,
    body: StatusRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    principal: Annotated[str, Depends(require_billing_service)],
    settings: SettingsDep,
) -> dict[str, Any]:
    """Apply a status decision unless a newer one is already in force, and record it either way."""
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason must not be blank")
    effective_at = body.effective_at.astimezone(UTC)
    if effective_at > datetime.now(UTC) + _STATUS_CLOCK_SKEW:
        raise HTTPException(status_code=422, detail="effective_at must not be in the future")

    org = await NorthRouterBillingAdapter(db, settings).ensure_org(organization_id)
    await lock_org(db, org.id)
    await db.refresh(org)
    current_effective_at = _aware(org.status_effective_at)
    applied = current_effective_at is None or effective_at > current_effective_at
    from_status = org.status
    if applied:
        org.status = body.status
        org.suspend_reason = reason if body.status == "suspended" else None
        org.status_effective_at = effective_at
    db.add(
        NrStatusEvent(
            org_id=org.id,
            from_status=from_status,
            to_status=body.status,
            reason=reason,
            effective_at=effective_at,
            applied=applied,
            principal=principal,
            created_at=datetime.now(UTC),
        )
    )
    status = org.status
    in_force_since = _aware(org.status_effective_at)
    await db.commit()
    logger.info(
        "Billing status %s for organization %s by %s: %s -> %s (%s)",
        "applied" if applied else "ignored as stale",
        organization_id,
        principal,
        from_status,
        body.status,
        effective_at.isoformat(),
    )
    return {
        "status": status,
        "applied": applied,
        "effective_at": in_force_since.isoformat() if in_force_since is not None else None,
    }


@billing_router.get("/context")
async def get_billing_context(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
    settings: SettingsDep,
) -> dict[str, str | None]:
    """Return the caller's organization ID and role for the NorthRouter billing site."""
    organizations = OrganizationService(db, membership_listener=None)
    api_key, is_master = auth
    if is_master:
        bootstrap_id = await organizations.bootstrap_operator_id()
        bootstrap_user = await organizations.users.get(bootstrap_id) if bootstrap_id is not None else None
        if bootstrap_user is not None:
            try:
                active_org = await organizations.get_active_organization_for_user(bootstrap_user)
            except NotAuthorizedError:
                active_org = None
            if active_org is not None:
                membership = await organizations.members.get_active_by_organization_and_user(
                    active_org.id, bootstrap_user.id
                )
                role = membership.role if membership is not None else "operator"
                return {"organization_id": str(active_org.id), "role": role}
        return {"organization_id": None, "role": "operator"}

    user, key_org_id = await _resolve_oauth_billing_caller(api_key, organizations, settings)
    if key_org_id is None:
        raise NotAuthorizedError
    membership = await organizations.members.get_active_by_organization_and_user(key_org_id, user.id)
    if membership is None:
        raise OrganizationNotFoundError(key_org_id)
    return {"organization_id": str(key_org_id), "role": membership.role}


@billing_router.post("/holds/sweep")
async def sweep_holds(
    db: Annotated[AsyncSession, Depends(get_db)],
    _principal: Annotated[str, Depends(require_billing_service)],
) -> dict[str, Any]:
    """Release up to ``SWEEP_BATCH`` expired open holds; ``more`` says whether to call again."""
    now = datetime.now(UTC)
    holds = list(
        await db.scalars(
            select(NrHold)
            .where(and_(NrHold.state == "open", expired_condition(now)))
            .order_by(NrHold.expires_at, NrHold.id)
            .limit(SWEEP_BATCH)
            .with_for_update(skip_locked=True)
        )
    )
    for hold in holds:
        hold.state = "released"
        hold.released_at = now
        await post_ledger(db, org_id=hold.org_id, entry_type="release", amount=hold.amount, ref=f"release:{hold.id}")
    await db.commit()
    return {"released": len(holds), "more": len(holds) == SWEEP_BATCH}


@billing_router.get("/orgs/{organization_id}/account")
async def get_account(
    reader: Annotated[BillingReader, Depends(require_billing_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    settings: SettingsDep,
) -> dict[str, Any]:
    """An organization's billing status and funds, as the request gates judge them."""
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == reader.organization_id))
    balance = _money(org.balance) if org is not None else _money(0)
    held = await held_amount(db, org.id, datetime.now(UTC)) if org is not None else _money(0)
    account: dict[str, Any] = {
        "organization_id": str(reader.organization_id),
        "status": org.status if org is not None else "active",
        "balance": str(balance),
        "held": str(held),
        "available": str(_money(balance - held)),
        "min_available": str(settings.min_available_usd),
        "concurrency_limit": settings.concurrency_limit,
    }
    if reader.internal:
        effective_at = _aware(org.status_effective_at) if org is not None else None
        account["suspend_reason"] = org.suspend_reason if org is not None else None
        account["status_effective_at"] = effective_at.isoformat() if effective_at is not None else None
    return account


def _encode_cursor(entry: NrLedgerEntry) -> str:
    raw = json.dumps([entry.created_at.isoformat(), str(entry.id)], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        created_at, entry_id = json.loads(raw)
        return datetime.fromisoformat(created_at), uuid.UUID(entry_id)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise HTTPException(status_code=422, detail="cursor is not one this endpoint returned") from exc


@billing_router.get("/orgs/{organization_id}/ledger")
async def get_ledger(
    reader: Annotated[BillingReader, Depends(require_billing_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict[str, Any]:
    """Read an organization's billing ledger, newest first, without hold/release noise.

    Pass ``next_cursor`` back as ``cursor`` for the next page. Internal
    references are shown only to the billing service and the master key.
    """
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == reader.organization_id))
    if org is None:
        return {"data": [], "next_cursor": None}
    query = select(NrLedgerEntry).where(NrLedgerEntry.org_id == org.id, NrLedgerEntry.type.not_in(_HIDDEN_LEDGER_TYPES))
    if cursor is not None:
        after_created_at, after_id = _decode_cursor(cursor)
        query = query.where(
            or_(
                NrLedgerEntry.created_at < after_created_at,
                and_(NrLedgerEntry.created_at == after_created_at, NrLedgerEntry.id < after_id),
            )
        )
    entries = list(
        await db.scalars(query.order_by(NrLedgerEntry.created_at.desc(), NrLedgerEntry.id.desc()).limit(limit + 1))
    )
    page, more = entries[:limit], len(entries) > limit
    data: list[dict[str, Any]] = []
    for entry in page:
        row: dict[str, Any] = {
            "id": str(entry.id),
            "type": entry.type,
            "amount": str(entry.amount),
            "description": entry.description,
            "created_at": _utc(entry.created_at).isoformat(),
        }
        if reader.internal:
            row["ref"] = entry.ref
        data.append(row)
    return {"data": data, "next_cursor": _encode_cursor(page[-1]) if more and page else None}
