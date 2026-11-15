"""Authenticated top-up, subscription, e-transfer, ledger, and webhook routes."""

import asyncio
import hmac
import os
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.api.deps import get_db, verify_api_key_or_master_key
from gateway.models.api_keys import APIKey
from gateway.models.tenancy import Workspace
from northrouter_billing.adapter import NorthRouterBillingAdapter
from northrouter_billing.models import NrETransfer, NrLedgerEntry, NrOrg, NrSubscription, NrWebhookEvent, _money
from northrouter_billing.stripe_client import StripeClient

billing_router = APIRouter(prefix="/nr/billing", tags=["NorthRouter billing"])
_TRANSFER_AMOUNTS = frozenset({Decimal("100"), Decimal("500"), Decimal("1000")})
_PLAN_CREDITS = {"starter": Decimal("100"), "growth": Decimal("500"), "scale": Decimal("1000")}


class TopUpRequest(BaseModel):
    """A fixed USD top-up amount."""

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class SubscriptionRequest(BaseModel):
    """A Stripe price selected from an approved NorthRouter plan."""

    plan: Literal["starter", "growth", "scale"]
    price_id: str = Field(min_length=1, max_length=255)


class ETransferRequest(BaseModel):
    """The fixed package and the customer's transfer reference."""

    organization_id: uuid.UUID
    amount: Decimal
    reference: str = Field(min_length=1, max_length=255)


async def _authorized_org(
    organization_id: uuid.UUID,
    auth: tuple[APIKey | None, bool],
    db: AsyncSession,
) -> NrOrg:
    api_key, is_master = auth
    if not is_master:
        if api_key is None:
            raise HTTPException(status_code=403, detail="Organization access denied")
        organization = await db.scalar(select(Workspace.organization_id).where(Workspace.id == api_key.workspace_id))
        if organization != organization_id:
            raise HTTPException(status_code=404, detail="Organization not found")
    return await _ensure_org(db, organization_id)


async def _ensure_org(db: AsyncSession, organization_id: uuid.UUID) -> NrOrg:
    adapter = NorthRouterBillingAdapter(db)
    return await adapter._org(organization_id)


def _stripe_data(event: Any) -> tuple[str, Any]:
    return str(_read(event, "type", "")), _read(_read(event, "data", {}), "object", {})


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _metadata(value: Any) -> Any:
    return _read(value, "metadata", {}) or {}


async def _insert_if_absent(db: AsyncSession, model: Any, values: dict[str, Any], conflict_columns: list[str]) -> bool:
    """Insert a replay-protection or ledger row without racing another delivery."""
    dialect = db.get_bind().dialect.name
    if dialect == "sqlite":
        statement = sqlite_insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
    elif dialect == "postgresql":
        statement = pg_insert(model).values(**values).on_conflict_do_nothing(index_elements=conflict_columns)
    else:
        existing = await db.scalar(
            select(model).where(*(getattr(model, name) == values[name] for name in conflict_columns))
        )
        if existing is not None:
            return False
        db.add(model(**values))
        await db.flush()
        return True
    result = await db.execute(statement)
    return result.rowcount == 1


async def _handle_stripe_event(db: AsyncSession, event_type: str, obj: Any) -> None:
    metadata = _metadata(obj)
    organization_value = _read(metadata, "organization_id")
    if event_type == "payment_intent.succeeded" and organization_value:
        org = await _ensure_org(db, uuid.UUID(str(organization_value)))
        amount = _money(Decimal(str(_read(obj, "amount_received", _read(obj, "amount", 0)))) / 100)
        payment_id = str(_read(obj, "id", ""))
        await _insert_if_absent(
            db,
            NrLedgerEntry,
            {
                "org_id": org.id,
                "type": "grant",
                "amount": amount,
                "ref": f"stripe-payment:{payment_id}",
                "description": "Stripe card top-up",
            },
            ["type", "ref"],
        )
    elif event_type == "customer.subscription.updated":
        org_id = organization_value
        customer_id = _read(obj, "customer")
        org = (
            await _ensure_org(db, uuid.UUID(str(org_id)))
            if org_id
            else await db.scalar(select(NrOrg).where(NrOrg.stripe_customer_id == str(customer_id)))
        )
        if org is None:
            return
        period_end = _read(obj, "current_period_end")
        if period_end is None:
            items = _read(_read(obj, "items", {}), "data", [])
            period_end = _read(items[0], "current_period_end") if items else None
        if period_end is None:
            return
        subscription_id = str(_read(obj, "id"))
        existing = await db.scalar(
            select(NrSubscription).where(NrSubscription.stripe_subscription_id == subscription_id)
        )
        values = {
            "org_id": org.id,
            "stripe_subscription_id": subscription_id,
            "plan": str(_read(metadata, "plan", "unknown")),
            "status": str(_read(obj, "status", "active")),
            "current_period_end": datetime.fromtimestamp(int(period_end), UTC),
            "credit_amount": _money(Decimal(str(_read(metadata, "credit_amount", "0")))),
        }
        if existing is None:
            db.add(NrSubscription(**values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
    elif event_type == "invoice.payment_failed":
        customer_id = _read(obj, "customer")
        org = await db.scalar(select(NrOrg).where(NrOrg.stripe_customer_id == str(customer_id)))
        if org is not None:
            org.status = "suspended"
    elif event_type == "charge.refunded":
        payment_intent_id = _read(obj, "payment_intent")
        cumulative_amount = _money(Decimal(str(_read(obj, "amount_refunded", 0))) / 100)
        if organization_value:
            org = await _ensure_org(db, uuid.UUID(str(organization_value)))
        else:
            original = await db.scalar(
                select(NrLedgerEntry).where(
                    NrLedgerEntry.type == "grant",
                    NrLedgerEntry.ref == f"stripe-payment:{payment_intent_id}",
                )
            )
            org = await db.get(NrOrg, original.org_id) if original is not None else None
        if org is not None and payment_intent_id:
            charge_id = str(_read(obj, "id"))
            already_refunded = await db.scalar(
                select(func.coalesce(func.sum(NrLedgerEntry.amount), 0)).where(
                    NrLedgerEntry.org_id == org.id,
                    NrLedgerEntry.type == "refund",
                    NrLedgerEntry.ref.like(f"stripe-refund:{charge_id}:%"),
                )
            )
            delta = _money(cumulative_amount + (already_refunded or Decimal(0)))
            if delta > 0:
                await _insert_if_absent(
                    db,
                    NrLedgerEntry,
                    {
                        "org_id": org.id,
                        "type": "refund",
                        "amount": _money(-delta),
                        "ref": f"stripe-refund:{charge_id}:{cumulative_amount}",
                        "description": "Stripe card refund",
                    },
                    ["type", "ref"],
                )


@billing_router.post("/orgs/{organization_id}/top-up")
async def create_top_up(
    organization_id: uuid.UUID,
    body: TopUpRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
) -> dict[str, str]:
    """Create a Stripe PaymentIntent for the authenticated organization's top-up."""
    org = await _authorized_org(organization_id, auth, db)
    if body.amount.as_tuple().exponent < -2:
        raise HTTPException(status_code=422, detail="Top-up amounts support at most two decimal places")
    stripe_client = StripeClient()
    if org.stripe_customer_id is None:
        customer = await asyncio.to_thread(stripe_client.create_customer, organization_id=str(organization_id))
        org.stripe_customer_id = str(_read(customer, "id"))
        await db.flush()
    amount_minor = int((body.amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    intent = await asyncio.to_thread(
        stripe_client.create_payment_intent,
        customer_id=org.stripe_customer_id,
        organization_id=str(organization_id),
        amount_minor=amount_minor,
    )
    await db.commit()
    return {"client_secret": str(_read(intent, "client_secret"))}


@billing_router.post("/orgs/{organization_id}/subscriptions")
async def create_subscription(
    organization_id: uuid.UUID,
    body: SubscriptionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
) -> dict[str, Any]:
    """Create a Stripe subscription for one approved monthly credit plan."""
    org = await _authorized_org(organization_id, auth, db)
    price_env = f"NORTHROUTER_STRIPE_PRICE_{body.plan.upper()}"
    configured_price = os.getenv(price_env)
    if not configured_price or body.price_id != configured_price:
        raise HTTPException(status_code=422, detail=f"The configured Stripe price for {body.plan} is required")
    stripe_client = StripeClient()
    if org.stripe_customer_id is None:
        customer = await asyncio.to_thread(stripe_client.create_customer, organization_id=str(organization_id))
        org.stripe_customer_id = str(_read(customer, "id"))
        await db.flush()
    subscription = await asyncio.to_thread(
        stripe_client.create_subscription,
        customer_id=org.stripe_customer_id,
        price_id=body.price_id,
        organization_id=str(organization_id),
        plan=body.plan,
        credit_amount=str(_PLAN_CREDITS[body.plan]),
    )
    await db.commit()
    return {"id": str(_read(subscription, "id")), "status": str(_read(subscription, "status"))}


@billing_router.get("/orgs/{organization_id}/subscriptions")
async def list_subscriptions(
    organization_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
) -> dict[str, Any]:
    """List subscriptions for the authenticated organization."""
    org = await _authorized_org(organization_id, auth, db)
    if org.stripe_customer_id is None:
        return {"data": []}
    result = await asyncio.to_thread(StripeClient().list_subscriptions, customer_id=org.stripe_customer_id)
    return {"data": [_public_subscription(item) for item in _read(result, "data", [])]}


@billing_router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> dict[str, bool]:
    """Verify and reconcile a Stripe webhook once per event ID."""
    if stripe_signature is None:
        raise HTTPException(status_code=400, detail="Missing Stripe signature")
    try:
        event = await asyncio.to_thread(StripeClient().construct_event, await request.body(), stripe_signature)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Stripe webhook signature") from exc
    event_id = str(_read(event, "id", ""))
    if not event_id:
        raise HTTPException(status_code=400, detail="Stripe event has no ID")
    inserted = await _insert_if_absent(
        db,
        NrWebhookEvent,
        {"event_id": event_id, "event_type": str(_read(event, "type", "")), "succeeded": True},
        ["event_id"],
    )
    if not inserted:
        return {"received": True}
    event_type, obj = _stripe_data(event)
    await _handle_stripe_event(db, event_type, obj)
    await db.commit()
    return {"received": True}


@billing_router.post("/e-transfers")
async def submit_e_transfer(
    body: ETransferRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
) -> dict[str, str]:
    """Submit an e-transfer package for operator review."""
    if body.amount not in _TRANSFER_AMOUNTS:
        raise HTTPException(status_code=422, detail="Choose a $100, $500, or $1000 package")
    org = await _authorized_org(body.organization_id, auth, db)
    transfer = NrETransfer(org_id=org.id, amount=_money(body.amount), reference=body.reference, status="pending")
    db.add(transfer)
    await db.commit()
    return {"id": str(transfer.id), "status": transfer.status}


async def _review_e_transfer(
    transfer_id: uuid.UUID,
    db: AsyncSession,
    operator_token: str | None,
    *,
    approve: bool,
) -> dict[str, str]:
    expected = os.getenv("NORTHROUTER_BILLING_OPERATOR_TOKEN", "")
    if not expected or operator_token is None or not hmac.compare_digest(operator_token, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Operator authorization required")
    transfer = await db.scalar(select(NrETransfer).where(NrETransfer.id == transfer_id).with_for_update())
    if transfer is None:
        raise HTTPException(status_code=404, detail="E-transfer not found")
    if transfer.status != "pending":
        raise HTTPException(status_code=409, detail="E-transfer has already been reviewed")
    transfer.status = "approved" if approve else "rejected"
    transfer.reviewed_by = "billing-operator"
    transfer.reviewed_at = datetime.now(UTC)
    if approve:
        db.add(
            NrLedgerEntry(
                org_id=transfer.org_id,
                type="grant",
                amount=transfer.amount,
                ref=f"etransfer:{transfer.id}",
                description="Approved e-transfer",
            )
        )
    await db.commit()
    return {"id": str(transfer.id), "status": transfer.status}


@billing_router.post("/e-transfers/{transfer_id}/approve")
async def approve_e_transfer(
    transfer_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    operator_token: Annotated[str | None, Header(alias="NorthRouter-Operator-Token")] = None,
) -> dict[str, str]:
    """Approve a pending e-transfer after operator-token verification."""
    return await _review_e_transfer(transfer_id, db, operator_token, approve=True)


@billing_router.post("/e-transfers/{transfer_id}/reject")
async def reject_e_transfer(
    transfer_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    operator_token: Annotated[str | None, Header(alias="NorthRouter-Operator-Token")] = None,
) -> dict[str, str]:
    """Reject a pending e-transfer after operator-token verification."""
    return await _review_e_transfer(transfer_id, db, operator_token, approve=False)


@billing_router.get("/orgs/{organization_id}/ledger")
async def get_ledger(
    organization_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[tuple[APIKey | None, bool], Depends(verify_api_key_or_master_key)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Read an organization's recent billing ledger."""
    org = await _authorized_org(organization_id, auth, db)
    entries = await db.scalars(
        select(NrLedgerEntry)
        .where(NrLedgerEntry.org_id == org.id)
        .order_by(NrLedgerEntry.created_at.desc())
        .limit(limit)
    )
    return {
        "data": [
            {
                "id": str(entry.id),
                "type": entry.type,
                "amount": str(entry.amount),
                "reference": entry.ref,
                "description": entry.description,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries
        ]
    }


def _public_subscription(item: Any) -> dict[str, str | None]:
    """Expose Stripe subscription state without returning customer or payment data."""
    return {
        "id": str(_read(item, "id")),
        "status": str(_read(item, "status")),
        "current_period_end": str(_read(item, "current_period_end", "")),
    }
