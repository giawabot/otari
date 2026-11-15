"""Billing adapter and contributed HTTP route behavior."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import get_protocol_members

import pytest
from fastapi import HTTPException
from northrouter_billing.adapter import NorthRouterBillingAdapter
from northrouter_billing.models import NrETransfer, NrHold, NrLedgerEntry, NrOrg, NrSubscription, NrWebhookEvent
from northrouter_billing.routes import (
    ETransferRequest,
    _handle_stripe_event,
    _review_e_transfer,
    reject_e_transfer,
    stripe_webhook,
    submit_e_transfer,
    sweep_holds,
    verify_operator_token,
)
from northrouter_billing.stripe_client import StripeClient
from sqlalchemy import func, select

from gateway.container import build_container
from gateway.ports.billing_port import NO_TOOLS, BillingPort, InsufficientFundsError


async def _org_and_credit(db, amount: Decimal = Decimal("25")) -> uuid.UUID:
    org_id = uuid.uuid4()
    adapter = NorthRouterBillingAdapter(db)
    org = await adapter._org(org_id)
    db.add(NrLedgerEntry(org_id=org.id, type="grant", amount=amount, ref=f"seed:{org_id}"))
    await db.flush()
    return org_id


@pytest.mark.asyncio
async def test_funded_gate_passes(db):
    org_id = await _org_and_credit(db)
    await NorthRouterBillingAdapter(db).require_funds_on_deposit(
        organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
    )


@pytest.mark.asyncio
async def test_zero_balance_raises_with_available_amount(db):
    org_id = uuid.uuid4()
    with pytest.raises(InsufficientFundsError) as raised:
        await NorthRouterBillingAdapter(db).require_funds_on_deposit(
            organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
        )
    assert raised.value.available == Decimal("0.000000")


@pytest.mark.asyncio
async def test_suspended_organization_is_refused(db):
    org_id = await _org_and_credit(db)
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    org.status = "suspended"
    with pytest.raises(InsufficientFundsError) as raised:
        await NorthRouterBillingAdapter(db).require_funds_on_deposit(
            organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
        )
    assert raised.value.available == Decimal("25.000000")
    assert raised.value.public_message == (
        "This organization is suspended; sign in at northrouter.ca to review your account details."
    )


@pytest.mark.asyncio
async def test_hold_then_release_returns_amount_to_unheld_funds(db):
    org_id = await _org_and_credit(db)
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.require_unheld_funds(organization_id=org_id)
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.hold(organization_id=org_id, amount=Decimal("25"))


@pytest.mark.asyncio
async def test_hold_beyond_unheld_funds_raises(db):
    org_id = await _org_and_credit(db, Decimal("10"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("8"))
    with pytest.raises(InsufficientFundsError) as raised:
        await adapter.hold(organization_id=org_id, amount=Decimal("3"))
    assert raised.value.available == Decimal("2.000000")


@pytest.mark.asyncio
async def test_zero_hold_is_a_noop(db):
    org_id = await _org_and_credit(db)
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("0"))
    count = await db.scalar(select(func.count()).select_from(NrHold))
    assert count == 0


@pytest.mark.asyncio
async def test_charge_records_consume_in_overdraft(db):
    org_id = await _org_and_credit(db, Decimal("2"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.charge(
        organization_id=org_id,
        amount=Decimal("5"),
        billed=None,
        tools=NO_TOOLS,
        description="served request",
    )
    consume = await db.scalar(
        select(NrLedgerEntry).where(NrLedgerEntry.type == "consume", NrLedgerEntry.description == "served request")
    )
    assert consume is not None
    assert consume.amount == Decimal("5.000000")
    with pytest.raises(InsufficientFundsError) as raised:
        await adapter.require_funds_on_deposit(
            organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
        )
    assert raised.value.available == Decimal("-3.000000")


@pytest.mark.asyncio
async def test_negative_charge_is_rejected(db):
    org_id = await _org_and_credit(db)
    with pytest.raises(ValueError, match="cannot be negative"):
        await NorthRouterBillingAdapter(db).charge(
            organization_id=org_id,
            amount=Decimal("-1"),
            billed=None,
            tools=NO_TOOLS,
        )


@pytest.mark.asyncio
async def test_created_subscription_grants_first_period_once(db):
    org_id = uuid.uuid4()
    org = await NorthRouterBillingAdapter(db)._org(org_id)
    period_end = int((datetime.now(UTC) + timedelta(days=30)).timestamp())
    await _handle_stripe_event(
        db,
        "customer.subscription.created",
        {
            "id": "sub_first_period",
            "customer": "cus_first_period",
            "status": "active",
            "current_period_end": period_end,
            "metadata": {
                "organization_id": str(org_id),
                "plan": "starter",
                "credit_amount": "100",
            },
        },
    )
    subscription = await db.scalar(
        select(NrSubscription).where(NrSubscription.stripe_subscription_id == "sub_first_period")
    )
    assert subscription is not None and subscription.org_id == org.id

    adapter = NorthRouterBillingAdapter(db)
    await adapter.apply_due_credit(organization_id=org_id)
    await adapter.apply_due_credit(organization_id=org_id)
    grants = await db.scalar(
        select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.ref.like("subscription:sub_first_period:%"))
    )
    assert grants == 1


@pytest.mark.asyncio
async def test_invoice_failure_suspends_and_success_reactivates(db):
    org_id = await _org_and_credit(db)
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    org.stripe_customer_id = "cus_invoice"

    await _handle_stripe_event(db, "invoice.payment_failed", {"customer": "cus_invoice"})
    assert org.status == "suspended" and org.suspend_reason == "payment_failed"
    with pytest.raises(InsufficientFundsError) as raised:
        await NorthRouterBillingAdapter(db).require_funds_on_deposit(
            organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
        )
    assert raised.value.public_message == (
        "This organization is suspended; sign in at northrouter.ca to review your account details."
    )

    await _handle_stripe_event(db, "invoice.payment_succeeded", {"customer": "cus_invoice"})
    await NorthRouterBillingAdapter(db).require_funds_on_deposit(
        organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
    )
    assert org.status == "active"
    assert org.suspend_reason is None


@pytest.mark.asyncio
async def test_refund_cumulative_amount_is_idempotent_and_charge_id_is_literal(db):
    org_id = await _org_and_credit(db, Decimal("25"))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    db.add(
        NrLedgerEntry(
            org_id=org.id,
            type="grant",
            amount=Decimal("25"),
            ref="stripe-payment:pi_refund",
        )
    )
    db.add(
        NrLedgerEntry(
            org_id=org.id,
            type="refund",
            amount=Decimal("-3"),
            ref="stripe-refund:chX1:10.000000",
        )
    )
    event = {
        "id": "ch_1",
        "payment_intent": "pi_refund",
        "amount_refunded": 500,
        "metadata": {"organization_id": str(org_id)},
    }
    await _handle_stripe_event(db, "charge.refunded", event)
    await _handle_stripe_event(db, "charge.refunded", event)
    own_refunds = await db.scalars(
        select(NrLedgerEntry).where(
            NrLedgerEntry.type == "refund",
            NrLedgerEntry.ref.like("stripe-refund:ch\\_1:%", escape="\\"),
        )
    )
    refunds = list(own_refunds)
    assert len(refunds) == 1
    assert refunds[0].amount == Decimal("-5.000000")


@pytest.mark.asyncio
async def test_expired_hold_does_not_reduce_funds_and_operator_sweep_releases_it(db, monkeypatch):
    monkeypatch.setenv("NORTHROUTER_BILLING_OPERATOR_TOKEN", "operator-secret")
    org_id = await _org_and_credit(db)
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    created_at = datetime.now(UTC) - timedelta(hours=2)
    hold = NrHold(
        org_id=org.id,
        amount=Decimal("25"),
        state="open",
        created_at=created_at,
        expires_at=created_at + timedelta(hours=1),
    )
    db.add(hold)
    await db.flush()

    await NorthRouterBillingAdapter(db).require_unheld_funds(organization_id=org_id)
    result = await sweep_holds(db, "operator-secret")
    assert result == {"released": 1}
    await db.refresh(hold)
    assert hold.state == "released"
    release = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == f"release:{hold.id}"))
    assert release is not None and release.amount == Decimal("25.000000")


@pytest.mark.asyncio
async def test_due_subscription_credit_is_idempotent(db):
    org_id = uuid.uuid4()
    adapter = NorthRouterBillingAdapter(db)
    org = await adapter._org(org_id)
    db.add(
        NrSubscription(
            org_id=org.id,
            stripe_subscription_id="sub_one",
            plan="starter",
            status="active",
            current_period_end=datetime.now(UTC) + timedelta(days=30),
            credit_amount=Decimal("100"),
        )
    )
    await db.flush()
    await adapter.apply_due_credit(organization_id=org_id)
    await adapter.apply_due_credit(organization_id=org_id)
    count = await db.scalar(
        select(func.count())
        .select_from(NrLedgerEntry)
        .where(NrLedgerEntry.type == "grant", NrLedgerEntry.ref.like("subscription:%"))
    )
    assert count == 1


@pytest.mark.asyncio
async def test_expired_active_subscription_neither_grants_nor_counts_as_funds(db):
    org_id = uuid.uuid4()
    adapter = NorthRouterBillingAdapter(db)
    org = await adapter._org(org_id)
    db.add(
        NrSubscription(
            org_id=org.id,
            stripe_subscription_id="sub_expired",
            plan="starter",
            status="active",
            current_period_end=datetime.now(UTC) - timedelta(seconds=1),
            credit_amount=Decimal("100"),
        )
    )
    await db.flush()

    await adapter.apply_due_credit(organization_id=org_id)

    assert not await adapter.has_funds_on_deposit(
        organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
    )
    assert await db.scalar(
        select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.ref.like("subscription:sub_expired:%"))
    ) == 0


@pytest.mark.asyncio
async def test_deleted_subscription_is_canceled_and_cannot_grant_credit(db):
    org_id = uuid.uuid4()
    org = await NorthRouterBillingAdapter(db)._org(org_id)
    subscription = NrSubscription(
        org_id=org.id,
        stripe_subscription_id="sub_deleted",
        plan="starter",
        status="active",
        current_period_end=datetime.now(UTC) + timedelta(days=30),
        credit_amount=Decimal("100"),
    )
    db.add(subscription)
    await db.flush()

    await _handle_stripe_event(
        db,
        "customer.subscription.deleted",
        {"id": "sub_deleted", "customer": "cus_deleted", "metadata": {"organization_id": str(org_id)}},
    )
    await NorthRouterBillingAdapter(db).apply_due_credit(organization_id=org_id)

    assert subscription.status == "canceled"
    grants = await db.scalar(
        select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.ref.like("subscription:sub_deleted:%"))
    )
    assert grants == 0


@pytest.mark.asyncio
async def test_invoice_success_does_not_clear_operator_suspension(db):
    org_id = await _org_and_credit(db)
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    org.stripe_customer_id = "cus_manual_suspension"
    org.status = "suspended"
    org.suspend_reason = "manual_review"

    await _handle_stripe_event(db, "invoice.payment_succeeded", {"customer": "cus_manual_suspension"})

    assert org.status == "suspended"
    assert org.suspend_reason == "manual_review"


@pytest.mark.asyncio
async def test_release_hold_accepts_expired_open_hold(db):
    org_id = await _org_and_credit(db)
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    created_at = datetime.now(UTC) - timedelta(hours=2)
    hold = NrHold(
        org_id=org.id,
        amount=Decimal("25"),
        state="open",
        created_at=created_at,
        expires_at=created_at + timedelta(hours=1),
    )
    db.add(hold)
    await db.flush()

    await NorthRouterBillingAdapter(db).release_hold(organization_id=org_id, amount=Decimal("25"))

    await db.refresh(hold)
    assert hold.state == "released"


@pytest.mark.asyncio
async def test_e_transfer_approval_grants_and_rejection_does_not(db, monkeypatch):
    monkeypatch.setenv("NORTHROUTER_BILLING_OPERATOR_TOKEN", "operator-secret")
    org_id = uuid.uuid4()
    pending = await submit_e_transfer(
        ETransferRequest(organization_id=org_id, amount=Decimal("100"), reference="etransfer-1"),
        db,
        (None, True),
    )
    with pytest.raises(HTTPException) as denied:
        verify_operator_token(None)
    assert denied.value.status_code == 403
    await _review_e_transfer(uuid.UUID(pending["id"]), db, approve=True)
    approved = await db.scalar(select(NrETransfer).where(NrETransfer.id == uuid.UUID(pending["id"])))
    assert approved is not None and approved.status == "approved"
    approved_grant = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == f"etransfer:{pending['id']}"))
    assert approved_grant is not None and approved_grant.amount == Decimal("100.000000")

    rejected = await submit_e_transfer(
        ETransferRequest(organization_id=org_id, amount=Decimal("500"), reference="etransfer-2"),
        db,
        (None, True),
    )
    await reject_e_transfer(uuid.UUID(rejected["id"]), db, "operator-secret")
    rejected_grant = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == f"etransfer:{rejected['id']}"))
    assert rejected_grant is None


@pytest.mark.asyncio
async def test_duplicate_e_transfer_reference_returns_conflict(db):
    body = ETransferRequest(organization_id=uuid.uuid4(), amount=Decimal("100"), reference="same-reference")
    await submit_e_transfer(body, db, (None, True))

    with pytest.raises(HTTPException) as raised:
        await submit_e_transfer(body, db, (None, True))

    assert raised.value.status_code == 409
    assert raised.value.detail == "duplicate reference"


@pytest.mark.asyncio
async def test_stripe_webhook_replay_grants_once(db, monkeypatch):
    org_id = uuid.uuid4()
    event = SimpleNamespace(
        id="evt_payment",
        type="payment_intent.succeeded",
        data=SimpleNamespace(
            object=SimpleNamespace(
                id="pi_one",
                amount_received=1250,
                metadata={"organization_id": str(org_id)},
            )
        ),
    )
    monkeypatch.setattr(StripeClient, "construct_event", lambda self, payload, signature: event)

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    from starlette.requests import Request

    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""},
        receive,
    )
    await stripe_webhook(request, db, "signed")
    await stripe_webhook(request, db, "signed")
    count = await db.scalar(
        select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.ref == "stripe-payment:pi_one")
    )
    assert count == 1
    grant = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == "stripe-payment:pi_one"))
    assert grant is not None and grant.amount == Decimal("12.500000")


@pytest.mark.asyncio
async def test_stripe_webhook_skips_invalid_metadata_and_tombstones_event(db, monkeypatch):
    event = SimpleNamespace(
        id="evt_bad_metadata",
        type="payment_intent.succeeded",
        data=SimpleNamespace(
            object=SimpleNamespace(
                id="pi_bad_metadata",
                amount_received=1250,
                metadata={"organization_id": "not-a-uuid"},
            )
        ),
    )
    monkeypatch.setattr(StripeClient, "construct_event", lambda self, payload, signature: event)

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    from starlette.requests import Request

    request = Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""},
        receive,
    )

    result = await stripe_webhook(request, db, "signed")

    assert result == {"received": True, "status": "skipped"}
    webhook = await db.get(NrWebhookEvent, "evt_bad_metadata")
    assert webhook is not None and webhook.succeeded is False
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry)) == 0


def test_bootstrap_binds_billing_port_without_shape_error():
    container = build_container("northrouter_billing.overlay:register")
    adapter = container.resolve(BillingPort, None)
    assert all(hasattr(adapter, method) for method in get_protocol_members(BillingPort))
    assert container.router_contributions()[0].capability == "northrouter_billing"
