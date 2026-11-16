"""Billing adapter, grant API, authorization, and ledger behavior."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_protocol_members

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from conftest import seed_tenancy_org
from gateway.adapters.api_key_format_adapter import DefaultApiKeyFormatAdapter
from gateway.api.deps import build_budget_service, get_config, get_db
from gateway.api.routes.organization_keys import CreateOwnKeyRequest, create_own_key
from gateway.container import build_container
from gateway.core.config import GatewayConfig
from gateway.exceptions import TenancyError
from gateway.exceptions.organizations_exceptions import NotAuthorizedError, OrganizationNotFoundError
from gateway.models.api_keys import SERVICE_KEY_OWNER_ID_PREFIX, APIKey
from gateway.models.tenancy import Organization
from gateway.ports.billing_port import NO_TOOLS, BilledModel, BillingPort, InsufficientFundsError
from gateway.repositories.api_keys.api_key_repository import ApiKeyRepository
from gateway.services.tenancy import OrganizationService
from northrouter_billing.adapter import NorthRouterBillingAdapter, _post_ledger, validate_billing_env
from northrouter_billing.models import NrGrantEvent, NrHold, NrLedgerEntry, NrOrg, _money
from northrouter_billing.routes import (
    GrantRequest,
    StatusRequest,
    _ensure_org,
    _grant_ledger_ref,
    billing_router,
    get_billing_context,
    get_ledger,
    grant_funds,
    require_billing_manager,
    set_organization_status,
    sweep_holds,
)

SERVICE_TOKEN = "billing-service-test-token"
MASTER_KEY = "master-secret-token"


async def _ensure_otari_org(db: AsyncSession, org_id: uuid.UUID | None = None) -> uuid.UUID:
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


async def _org_and_credit(db: AsyncSession, amount: Decimal = Decimal("25")) -> uuid.UUID:
    org_id = await _ensure_otari_org(db)
    org = await _ensure_org(db, org_id)
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


async def _ledger_sum(db: AsyncSession, org_pk: uuid.UUID) -> Decimal:
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


def _http_app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.state.container = build_container("northrouter_billing.overlay:register")
    app.include_router(billing_router, prefix="/api/v1")

    @app.exception_handler(TenancyError)
    async def _handle_tenancy_error(_request, exc: TenancyError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    async def _override_db():
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_config] = lambda: GatewayConfig(master_key=MASTER_KEY)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_and_admin_can_read_billing_ledger_without_creating_billing_state(db, role: str):
    org_id, _user, _workspace, api_key = await seed_tenancy_org(db, role=role)
    assert await require_billing_manager(org_id, (api_key, False), db) == org_id
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0
    assert await get_ledger(org_id, db) == {"data": []}
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_member_and_viewer_cannot_manage_billing_reads(db, role: str):
    org_id, _user, _workspace, api_key = await seed_tenancy_org(db, role=role)
    with pytest.raises(NotAuthorizedError) as raised:
        await require_billing_manager(org_id, (api_key, False), db)
    assert raised.value.status_code == 403
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_billing_manager_scoping_and_master_key_require_existing_org(db):
    org_id, _user, _workspace, api_key = await seed_tenancy_org(db, role="owner")
    foreign_org_id = await _ensure_otari_org(db)
    with pytest.raises(OrganizationNotFoundError):
        await require_billing_manager(foreign_org_id, (api_key, False), db)

    assert await require_billing_manager(org_id, (None, True), db) == org_id
    with pytest.raises(OrganizationNotFoundError):
        await require_billing_manager(uuid.uuid4(), (None, True), db)
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_inference_key_and_wrong_oauth_client_cannot_manage_billing(db):
    org_id, _user, _workspace, inference_key = await seed_tenancy_org(
        db,
        role="owner",
        self_service=False,
        oauth_client_id=None,
    )
    with pytest.raises(NotAuthorizedError) as inference_refused:
        await require_billing_manager(org_id, (inference_key, False), db)
    assert inference_refused.value.status_code == 403

    _, _user, _workspace, unrelated_key = await seed_tenancy_org(
        db,
        org_id=org_id,
        role="owner",
        self_service=True,
        oauth_client_id="other-app",
    )
    with pytest.raises(NotAuthorizedError) as app_refused:
        await require_billing_manager(org_id, (unrelated_key, False), db)
    assert app_refused.value.status_code == 403


@pytest.mark.asyncio
async def test_billing_context_returns_org_and_membership_role(db):
    org_id, _owner, _workspace, owner_key = await seed_tenancy_org(db, role="owner")
    _, _viewer, _workspace2, viewer_key = await seed_tenancy_org(db, org_id=org_id, role="viewer")
    assert await get_billing_context(db, (owner_key, False)) == {
        "organization_id": str(org_id),
        "role": "owner",
    }
    assert await get_billing_context(db, (viewer_key, False)) == {
        "organization_id": str(org_id),
        "role": "viewer",
    }
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_tenant_key_creation_cannot_impersonate_another_user(db):
    """Tenant-facing key creation always binds a key to the authenticated user."""
    org_id, caller_user, workspace, _api_key = await seed_tenancy_org(db, role="owner")
    victim_user_id = uuid.uuid4()
    with pytest.raises(ValidationError):
        CreateOwnKeyRequest.model_validate(
            {
                "key_name": "spoof",
                "user_id": str(victim_user_id),
                "metadata": {"oauth_client_id": "northrouter-web"},
            }
        )

    from fastapi import BackgroundTasks

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
    own_key = await create_own_key(
        request=CreateOwnKeyRequest(
            key_name="member-key",
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
    assert own_key.user_id == str(caller_user.id)
    assert own_key.user_id != str(victim_user_id)

    service_key = await create_own_key(
        request=CreateOwnKeyRequest(
            key_name="organization-key",
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
    assert service_key.user_id is not None
    assert service_key.user_id.startswith(SERVICE_KEY_OWNER_ID_PREFIX)
    service_key_row = await db.get(APIKey, service_key.id)
    assert service_key_row is not None and service_key_row.self_service is True
    with pytest.raises(NotAuthorizedError):
        await require_billing_manager(org_id, (service_key_row, False), db)

    oauth_key_id = str(uuid.uuid4())
    await ApiKeyRepository(db).issue_app_key(
        key_id=oauth_key_id,
        key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        prefix="otari_",
        suffix="9999",
        name="Billing OAuth",
        user_id=str(caller_user.id),
        workspace_id=workspace.id,
        client_id="northrouter-web",
    )
    issued = await db.get(APIKey, oauth_key_id)
    assert issued is not None
    assert issued.self_service is True
    assert issued.user_id == str(caller_user.id)


@pytest.mark.asyncio
async def test_read_route_http_auth_matrix(db):
    org_id, _owner, _workspace, api_key = await seed_tenancy_org(db, role="owner")
    from gateway.auth.models import hash_key

    api_key.key_hash = hash_key("owner-token")
    await db.commit()
    app = _http_app(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        no_auth = await client.get(f"/api/v1/nr/billing/orgs/{org_id}/ledger")
        assert no_auth.status_code == 401
        owner = await client.get(
            f"/api/v1/nr/billing/orgs/{org_id}/ledger",
            headers={"Authorization": "Bearer owner-token"},
        )
        assert owner.status_code == 200
        master = await client.get(
            f"/api/v1/nr/billing/orgs/{org_id}/ledger",
            headers={"Authorization": f"Bearer {MASTER_KEY}"},
        )
        assert master.status_code == 200
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_grant_replay_credits_once(db):
    org_id = await _ensure_otari_org(db)
    request = GrantRequest(amount="12.50", idempotency_key="card-event-1", reason="Card payment")
    first = await grant_funds(org_id, request, db, None)
    second = await grant_funds(org_id, request, db, None)
    assert first["granted"] is True
    assert first["replayed"] is False
    assert second == {
        "granted": False,
        "replayed": True,
        "amount": "12.500000",
        "ledger_entry_id": first["ledger_entry_id"],
        "balance_after": "12.500000",
    }
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry)) == 1
    event = await db.get(NrGrantEvent, "card-event-1")
    assert event is not None and event.payload_fingerprint is not None


@pytest.mark.asyncio
async def test_grant_key_reuse_with_different_payload_is_rejected(db):
    org_id = await _ensure_otari_org(db)
    await grant_funds(
        org_id, GrantRequest(amount="12.50", idempotency_key="card-event-2", reason="Card payment"), db, None
    )
    with pytest.raises(HTTPException) as exc_info:
        await grant_funds(
            org_id, GrantRequest(amount="99.00", idempotency_key="card-event-2", reason="Card payment"), db, None
        )
    assert exc_info.value.status_code == 409
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry)) == 1
    org = await db.scalar(select(NrOrg))
    assert str(org.balance) == "12.500000"


@pytest.mark.asyncio
async def test_negative_grant_writes_refund_and_lowers_balance(db):
    org_id = await _org_and_credit(db, Decimal("20"))
    result = await grant_funds(
        org_id,
        GrantRequest(amount="-7.25", idempotency_key="refund-1", reason="Reversal"),
        db,
        None,
    )
    entry = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.id == uuid.UUID(result["ledger_entry_id"])))
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert entry is not None and entry.type == "refund" and entry.amount == Decimal("-7.250000")
    assert org is not None and org.balance == Decimal("12.750000")
    assert org.balance == await _ledger_sum(db, org.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["0", "-0.0000001", "NaN", "Infinity", "-Infinity", "1e3", "not-money"])
async def test_grant_rejects_zero_non_finite_or_invalid_amount(db, amount: str):
    org_id = await _ensure_otari_org(db)
    with pytest.raises(HTTPException) as raised:
        await grant_funds(
            org_id,
            GrantRequest(amount=amount, idempotency_key=f"bad-{uuid.uuid4()}", reason="Invalid amount"),
            db,
            None,
        )
    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_machine_routes_accept_only_service_token_or_master_key(db):
    org_id = await _ensure_otari_org(db)
    app = _http_app(db)
    grant_url = f"/api/v1/nr/billing/orgs/{org_id}/grants"
    status_url = f"/api/v1/nr/billing/orgs/{org_id}/status"
    sweep_url = "/api/v1/nr/billing/holds/sweep"
    grant_body = {"amount": "1.25", "idempotency_key": "auth-grant-1", "reason": "Authorized"}
    status_body = {"status": "active", "reason": "Authorized"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for path, body in ((grant_url, grant_body), (status_url, status_body), (sweep_url, {})):
            assert (await client.post(path, json=body)).status_code == 401
            wrong = await client.post(path, json=body, headers={"Authorization": "Bearer wrong-service-token"})
            assert wrong.status_code == 401

        service_headers = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
        assert (await client.post(grant_url, json=grant_body, headers=service_headers)).status_code == 200
        assert (await client.post(status_url, json=status_body, headers=service_headers)).status_code == 200
        assert (await client.post(sweep_url, headers=service_headers)).status_code == 200

        master_headers = {"Authorization": f"Bearer {MASTER_KEY}"}
        assert (await client.post(grant_url, json=grant_body, headers=master_headers)).status_code == 200
        assert (await client.post(status_url, json=status_body, headers=master_headers)).status_code == 200
        assert (await client.post(sweep_url, headers=master_headers)).status_code == 200


@pytest.mark.asyncio
async def test_status_suspend_blocks_gate_and_activation_restores_it(db):
    org_id = await _org_and_credit(db, Decimal("25"))
    suspended = await set_organization_status(
        org_id,
        StatusRequest(status="suspended", reason="Payment issue"),
        db,
        None,
    )
    assert suspended == {"status": "suspended"}
    with pytest.raises(InsufficientFundsError):
        await NorthRouterBillingAdapter(db).require_funds_on_deposit(
            organization_id=org_id,
            billed=None,
            tools=frozenset(),
            actor_user_id=None,
        )

    active = await set_organization_status(org_id, StatusRequest(status="active", reason="Recovered"), db, None)
    assert active == {"status": "active"}
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None and org.suspend_reason is None
    await NorthRouterBillingAdapter(db).require_funds_on_deposit(
        organization_id=org_id,
        billed=None,
        tools=frozenset(),
        actor_user_id=None,
    )


@pytest.mark.asyncio
async def test_status_route_rejects_blank_reason_when_suspending(db):
    org_id = await _ensure_otari_org(db)
    with pytest.raises(HTTPException) as raised:
        await set_organization_status(org_id, StatusRequest(status="suspended", reason="  "), db, None)
    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_env_validation_requires_service_token_and_validates_limits(monkeypatch):
    monkeypatch.delenv("NORTHROUTER_BILLING_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="NORTHROUTER_BILLING_SERVICE_TOKEN"):
        validate_billing_env()
    monkeypatch.setenv("NORTHROUTER_BILLING_SERVICE_TOKEN", SERVICE_TOKEN)

    monkeypatch.setenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "0")
    with pytest.raises(ValueError, match="NORTHROUTER_ORG_CONCURRENCY_LIMIT"):
        validate_billing_env()
    monkeypatch.setenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "4")
    monkeypatch.setenv("NORTHROUTER_MIN_AVAILABLE_USD", "-1")
    with pytest.raises(ValueError, match="NORTHROUTER_MIN_AVAILABLE_USD"):
        validate_billing_env()
    monkeypatch.setenv("NORTHROUTER_MIN_AVAILABLE_USD", "0")
    validate_billing_env()


@pytest.mark.asyncio
async def test_minimum_balance_and_concurrency_limit_configuration_are_enforced(db, monkeypatch):
    monkeypatch.setenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "4")
    monkeypatch.setenv("NORTHROUTER_MIN_AVAILABLE_USD", "10")
    validate_billing_env()
    adapter = NorthRouterBillingAdapter(db)
    org_id = await _org_and_credit(db, Decimal("8"))
    limit = await adapter.concurrency_limit(
        organization_id=org_id,
        billed=BilledModel(provider="openai", model="gpt-4o", open_weights=False),
        actor_user_id=None,
    )
    assert limit == 4
    with pytest.raises(InsufficientFundsError):
        await adapter.require_funds_on_deposit(
            organization_id=org_id,
            billed=None,
            tools=frozenset(),
            actor_user_id=None,
        )
    with pytest.raises(InsufficientFundsError):
        await adapter.require_unheld_funds(organization_id=org_id)


@pytest.mark.asyncio
async def test_funded_gate_passes_and_zero_balance_is_refused(db):
    org_id = await _org_and_credit(db)
    await NorthRouterBillingAdapter(db).require_funds_on_deposit(
        organization_id=org_id,
        billed=None,
        tools=frozenset(),
        actor_user_id=None,
    )
    with pytest.raises(InsufficientFundsError) as raised:
        await NorthRouterBillingAdapter(db).require_funds_on_deposit(
            organization_id=uuid.uuid4(),
            billed=None,
            tools=frozenset(),
            actor_user_id=None,
        )
    assert raised.value.available == Decimal("0.000000")


@pytest.mark.asyncio
async def test_suspended_organization_is_refused_with_available_amount(db):
    org_id = await _org_and_credit(db)
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    org.status = "suspended"
    with pytest.raises(InsufficientFundsError) as raised:
        await NorthRouterBillingAdapter(db).require_funds_on_deposit(
            organization_id=org_id,
            billed=None,
            tools=frozenset(),
            actor_user_id=None,
        )
    assert raised.value.available == Decimal("25.000000")
    assert raised.value.public_message.startswith("This organization is suspended")


@pytest.mark.asyncio
async def test_holds_reserve_and_release_unheld_funds(db):
    org_id = await _org_and_credit(db)
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.require_unheld_funds(organization_id=org_id)
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.hold(organization_id=org_id, amount=Decimal("25"))


@pytest.mark.asyncio
async def test_hold_beyond_unheld_funds_raises_and_zero_hold_is_noop(db):
    org_id = await _org_and_credit(db, Decimal("10"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("8"))
    with pytest.raises(InsufficientFundsError) as raised:
        await adapter.hold(organization_id=org_id, amount=Decimal("3"))
    assert raised.value.available == Decimal("2.000000")
    await adapter.hold(organization_id=org_id, amount=Decimal("0"))
    assert await db.scalar(select(func.count()).select_from(NrHold)) == 1


@pytest.mark.asyncio
async def test_release_hold_without_match_logs_warning(db, caplog):
    org_id = await _org_and_credit(db)
    await NorthRouterBillingAdapter(db).release_hold(organization_id=org_id, amount=Decimal("10"))
    assert "No open hold matches amount" in caplog.text


@pytest.mark.asyncio
async def test_release_hold_prefers_unexpired_match_before_expired_match(db):
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
async def test_release_hold_zero_is_a_noop(db):
    org_id = await _org_and_credit(db)
    await NorthRouterBillingAdapter(db).release_hold(organization_id=org_id, amount=Decimal("0"))
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.type == "release")) == 0


@pytest.mark.asyncio
async def test_expired_hold_does_not_reduce_funds_and_service_sweep_releases_it(db):
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
    assert await sweep_holds(db, None) == {"released": 1}
    await db.refresh(hold)
    assert hold.state == "released"
    release = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == f"release:{hold.id}"))
    assert release is not None and release.amount == Decimal("25.000000")


@pytest.mark.asyncio
async def test_charge_records_overdraft_and_next_gate_refuses(db):
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
    assert consume is not None and consume.amount == Decimal("5.000000")
    with pytest.raises(InsufficientFundsError) as raised:
        await adapter.require_funds_on_deposit(
            organization_id=org_id,
            billed=None,
            tools=frozenset(),
            actor_user_id=None,
        )
    assert raised.value.available == Decimal("-3.000000")


@pytest.mark.asyncio
async def test_charge_rejects_negative_amount(db):
    org_id = await _org_and_credit(db)
    with pytest.raises(ValueError, match="cannot be negative"):
        await NorthRouterBillingAdapter(db).charge(
            organization_id=org_id,
            amount=Decimal("-1"),
            billed=None,
            tools=NO_TOOLS,
        )


@pytest.mark.asyncio
async def test_customer_ledger_hides_hold_release_and_internal_refs(db):
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
    assert "hold" not in types and "release" not in types
    assert set(types) == {"grant", "consume"}
    assert all("ref" not in row and "reference" not in row for row in response["data"])


@pytest.mark.asyncio
async def test_stored_balance_equals_ledger_sum_for_grants_refunds_holds_and_charges(db):
    org_id = await _org_and_credit(db, Decimal("25"))
    await grant_funds(org_id, GrantRequest(amount="12.50", idempotency_key="balance-grant", reason="Top up"), db, None)
    await grant_funds(
        org_id,
        GrantRequest(amount="-2.50", idempotency_key="balance-refund", reason="Reversal"),
        db,
        None,
    )
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.charge(organization_id=org_id, amount=Decimal("4.25"), billed=None, tools=NO_TOOLS)
    await db.commit()
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
    assert org is not None
    assert org.balance == await _ledger_sum(db, org.id)
    assert org.balance == Decimal("30.750000")


def test_money_rejects_nan_and_infinity():
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), "NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="finite"):
            _money(bad)


def test_bootstrap_binds_billing_port_without_shape_error():
    container = build_container("northrouter_billing.overlay:register")
    adapter = container.resolve(BillingPort, None)
    assert all(hasattr(adapter, method) for method in get_protocol_members(BillingPort))
    assert container.router_contributions()[0].capability == "northrouter_billing"


def test_migration_0005_from_0004_drops_retired_tables_and_reshapes_idempotency_store(tmp_path, monkeypatch):
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, inspect, text

    database_file = tmp_path / "overlay-migration.db"
    monkeypatch.setenv("NORTHROUTER_DATABASE_URL", f"sqlite+aiosqlite:///{database_file}")
    alembic_ini = Path(__file__).resolve().parents[1] / "northrouter_billing" / "alembic.ini"
    config = Config(str(alembic_ini))
    config.set_main_option("script_location", str(alembic_ini.parent / "alembic"))
    command.upgrade(config, "0004_billing_fx_disputes_balance")
    engine = create_engine(f"sqlite:///{database_file}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO nr_webhook_events (event_id, event_type, processed_at, succeeded) "
                "VALUES ('legacy-event-key', 'legacy', CURRENT_TIMESTAMP, 1)"
            )
        )
    engine.dispose()
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite:///{database_file}")
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    assert "nr_e_transfers" not in table_names
    assert "nr_subscriptions" not in table_names
    assert "nr_grant_events" in table_names
    assert "nr_webhook_events" not in table_names
    assert "customer_id" not in {column["name"] for column in inspector.get_columns("nr_orgs")}
    assert {
        "idempotency_key",
        "payload_fingerprint",
        "amount",
        "ledger_entry_id",
        "balance_after",
        "created_at",
    } <= {column["name"] for column in inspector.get_columns("nr_grant_events")}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM nr_grant_events")) == 0
    engine.dispose()


@pytest.mark.asyncio
async def test_pg_concurrent_grants_same_key(pg_session_factory):
    async with pg_session_factory() as setup_db:
        org_id = await _ensure_otari_org(setup_db)
        await _ensure_org(setup_db, org_id)
        await setup_db.commit()

    grant = GrantRequest(amount="25.00", idempotency_key="pg-grant-same-key", reason="Concurrent payment")

    async def deliver() -> dict[str, Any]:
        async with pg_session_factory() as session:
            return await grant_funds(org_id, grant, session, None)

    results = await asyncio.gather(deliver(), deliver())
    assert sum(result["granted"] is True for result in results) == 1
    assert sum(result["replayed"] is True for result in results) == 1

    async with pg_session_factory() as verify_db:
        entry_count = await verify_db.scalar(
            select(func.count())
            .select_from(NrLedgerEntry)
            .where(NrLedgerEntry.ref == _grant_ledger_ref("pg-grant-same-key"))
        )
        org = await verify_db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id))
        assert entry_count == 1
        assert org is not None and org.balance == Decimal("25.000000")


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
