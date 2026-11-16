"""Billing adapter, FX, webhook, authorization, and HTTP route behavior."""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, get_protocol_members

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import case, func, select
from starlette.requests import Request
from tests.conftest import seed_tenancy_org

from gateway.api.deps import build_budget_service, get_config, get_db
from gateway.api.routes.organization_keys import CreateOwnKeyRequest, create_own_key
from gateway.container import build_container
from gateway.core.config import GatewayConfig
from gateway.exceptions import TenancyError
from gateway.exceptions.organizations_exceptions import NotAuthorizedError, OrganizationNotFoundError
from gateway.models.api_keys import SERVICE_KEY_OWNER_ID_PREFIX, APIKey
from gateway.models.tenancy import Organization
from gateway.models.tenancy import User as TenancyUser
from gateway.ports.billing_port import NO_TOOLS, BilledModel, BillingPort, InsufficientFundsError
from gateway.repositories.api_keys.api_key_repository import ApiKeyRepository
from gateway.services.tenancy import OrganizationService
from northrouter_billing.adapter import NorthRouterBillingAdapter, _post_ledger
from northrouter_billing.fx import (
    FxQuote,
    FxRateError,
    convert_cad_to_usd,
    parse_boc_usdcad_payload,
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
from northrouter_billing.routes import (
    ETransferRequest,
    SubscriptionRequest,
    _handle_stripe_event,
    _public_subscription,
    _review_e_transfer,
    approve_e_transfer,
    billing_router,
    create_subscription,
    get_billing_context,
    get_ledger,
    list_e_transfers,
    list_subscriptions,
    reject_e_transfer,
    require_billing_manager,
    stripe_webhook,
    submit_e_transfer,
    sweep_holds,
)
from northrouter_billing.stripe_client import STRIPE_API_VERSION, StripeClient


async def _ensure_otari_org(db: Any, org_id: uuid.UUID | None = None) -> uuid.UUID:
    resolved_id = org_id or uuid.uuid4()
    if await db.get(Organization, resolved_id) is None:
        db.add(
            Organization(
                id=resolved_id,
                name=f"Org {resolved_id.hex[:8]}",
                slug=f"org-{resolved_id.hex[:12]}",
            )
        )
        await db.flush()
    return resolved_id


async def _org_and_credit(db: Any, amount: Decimal = Decimal("25")) -> uuid.UUID:
    org_id = await _ensure_otari_org(db)
    adapter = NorthRouterBillingAdapter(db)
    org = await adapter._org(org_id)
    await _post_ledger(
        db,
        org_id=org.id,
        entry_type="grant",
        amount=amount,
        ref=f"seed:{org_id}:{uuid.uuid4()}",
        description="Seed credit",
    )
    await db.flush()
    return org_id


def _webhook_request(body: bytes = b"{}") -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""},
        receive,
    )


def _mock_boc_quote(monkeypatch: pytest.MonkeyPatch, rate: str = "1.4257", rate_date: str | None = None) -> FxQuote:
    resolved_date = rate_date or datetime.now(UTC).date().isoformat()
    quote = FxQuote(rate=Decimal(rate), rate_date=resolved_date)

    async def fake_fetch(*, now: datetime | None = None) -> FxQuote:
        del now
        return quote

    monkeypatch.setattr("northrouter_billing.routes.fetch_boc_usdcad_rate", fake_fetch)
    return quote


async def _ledger_sum(db: Any, org_pk: uuid.UUID) -> Decimal:
    total = await db.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (NrLedgerEntry.type.in_(("grant", "refund", "invoice", "dispute")), NrLedgerEntry.amount),
                        (NrLedgerEntry.type == "consume", -NrLedgerEntry.amount),
                        else_=Decimal("0"),
                    )
                ),
                0,
            )
        ).where(NrLedgerEntry.org_id == org_pk)
    )
    return _money(total or 0)


# =============================================================================
# Phase 0: Authentication and authorization for the separate billing website
# =============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_phase0_owner_and_admin_can_access_billing_routes(db, role: str):
    org_id, _user, _ws, api_key = await seed_tenancy_org(db, role=role)
    authorized_org_id = await require_billing_manager(org_id, (api_key, False), db)
    assert authorized_org_id == org_id
    # GET routes and require_billing_manager must never create NrOrg rows
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0

    ledger_resp = await get_ledger(authorized_org_id, db)
    assert ledger_resp == {"data": []}
    subs_resp = await list_subscriptions(authorized_org_id, db)
    assert subs_resp == {"data": []}
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_phase0_member_and_viewer_are_refused_with_403(db, role: str):
    org_id, _user, _ws, api_key = await seed_tenancy_org(db, role=role)
    with pytest.raises(NotAuthorizedError) as raised:
        await require_billing_manager(org_id, (api_key, False), db)
    assert raised.value.status_code == 403
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_phase0_other_org_or_missing_membership_returns_404(db):
    org_id, _user, _ws, api_key = await seed_tenancy_org(db, role="owner")
    other_org_id = await _ensure_otari_org(db)

    with pytest.raises(OrganizationNotFoundError) as wrong_org:
        await require_billing_manager(other_org_id, (api_key, False), db)
    assert wrong_org.value.status_code == 404

    no_member_org_id, _u2, _w2, no_member_key = await seed_tenancy_org(db, role="owner", member_status="none")
    with pytest.raises(OrganizationNotFoundError) as no_membership:
        await require_billing_manager(no_member_org_id, (no_member_key, False), db)
    assert no_membership.value.status_code == 404

    assert await db.scalar(select(func.count()).select_from(NrOrg).where(NrOrg.otari_org_id == org_id)) == 0


@pytest.mark.asyncio
async def test_phase0_ordinary_inference_key_or_wrong_oauth_client_returns_403(db):
    org_id, _user, _ws, inference_key = await seed_tenancy_org(
        db, role="owner", self_service=False, oauth_client_id=None
    )
    with pytest.raises(NotAuthorizedError) as raised:
        await require_billing_manager(org_id, (inference_key, False), db)
    assert raised.value.status_code == 403

    _org2, _u2, _w2, untagged_self_service = await seed_tenancy_org(
        db, org_id=org_id, role="owner", self_service=True, oauth_client_id="other-app"
    )
    with pytest.raises(NotAuthorizedError) as wrong_app:
        await require_billing_manager(org_id, (untagged_self_service, False), db)
    assert wrong_app.value.status_code == 403
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_phase0_master_key_passes_for_existing_org_and_404s_for_unknown_org(db):
    org_id = await _ensure_otari_org(db)
    await db.commit()
    resolved = await require_billing_manager(org_id, (None, True), db)
    assert resolved == org_id
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0

    unknown_org_id = uuid.uuid4()
    with pytest.raises(OrganizationNotFoundError) as raised:
        await require_billing_manager(unknown_org_id, (None, True), db)
    assert raised.value.status_code == 404
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_phase0_http_matrix_no_credential_401_and_roles(db):
    org_id, _owner, _ws, owner_key = await seed_tenancy_org(db, role="owner")
    _, _admin, _ws2, admin_key = await seed_tenancy_org(db, org_id=org_id, role="admin")
    _, _member, _ws3, member_key = await seed_tenancy_org(db, org_id=org_id, role="member")
    _, _viewer, _ws4, viewer_key = await seed_tenancy_org(db, org_id=org_id, role="viewer")
    _, _inf_user, _ws5, inference_key = await seed_tenancy_org(
        db, org_id=org_id, role="owner", self_service=False, oauth_client_id=None
    )
    other_org_id = await _ensure_otari_org(db)
    await db.commit()

    app = FastAPI()
    app.state.container = build_container("northrouter_billing.overlay:register")

    @app.exception_handler(TenancyError)
    async def _handle_tenancy_error(_req: Request, exc: TenancyError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    app.include_router(billing_router, prefix="/api/v1")

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_config] = lambda: GatewayConfig(master_key="master-secret-token")

    from gateway.auth.models import hash_key

    # Set known hashes on our seeded keys so verify_api_key_or_master_key authenticates them
    raw_tokens = {
        "owner": "otari_owner_token_123456",
        "admin": "otari_admin_token_123456",
        "member": "otari_member_token_123456",
        "viewer": "otari_viewer_token_123456",
        "inference": "otari_inference_token_123456",
    }
    for key_obj, raw in (
        (owner_key, raw_tokens["owner"]),
        (admin_key, raw_tokens["admin"]),
        (member_key, raw_tokens["member"]),
        (viewer_key, raw_tokens["viewer"]),
        (inference_key, raw_tokens["inference"]),
    ):
        key_obj.key_hash = hash_key(raw)
    await db.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # No credential -> 401
        r_unauth = await client.get(f"/api/v1/nr/billing/orgs/{org_id}/ledger")
        assert r_unauth.status_code == 401

        # Owner and admin -> 200
        for role_name in ("owner", "admin"):
            r_ok = await client.get(
                f"/api/v1/nr/billing/orgs/{org_id}/ledger",
                headers={"Authorization": f"Bearer {raw_tokens[role_name]}"},
            )
            assert r_ok.status_code == 200, role_name

        # Member and viewer -> 403
        for role_name in ("member", "viewer"):
            r_forbidden = await client.get(
                f"/api/v1/nr/billing/orgs/{org_id}/ledger",
                headers={"Authorization": f"Bearer {raw_tokens[role_name]}"},
            )
            assert r_forbidden.status_code == 403, role_name

        # Ordinary inference key -> 403
        r_inf = await client.get(
            f"/api/v1/nr/billing/orgs/{org_id}/ledger",
            headers={"Authorization": f"Bearer {raw_tokens['inference']}"},
        )
        assert r_inf.status_code == 403

        # Other org -> 404
        r_other = await client.get(
            f"/api/v1/nr/billing/orgs/{other_org_id}/ledger",
            headers={"Authorization": f"Bearer {raw_tokens['owner']}"},
        )
        assert r_other.status_code == 404

        # Master key -> 200
        r_master = await client.get(
            f"/api/v1/nr/billing/orgs/{org_id}/ledger",
            headers={"Authorization": "Bearer master-secret-token"},
        )
        assert r_master.status_code == 200

        # GET routes must never create NrOrg rows
        assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_phase0_billing_context_endpoint_returns_org_and_role(db):
    org_id, _owner, _ws, owner_key = await seed_tenancy_org(db, role="owner")
    _, _viewer, _ws2, viewer_key = await seed_tenancy_org(db, org_id=org_id, role="viewer")
    _, _inf, _ws3, inference_key = await seed_tenancy_org(
        db, org_id=org_id, role="owner", self_service=False, oauth_client_id=None
    )

    owner_ctx = await get_billing_context(db, (owner_key, False))
    assert owner_ctx == {"organization_id": str(org_id), "role": "owner"}

    viewer_ctx = await get_billing_context(db, (viewer_key, False))
    assert viewer_ctx == {"organization_id": str(org_id), "role": "viewer"}

    with pytest.raises(NotAuthorizedError):
        await get_billing_context(db, (inference_key, False))

    master_ctx = await get_billing_context(db, (None, True))
    assert master_ctx["role"] in ("owner", "operator")
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_phase0_step3_tenant_key_creation_cannot_impersonate_another_users_uuid(db):
    """Verify no tenant-facing key endpoint can create a self_service key with another user's UUID."""
    org_id, caller_user, workspace, _ = await seed_tenancy_org(db, role="owner")
    victim_user_id = uuid.uuid4()

    # 1. CreateOwnKeyRequest forbids any user_id field on the request body
    with pytest.raises(ValidationError):
        CreateOwnKeyRequest.model_validate(
            {
                "key_name": "spoof",
                "user_id": str(victim_user_id),
                "metadata": {"oauth_client_id": "northrouter-web"},
            }
        )

    # 2. Minting a self-service member key via create_own_key binds user_id to caller_user.id,
    # even when the caller supplies metadata={"oauth_client_id": "northrouter-web"}.
    from fastapi import BackgroundTasks

    from gateway.adapters.api_key_format_adapter import DefaultApiKeyFormatAdapter
    from gateway.models.tenancy import WorkspaceMember

    db.add(
        WorkspaceMember(
            id=uuid.uuid4(),
            workspace_id=workspace.id,
            user_id=caller_user.id,
            role="owner",
            status="active",
        )
    )
    await db.commit()

    organizations = OrganizationService(db, membership_listener=None)
    budgets = build_budget_service(db)
    own_key_resp = await create_own_key(
        request=CreateOwnKeyRequest(
            key_name="member-key-with-spoofed-tag",
            workspace_id=workspace.id,
            metadata={"oauth_client_id": "northrouter-web"},
        ),
        identity=caller_user,
        background_tasks=BackgroundTasks(),
        db=db,
        config=GatewayConfig(),
        growth=None,
        key_format=DefaultApiKeyFormatAdapter(db),
        organizations=organizations,
        budgets=budgets,
    )
    assert own_key_resp.user_id == str(caller_user.id)
    assert own_key_resp.user_id != str(victim_user_id)

    # 3. Minting a self-service organization service key creates a synthetic service-key owner ID
    # that is not a UUID, so require_billing_manager rejects it with 403.
    service_key_resp = await create_own_key(
        request=CreateOwnKeyRequest(
            key_name="org-service-key",
            workspace_id=workspace.id,
            is_service_key=True,
            metadata={"oauth_client_id": "northrouter-web"},
        ),
        identity=caller_user,
        background_tasks=BackgroundTasks(),
        db=db,
        config=GatewayConfig(),
        growth=None,
        key_format=DefaultApiKeyFormatAdapter(db),
        organizations=organizations,
        budgets=budgets,
    )
    assert service_key_resp.user_id is not None
    assert service_key_resp.user_id.startswith(SERVICE_KEY_OWNER_ID_PREFIX)
    service_key_row = await db.get(APIKey, service_key_resp.id)
    assert service_key_row is not None and service_key_row.self_service is True
    with pytest.raises(NotAuthorizedError):
        await require_billing_manager(org_id, (service_key_row, False), db)

    # 4. OAuth app key issuance (ApiKeyRepository.issue_app_key) binds user_id to the approving user.
    repo = ApiKeyRepository(db)
    oauth_key_id = str(uuid.uuid4())
    await repo.issue_app_key(
        key_id=oauth_key_id,
        key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        prefix="otari_",
        suffix="9999",
        name="NorthRouter Billing (OAuth)",
        user_id=str(caller_user.id),
        workspace_id=workspace.id,
        client_id="northrouter-web",
    )
    issued = await db.get(APIKey, oauth_key_id)
    assert issued is not None
    assert issued.self_service is True
    assert issued.user_id == str(caller_user.id)


@pytest.mark.asyncio
async def test_phase0_operator_routes_record_reviewer_and_list_pending_transfers(db, monkeypatch):
    _mock_boc_quote(monkeypatch, "1.4000")
    org_id = await _ensure_otari_org(db)
    pending_one = await submit_e_transfer(
        org_id,
        ETransferRequest(amount=Decimal("100"), reference="ref-op-1"),
        db,
    )
    pending_two = await submit_e_transfer(
        org_id,
        ETransferRequest(amount=Decimal("500"), reference="ref-op-2"),
        db,
    )

    operator_user = TenancyUser(
        id=uuid.uuid4(),
        email="ops@northrouter.ca",
        is_active=True,
        is_superuser=True,
    )
    listed = await list_e_transfers(db, None, operator_user, status_filter="pending", limit=500)
    assert [item["id"] for item in listed["data"]] == [pending_one["id"], pending_two["id"]]

    # Session-authenticated operator records identity email
    await approve_e_transfer(uuid.UUID(pending_one["id"]), db, None, operator_user, master_key=None)
    row_one = await db.get(NrETransfer, uuid.UUID(pending_one["id"]))
    assert row_one is not None
    assert row_one.status == "approved"
    assert row_one.reviewed_by == "ops@northrouter.ca"

    # Header master-key operator records "master-key"
    await reject_e_transfer(uuid.UUID(pending_two["id"]), db, None, operator_user, master_key="raw-master-token")
    row_two = await db.get(NrETransfer, uuid.UUID(pending_two["id"]))
    assert row_two is not None
    assert row_two.status == "rejected"
    assert row_two.reviewed_by == "master-key"


# =============================================================================
# Phase 1: Stripe subscriptions and invoice-driven credits
# =============================================================================


def test_phase1_stripe_client_pins_basil_api_version(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_sub_create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return {"id": "sub_123", "status": "incomplete"}

    monkeypatch.setattr("stripe.Subscription.create", fake_sub_create)
    client = StripeClient()
    client.create_subscription(
        customer_id="cus_1",
        price_id="price_starter",
        organization_id="org_1",
        plan="starter",
        credit_amount="100",
        idempotency_key="idem-1",
    )
    assert captured["stripe_version"] == STRIPE_API_VERSION
    assert captured["stripe_version"] >= "2025-03-31.basil"
    assert captured["payment_behavior"] == "default_incomplete"
    assert captured["payment_settings"] == {"save_default_payment_method": "on_subscription"}
    assert captured["expand"] == ["latest_invoice.confirmation_secret"]
    assert captured["idempotency_key"] == "idem-1"


@pytest.mark.asyncio
async def test_phase1_create_subscription_returns_client_secret_and_refuses_second_subscription_409(db, monkeypatch):
    org_id = await _ensure_otari_org(db)
    monkeypatch.setattr(
        StripeClient,
        "create_customer",
        lambda self, **kwargs: {"id": "cus_sub_test"},
    )
    created_calls: list[dict[str, Any]] = []

    def fake_create_subscription(self: StripeClient, **kwargs: Any) -> dict[str, Any]:
        created_calls.append(kwargs)
        return {
            "id": "sub_new_1",
            "status": "incomplete",
            "items": {"data": [{"current_period_end": 1800000000, "price": {"id": kwargs["price_id"]}}]},
            "latest_invoice": {"confirmation_secret": {"client_secret": "pi_secret_123"}},
        }

    monkeypatch.setattr(StripeClient, "create_subscription", fake_create_subscription)

    # price_id is no longer accepted on SubscriptionRequest
    with pytest.raises(ValidationError):
        SubscriptionRequest.model_validate({"plan": "starter", "price_id": "price_starter"})

    resp = await create_subscription(
        org_id,
        SubscriptionRequest(plan="starter"),
        db,
        idempotency_key="client-idem-key",
    )
    assert resp == {
        "id": "sub_new_1",
        "status": "incomplete",
        "client_secret": "pi_secret_123",
    }
    assert created_calls[0]["price_id"] == "price_starter"
    assert created_calls[0]["idempotency_key"] == "client-idem-key"

    # Second subscription while incomplete/active/trialing/past_due -> 409
    for blocking_status in ("incomplete", "active", "trialing", "past_due"):
        sub_row = await db.scalar(select(NrSubscription).where(NrSubscription.stripe_subscription_id == "sub_new_1"))
        assert sub_row is not None
        sub_row.status = blocking_status
        await db.commit()
        with pytest.raises(HTTPException) as conflict:
            await create_subscription(org_id, SubscriptionRequest(plan="growth"), db)
        assert conflict.value.status_code == 409


@pytest.mark.asyncio
async def test_phase1_no_credit_before_invoice_paid_or_while_trialing(db):
    org_id = await _ensure_otari_org(db)
    period_end = int((datetime.now(UTC) + timedelta(days=30)).timestamp())

    # Subscription created as active or trialing does NOT grant credit until invoice.paid
    for status_val in ("trialing", "active"):
        await _handle_stripe_event(
            db,
            "customer.subscription.created",
            {
                "id": f"sub_{status_val}",
                "customer": f"cus_{status_val}",
                "status": status_val,
                "items": {"data": [{"current_period_end": period_end, "price": {"id": "price_starter"}}]},
                "metadata": {"organization_id": str(org_id)},
            },
            event_created=100,
        )

    adapter = NorthRouterBillingAdapter(db)
    await adapter.apply_due_credit(organization_id=org_id)
    assert not await adapter.has_funds_on_deposit(
        organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
    )

    # A $0 trial invoice (amount_paid=0) grants nothing
    await _handle_stripe_event(
        db,
        "invoice.paid",
        {
            "id": "in_trial_zero",
            "billing_reason": "subscription_create",
            "amount_paid": 0,
            "customer": "cus_trialing",
            "parent": {
                "subscription_details": {
                    "subscription": "sub_trialing",
                    "metadata": {"organization_id": str(org_id)},
                }
            },
            "lines": {"data": [{"pricing": {"price_details": {"price": "price_starter"}}}]},
        },
    )
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry)) == 0


@pytest.mark.asyncio
async def test_phase1_invoice_paid_grants_once_per_invoice_and_derives_plan_from_price_not_metadata(db):
    org_id = await _ensure_otari_org(db)
    invoice_event = {
        "id": "in_paid_1",
        "billing_reason": "subscription_create",
        "amount_paid": 10000,
        "customer": "cus_paid_1",
        "parent": {
            "subscription_details": {
                "subscription": "sub_paid_1",
                # Stale metadata says scale ($1000), but line-item price is starter ($100)
                "metadata": {"organization_id": str(org_id), "plan": "scale", "credit_amount": "1000"},
            }
        },
        "lines": {"data": [{"pricing": {"price_details": {"price": "price_starter"}}}]},
    }
    await _handle_stripe_event(db, "invoice.paid", invoice_event)
    await _handle_stripe_event(db, "invoice.paid", invoice_event)

    entries = list(await db.scalars(select(NrLedgerEntry).where(NrLedgerEntry.ref == "stripe-invoice:in_paid_1")))
    assert len(entries) == 1
    assert entries[0].amount == Decimal("100.000000")

    # Renewal after plan downgrade (metadata still says scale, price is growth -> $500)
    renewal_event = {
        "id": "in_paid_2",
        "billing_reason": "subscription_cycle",
        "amount_paid": 50000,
        "customer": "cus_paid_1",
        "parent": {
            "subscription_details": {
                "subscription": "sub_paid_1",
                "metadata": {"organization_id": str(org_id), "plan": "scale", "credit_amount": "1000"},
            }
        },
        "lines": {"data": [{"price": {"id": "price_growth"}}]},
    }
    await _handle_stripe_event(db, "invoice.paid", renewal_event)
    renewal_entry = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == "stripe-invoice:in_paid_2"))
    assert renewal_entry is not None
    assert renewal_entry.amount == Decimal("500.000000")

    # subscription_update invoice is ignored with a warning
    update_invoice = {
        "id": "in_update_ignored",
        "billing_reason": "subscription_update",
        "amount_paid": 2000,
        "customer": "cus_paid_1",
        "lines": {"data": [{"price": {"id": "price_scale"}}]},
    }
    await _handle_stripe_event(db, "invoice.paid", update_invoice)
    assert await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == "stripe-invoice:in_update_ignored")) is None


@pytest.mark.asyncio
async def test_phase1_payment_failed_only_logs_and_top_up_does_not_reactivate_suspended_org(db, monkeypatch):
    _mock_boc_quote(monkeypatch, "1.4000")
    org_id = await _org_and_credit(db, Decimal("25"))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    org.stripe_customer_id = "cus_no_suspend"

    # invoice.payment_failed only logs and does not suspend the organization
    await _handle_stripe_event(db, "invoice.payment_failed", {"id": "in_fail", "customer": "cus_no_suspend"})
    assert org.status == "active"
    assert org.suspend_reason is None

    # Manually suspend the org (or dispute-suspend it); neither top-up nor e-transfer reactivates it
    org.status = "suspended"
    org.suspend_reason = "dispute"
    await db.flush()

    await _handle_stripe_event(
        db,
        "payment_intent.succeeded",
        {
            "id": "pi_topup_suspended",
            "currency": "usd",
            "amount_received": 1000,
            "metadata": {"organization_id": str(org_id)},
        },
    )
    assert org.status == "suspended"
    assert org.suspend_reason == "dispute"

    pending = await submit_e_transfer(
        org_id,
        ETransferRequest(amount=Decimal("100"), reference="ref-no-reactivate"),
        db,
    )
    await _review_e_transfer(uuid.UUID(pending["id"]), db, approve=True)
    await db.refresh(org)
    assert org.status == "suspended"
    assert org.suspend_reason == "dispute"


@pytest.mark.asyncio
async def test_phase1_subscription_events_read_item_period_and_ignore_stale_events(db):
    org_id = await _ensure_otari_org(db)
    newer_period = int((datetime.now(UTC) + timedelta(days=60)).timestamp())
    older_period = int((datetime.now(UTC) + timedelta(days=30)).timestamp())

    await _handle_stripe_event(
        db,
        "customer.subscription.updated",
        {
            "id": "sub_ordering",
            "customer": "cus_ordering",
            "status": "active",
            "items": {"data": [{"current_period_end": newer_period, "price": {"id": "price_growth"}}]},
            "metadata": {"organization_id": str(org_id)},
        },
        event_created=200,
    )
    # Out-of-order older event is ignored
    await _handle_stripe_event(
        db,
        "customer.subscription.updated",
        {
            "id": "sub_ordering",
            "customer": "cus_ordering",
            "status": "past_due",
            "items": {"data": [{"current_period_end": older_period, "price": {"id": "price_starter"}}]},
            "metadata": {"organization_id": str(org_id)},
        },
        event_created=150,
    )
    sub = await db.scalar(select(NrSubscription).where(NrSubscription.stripe_subscription_id == "sub_ordering"))
    assert sub is not None
    assert sub.plan == "growth"
    assert sub.price_id == "price_growth"
    assert sub.status == "active"
    assert sub.last_event_created == 200
    stored_period = (
        sub.current_period_end.replace(tzinfo=UTC) if sub.current_period_end.tzinfo is None else sub.current_period_end
    )
    assert int(stored_period.timestamp()) == newer_period

    public_view = _public_subscription(
        {
            "id": "sub_ordering",
            "status": "active",
            "items": {"data": [{"current_period_end": newer_period}]},
        }
    )
    assert public_view["current_period_end"] == datetime.fromtimestamp(newer_period, UTC).isoformat()


# =============================================================================
# Phase 2: Money correctness (FX, disputes, currency checks, webhook errors)
# =============================================================================


def test_phase2_fx_rounding_100_cad_at_1_4257_is_70_14_usd_and_margin():
    # Exact plan test case: 100 CAD at 1.4257 = 70.14 USD
    assert convert_cad_to_usd(Decimal("100"), rate=Decimal("1.4257"), margin=Decimal("0")) == Decimal("70.14")
    # With 2% margin: 100 / 1.4257 * 0.98 = 68.73816... -> rounded down to 68.73
    assert convert_cad_to_usd(Decimal("100"), rate=Decimal("1.4257"), margin=Decimal("0.02")) == Decimal("68.73")


def test_phase2_fx_payload_validation_rejects_stale_and_out_of_range_rates():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
    valid = parse_boc_usdcad_payload(
        {"observations": [{"d": "2026-11-13", "FXUSDCAD": {"v": "1.4257"}}]},
        now=now,
    )
    assert valid.rate == Decimal("1.4257")
    assert valid.rate_date == "2026-11-13"

    # Older than 7 days -> rejected
    with pytest.raises(FxRateError, match="stale"):
        parse_boc_usdcad_payload(
            {"observations": [{"d": "2026-11-01", "FXUSDCAD": {"v": "1.4257"}}]},
            now=now,
        )

    # Outside [0.5, 3.0] or non-finite -> rejected
    for bad_rate in ("0.49", "3.01", "NaN", "Infinity"):
        with pytest.raises(FxRateError):
            parse_boc_usdcad_payload(
                {"observations": [{"d": "2026-11-13", "FXUSDCAD": {"v": bad_rate}}]},
                now=now,
            )


@pytest.mark.asyncio
async def test_phase2_e_transfer_approval_stores_fx_and_handles_failed_fetch_and_double_approve(db, monkeypatch):
    monkeypatch.setenv("NORTHROUTER_ETRANSFER_FX_MARGIN", "0.01")
    org_id = await _ensure_otari_org(db)

    with pytest.raises(HTTPException) as bad_pkg:
        await submit_e_transfer(org_id, ETransferRequest(amount=Decimal("250"), reference="bad-pkg"), db)
    assert bad_pkg.value.status_code == 422
    assert "CA$100, CA$500 or CA$1000" in bad_pkg.value.detail

    pending = await submit_e_transfer(
        org_id,
        ETransferRequest(amount=Decimal("100"), reference="fx-transfer-1"),
        db,
    )
    transfer_uuid = uuid.UUID(pending["id"])

    # Failed FX fetch -> 503 and transfer remains pending
    async def failing_fetch(*, now: datetime | None = None) -> FxQuote:
        raise FxRateError("Bank of Canada timeout")

    monkeypatch.setattr("northrouter_billing.routes.fetch_boc_usdcad_rate", failing_fetch)
    with pytest.raises(HTTPException) as fetch_err:
        await _review_e_transfer(transfer_uuid, db, approve=True, reviewed_by="ops@northrouter.ca")
    assert fetch_err.value.status_code == 503
    still_pending = await db.get(NrETransfer, transfer_uuid)
    assert still_pending is not None and still_pending.status == "pending"

    # Successful FX fetch at 1.4257 with 1% margin: 100 / 1.4257 * 0.99 = 69.43957... -> 69.43 USD
    _mock_boc_quote(monkeypatch, "1.4257", "2026-11-13")
    await _review_e_transfer(transfer_uuid, db, approve=True, reviewed_by="ops@northrouter.ca")
    approved = await db.get(NrETransfer, transfer_uuid)
    assert approved is not None
    assert approved.status == "approved"
    assert approved.currency == "CAD"
    assert approved.fx_rate == Decimal("1.425700")
    assert approved.fx_rate_date == "2026-11-13"
    assert approved.fx_source == "boc:FXUSDCAD"
    assert approved.fx_margin == Decimal("0.010000")
    assert approved.credited_usd == Decimal("69.430000")

    grant = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == f"etransfer:{transfer_uuid}"))
    assert grant is not None
    assert grant.amount == Decimal("69.430000")
    assert grant.metadata_json is not None
    meta = json.loads(grant.metadata_json)
    assert meta["currency"] == "CAD"
    assert meta["fx_source"] == "boc:FXUSDCAD"
    assert meta["credited_usd"] == "69.430000"

    # Approving twice -> 409
    with pytest.raises(HTTPException) as double_approve:
        await _review_e_transfer(transfer_uuid, db, approve=True, reviewed_by="ops@northrouter.ca")
    assert double_approve.value.status_code == 409


@pytest.mark.asyncio
async def test_phase2_dispute_created_suspends_and_closed_won_reverses_when_no_other_open_disputes(db):
    org_id = await _org_and_credit(db, Decimal("50"))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None

    # Create two disputes: dp_1 ($20) and dp_2 ($10)
    await _handle_stripe_event(
        db,
        "charge.dispute.created",
        {"id": "dp_1", "amount": 2000, "currency": "usd", "metadata": {"organization_id": str(org_id)}},
    )
    await _handle_stripe_event(
        db,
        "charge.dispute.created",
        {"id": "dp_2", "amount": 1000, "currency": "usd", "metadata": {"organization_id": str(org_id)}},
    )
    await db.refresh(org)
    assert org.status == "suspended"
    assert org.suspend_reason == "dispute"
    assert org.balance == Decimal("20.000000")

    # Winning dp_1 credits $20 back, but org stays suspended because dp_2 is still open
    await _handle_stripe_event(
        db,
        "charge.dispute.closed",
        {"id": "dp_1", "amount": 2000, "currency": "usd", "status": "won"},
    )
    await db.refresh(org)
    assert org.balance == Decimal("40.000000")
    assert org.status == "suspended"
    assert org.suspend_reason == "dispute"

    # Winning dp_2 credits $10 back and reactivates the org
    await _handle_stripe_event(
        db,
        "charge.dispute.closed",
        {"id": "dp_2", "amount": 1000, "currency": "usd", "status": "won"},
    )
    await db.refresh(org)
    assert org.balance == Decimal("50.000000")
    assert org.status == "active"
    assert org.suspend_reason is None


@pytest.mark.asyncio
async def test_phase2_non_usd_currency_events_are_logged_and_recorded_as_skipped(db, monkeypatch):
    org_id = await _org_and_credit(db, Decimal("25"))
    for idx, (ev_type, payload) in enumerate(
        [
            (
                "payment_intent.succeeded",
                {
                    "id": "pi_cad",
                    "amount_received": 1000,
                    "currency": "cad",
                    "metadata": {"organization_id": str(org_id)},
                },
            ),
            (
                "charge.refunded",
                {
                    "id": "ch_cad",
                    "amount_refunded": 500,
                    "currency": "cad",
                    "metadata": {"organization_id": str(org_id)},
                },
            ),
            (
                "charge.dispute.created",
                {
                    "id": "dp_jpy",
                    "amount": 500,
                    "currency": "jpy",
                    "metadata": {"organization_id": str(org_id)},
                },
            ),
        ]
    ):
        event = SimpleNamespace(
            id=f"evt_non_usd_{idx}",
            type=ev_type,
            created=100 + idx,
            data=SimpleNamespace(object=payload),
        )
        monkeypatch.setattr(StripeClient, "construct_event", lambda self, body, sig, e=event: e)
        resp = await stripe_webhook(_webhook_request(), db, "signed")
        assert resp == {"received": True, "status": "skipped"}
        ev_row = await db.get(NrWebhookEvent, f"evt_non_usd_{idx}")
        assert ev_row is not None and ev_row.succeeded is False


@pytest.mark.asyncio
async def test_phase2_webhook_unknown_org_or_subscription_returns_200_skipped_and_secret_errors_split(db, monkeypatch):
    unknown_org = uuid.uuid4()
    for idx, (ev_type, payload) in enumerate(
        [
            (
                "customer.subscription.created",
                {
                    "id": "sub_unknown_org",
                    "customer": "cus_unknown",
                    "status": "active",
                    "items": {"data": [{"current_period_end": 1800000000, "price": {"id": "price_starter"}}]},
                    "metadata": {"organization_id": str(unknown_org)},
                },
            ),
            (
                "customer.subscription.deleted",
                {"id": "sub_never_stored", "customer": "cus_unknown"},
            ),
            (
                "invoice.paid",
                {
                    "id": "in_unknown_org",
                    "billing_reason": "subscription_create",
                    "amount_paid": 10000,
                    "customer": "cus_unknown",
                    "lines": {"data": [{"price": {"id": "price_starter"}}]},
                },
            ),
        ]
    ):
        event = SimpleNamespace(
            id=f"evt_unknown_{idx}",
            type=ev_type,
            created=200 + idx,
            data=SimpleNamespace(object=payload),
        )
        monkeypatch.setattr(StripeClient, "construct_event", lambda self, body, sig, e=event: e)
        result = await stripe_webhook(_webhook_request(), db, "signed")
        assert result == {"received": True, "status": "skipped"}
        webhook_row = await db.get(NrWebhookEvent, f"evt_unknown_{idx}")
        assert webhook_row is not None and webhook_row.succeeded is False

    # Missing webhook secret -> 500
    monkeypatch.undo()
    monkeypatch.delenv("NORTHROUTER_STRIPE_WEBHOOK_SECRET", raising=False)
    with pytest.raises(HTTPException) as missing_secret:
        await stripe_webhook(_webhook_request(), db, "sig_header")
    assert missing_secret.value.status_code == 500

    # Bad signature -> 400
    monkeypatch.setenv("NORTHROUTER_STRIPE_WEBHOOK_SECRET", "whsec_real")
    with pytest.raises(HTTPException) as bad_sig:
        await stripe_webhook(_webhook_request(b"invalid-payload"), db, "t=1,v1=bad")
    assert bad_sig.value.status_code == 400


# =============================================================================
# Phase 3: Limits, cross-org e-transfer references, hold release, ledger privacy
# =============================================================================


@pytest.mark.asyncio
async def test_phase3_env_vars_validated_at_startup_and_enforced(db, monkeypatch):
    # Invalid startup env vars fail build_container / register()
    monkeypatch.setenv("NORTHROUTER_ETRANSFER_FX_MARGIN", "0.25")
    with pytest.raises(ValueError, match="NORTHROUTER_ETRANSFER_FX_MARGIN"):
        build_container("northrouter_billing.overlay:register")
    monkeypatch.setenv("NORTHROUTER_ETRANSFER_FX_MARGIN", "0.05")

    monkeypatch.setenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "0")
    with pytest.raises(ValueError, match="NORTHROUTER_ORG_CONCURRENCY_LIMIT"):
        build_container("northrouter_billing.overlay:register")
    monkeypatch.setenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "4")

    monkeypatch.setenv("NORTHROUTER_MIN_AVAILABLE_USD", "-1")
    with pytest.raises(ValueError, match="NORTHROUTER_MIN_AVAILABLE_USD"):
        build_container("northrouter_billing.overlay:register")
    monkeypatch.setenv("NORTHROUTER_MIN_AVAILABLE_USD", "10")

    container = build_container("northrouter_billing.overlay:register")
    adapter = container.resolve(BillingPort, db)
    org_id = await _org_and_credit(db, Decimal("8"))

    limit = await adapter.concurrency_limit(
        organization_id=org_id,
        billed=BilledModel(provider="openai", model="gpt-4o", open_weights=False),
        actor_user_id=None,
    )
    assert limit == 4

    with pytest.raises(InsufficientFundsError):
        await adapter.require_funds_on_deposit(
            organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
        )
    with pytest.raises(InsufficientFundsError):
        await adapter.require_unheld_funds(organization_id=org_id)


@pytest.mark.asyncio
async def test_phase3_cross_org_normalized_e_transfer_reference_uniqueness(db):
    org_one = await _ensure_otari_org(db)
    org_two = await _ensure_otari_org(db)

    await submit_e_transfer(org_one, ETransferRequest(amount=Decimal("100"), reference="  Interac-ABC-123 "), db)
    with pytest.raises(HTTPException) as duplicate:
        await submit_e_transfer(org_two, ETransferRequest(amount=Decimal("500"), reference="interac-abc-123"), db)
    assert duplicate.value.status_code == 409
    assert duplicate.value.detail == "duplicate reference"


@pytest.mark.asyncio
async def test_phase3_release_hold_with_no_match_logs_warning_and_returns(db, caplog):
    org_id = await _org_and_credit(db, Decimal("25"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    assert "No open hold matches amount" in caplog.text


@pytest.mark.asyncio
async def test_phase3_customer_ledger_hides_hold_release_and_internal_refs(db):
    org_id = await _org_and_credit(db, Decimal("50"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.charge(
        organization_id=org_id,
        amount=Decimal("3"),
        billed=None,
        tools=NO_TOOLS,
        description="chat completion",
    )
    await db.commit()

    response = await get_ledger(org_id, db)
    types = [row["type"] for row in response["data"]]
    assert "hold" not in types
    assert "release" not in types
    assert set(types) == {"grant", "consume"}
    for row in response["data"]:
        assert "reference" not in row
        assert "ref" not in row


# =============================================================================
# Phase 4: Running balance invariant and existing adapter behavior
# =============================================================================


@pytest.mark.asyncio
async def test_phase4_stored_balance_equals_ledger_sum_across_all_entry_types(db, monkeypatch):
    _mock_boc_quote(monkeypatch, "1.4257", "2026-11-13")
    org_id = await _org_and_credit(db, Decimal("25"))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None

    # Card top-up (+$12.50) plus replay (no duplicate change)
    pi_obj = {
        "id": "pi_bal_1",
        "currency": "usd",
        "amount_received": 1250,
        "metadata": {"organization_id": str(org_id)},
    }
    await _handle_stripe_event(db, "payment_intent.succeeded", pi_obj)
    await _handle_stripe_event(db, "payment_intent.succeeded", pi_obj)

    # Subscription invoice (+$100)
    await _handle_stripe_event(
        db,
        "invoice.paid",
        {
            "id": "in_bal_1",
            "billing_reason": "subscription_create",
            "amount_paid": 10000,
            "metadata": {"organization_id": str(org_id)},
            "lines": {"data": [{"price": {"id": "price_starter"}}]},
        },
    )

    # E-transfer approval (+$70.14)
    pending = await submit_e_transfer(org_id, ETransferRequest(amount=Decimal("100"), reference="bal-etransfer"), db)
    await _review_e_transfer(uuid.UUID(pending["id"]), db, approve=True)

    # Hold, release, and charge (-$4.25)
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.charge(organization_id=org_id, amount=Decimal("4.25"), billed=None, tools=NO_TOOLS)

    # Partial refund (-$2.50)
    await _handle_stripe_event(
        db,
        "charge.refunded",
        {
            "id": "ch_bal_1",
            "payment_intent": "pi_bal_1",
            "currency": "usd",
            "amount_refunded": 250,
            "metadata": {"organization_id": str(org_id)},
        },
    )

    # Dispute (-$5.00)
    await _handle_stripe_event(
        db,
        "charge.dispute.created",
        {
            "id": "dp_bal_1",
            "amount": 500,
            "currency": "usd",
            "metadata": {"organization_id": str(org_id)},
        },
    )
    await db.commit()
    await db.refresh(org)

    expected = await _ledger_sum(db, org.id)
    assert org.balance == expected
    assert org.balance == Decimal("195.890000")


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
async def test_refund_cumulative_amount_is_idempotent_and_charge_id_is_literal(db):
    org_id = await _org_and_credit(db, Decimal("25"))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    await _post_ledger(
        db,
        org_id=org.id,
        entry_type="grant",
        amount=Decimal("25"),
        ref="stripe-payment:pi_refund",
    )
    await _post_ledger(
        db,
        org_id=org.id,
        entry_type="refund",
        amount=Decimal("-3"),
        ref="stripe-refund:chX1:10.000000",
    )
    event = {
        "id": "ch_1",
        "payment_intent": "pi_refund",
        "currency": "usd",
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
async def test_expired_hold_does_not_reduce_funds_and_operator_sweep_releases_it(db):
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
    operator_user = TenancyUser(id=uuid.uuid4(), email="ops@northrouter.ca", is_active=True, is_superuser=True)
    result = await sweep_holds(db, None, operator_user)
    assert result == {"released": 1}
    await db.refresh(hold)
    assert hold.state == "released"
    release = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == f"release:{hold.id}"))
    assert release is not None and release.amount == Decimal("25.000000")


@pytest.mark.asyncio
async def test_deleted_subscription_is_canceled(db):
    org_id = await _ensure_otari_org(db)
    org = await NorthRouterBillingAdapter(db)._org(org_id)
    subscription = NrSubscription(
        org_id=org.id,
        stripe_subscription_id="sub_deleted",
        price_id="price_starter",
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
    assert subscription.status == "canceled"


@pytest.mark.asyncio
async def test_release_hold_accepts_expired_open_hold(db):
    org_id = await _org_and_credit(db, Decimal("60"))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    now = datetime.now(UTC)
    expired_created_at = now - timedelta(hours=2)
    expired_hold = NrHold(
        org_id=org.id,
        amount=Decimal("25"),
        state="open",
        created_at=expired_created_at,
        expires_at=expired_created_at + timedelta(hours=1),
    )
    unexpired_hold = NrHold(
        org_id=org.id,
        amount=Decimal("25"),
        state="open",
        created_at=now - timedelta(minutes=5),
        expires_at=now + timedelta(minutes=55),
    )
    db.add_all([expired_hold, unexpired_hold])
    await db.flush()

    adapter = NorthRouterBillingAdapter(db)
    await adapter.release_hold(organization_id=org_id, amount=Decimal("25"))

    await db.refresh(unexpired_hold)
    await db.refresh(expired_hold)
    assert unexpired_hold.state == "released"
    assert expired_hold.state == "open"

    await adapter.release_hold(organization_id=org_id, amount=Decimal("25"))

    await db.refresh(expired_hold)
    assert expired_hold.state == "released"


@pytest.mark.asyncio
async def test_charge_refunded_for_unknown_charge_returns_cleanly(db):
    handled = await _handle_stripe_event(
        db,
        "charge.refunded",
        {
            "id": "ch_unknown",
            "payment_intent": "pi_unknown",
            "currency": "usd",
            "amount_refunded": 500,
        },
    )
    assert handled is False
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry)) == 0


def test_money_rejects_nan_and_infinity():
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), "NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="finite"):
            _money(bad)


@pytest.mark.asyncio
async def test_incremental_partial_refunds_on_one_charge(db):
    org_id = await _org_and_credit(db, Decimal("25"))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    await _post_ledger(
        db,
        org_id=org.id,
        entry_type="grant",
        amount=Decimal("25"),
        ref="stripe-payment:pi_partial",
    )
    await db.flush()

    await _handle_stripe_event(
        db,
        "charge.refunded",
        {
            "id": "ch_partial",
            "payment_intent": "pi_partial",
            "currency": "usd",
            "amount_refunded": 200,
        },
    )
    await _handle_stripe_event(
        db,
        "charge.refunded",
        {
            "id": "ch_partial",
            "currency": "usd",
            "amount_refunded": 500,
        },
    )
    entries = list(
        await db.scalars(
            select(NrLedgerEntry)
            .where(
                NrLedgerEntry.org_id == org.id,
                NrLedgerEntry.type == "refund",
            )
            .order_by(NrLedgerEntry.created_at, NrLedgerEntry.ref)
        )
    )
    assert [entry.amount for entry in entries] == [Decimal("-2.000000"), Decimal("-3.000000")]


@pytest.mark.asyncio
async def test_release_hold_zero_is_a_noop(db):
    org_id = await _org_and_credit(db)
    await NorthRouterBillingAdapter(db).release_hold(organization_id=org_id, amount=Decimal("0"))
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.type == "release")) == 0


@pytest.mark.asyncio
async def test_stripe_webhook_replay_grants_once(db, monkeypatch):
    org_id = await _ensure_otari_org(db)
    await db.commit()
    event = SimpleNamespace(
        id="evt_payment",
        type="payment_intent.succeeded",
        created=100,
        data=SimpleNamespace(
            object=SimpleNamespace(
                id="pi_one",
                currency="usd",
                amount_received=1250,
                metadata={"organization_id": str(org_id)},
            )
        ),
    )
    monkeypatch.setattr(StripeClient, "construct_event", lambda self, payload, signature: event)

    await stripe_webhook(_webhook_request(), db, "signed")
    await stripe_webhook(_webhook_request(), db, "signed")
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
        created=100,
        data=SimpleNamespace(
            object=SimpleNamespace(
                id="pi_bad_metadata",
                currency="usd",
                amount_received=1250,
                metadata={"organization_id": "not-a-uuid"},
            )
        ),
    )
    monkeypatch.setattr(StripeClient, "construct_event", lambda self, payload, signature: event)

    result = await stripe_webhook(_webhook_request(), db, "signed")

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


def test_migration_0004_backfills_balance_reactivates_payment_failed_and_downgrades(tmp_path, monkeypatch):
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    db_file = tmp_path / "nr_mig_test.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    monkeypatch.setenv("NORTHROUTER_DATABASE_URL", db_url)
    alembic_ini = Path(__file__).resolve().parents[1] / "northrouter_billing" / "alembic.ini"
    cfg = Config(str(alembic_ini))
    cfg.set_main_option("script_location", str(alembic_ini.parent / "alembic"))

    command.upgrade(cfg, "0003_billing_hardening")
    sync_engine = create_engine(f"sqlite:///{db_file}")
    org_pk = uuid.uuid4().hex
    otari_id = uuid.uuid4().hex
    manual_pk = uuid.uuid4().hex
    manual_otari_id = uuid.uuid4().hex
    with sync_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO nr_orgs (id, otari_org_id, status, suspend_reason, created_at, updated_at) "
                "VALUES (:id, :otari_id, 'suspended', 'payment_failed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": org_pk, "otari_id": otari_id},
        )
        conn.execute(
            text(
                "INSERT INTO nr_orgs (id, otari_org_id, status, suspend_reason, created_at, updated_at) "
                "VALUES (:id, :otari_id, 'suspended', 'manual_review', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"id": manual_pk, "otari_id": manual_otari_id},
        )
        conn.execute(
            text(
                "INSERT INTO nr_ledger_entries (id, org_id, type, amount, ref, created_at) "
                "VALUES (:id1, :org, 'grant', 50.000000, 'g1', CURRENT_TIMESTAMP), "
                "(:id2, :org, 'consume', 12.500000, 'c1', CURRENT_TIMESTAMP)"
            ),
            {"id1": uuid.uuid4().hex, "id2": uuid.uuid4().hex, "org": org_pk},
        )
        conn.execute(
            text(
                "INSERT INTO nr_e_transfers (id, org_id, amount, reference, status, created_at) "
                "VALUES (:id, :org, 100.000000, '  Ref-Mixed-Case ', 'pending', CURRENT_TIMESTAMP)"
            ),
            {"id": uuid.uuid4().hex, "org": org_pk},
        )

    command.upgrade(cfg, "head")
    with sync_engine.begin() as conn:
        row = conn.execute(
            text("SELECT status, suspend_reason, balance FROM nr_orgs WHERE id = :id"),
            {"id": org_pk},
        ).one()
        assert row[0] == "active"
        assert row[1] is None
        assert Decimal(str(row[2])) == Decimal("37.5")

        manual_row = conn.execute(
            text("SELECT status, suspend_reason FROM nr_orgs WHERE id = :id"),
            {"id": manual_pk},
        ).one()
        assert manual_row[0] == "suspended"
        assert manual_row[1] == "manual_review"

        norm_ref = conn.execute(text("SELECT reference_normalized, currency FROM nr_e_transfers")).one()
        assert norm_ref[0] == "ref-mixed-case"
        assert norm_ref[1] == "CAD"

    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    sync_engine.dispose()


# =============================================================================
# PostgreSQL Concurrency Tests (skipped when TEST_DATABASE_URL is unset)
# =============================================================================


@pytest.mark.asyncio
async def test_pg_concurrent_holds_on_same_funds(pg_session_factory):
    async with pg_session_factory() as setup_db:
        org_id = await _org_and_credit(setup_db, Decimal("10"))
        await setup_db.commit()

    async def place_hold() -> bool:
        async with pg_session_factory() as session:
            try:
                await NorthRouterBillingAdapter(session).hold(organization_id=org_id, amount=Decimal("8"))
                await session.commit()
                return True
            except InsufficientFundsError:
                await session.rollback()
                return False

    results = await asyncio.gather(place_hold(), place_hold())
    assert sorted(results) == [False, True]


@pytest.mark.asyncio
async def test_pg_concurrent_webhook_deliveries_of_same_event(pg_session_factory, monkeypatch):
    async with pg_session_factory() as setup_db:
        org_id = await _ensure_otari_org(setup_db)
        await setup_db.commit()

    event = SimpleNamespace(
        id="evt_pg_concurrent",
        type="payment_intent.succeeded",
        created=500,
        data=SimpleNamespace(
            object=SimpleNamespace(
                id="pi_pg_concurrent",
                currency="usd",
                amount_received=2500,
                metadata={"organization_id": str(org_id)},
            )
        ),
    )
    monkeypatch.setattr(StripeClient, "construct_event", lambda self, payload, signature: event)

    async def deliver() -> dict[str, Any]:
        async with pg_session_factory() as session:
            return await stripe_webhook(_webhook_request(), session, "signed")

    await asyncio.gather(deliver(), deliver())

    async with pg_session_factory() as verify_db:
        grants = await verify_db.scalar(
            select(func.count())
            .select_from(NrLedgerEntry)
            .where(NrLedgerEntry.ref == "stripe-payment:pi_pg_concurrent")
        )
        org = await verify_db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
        assert grants == 1
        assert org is not None and org.balance == Decimal("25.000000")


@pytest.mark.asyncio
async def test_pg_concurrent_e_transfer_approvals(pg_session_factory, monkeypatch):
    _mock_boc_quote(monkeypatch, "1.4257", "2026-11-13")
    async with pg_session_factory() as setup_db:
        org_id = await _ensure_otari_org(setup_db)
        pending = await submit_e_transfer(
            org_id,
            ETransferRequest(amount=Decimal("100"), reference="pg-concurrent-ref"),
            setup_db,
        )
        transfer_id = uuid.UUID(pending["id"])

    async def approve_once() -> int:
        async with pg_session_factory() as session:
            try:
                await _review_e_transfer(transfer_id, session, approve=True, reviewed_by="ops@northrouter.ca")
                return 200
            except HTTPException as exc:
                await session.rollback()
                return exc.status_code

    statuses = await asyncio.gather(approve_once(), approve_once())
    assert sorted(statuses) == [200, 409]

    async with pg_session_factory() as verify_db:
        grants = await verify_db.scalar(
            select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.ref == f"etransfer:{transfer_id}")
        )
        org = await verify_db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
        assert grants == 1
        assert org is not None and org.balance == Decimal("70.140000")
