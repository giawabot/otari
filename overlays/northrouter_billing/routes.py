"""Authenticated top-up, subscription, e-transfer, ledger, and webhook routes."""

import asyncio
import json
import logging
import os
import uuid
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal

import stripe
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.api.deps import (
    CurrentIdentity,
    get_db,
    require_deployment_operator,
    verify_api_key_or_master_key,
    verify_master_key,
)
from gateway.core.database import release_session
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
from northrouter_billing.fx import (
    FxRateError,
    convert_cad_to_usd,
    fetch_boc_usdcad_rate,
    get_fx_margin,
)
from northrouter_billing.models import (
    NrETransfer,
    NrHold,
    NrLedgerEntry,
    NrOrg,
    NrSubscription,
    NrWebhookEvent,
    _money,
)
from northrouter_billing.stripe_client import StripeClient

billing_router = APIRouter(prefix="/nr/billing", tags=["NorthRouter billing"])
_TRANSFER_AMOUNTS = frozenset({Decimal("100"), Decimal("500"), Decimal("1000")})
_PLAN_CREDITS: dict[str, Decimal] = {
    "starter": Decimal("100"),
    "growth": Decimal("500"),
    "scale": Decimal("1000"),
}
_BLOCKING_SUBSCRIPTION_STATUSES = ("active", "trialing", "incomplete", "past_due")
logger = logging.getLogger(__name__)


class TopUpRequest(BaseModel):
    """A fixed USD top-up amount."""

    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class SubscriptionRequest(BaseModel):
    """A NorthRouter subscription plan whose Stripe price is resolved from server config."""

    model_config = ConfigDict(extra="forbid")

    plan: Literal["starter", "growth", "scale"]


class ETransferRequest(BaseModel):
    """The fixed CAD package and the customer's Interac transfer reference."""

    model_config = ConfigDict(extra="forbid")

    amount: Decimal
    reference: str = Field(min_length=1, max_length=255)


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
    adapter = NorthRouterBillingAdapter(db)
    return await adapter._org(organization_id)


async def _call_stripe(func: Any, /, *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(func, *args, **kwargs)
    except (stripe.CardError, stripe.InvalidRequestError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Stripe rejected the request") from exc
    except stripe.IdempotencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Conflicting idempotent Stripe request"
        ) from exc
    except stripe.StripeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Stripe service error") from exc


async def _ensure_stripe_customer(
    db: AsyncSession,
    org: NrOrg,
    organization_id: uuid.UUID,
    stripe_client: StripeClient,
) -> str:
    existing_customer_id = org.stripe_customer_id
    if existing_customer_id is not None:
        await release_session(db)
        return existing_customer_id
    await release_session(db)
    customer = await _call_stripe(
        stripe_client.create_customer,
        organization_id=str(organization_id),
        idempotency_key=f"nr-org-{organization_id}",
    )
    new_customer_id = str(_read(customer, "id"))
    await db.execute(
        update(NrOrg)
        .where(NrOrg.otari_org_id == organization_id, NrOrg.stripe_customer_id.is_(None))
        .values(stripe_customer_id=new_customer_id, updated_at=datetime.now(UTC))
    )
    await db.commit()
    fresh_org = await _ensure_org(db, organization_id)
    await db.refresh(fresh_org)
    resolved_customer_id = fresh_org.stripe_customer_id or new_customer_id
    await release_session(db)
    return resolved_customer_id


def _reviewer_label(identity: TenancyUser, master_key: str | None) -> str:
    if master_key is not None:
        return "master-key"
    return identity.email or str(identity.id)


def _stripe_data(event: Any) -> tuple[str, Any]:
    return str(_read(event, "type", "")), _read(_read(event, "data", {}), "object", {})


def _read(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _metadata(value: Any) -> Any:
    return _read(value, "metadata", {}) or {}


def _int_or_none(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _plan_for_price_id(price_id: str | None) -> str | None:
    if not price_id:
        return None
    for plan in _PLAN_CREDITS:
        configured = os.getenv(f"NORTHROUTER_STRIPE_PRICE_{plan.upper()}", "").strip()
        if configured and configured == price_id:
            return plan
    return None


def _match_invoice_plan(invoice_obj: Any) -> str | None:
    for price_id in _extract_invoice_price_ids(invoice_obj):
        plan = _plan_for_price_id(price_id)
        if plan is not None:
            return plan
    return None


def _extract_subscription_item_fields(obj: Any) -> tuple[int | None, str | None]:
    items = _read(_read(obj, "items", {}), "data", []) or []
    first_item = items[0] if items else None
    period_end = _int_or_none(_read(first_item, "current_period_end")) if first_item is not None else None
    price_id: str | None = None
    if first_item is not None:
        price_obj = _read(first_item, "price")
        if isinstance(price_obj, str) and price_obj:
            price_id = price_obj
        elif price_obj is not None:
            raw_id = _read(price_obj, "id")
            if raw_id:
                price_id = str(raw_id)
        if price_id is None:
            direct_price_id = _read(first_item, "price_id")
            if direct_price_id:
                price_id = str(direct_price_id)
    return period_end, price_id


def _extract_invoice_price_ids(invoice_obj: Any) -> list[str]:
    lines = _read(_read(invoice_obj, "lines", {}), "data", []) or []
    price_ids: list[str] = []
    for line in lines:
        pricing = _read(line, "pricing")
        price_details = _read(pricing, "price_details") if pricing is not None else None
        basil_price = _read(price_details, "price") if price_details is not None else None
        if isinstance(basil_price, str) and basil_price:
            price_ids.append(basil_price)
            continue
        if basil_price is not None:
            basil_id = _read(basil_price, "id")
            if basil_id:
                price_ids.append(str(basil_id))
                continue
        price_obj = _read(line, "price")
        if isinstance(price_obj, str) and price_obj:
            price_ids.append(price_obj)
        elif price_obj is not None:
            raw_id = _read(price_obj, "id")
            if raw_id:
                price_ids.append(str(raw_id))
    return price_ids


def _extract_invoice_subscription_details(invoice_obj: Any) -> tuple[str | None, Any]:
    parent = _read(invoice_obj, "parent")
    sub_details = _read(parent, "subscription_details") if parent is not None else None
    if sub_details is None:
        sub_details = _read(invoice_obj, "subscription_details")
    sub_meta = _metadata(sub_details) if sub_details is not None else {}
    sub_ref = _read(sub_details, "subscription") if sub_details is not None else None
    if sub_ref is None:
        sub_ref = _read(invoice_obj, "subscription")
    sub_id: str | None = None
    if isinstance(sub_ref, str) and sub_ref:
        sub_id = sub_ref
    elif sub_ref is not None:
        raw_id = _read(sub_ref, "id")
        if raw_id:
            sub_id = str(raw_id)
    return sub_id, sub_meta


def _require_usd_currency(obj: Any, event_type: str) -> bool:
    currency = str(_read(obj, "currency", "") or "").lower()
    if currency != "usd":
        logger.warning("Skipping %s with non-USD currency %r", event_type, currency)
        return False
    return True


async def _resolve_webhook_org(
    db: AsyncSession,
    *,
    organization_value: Any,
    customer_id: Any,
) -> NrOrg | None:
    org: NrOrg | None = None
    if organization_value:
        org_uuid = uuid.UUID(str(organization_value))
        org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_uuid))
        if org is None and await OrganizationService(db, membership_listener=None).has_organization(org_uuid):
            org = await _ensure_org(db, org_uuid)
    if org is None and customer_id:
        org = await db.scalar(select(NrOrg).where(NrOrg.stripe_customer_id == str(customer_id)))
    if org is not None and customer_id and org.stripe_customer_id is None:
        org.stripe_customer_id = str(customer_id)
    return org


async def _resolve_dispute_org(
    db: AsyncSession,
    obj: Any,
    *,
    dispute_id: str,
    organization_value: Any,
) -> NrOrg | None:
    if dispute_id:
        existing_dispute = await db.scalar(
            select(NrLedgerEntry).where(
                NrLedgerEntry.type == "dispute",
                NrLedgerEntry.ref == f"stripe-dispute:{dispute_id}",
            )
        )
        if existing_dispute is not None:
            return await db.get(NrOrg, existing_dispute.org_id)
    payment_intent_id = _read(obj, "payment_intent")
    if payment_intent_id:
        original = await db.scalar(
            select(NrLedgerEntry).where(
                NrLedgerEntry.type == "grant",
                NrLedgerEntry.ref == f"stripe-payment:{payment_intent_id}",
            )
        )
        if original is not None:
            return await db.get(NrOrg, original.org_id)
    return await _resolve_webhook_org(
        db,
        organization_value=organization_value,
        customer_id=_read(obj, "customer"),
    )


async def _has_open_disputes(db: AsyncSession, org_id: uuid.UUID) -> bool:
    dispute_refs = set(
        await db.scalars(
            select(NrLedgerEntry.ref).where(
                NrLedgerEntry.org_id == org_id,
                NrLedgerEntry.type == "dispute",
                NrLedgerEntry.ref.is_not(None),
            )
        )
    )
    if not dispute_refs:
        return False
    won_refs = set(
        await db.scalars(
            select(NrLedgerEntry.ref).where(
                NrLedgerEntry.org_id == org_id,
                NrLedgerEntry.type == "grant",
                NrLedgerEntry.ref.in_(list(dispute_refs)),
            )
        )
    )
    return bool(dispute_refs - won_refs)


async def _handle_stripe_event(
    db: AsyncSession,
    event_type: str,
    obj: Any,
    *,
    event_created: int | None = None,
) -> bool:
    metadata = _metadata(obj)
    organization_value = _read(metadata, "organization_id")
    resolved_created = event_created if event_created is not None else _int_or_none(_read(obj, "created"))

    if event_type == "payment_intent.succeeded":
        if not _require_usd_currency(obj, event_type):
            return False
        customer_id = _read(obj, "customer")
        org = await _resolve_webhook_org(db, organization_value=organization_value, customer_id=customer_id)
        if org is None:
            logger.warning("Skipping payment_intent.succeeded for unknown organization")
            return False
        amount = _money(Decimal(str(_read(obj, "amount_received", _read(obj, "amount", 0)))) / 100)
        payment_id = str(_read(obj, "id", ""))
        await _post_ledger(
            db,
            org_id=org.id,
            entry_type="grant",
            amount=amount,
            ref=f"stripe-payment:{payment_id}",
            description="Stripe card top-up",
        )
        return True

    if event_type == "invoice.paid":
        invoice_id = str(_read(obj, "id", ""))
        billing_reason = _read(obj, "billing_reason")
        if billing_reason == "subscription_update":
            logger.warning("Ignoring subscription_update invoice %s; plan changes are not supported yet", invoice_id)
            return True
        if billing_reason not in ("subscription_create", "subscription_cycle"):
            return True
        amount_paid = _int_or_none(_read(obj, "amount_paid", 0)) or 0
        if amount_paid <= 0:
            return True
        sub_id, sub_meta = _extract_invoice_subscription_details(obj)
        sub_org_value = organization_value or _read(sub_meta, "organization_id")
        org = await _resolve_webhook_org(
            db,
            organization_value=sub_org_value,
            customer_id=_read(obj, "customer"),
        )
        if org is None and sub_id:
            existing_sub = await db.scalar(
                select(NrSubscription).where(NrSubscription.stripe_subscription_id == sub_id)
            )
            if existing_sub is not None:
                org = await db.get(NrOrg, existing_sub.org_id)
        if org is None:
            logger.warning("Skipping invoice.paid %s for unknown organization", invoice_id)
            return False
        matched_plan = _match_invoice_plan(obj)
        if matched_plan is None:
            logger.warning("Skipping invoice.paid %s with no configured plan price", invoice_id)
            return False
        await _post_ledger(
            db,
            org_id=org.id,
            entry_type="grant",
            amount=_PLAN_CREDITS[matched_plan],
            ref=f"stripe-invoice:{invoice_id}",
            description=f"Subscription credit for {matched_plan}",
        )
        return True

    if event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        subscription_id = str(_read(obj, "id", ""))
        existing = await db.scalar(
            select(NrSubscription).where(NrSubscription.stripe_subscription_id == subscription_id)
        )
        customer_id = _read(obj, "customer")
        org = await _resolve_webhook_org(db, organization_value=organization_value, customer_id=customer_id)
        if org is None and existing is not None:
            org = await db.get(NrOrg, existing.org_id)

        if event_type == "customer.subscription.deleted":
            if existing is None:
                logger.warning("Skipping customer.subscription.deleted for unknown subscription %s", subscription_id)
                return False
            if (
                existing.last_event_created is not None
                and resolved_created is not None
                and resolved_created < existing.last_event_created
            ):
                return True
            existing.status = "canceled"
            if resolved_created is not None:
                existing.last_event_created = resolved_created
            return True

        if org is None:
            logger.warning("Skipping %s for unknown organization on subscription %s", event_type, subscription_id)
            return False

        if existing is not None:
            if (
                existing.last_event_created is not None
                and resolved_created is not None
                and resolved_created < existing.last_event_created
            ):
                return True
            if existing.status == "canceled":
                return True

        period_end, price_id = _extract_subscription_item_fields(obj)
        if period_end is None:
            logger.warning("Subscription %s has no item current_period_end; skipping", subscription_id)
            return False
        plan = _plan_for_price_id(price_id)
        if plan is None:
            logger.warning("Subscription %s has unconfigured price %r; skipping", subscription_id, price_id)
            return False
        values: dict[str, Any] = {
            "org_id": org.id,
            "stripe_subscription_id": subscription_id,
            "price_id": price_id,
            "plan": plan,
            "status": str(_read(obj, "status", "active")),
            "current_period_end": datetime.fromtimestamp(period_end, UTC),
            "credit_amount": _PLAN_CREDITS[plan],
            "last_event_created": resolved_created,
        }
        if existing is None:
            db.add(NrSubscription(**values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
        return True

    if event_type == "invoice.payment_failed":
        logger.warning(
            "Stripe invoice payment failed for invoice %s (customer %s)",
            _read(obj, "id"),
            _read(obj, "customer"),
        )
        return True

    if event_type == "charge.refunded":
        if not _require_usd_currency(obj, event_type):
            return False
        payment_intent_id = _read(obj, "payment_intent")
        cumulative_amount = _money(Decimal(str(_read(obj, "amount_refunded", 0))) / 100)
        charge_id = str(_read(obj, "id", ""))
        original = (
            await db.scalar(
                select(NrLedgerEntry).where(
                    NrLedgerEntry.type == "grant",
                    NrLedgerEntry.ref == f"stripe-payment:{payment_intent_id}",
                )
            )
            if payment_intent_id
            else None
        )
        context_refund = None
        if original is None and charge_id:
            escaped_charge_id = charge_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            context_refund = await db.scalar(
                select(NrLedgerEntry)
                .where(
                    NrLedgerEntry.type == "refund",
                    NrLedgerEntry.ref.like(f"stripe-refund:{escaped_charge_id}:%", escape="\\"),
                )
                .limit(1)
            )
        context = original or context_refund
        refund_org: NrOrg | None = None
        if context is not None:
            refund_org = await db.get(NrOrg, context.org_id)
        else:
            customer_id = _read(obj, "customer")
            refund_org = await _resolve_webhook_org(db, organization_value=organization_value, customer_id=customer_id)
        if refund_org is None:
            logger.warning("Skipping charge.refunded %s for unknown organization", charge_id)
            return False
        if charge_id:
            await db.execute(update(NrOrg).where(NrOrg.id == refund_org.id).values(updated_at=datetime.now(UTC)))
            escaped_charge_id = charge_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            already_refunded = await db.scalar(
                select(func.coalesce(func.sum(NrLedgerEntry.amount), 0)).where(
                    NrLedgerEntry.org_id == refund_org.id,
                    NrLedgerEntry.type == "refund",
                    NrLedgerEntry.ref.like(f"stripe-refund:{escaped_charge_id}:%", escape="\\"),
                )
            )
            delta = _money(cumulative_amount + (already_refunded or Decimal(0)))
            if delta > 0:
                await _post_ledger(
                    db,
                    org_id=refund_org.id,
                    entry_type="refund",
                    amount=_money(-delta),
                    ref=f"stripe-refund:{charge_id}:{cumulative_amount}",
                    description="Stripe card refund",
                )
        return True

    if event_type == "charge.dispute.created":
        if not _require_usd_currency(obj, event_type):
            return False
        dispute_id = str(_read(obj, "id", ""))
        dispute_org = await _resolve_dispute_org(db, obj, dispute_id=dispute_id, organization_value=organization_value)
        if dispute_org is None:
            logger.warning("Skipping charge.dispute.created %s for unknown organization", dispute_id)
            return False
        dispute_amount = _money(Decimal(str(_read(obj, "amount", 0))) / 100)
        await _post_ledger(
            db,
            org_id=dispute_org.id,
            entry_type="dispute",
            amount=_money(-dispute_amount),
            ref=f"stripe-dispute:{dispute_id}",
            description="Stripe charge dispute",
        )
        dispute_org.status = "suspended"
        dispute_org.suspend_reason = "dispute"
        await db.flush()
        return True

    if event_type == "charge.dispute.closed":
        if not _require_usd_currency(obj, event_type):
            return False
        dispute_id = str(_read(obj, "id", ""))
        dispute_org = await _resolve_dispute_org(db, obj, dispute_id=dispute_id, organization_value=organization_value)
        if dispute_org is None:
            logger.warning("Skipping charge.dispute.closed %s for unknown organization", dispute_id)
            return False
        if str(_read(obj, "status", "")) == "won":
            existing_dispute = await db.scalar(
                select(NrLedgerEntry).where(
                    NrLedgerEntry.org_id == dispute_org.id,
                    NrLedgerEntry.type == "dispute",
                    NrLedgerEntry.ref == f"stripe-dispute:{dispute_id}",
                )
            )
            reversal_amount = (
                _money(-existing_dispute.amount)
                if existing_dispute is not None
                else _money(Decimal(str(_read(obj, "amount", 0))) / 100)
            )
            await _post_ledger(
                db,
                org_id=dispute_org.id,
                entry_type="grant",
                amount=reversal_amount,
                ref=f"stripe-dispute:{dispute_id}",
                description="Stripe charge dispute won",
            )
            if (
                not await _has_open_disputes(db, dispute_org.id)
                and dispute_org.status == "suspended"
                and dispute_org.suspend_reason == "dispute"
            ):
                dispute_org.status = "active"
                dispute_org.suspend_reason = None
                await db.flush()
        return True

    return True


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


@billing_router.post("/orgs/{organization_id}/top-up")
async def create_top_up(
    organization_id: Annotated[uuid.UUID, Depends(require_billing_manager)],
    body: TopUpRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Create a Stripe PaymentIntent for the authenticated organization's top-up."""
    org = await _ensure_org(db, organization_id)
    stripe_client = StripeClient()
    customer_id = await _ensure_stripe_customer(db, org, organization_id, stripe_client)
    amount_minor = int((body.amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    intent = await _call_stripe(
        stripe_client.create_payment_intent,
        customer_id=customer_id,
        organization_id=str(organization_id),
        amount_minor=amount_minor,
    )
    return {"client_secret": str(_read(intent, "client_secret"))}


@billing_router.post("/orgs/{organization_id}/subscriptions")
async def create_subscription(
    organization_id: Annotated[uuid.UUID, Depends(require_billing_manager)],
    body: SubscriptionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Create a Stripe subscription for one approved monthly credit plan."""
    price_env = f"NORTHROUTER_STRIPE_PRICE_{body.plan.upper()}"
    configured_price = os.getenv(price_env, "").strip()
    if not configured_price:
        raise HTTPException(status_code=422, detail=f"The configured Stripe price for {body.plan} is required")
    org = await _ensure_org(db, organization_id)
    existing_sub = await db.scalar(
        select(NrSubscription).where(
            NrSubscription.org_id == org.id,
            NrSubscription.status.in_(_BLOCKING_SUBSCRIPTION_STATUSES),
        )
    )
    if existing_sub is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Organization already has an active or pending subscription",
        )
    stripe_client = StripeClient()
    customer_id = await _ensure_stripe_customer(db, org, organization_id, stripe_client)
    resolved_idempotency_key = (idempotency_key or "").strip() or f"nr-sub-{organization_id}-{body.plan}"
    subscription = await _call_stripe(
        stripe_client.create_subscription,
        customer_id=customer_id,
        price_id=configured_price,
        organization_id=str(organization_id),
        plan=body.plan,
        credit_amount=str(_PLAN_CREDITS[body.plan]),
        idempotency_key=resolved_idempotency_key,
    )
    sub_id = str(_read(subscription, "id", ""))
    sub_status = str(_read(subscription, "status", "incomplete"))
    period_end, _ = _extract_subscription_item_fields(subscription)
    if sub_id:
        fresh_org = await _ensure_org(db, organization_id)
        stored_sub = await db.scalar(select(NrSubscription).where(NrSubscription.stripe_subscription_id == sub_id))
        if stored_sub is None:
            db.add(
                NrSubscription(
                    org_id=fresh_org.id,
                    stripe_subscription_id=sub_id,
                    price_id=configured_price,
                    plan=body.plan,
                    status=sub_status,
                    current_period_end=(
                        datetime.fromtimestamp(period_end, UTC) if period_end is not None else datetime.now(UTC)
                    ),
                    credit_amount=_PLAN_CREDITS[body.plan],
                    last_event_created=None,
                )
            )
            await db.commit()
    latest_invoice = _read(subscription, "latest_invoice")
    confirmation_secret = _read(latest_invoice, "confirmation_secret") if latest_invoice is not None else None
    client_secret = _read(confirmation_secret, "client_secret") if confirmation_secret is not None else None
    return {
        "id": sub_id,
        "status": sub_status,
        "client_secret": str(client_secret) if client_secret is not None else None,
    }


@billing_router.get("/orgs/{organization_id}/subscriptions")
async def list_subscriptions(
    organization_id: Annotated[uuid.UUID, Depends(require_billing_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    """List subscriptions for the authenticated organization without creating database rows."""
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == organization_id))
    if org is None or org.stripe_customer_id is None:
        return {"data": []}
    customer_id = org.stripe_customer_id
    await release_session(db)
    result = await _call_stripe(StripeClient().list_subscriptions, customer_id=customer_id)
    return {"data": [_public_subscription(item) for item in _read(result, "data", [])]}


@billing_router.post("/webhooks/stripe")
async def stripe_webhook(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
) -> dict[str, bool | str]:
    """Verify and reconcile a Stripe webhook once per event ID."""
    if stripe_signature is None:
        raise HTTPException(status_code=400, detail="Missing Stripe signature")
    stripe_client = StripeClient()
    try:
        event = await asyncio.to_thread(stripe_client.construct_event, await request.body(), stripe_signature)
    except RuntimeError as exc:
        logger.error("Stripe webhook secret is not configured")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook secret is not configured",
        ) from exc
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
    event_created = _int_or_none(_read(event, "created"))
    skipped = False
    try:
        async with db.begin_nested():
            handled = await _handle_stripe_event(db, event_type, obj, event_created=event_created)
            if not handled:
                skipped = True
    except (ValueError, ArithmeticError):
        logger.warning("Skipping invalid Stripe event %s", event_id)
        skipped = True
    if skipped:
        await db.execute(update(NrWebhookEvent).where(NrWebhookEvent.event_id == event_id).values(succeeded=False))
    await db.commit()
    return {"received": True, **({"status": "skipped"} if skipped else {})}


@billing_router.post("/orgs/{organization_id}/e-transfers")
async def submit_e_transfer(
    organization_id: Annotated[uuid.UUID, Depends(require_billing_manager)],
    body: ETransferRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, str]:
    """Submit a CAD e-transfer package for operator review."""
    if body.amount not in _TRANSFER_AMOUNTS:
        raise HTTPException(status_code=422, detail="Choose a CA$100, CA$500 or CA$1000 package")
    normalized_reference = body.reference.strip().lower()
    if not normalized_reference:
        raise HTTPException(status_code=422, detail="Transfer reference cannot be blank")
    org = await _ensure_org(db, organization_id)
    transfer = NrETransfer(
        org_id=org.id,
        amount=_money(body.amount),
        currency="CAD",
        reference=body.reference.strip(),
        reference_normalized=normalized_reference,
        status="pending",
    )
    db.add(transfer)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate reference") from exc
    return {"id": str(transfer.id), "status": transfer.status}


@billing_router.get("/e-transfers")
async def list_e_transfers(
    db: Annotated[AsyncSession, Depends(get_db)],
    _operator: Annotated[None, Depends(require_deployment_operator)],
    _identity: CurrentIdentity,
    status_filter: Annotated[Literal["pending", "approved", "rejected"], Query(alias="status")] = "pending",
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, list[dict[str, Any]]]:
    """List e-transfers for deployment operators by status."""
    rows = (
        await db.execute(
            select(NrETransfer, NrOrg.otari_org_id)
            .join(NrOrg, NrOrg.id == NrETransfer.org_id)
            .where(NrETransfer.status == status_filter)
            .order_by(NrETransfer.created_at.asc(), NrETransfer.id.asc())
            .limit(limit)
        )
    ).all()
    return {
        "data": [
            {
                "id": str(transfer.id),
                "organization_id": str(otari_org_id),
                "amount": str(transfer.amount),
                "currency": transfer.currency,
                "reference": transfer.reference,
                "status": transfer.status,
                "credited_usd": str(transfer.credited_usd) if transfer.credited_usd is not None else None,
                "fx_rate": str(transfer.fx_rate) if transfer.fx_rate is not None else None,
                "fx_rate_date": transfer.fx_rate_date,
                "fx_source": transfer.fx_source,
                "fx_margin": str(transfer.fx_margin) if transfer.fx_margin is not None else None,
                "reviewed_by": transfer.reviewed_by,
                "created_at": transfer.created_at.isoformat(),
                "reviewed_at": transfer.reviewed_at.isoformat() if transfer.reviewed_at is not None else None,
            }
            for transfer, otari_org_id in rows
        ]
    }


async def _review_e_transfer(
    transfer_id: uuid.UUID,
    db: AsyncSession,
    *,
    approve: bool,
    reviewed_by: str = "master-key",
) -> dict[str, str]:
    existing = await db.scalar(select(NrETransfer).where(NrETransfer.id == transfer_id))
    if existing is None:
        raise HTTPException(status_code=404, detail="E-transfer not found")
    if existing.status != "pending":
        raise HTTPException(status_code=409, detail="E-transfer has already been reviewed")

    quote = None
    margin = Decimal("0")
    if approve:
        margin = get_fx_margin()
        await release_session(db)
        try:
            quote = await fetch_boc_usdcad_rate()
        except FxRateError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Bank of Canada FX rate is currently unavailable",
            ) from exc

    transfer = await db.scalar(select(NrETransfer).where(NrETransfer.id == transfer_id).with_for_update())
    if transfer is None:
        raise HTTPException(status_code=404, detail="E-transfer not found")
    if transfer.status != "pending":
        raise HTTPException(status_code=409, detail="E-transfer has already been reviewed")
    transfer.status = "approved" if approve else "rejected"
    transfer.reviewed_by = reviewed_by
    transfer.reviewed_at = datetime.now(UTC)
    if approve and quote is not None:
        credited_usd = convert_cad_to_usd(transfer.amount, rate=quote.rate, margin=margin)
        transfer.currency = "CAD"
        transfer.fx_rate = _money(quote.rate)
        transfer.fx_rate_date = quote.rate_date
        transfer.fx_source = quote.source
        transfer.fx_margin = _money(margin)
        transfer.credited_usd = _money(credited_usd)
        fx_metadata = json.dumps(
            {
                "cad_amount": str(_money(transfer.amount)),
                "credited_usd": str(_money(credited_usd)),
                "currency": "CAD",
                "fx_margin": str(_money(margin)),
                "fx_rate": str(_money(quote.rate)),
                "fx_rate_date": quote.rate_date,
                "fx_source": quote.source,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        await _post_ledger(
            db,
            org_id=transfer.org_id,
            entry_type="grant",
            amount=_money(credited_usd),
            ref=f"etransfer:{transfer.id}",
            description="Approved e-transfer",
            metadata_json=fx_metadata,
        )
    await db.commit()
    return {"id": str(transfer.id), "status": transfer.status}


@billing_router.post("/e-transfers/{transfer_id}/approve")
async def approve_e_transfer(
    transfer_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _operator: Annotated[None, Depends(require_deployment_operator)],
    identity: CurrentIdentity,
    master_key: Annotated[str | None, Depends(verify_master_key)] = None,
) -> dict[str, str]:
    """Approve a pending CAD e-transfer and credit the organization in USD."""
    return await _review_e_transfer(
        transfer_id,
        db,
        approve=True,
        reviewed_by=_reviewer_label(identity, master_key),
    )


@billing_router.post("/e-transfers/{transfer_id}/reject")
async def reject_e_transfer(
    transfer_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    _operator: Annotated[None, Depends(require_deployment_operator)],
    identity: CurrentIdentity,
    master_key: Annotated[str | None, Depends(verify_master_key)] = None,
) -> dict[str, str]:
    """Reject a pending e-transfer after deployment operator verification."""
    return await _review_e_transfer(
        transfer_id,
        db,
        approve=False,
        reviewed_by=_reviewer_label(identity, master_key),
    )


@billing_router.post("/holds/sweep")
async def sweep_holds(
    db: Annotated[AsyncSession, Depends(get_db)],
    _operator: Annotated[None, Depends(require_deployment_operator)],
    _identity: CurrentIdentity,
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


def _public_subscription(item: Any) -> dict[str, str | None]:
    """Expose Stripe subscription state without returning customer or payment data."""
    period_end, _ = _extract_subscription_item_fields(item)
    if period_end is None:
        period_end = _int_or_none(_read(item, "current_period_end"))
    return {
        "id": str(_read(item, "id")),
        "status": str(_read(item, "status")),
        "current_period_end": (datetime.fromtimestamp(period_end, UTC).isoformat() if period_end is not None else None),
    }
