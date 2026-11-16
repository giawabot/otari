"""Billing read, grant, status, and hold-maintenance routes."""

import hashlib
import json
import os
import re
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, select
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
    _expired_condition,
    _insert_if_absent,
    _post_ledger,
)
from northrouter_billing.models import NrGrantEvent, NrHold, NrLedgerEntry, NrOrg, _money

billing_router = APIRouter(prefix="/nr/billing", tags=["NorthRouter billing"])
_DECIMAL_AMOUNT = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


class GrantRequest(BaseModel):
    """A signed USD grant or reversal and its stable idempotency key."""

    model_config = ConfigDict(extra="forbid")

    amount: str = Field(min_length=1, max_length=64)
    idempotency_key: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=1, max_length=500)


class StatusRequest(BaseModel):
    """An organization status change from the billing service."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["active", "suspended"]
    reason: str = Field(min_length=1, max_length=64)


def _person_uuid(user_id: str | None) -> uuid.UUID | None:
    if user_id is None:
        return None
    try:
        person = uuid.UUID(user_id)
    except ValueError:
        return None
    return person if str(person) == user_id else None


async def _resolve_oauth_billing_caller(
    api_key: APIKey | None,
    organizations: OrganizationService,
) -> tuple[TenancyUser, uuid.UUID | None]:
    """Verify that an API key is a self-service OAuth app key owned by an active user."""
    if api_key is None or not api_key.self_service:
        raise NotAuthorizedError
    expected_client_id = os.getenv("NORTHROUTER_BILLING_OAUTH_CLIENT_ID", "").strip()
    metadata = api_key.metadata_ or {}
    if not expected_client_id or metadata.get("oauth_client_id") != expected_client_id:
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
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> uuid.UUID:
    """Authorize an organization owner/admin (via billing OAuth key) or the master key.

    Does not create an ``NrOrg`` row so unauthorized requests and GET routes
    leave no database state behind.
    """
    organizations = OrganizationService(db, membership_listener=None)
    api_key, is_master = auth
    if is_master:
        if not await organizations.has_organization(organization_id):
            raise OrganizationNotFoundError(organization_id)
        return organization_id

    user, key_org_id = await _resolve_oauth_billing_caller(api_key, organizations)
    if key_org_id != organization_id or not await organizations.user_has_active_membership(
        organization_id=organization_id, user_id=user.id
    ):
        raise OrganizationNotFoundError(organization_id)
    organization = await organizations.organizations.get(organization_id)
    if organization is None:
        raise OrganizationNotFoundError(organization_id)
    await organizations.require_active_organization_management_access(user=user, organization=organization)
    return organization_id


async def _ensure_org(db: AsyncSession, organization_id: uuid.UUID) -> NrOrg:
    return await NorthRouterBillingAdapter(db)._org(organization_id)


async def require_billing_service_or_master_key(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    config: Annotated[GatewayConfig, Depends(get_config)],
) -> None:
    """Accept the billing service bearer token or Otari's master-key credential."""
    expected_token = os.getenv("NORTHROUTER_BILLING_SERVICE_TOKEN", "").strip()
    authorization = request.headers.get("Authorization", "")
    scheme, separator, presented_token = authorization.partition(" ")
    if (
        expected_token
        and separator
        and scheme == "Bearer"
        and presented_token.strip()
        and secrets.compare_digest(presented_token.strip().encode(), expected_token.encode())
    ):
        return
    await verify_master_key(request, db, config, None)


def _grant_ledger_ref(idempotency_key: str) -> str:
    return f"grant:{hashlib.sha256(idempotency_key.encode()).hexdigest()}"


def _grant_fingerprint(*, organization_id: uuid.UUID, amount: Decimal, reason: str) -> str:
    payload = json.dumps(
        {"amount": str(amount), "organization_id": str(organization_id), "reason": reason},
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


@billing_router.post("/orgs/{organization_id}/grants")
async def grant_funds(
    organization_id: uuid.UUID,
    body: GrantRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _auth: Annotated[None, Depends(require_billing_service_or_master_key)],
) -> dict[str, Any]:
    """Apply one signed credit or reversal, replay-safe by idempotency key."""
    raw_amount = body.amount.strip()
    if not _DECIMAL_AMOUNT.fullmatch(raw_amount):
        raise HTTPException(status_code=422, detail="amount must be a finite non-zero decimal USD amount")
    try:
        amount = _money(Decimal(raw_amount))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="amount must be a finite non-zero decimal USD amount") from exc
    if amount == 0:
        raise HTTPException(status_code=422, detail="amount must be a finite non-zero decimal USD amount")
    if not body.idempotency_key.strip():
        raise HTTPException(status_code=422, detail="idempotency_key must not be blank")
    reason = body.reason.strip()
    if not reason:
        raise HTTPException(status_code=422, detail="reason must not be blank")

    fingerprint = _grant_fingerprint(organization_id=organization_id, amount=amount, reason=reason)
    existing = await db.get(NrGrantEvent, body.idempotency_key)
    if existing is not None:
        _reject_fingerprint_mismatch(existing, fingerprint)
        return _grant_result(existing)

    org = await _ensure_org(db, organization_id)
    event_values: dict[str, Any] = {
        "idempotency_key": body.idempotency_key,
        "payload_fingerprint": fingerprint,
        "org_id": org.id,
        "amount": amount,
        "reason": reason,
        "created_at": datetime.now(UTC),
    }
    inserted = await _insert_if_absent(db, NrGrantEvent, event_values, ["idempotency_key"])
    if not inserted:
        await db.refresh(org)
        existing = await db.get(NrGrantEvent, body.idempotency_key)
        if existing is None:
            raise RuntimeError("Grant idempotency record was not visible after a key collision")
        _reject_fingerprint_mismatch(existing, fingerprint)
        return _grant_result(existing)

    ledger_type = "grant" if amount > 0 else "refund"
    await _post_ledger(
        db,
        org_id=org.id,
        entry_type=ledger_type,
        amount=amount,
        ref=_grant_ledger_ref(body.idempotency_key),
        description=reason,
    )
    ledger_entry = await db.scalar(
        select(NrLedgerEntry).where(
            NrLedgerEntry.type == ledger_type,
            NrLedgerEntry.ref == _grant_ledger_ref(body.idempotency_key),
        )
    )
    if ledger_entry is None:
        raise RuntimeError("Grant ledger entry was not created")
    await db.refresh(org)
    event = await db.get(NrGrantEvent, body.idempotency_key)
    if event is None:
        raise RuntimeError("Grant idempotency record disappeared during processing")
    event.ledger_entry_id = ledger_entry.id
    event.balance_after = org.balance
    await db.commit()
    return {
        "granted": True,
        "replayed": False,
        "amount": str(amount),
        "ledger_entry_id": str(ledger_entry.id),
        "balance_after": str(org.balance),
    }


@billing_router.post("/orgs/{organization_id}/status")
async def set_organization_status(
    organization_id: uuid.UUID,
    body: StatusRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _auth: Annotated[None, Depends(require_billing_service_or_master_key)],
) -> dict[str, str]:
    """Set the billing gate state for an organization."""
    reason = body.reason.strip()
    if body.status == "suspended" and not reason:
        raise HTTPException(status_code=422, detail="reason must not be blank")
    org = await _ensure_org(db, organization_id)
    org.status = body.status
    org.suspend_reason = reason if body.status == "suspended" else None
    org.updated_at = datetime.now(UTC)
    await db.commit()
    return {"status": org.status}


@billing_router.get("/context")
async def get_billing_context(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
) -> dict[str, str | None]:
    """Return the caller's organization ID and role for the NorthRouter billing site."""
    organizations = OrganizationService(db, membership_listener=None)
    api_key, is_master = auth
    if is_master:
        bootstrap_id = await organizations.bootstrap_operator_id()
        if bootstrap_id is not None:
            bootstrap_user = await organizations.users.get(bootstrap_id)
            if bootstrap_user is not None:
                try:
                    active_org = await organizations.get_active_organization_for_user(bootstrap_user)
                    return {"organization_id": str(active_org.id), "role": "owner"}
                except NotAuthorizedError:
                    pass
        return {"organization_id": None, "role": "operator"}

    user, key_org_id = await _resolve_oauth_billing_caller(api_key, organizations)
    if key_org_id is None or not await organizations.user_has_active_membership(
        organization_id=key_org_id, user_id=user.id
    ):
        raise OrganizationNotFoundError(key_org_id)
    membership = await organizations.members.get_active_by_organization_and_user(key_org_id, user.id)
    if membership is None:
        raise OrganizationNotFoundError(key_org_id)
    return {"organization_id": str(key_org_id), "role": membership.role}


@billing_router.post("/holds/sweep")
async def sweep_holds(
    db: Annotated[AsyncSession, Depends(get_db)],
    _auth: Annotated[None, Depends(require_billing_service_or_master_key)],
) -> dict[str, int]:
    """Release open holds that have been outstanding for at least one hour."""
    now = datetime.now(UTC)
    holds = await db.scalars(
        select(NrHold)
        .where(and_(NrHold.state == "open", _expired_condition(now)))
        .order_by(NrHold.created_at, NrHold.id)
        .with_for_update(skip_locked=True)
    )
    count = 0
    for hold in holds:
        hold.state = "released"
        hold.released_at = now
        await _post_ledger(
            db,
            org_id=hold.org_id,
            entry_type="release",
            amount=hold.amount,
            ref=f"release:{hold.id}",
        )
        count += 1
    await db.commit()
    return {"released": count}


@billing_router.get("/orgs/{organization_id}/ledger")
async def get_ledger(
    organization_id: Annotated[uuid.UUID, Depends(require_billing_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Read an organization's recent billing ledger without hold/release noise or internal refs."""
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
    if org is None:
        return {"data": []}
    entries = await db.scalars(
        select(NrLedgerEntry)
        .where(
            NrLedgerEntry.org_id == org.id,
            NrLedgerEntry.type.not_in(("hold", "release")),
        )
        .order_by(NrLedgerEntry.created_at.desc())
        .limit(limit)
    )
    return {
        "data": [
            {
                "id": str(entry.id),
                "type": entry.type,
                "amount": str(entry.amount),
                "description": entry.description,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries
        ]
    }
