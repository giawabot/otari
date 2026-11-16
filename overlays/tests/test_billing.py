"""Billing adapter, machine API, authorization, ledger and migration behavior."""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, get_protocol_members
from unittest.mock import MagicMock

import httpx
import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import case, create_engine, func, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

import northrouter_billing.overlay as overlay_module
import northrouter_billing.routes as routes_module
from conftest import (
    OAUTH_CLIENT_ID,
    PREVIOUS_TOKEN,
    SERVICE_TOKEN,
    _server_url,
    configure_from_environment,
    seed_tenancy_org,
    throwaway_database,
)
from gateway.adapters.api_key_format_adapter import DefaultApiKeyFormatAdapter
from gateway.api.deps import build_budget_service, get_config, get_db
from gateway.api.routes.organization_keys import CreateOwnKeyRequest, create_own_key
from gateway.auth.models import hash_key
from gateway.container import build_container
from gateway.core.config import GatewayConfig
from gateway.exceptions import TenancyError
from gateway.exceptions.organizations_exceptions import NotAuthorizedError, OrganizationNotFoundError
from gateway.models.api_keys import APIKey
from gateway.models.tenancy import Organization, WorkspaceMember
from gateway.ports.billing_port import NO_TOOLS, BilledModel, BillingPort, InsufficientFundsError
from gateway.repositories.api_keys.api_key_repository import ApiKeyRepository
from gateway.services.tenancy import OrganizationService
from northrouter_billing.adapter import NorthRouterBillingAdapter, post_ledger
from northrouter_billing.migration_filter import include_object
from northrouter_billing.models import (
    BillingBase,
    NrGrantEvent,
    NrHold,
    NrLedgerEntry,
    NrOrg,
    NrStatusEvent,
    _money,
)
from northrouter_billing.routes import (
    BillingReader,
    GrantRequest,
    StatusRequest,
    _grant_ledger_ref,
    billing_router,
    get_account,
    get_billing_context,
    get_ledger,
    grant_funds,
    require_billing_manager,
    set_organization_status,
    sweep_holds,
)
from northrouter_billing.settings import (
    DEFAULT_CONCURRENCY_LIMIT,
    DEFAULT_MAX_GRANT_USD,
    DEFAULT_MIN_AVAILABLE_USD,
    BillingSettings,
    current_settings,
    load_settings,
)

MASTER_KEY = "master-secret-token"
SERVICE = {"Authorization": f"Bearer {SERVICE_TOKEN}"}
MASTER = {"Authorization": f"Bearer {MASTER_KEY}"}
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "northrouter_billing" / "alembic.ini"


def _settings() -> BillingSettings:
    return current_settings()


async def _ensure_otari_org(db: AsyncSession, org_id: uuid.UUID | None = None) -> uuid.UUID:
    resolved_id = org_id or uuid.uuid4()
    if await db.get(Organization, resolved_id) is None:
        db.add(Organization(id=resolved_id, name=f"Org {resolved_id.hex[:8]}", slug=f"org-{resolved_id.hex[:12]}"))
        await db.flush()
    return resolved_id


async def _ensure_org(db: AsyncSession, org_id: uuid.UUID) -> NrOrg:
    return await NorthRouterBillingAdapter(db).ensure_org(org_id)


async def _org_and_credit(db: AsyncSession, amount: Decimal = Decimal("25")) -> uuid.UUID:
    org_id = await _ensure_otari_org(db)
    org = await _ensure_org(db, org_id)
    await post_ledger(
        db, org_id=org.id, entry_type="grant", amount=amount, ref=f"seed:{uuid.uuid4()}", description="Seed credit"
    )
    await db.flush()
    return org_id


async def _nr_org(db: AsyncSession, org_id: uuid.UUID) -> NrOrg:
    org = await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == org_id).execution_options(populate_existing=True))
    assert org is not None
    return org


async def _ledger_sum(db: AsyncSession, org_pk: uuid.UUID) -> Decimal:
    total = await db.scalar(
        select(
            func.coalesce(
                func.sum(
                    case(
                        (NrLedgerEntry.type.in_(("grant", "refund")), NrLedgerEntry.amount),
                        (NrLedgerEntry.type == "consume", -NrLedgerEntry.amount),
                        else_=Decimal("0"),
                    )
                ),
                0,
            )
        ).where(NrLedgerEntry.org_id == org_pk)
    )
    return _money(total or 0)


async def _grant(
    db: AsyncSession, org_id: uuid.UUID, amount: str, key: str, *, principal: str = "service", **fields: Any
) -> dict[str, Any]:
    body = GrantRequest(amount=amount, idempotency_key=key, reason=fields.pop("reason", "Top up"), **fields)
    return await grant_funds(org_id, body, db, principal, _settings())


async def _status(
    db: AsyncSession, org_id: uuid.UUID, status: str, *, at: datetime | None = None, reason: str = "Billing decision"
) -> dict[str, Any]:
    body = StatusRequest.model_validate({"status": status, "reason": reason, "effective_at": at or datetime.now(UTC)})
    return await set_organization_status(org_id, body, db, "service", _settings())


async def _gate(db: AsyncSession, org_id: uuid.UUID) -> None:
    await NorthRouterBillingAdapter(db).require_funds_on_deposit(
        organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
    )


def _http_app(db: AsyncSession) -> FastAPI:
    app = FastAPI()
    app.state.container = build_container("northrouter_billing.overlay:register")
    app.include_router(billing_router, prefix="/api/v1")

    @app.exception_handler(TenancyError)
    async def _handle_tenancy_error(_request: Any, exc: TenancyError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})

    async def _override_db() -> Any:
        yield db

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[get_config] = lambda: GatewayConfig(master_key=MASTER_KEY)
    return app


def _client(db: AsyncSession) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=_http_app(db)), base_url="http://test")


# Settings


def test_settings_default_to_safe_limits() -> None:
    settings = load_settings({"NORTHROUTER_BILLING_SERVICE_TOKEN": SERVICE_TOKEN})
    assert settings.concurrency_limit == DEFAULT_CONCURRENCY_LIMIT == 8
    assert settings.min_available_usd == DEFAULT_MIN_AVAILABLE_USD == Decimal("1.00")
    assert settings.max_grant_usd == DEFAULT_MAX_GRANT_USD
    assert settings.oauth_client_id is None
    assert settings.service_tokens == (("service", SERVICE_TOKEN),)


_TOKEN = {"NORTHROUTER_BILLING_SERVICE_TOKEN": SERVICE_TOKEN}


@pytest.mark.parametrize(
    ("environment", "names"),
    [
        ({}, "NORTHROUTER_BILLING_SERVICE_TOKEN"),
        ({"NORTHROUTER_BILLING_SERVICE_TOKEN": "   "}, "NORTHROUTER_BILLING_SERVICE_TOKEN"),
        ({"NORTHROUTER_BILLING_SERVICE_TOKEN": "too-short-a-secret"}, "NORTHROUTER_BILLING_SERVICE_TOKEN"),
        ({**_TOKEN, "NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS": "short-old-one"}, "_PREVIOUS"),
        ({**_TOKEN, "NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS": SERVICE_TOKEN}, "must differ"),
        ({**_TOKEN, "NORTHROUTER_ORG_CONCURRENCY_LIMIT": "0"}, "NORTHROUTER_ORG_CONCURRENCY_LIMIT"),
        ({**_TOKEN, "NORTHROUTER_ORG_CONCURRENCY_LIMIT": "many"}, "NORTHROUTER_ORG_CONCURRENCY_LIMIT"),
        ({**_TOKEN, "NORTHROUTER_MIN_AVAILABLE_USD": "-1"}, "NORTHROUTER_MIN_AVAILABLE_USD"),
        ({**_TOKEN, "NORTHROUTER_MIN_AVAILABLE_USD": "NaN"}, "NORTHROUTER_MIN_AVAILABLE_USD"),
        ({**_TOKEN, "NORTHROUTER_MAX_GRANT_USD": "0"}, "NORTHROUTER_MAX_GRANT_USD"),
        ({**_TOKEN, "NORTHROUTER_MAX_GRANT_USD": "1e13"}, "NORTHROUTER_MAX_GRANT_USD"),
    ],
)
def test_settings_refuse_a_malformed_environment_without_echoing_a_secret(
    environment: dict[str, str], names: str
) -> None:
    with pytest.raises(ValueError, match=names) as raised:
        load_settings(environment)
    for name, value in environment.items():
        if "TOKEN" in name and value.strip():
            assert value.strip() not in str(raised.value)


def test_settings_never_render_a_token() -> None:
    settings = load_settings(
        {
            "NORTHROUTER_BILLING_SERVICE_TOKEN": SERVICE_TOKEN,
            "NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS": PREVIOUS_TOKEN,
        }
    )
    assert SERVICE_TOKEN not in repr(settings) and PREVIOUS_TOKEN not in repr(settings)
    assert settings.service_tokens == (("service", SERVICE_TOKEN), ("service_previous", PREVIOUS_TOKEN))


def test_bootstrap_binds_billing_port_and_warns_while_jobs_are_on(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = MagicMock()
    monkeypatch.setattr(overlay_module, "logger", logger)
    container = build_container("northrouter_billing.overlay:register")
    adapter = container.resolve(BillingPort, None)
    assert all(hasattr(adapter, method) for method in get_protocol_members(BillingPort))
    assert container.router_contributions()[0].capability == "northrouter_billing"
    assert any("OTARI_DEPLOYMENT_PAID_JOBS" in str(call) for call in logger.warning.call_args_list)

    logger.reset_mock()
    monkeypatch.setenv("OTARI_DEPLOYMENT_PAID_JOBS", "false")
    build_container("northrouter_billing.overlay:register")
    logger.warning.assert_not_called()


def test_bootstrap_refuses_a_missing_service_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NORTHROUTER_BILLING_SERVICE_TOKEN")
    with pytest.raises(Exception, match="NORTHROUTER_BILLING_SERVICE_TOKEN"):
        build_container("northrouter_billing.overlay:register")


# Who may read


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_and_admin_can_read_billing_ledger_without_creating_billing_state(
    db: AsyncSession, role: str
) -> None:
    org_id, _user, _workspace, api_key = await seed_tenancy_org(db, role=role)
    assert await require_billing_manager(org_id, (api_key, False), db, _settings()) == org_id
    reader = BillingReader(organization_id=org_id, internal=False)
    assert await get_ledger(reader, db) == {"data": [], "next_cursor": None}
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["member", "viewer"])
async def test_member_and_viewer_cannot_read_billing(db: AsyncSession, role: str) -> None:
    org_id, _user, _workspace, api_key = await seed_tenancy_org(db, role=role)
    with pytest.raises(NotAuthorizedError) as raised:
        await require_billing_manager(org_id, (api_key, False), db, _settings())
    assert raised.value.status_code == 403


@pytest.mark.asyncio
async def test_billing_manager_scoping_and_master_key_require_existing_org(db: AsyncSession) -> None:
    org_id, _user, _workspace, api_key = await seed_tenancy_org(db, role="owner")
    foreign_org_id = await _ensure_otari_org(db)
    with pytest.raises(OrganizationNotFoundError):
        await require_billing_manager(foreign_org_id, (api_key, False), db, _settings())
    assert await require_billing_manager(org_id, (None, True), db, _settings()) == org_id
    with pytest.raises(OrganizationNotFoundError):
        await require_billing_manager(uuid.uuid4(), (None, True), db, _settings())


@pytest.mark.asyncio
async def test_inference_key_and_wrong_oauth_client_cannot_read_billing(db: AsyncSession) -> None:
    org_id, _user, _workspace, inference_key = await seed_tenancy_org(
        db, role="owner", self_service=False, oauth_client_id=None
    )
    with pytest.raises(NotAuthorizedError):
        await require_billing_manager(org_id, (inference_key, False), db, _settings())
    _, _user, _workspace, unrelated_key = await seed_tenancy_org(db, org_id=org_id, oauth_client_id="other-app")
    with pytest.raises(NotAuthorizedError):
        await require_billing_manager(org_id, (unrelated_key, False), db, _settings())


@pytest.mark.asyncio
async def test_a_key_whose_metadata_names_the_app_is_not_the_apps_key(db: AsyncSession) -> None:
    """``metadata`` is the key owner's to write, so it proves nothing about which app the key was issued to."""
    org_id, _user, _workspace, forged = await seed_tenancy_org(
        db, oauth_client_id=None, metadata={"oauth_client_id": OAUTH_CLIENT_ID}
    )
    with pytest.raises(NotAuthorizedError):
        await require_billing_manager(org_id, (forged, False), db, _settings())
    with pytest.raises(NotAuthorizedError):
        await get_billing_context(db, (forged, False), _settings())


@pytest.mark.asyncio
async def test_no_app_key_reads_billing_when_no_app_is_configured(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id, _user, _workspace, api_key = await seed_tenancy_org(db)
    monkeypatch.delenv("NORTHROUTER_BILLING_OAUTH_CLIENT_ID")
    configure_from_environment()
    with pytest.raises(NotAuthorizedError):
        await require_billing_manager(org_id, (api_key, False), db, _settings())


@pytest.mark.asyncio
async def test_member_minted_keys_never_carry_the_app_marker(db: AsyncSession) -> None:
    """The member key routes cannot set the column the overlay trusts; only the app grant exchange does."""
    org_id, caller, workspace, _api_key = await seed_tenancy_org(db, role="owner")
    with pytest.raises(ValidationError):
        CreateOwnKeyRequest.model_validate({"key_name": "spoof", "oauth_client_id": OAUTH_CLIENT_ID})
    db.add(
        WorkspaceMember(id=uuid.uuid4(), workspace_id=workspace.id, user_id=caller.id, role="owner", status="active")
    )
    await db.commit()
    organizations = OrganizationService(db, membership_listener=None)
    for is_service_key in (False, True):
        created = await create_own_key(
            request=CreateOwnKeyRequest(
                key_name=f"member-key-{is_service_key}",
                workspace_id=workspace.id,
                is_service_key=is_service_key,
                metadata={"oauth_client_id": OAUTH_CLIENT_ID},
            ),
            identity=caller,
            background_tasks=BackgroundTasks(),
            db=db,
            config=GatewayConfig(),
            growth=None,
            key_format=DefaultApiKeyFormatAdapter(db),
            organizations=organizations,
            budgets=build_budget_service(db),
        )
        row = await db.get(APIKey, created.id)
        assert row is not None and row.oauth_client_id is None
        with pytest.raises(NotAuthorizedError):
            await require_billing_manager(org_id, (row, False), db, _settings())

    issued_id = str(uuid.uuid4())
    await ApiKeyRepository(db).issue_app_key(
        key_id=issued_id,
        key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        prefix="otari_",
        suffix="9999",
        name="Billing OAuth",
        user_id=str(caller.id),
        workspace_id=workspace.id,
        client_id=OAUTH_CLIENT_ID,
    )
    issued = await db.get(APIKey, issued_id)
    assert issued is not None and issued.oauth_client_id == OAUTH_CLIENT_ID
    assert await require_billing_manager(org_id, (issued, False), db, _settings()) == org_id


@pytest.mark.asyncio
async def test_billing_context_returns_org_and_membership_role(db: AsyncSession) -> None:
    org_id, _owner, _workspace, owner_key = await seed_tenancy_org(db, role="owner")
    _, _viewer, _workspace2, viewer_key = await seed_tenancy_org(db, org_id=org_id, role="viewer")
    assert await get_billing_context(db, (owner_key, False), _settings()) == {
        "organization_id": str(org_id),
        "role": "owner",
    }
    assert await get_billing_context(db, (viewer_key, False), _settings()) == {
        "organization_id": str(org_id),
        "role": "viewer",
    }
    assert await get_billing_context(db, (None, True), _settings()) == {"organization_id": None, "role": "operator"}
    assert await db.scalar(select(func.count()).select_from(NrOrg)) == 0


@pytest.mark.asyncio
async def test_read_routes_http_auth_matrix(db: AsyncSession) -> None:
    org_id, _owner, _workspace, api_key = await seed_tenancy_org(db, role="owner")
    api_key.key_hash = hash_key("owner-token")
    await db.commit()
    await _grant(db, org_id, "5", "matrix-grant", source_ref="pay_123")
    owner = {"Authorization": "Bearer owner-token"}
    async with _client(db) as client:
        for path in (f"/api/v1/nr/billing/orgs/{org_id}/ledger", f"/api/v1/nr/billing/orgs/{org_id}/account"):
            assert (await client.get(path)).status_code == 401
            assert (await client.get(path, headers={"Authorization": "Bearer wrong"})).status_code == 401
            for headers in (owner, MASTER, SERVICE):
                assert (await client.get(path, headers=headers)).status_code == 200, headers
        missing = f"/api/v1/nr/billing/orgs/{uuid.uuid4()}/account"
        assert (await client.get(missing, headers=SERVICE)).status_code == 404

        as_owner = (await client.get(f"/api/v1/nr/billing/orgs/{org_id}/ledger", headers=owner)).json()
        as_service = (await client.get(f"/api/v1/nr/billing/orgs/{org_id}/ledger", headers=SERVICE)).json()
    assert "ref" not in as_owner["data"][0]
    assert as_service["data"][0]["ref"] == _grant_ledger_ref("matrix-grant")


# Grants


@pytest.mark.asyncio
async def test_grant_replay_credits_once(db: AsyncSession) -> None:
    org_id = await _ensure_otari_org(db)
    first = await _grant(db, org_id, "12.50", "card-event-1", reason="Card payment")
    second = await _grant(db, org_id, "12.50", "card-event-1", reason="Card payment")
    assert first["granted"] is True and first["replayed"] is False
    assert second == {
        "granted": False,
        "replayed": True,
        "amount": "12.500000",
        "ledger_entry_id": first["ledger_entry_id"],
        "balance_after": "12.500000",
    }
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry)) == 1
    event = await db.get(NrGrantEvent, "card-event-1")
    assert event is not None and event.principal == "service"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"amount": "99.00"},
        {"reason": "Something else"},
        {"source_ref": "pay_other"},
    ],
)
async def test_grant_key_reuse_with_a_different_payload_is_refused(db: AsyncSession, change: dict[str, str]) -> None:
    org_id = await _ensure_otari_org(db)
    original = {"amount": "12.50", "reason": "Card payment", "source_ref": "pay_1"}
    await _grant(db, org_id, original["amount"], "card-event-2", reason=original["reason"], source_ref="pay_1")
    changed = {**original, **change}
    with pytest.raises(HTTPException) as raised:
        await _grant(
            db, org_id, changed["amount"], "card-event-2", reason=changed["reason"], source_ref=changed["source_ref"]
        )
    assert raised.value.status_code == 409
    assert (await _nr_org(db, org_id)).balance == Decimal("12.500000")


@pytest.mark.asyncio
async def test_grant_key_reuse_for_another_organization_is_refused(db: AsyncSession) -> None:
    first_org = await _ensure_otari_org(db)
    second_org = await _ensure_otari_org(db)
    await _grant(db, first_org, "5", "shared-key")
    with pytest.raises(HTTPException) as raised:
        await _grant(db, second_org, "5", "shared-key")
    assert raised.value.status_code == 409
    assert await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == second_org)) is None


@pytest.mark.asyncio
async def test_negative_grant_writes_refund_and_lowers_balance(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("20"))
    result = await _grant(db, org_id, "-7.25", "refund-1", reason="Reversal")
    entry = await db.get(NrLedgerEntry, uuid.UUID(result["ledger_entry_id"]))
    org = await _nr_org(db, org_id)
    assert entry is not None and entry.type == "refund" and entry.amount == Decimal("-7.250000")
    assert org.balance == Decimal("12.750000") == await _ledger_sum(db, org.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["0", "-0.0000001", "NaN", "Infinity", "-Infinity", "1e3", "not-money"])
async def test_grant_rejects_zero_non_finite_or_invalid_amount(db: AsyncSession, amount: str) -> None:
    org_id = await _ensure_otari_org(db)
    with pytest.raises(HTTPException) as raised:
        await _grant(db, org_id, amount, f"bad-{uuid.uuid4()}")
    assert raised.value.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["10000.000001", "-10000.01", "9" * 40])
async def test_grant_beyond_the_cap_is_refused_either_way(db: AsyncSession, amount: str) -> None:
    org_id = await _ensure_otari_org(db)
    with pytest.raises(HTTPException) as raised:
        await _grant(db, org_id, amount, f"cap-{uuid.uuid4()}")
    assert raised.value.status_code == 422
    assert await db.scalar(select(func.count()).select_from(NrGrantEvent)) == 0
    await _grant(db, org_id, "10000", "at-the-cap")


@pytest.mark.asyncio
async def test_machine_routes_accept_only_service_tokens_or_master_key(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS", PREVIOUS_TOKEN)
    org_id = await _ensure_otari_org(db)
    await db.commit()
    grant_url = f"/api/v1/nr/billing/orgs/{org_id}/grants"
    status_url = f"/api/v1/nr/billing/orgs/{org_id}/status"
    sweep_url = "/api/v1/nr/billing/holds/sweep"

    def bodies(n: int) -> tuple[tuple[str, dict[str, Any]], ...]:
        at = (datetime.now(UTC) + timedelta(seconds=n)).isoformat()
        return (
            (grant_url, {"amount": "1.25", "idempotency_key": f"auth-grant-{n}", "reason": "Authorized"}),
            (status_url, {"status": "active", "reason": "Authorized", "effective_at": at}),
            (sweep_url, {}),
        )

    async with _client(db) as client:
        for path, body in bodies(0):
            assert (await client.post(path, json=body)).status_code == 401
            for token in ("wrong", SERVICE_TOKEN[:-1], f"{SERVICE_TOKEN}x"):
                refused = await client.post(path, json=body, headers={"Authorization": f"Bearer {token}"})
                assert refused.status_code == 401
            assert (await client.post(path, json=body, headers={"Authorization": SERVICE_TOKEN})).status_code == 401
        for n, headers in enumerate((SERVICE, {"Authorization": f"Bearer {PREVIOUS_TOKEN}"}, MASTER), start=1):
            for path, body in bodies(n):
                response = await client.post(path, json=body, headers=headers)
                assert response.status_code == 200, (path, response.text)

    principals = {event.idempotency_key: event.principal for event in await db.scalars(select(NrGrantEvent))}
    assert principals == {"auth-grant-1": "service", "auth-grant-2": "service_previous", "auth-grant-3": "master"}
    status_principals = [event.principal for event in await db.scalars(select(NrStatusEvent))]
    assert sorted(status_principals) == ["master", "service", "service_previous"]


@pytest.mark.asyncio
async def test_an_oversized_grant_over_http_is_a_422_not_a_server_error(db: AsyncSession) -> None:
    org_id = await _ensure_otari_org(db)
    await db.commit()
    async with _client(db) as client:
        response = await client.post(
            f"/api/v1/nr/billing/orgs/{org_id}/grants",
            json={"amount": "9" * 40, "idempotency_key": "huge", "reason": "Overflow"},
            headers=SERVICE,
        )
    assert response.status_code == 422


# Status


@pytest.mark.asyncio
async def test_status_suspend_blocks_gate_and_activation_restores_it(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("25"))
    start = datetime.now(UTC) - timedelta(minutes=1)
    assert (await _status(db, org_id, "suspended", at=start, reason="Payment issue"))["applied"] is True
    with pytest.raises(InsufficientFundsError) as raised:
        await _gate(db, org_id)
    assert raised.value.public_message.startswith("This organization is suspended")

    active = await _status(db, org_id, "active", at=start + timedelta(seconds=1), reason="Recovered")
    assert active["status"] == "active" and active["applied"] is True
    assert (await _nr_org(db, org_id)).suspend_reason is None
    await _gate(db, org_id)


@pytest.mark.asyncio
async def test_a_stale_status_decision_is_recorded_and_ignored(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db)
    newer = datetime.now(UTC) - timedelta(seconds=10)
    await _status(db, org_id, "suspended", at=newer, reason="Chargeback")
    stale = await _status(db, org_id, "active", at=newer - timedelta(seconds=30), reason="Late retry")
    assert stale == {"status": "suspended", "applied": False, "effective_at": newer.isoformat()}
    replay = await _status(db, org_id, "suspended", at=newer, reason="Chargeback")
    assert replay["applied"] is False
    with pytest.raises(InsufficientFundsError):
        await _gate(db, org_id)
    events = list(await db.scalars(select(NrStatusEvent).order_by(NrStatusEvent.created_at)))
    assert [(event.to_status, event.applied) for event in events] == [
        ("suspended", True),
        ("active", False),
        ("suspended", False),
    ]
    assert {event.principal for event in events} == {"service"}


@pytest.mark.asyncio
async def test_a_status_decision_from_the_future_is_refused(db: AsyncSession) -> None:
    org_id = await _ensure_otari_org(db)
    with pytest.raises(HTTPException) as raised:
        await _status(db, org_id, "active", at=datetime.now(UTC) + timedelta(hours=1))
    assert raised.value.status_code == 422
    assert await db.scalar(select(func.count()).select_from(NrStatusEvent)) == 0


def test_a_status_decision_needs_a_timezone_and_a_reason() -> None:
    with pytest.raises(ValidationError):
        StatusRequest.model_validate({"status": "active", "reason": "ok", "effective_at": "2026-11-16T12:00:00"})
    with pytest.raises(ValidationError):
        StatusRequest.model_validate({"status": "active", "reason": "", "effective_at": "2026-11-16T12:00:00Z"})
    with pytest.raises(ValidationError):
        StatusRequest.model_validate({"status": "active", "reason": "ok"})


@pytest.mark.asyncio
async def test_status_route_rejects_blank_reason(db: AsyncSession) -> None:
    org_id = await _ensure_otari_org(db)
    with pytest.raises(HTTPException) as raised:
        await _status(db, org_id, "suspended", reason="  ")
    assert raised.value.status_code == 422


# Gates


@pytest.mark.asyncio
async def test_minimum_balance_and_concurrency_limit_configuration_are_enforced(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "4")
    monkeypatch.setenv("NORTHROUTER_MIN_AVAILABLE_USD", "10")
    configure_from_environment()
    adapter = NorthRouterBillingAdapter(db)
    org_id = await _org_and_credit(db, Decimal("8"))
    billed = BilledModel(provider="openai", model="gpt-4o", open_weights=False)
    assert await adapter.concurrency_limit(organization_id=org_id, billed=billed, actor_user_id=None) == 4
    with pytest.raises(InsufficientFundsError):
        await adapter.require_funds_on_deposit(
            organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
        )
    with pytest.raises(InsufficientFundsError):
        await adapter.require_unheld_funds(organization_id=org_id)
    assert not await adapter.has_funds_on_deposit(
        organization_id=org_id, billed=None, tools=frozenset(), actor_user_id=None
    )


@pytest.mark.asyncio
async def test_the_default_floor_refuses_a_balance_under_a_dollar(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("0.50"))
    with pytest.raises(InsufficientFundsError):
        await _gate(db, org_id)
    await _grant(db, org_id, "0.50", "to-the-floor")
    await _gate(db, org_id)


@pytest.mark.asyncio
async def test_funded_gate_passes_and_unknown_organization_is_refused(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db)
    await _gate(db, org_id)
    with pytest.raises(InsufficientFundsError) as raised:
        await _gate(db, uuid.uuid4())
    assert raised.value.available == Decimal("0.000000")


@pytest.mark.asyncio
async def test_a_gate_never_decides_on_a_balance_the_session_cached(db: AsyncSession) -> None:
    """A row the session already holds is read again, as another transaction may have moved it."""
    org_id = await _org_and_credit(db, Decimal("0.10"))
    await _nr_org(db, org_id)
    with pytest.raises(InsufficientFundsError):
        await _gate(db, org_id)
    # Written beneath the ORM, as another transaction's commit would be, so the session's copy goes stale.
    connection = await db.connection()
    table = NrOrg.__table__
    await connection.execute(table.update().where(table.c.otari_org_id == org_id).values(balance=Decimal("20")))
    await _gate(db, org_id)
    await connection.execute(table.update().where(table.c.otari_org_id == org_id).values(status="suspended"))
    with pytest.raises(InsufficientFundsError):
        await _gate(db, org_id)


@pytest.mark.asyncio
async def test_suspended_organization_is_refused_with_available_amount(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db)
    await _status(db, org_id, "suspended", reason="Fraud review")
    with pytest.raises(InsufficientFundsError) as raised:
        await _gate(db, org_id)
    assert raised.value.available == Decimal("25.000000")


# Holds


@pytest.mark.asyncio
async def test_holds_reserve_and_release_unheld_funds(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db)
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.require_unheld_funds(organization_id=org_id)
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.hold(organization_id=org_id, amount=Decimal("25"))


@pytest.mark.asyncio
async def test_hold_beyond_unheld_funds_raises_and_zero_hold_is_noop(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("10"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("8"))
    with pytest.raises(InsufficientFundsError) as raised:
        await adapter.hold(organization_id=org_id, amount=Decimal("3"))
    assert raised.value.available == Decimal("2.000000")
    await adapter.hold(organization_id=org_id, amount=Decimal("0"))
    assert await db.scalar(select(func.count()).select_from(NrHold)) == 1


@pytest.mark.asyncio
async def test_release_hold_without_match_logs_warning(db: AsyncSession, caplog: pytest.LogCaptureFixture) -> None:
    org_id = await _org_and_credit(db)
    await NorthRouterBillingAdapter(db).release_hold(organization_id=org_id, amount=Decimal("10"))
    assert "No open hold matches amount" in caplog.text


def _hold(org: NrOrg, *, amount: str, created_at: datetime, ttl: timedelta = timedelta(hours=1)) -> NrHold:
    return NrHold(
        org_id=org.id, amount=Decimal(amount), state="open", created_at=created_at, expires_at=created_at + ttl
    )


@pytest.mark.asyncio
async def test_release_hold_prefers_unexpired_match_before_expired_match(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("60"))
    org = await _nr_org(db, org_id)
    now = datetime.now(UTC)
    expired_hold = _hold(org, amount="25", created_at=now - timedelta(hours=2))
    unexpired_hold = _hold(org, amount="25", created_at=now - timedelta(minutes=5))
    db.add_all([expired_hold, unexpired_hold])
    await db.flush()
    adapter = NorthRouterBillingAdapter(db)
    await adapter.release_hold(organization_id=org_id, amount=Decimal("25"))
    await db.refresh(unexpired_hold)
    await db.refresh(expired_hold)
    assert (unexpired_hold.state, expired_hold.state) == ("released", "open")
    await adapter.release_hold(organization_id=org_id, amount=Decimal("25"))
    await db.refresh(expired_hold)
    assert expired_hold.state == "released"


@pytest.mark.asyncio
async def test_release_hold_zero_is_a_noop(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db)
    await NorthRouterBillingAdapter(db).release_hold(organization_id=org_id, amount=Decimal("0"))
    assert await db.scalar(select(func.count()).select_from(NrLedgerEntry).where(NrLedgerEntry.type == "release")) == 0


@pytest.mark.asyncio
async def test_expired_hold_does_not_reduce_funds_and_service_sweep_releases_it(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db)
    hold = _hold(await _nr_org(db, org_id), amount="25", created_at=datetime.now(UTC) - timedelta(hours=2))
    db.add(hold)
    await db.flush()
    await NorthRouterBillingAdapter(db).require_unheld_funds(organization_id=org_id)
    assert await sweep_holds(db, "service") == {"released": 1, "more": False}
    await db.refresh(hold)
    assert hold.state == "released"
    release = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.ref == f"release:{hold.id}"))
    assert release is not None and release.amount == Decimal("25.000000")


@pytest.mark.asyncio
async def test_the_sweep_works_in_batches(db: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(routes_module, "SWEEP_BATCH", 2)
    org_id = await _org_and_credit(db)
    org = await _nr_org(db, org_id)
    past = datetime.now(UTC) - timedelta(hours=3)
    db.add_all([_hold(org, amount=str(n), created_at=past + timedelta(minutes=n)) for n in (1, 2, 3)])
    db.add(_hold(org, amount="4", created_at=datetime.now(UTC)))
    await db.flush()
    assert await sweep_holds(db, "service") == {"released": 2, "more": True}
    assert await sweep_holds(db, "service") == {"released": 1, "more": False}
    open_amounts = list(await db.scalars(select(NrHold.amount).where(NrHold.state == "open")))
    assert open_amounts == [Decimal("4.000000")]


# Charges and ledger


@pytest.mark.asyncio
async def test_charge_records_overdraft_and_next_gate_refuses(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("2"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.charge(organization_id=org_id, amount=Decimal("5"), billed=None, tools=NO_TOOLS, description="served")
    consume = await db.scalar(select(NrLedgerEntry).where(NrLedgerEntry.type == "consume"))
    assert consume is not None and consume.amount == Decimal("5.000000")
    with pytest.raises(InsufficientFundsError) as raised:
        await _gate(db, org_id)
    assert raised.value.available == Decimal("-3.000000")


@pytest.mark.asyncio
async def test_charge_rejects_negative_amount(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db)
    with pytest.raises(ValueError, match="cannot be negative"):
        await NorthRouterBillingAdapter(db).charge(
            organization_id=org_id, amount=Decimal("-1"), billed=None, tools=NO_TOOLS
        )


@pytest.mark.asyncio
async def test_customer_ledger_hides_hold_release_and_internal_refs(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("50"))
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.charge(organization_id=org_id, amount=Decimal("3"), billed=None, tools=NO_TOOLS, description="chat")
    await db.commit()
    response = await get_ledger(BillingReader(organization_id=org_id, internal=False), db)
    assert {row["type"] for row in response["data"]} == {"grant", "consume"}
    assert all("ref" not in row for row in response["data"])
    assert all(row["created_at"].endswith("+00:00") for row in response["data"])


@pytest.mark.asyncio
async def test_the_ledger_pages_newest_first_without_gaps(db: AsyncSession) -> None:
    org_id = await _ensure_otari_org(db)
    for n in range(7):
        await _grant(db, org_id, f"{n + 1}", f"page-{n}")
    reader = BillingReader(organization_id=org_id, internal=True)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        page = await get_ledger(reader, db, limit=3, cursor=cursor)
        seen.extend(row["id"] for row in page["data"])
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 3
    newest_first = await db.scalars(
        select(NrLedgerEntry.id).order_by(NrLedgerEntry.created_at.desc(), NrLedgerEntry.id.desc())
    )
    assert seen == [str(entry_id) for entry_id in newest_first]
    assert len(set(seen)) == 7
    with pytest.raises(HTTPException) as raised:
        await get_ledger(reader, db, cursor="not-a-cursor")
    assert raised.value.status_code == 422


@pytest.mark.asyncio
async def test_the_account_reports_what_the_gates_judge(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("30"))
    await NorthRouterBillingAdapter(db).hold(organization_id=org_id, amount=Decimal("4"))
    await _status(db, org_id, "suspended", reason="Card declined")
    customer = await get_account(BillingReader(organization_id=org_id, internal=False), db, _settings())
    assert customer == {
        "organization_id": str(org_id),
        "status": "suspended",
        "balance": "30.000000",
        "held": "4.000000",
        "available": "26.000000",
        "min_available": "1.000000",
        "concurrency_limit": 8,
    }
    service = await get_account(BillingReader(organization_id=org_id, internal=True), db, _settings())
    assert service["suspend_reason"] == "Card declined"
    assert service["status_effective_at"] is not None

    unknown = await _ensure_otari_org(db)
    fresh = await get_account(BillingReader(organization_id=unknown, internal=False), db, _settings())
    assert (fresh["status"], fresh["balance"]) == ("active", "0.000000")
    assert await db.scalar(select(NrOrg).where(NrOrg.otari_org_id == unknown)) is None


@pytest.mark.asyncio
async def test_stored_balance_equals_ledger_sum_for_grants_refunds_holds_and_charges(db: AsyncSession) -> None:
    org_id = await _org_and_credit(db, Decimal("25"))
    await _grant(db, org_id, "12.50", "balance-grant")
    await _grant(db, org_id, "-2.50", "balance-refund", reason="Reversal")
    adapter = NorthRouterBillingAdapter(db)
    await adapter.hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.release_hold(organization_id=org_id, amount=Decimal("10"))
    await adapter.charge(organization_id=org_id, amount=Decimal("4.25"), billed=None, tools=NO_TOOLS)
    await db.commit()
    org = await _nr_org(db, org_id)
    assert org.balance == await _ledger_sum(db, org.id) == Decimal("30.750000")


def test_money_rejects_nan_and_infinity() -> None:
    for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), "NaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError, match="finite"):
            _money(bad)


# Migrations


def _alembic(monkeypatch: pytest.MonkeyPatch, url: str) -> Config:
    monkeypatch.setenv("NORTHROUTER_DATABASE_URL", url)
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_INI.parent / "alembic"))
    return config


def _assert_chain_matches_models(monkeypatch: pytest.MonkeyPatch, url: str, sync_url: str) -> None:
    config = _alembic(monkeypatch, url)
    command.upgrade(config, "head")
    engine = create_engine(sync_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection, opts={"include_object": include_object, "compare_type": True}
            )
            assert compare_metadata(context, BillingBase.metadata) == []
        command.downgrade(config, "base")
        assert not {
            name for name in inspect(engine).get_table_names() if include_object(None, name, "table", True, None)
        }
    finally:
        engine.dispose()


def test_the_migration_chain_builds_exactly_the_models_on_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database_file = tmp_path / "overlay-migration.db"
    _assert_chain_matches_models(monkeypatch, f"sqlite+aiosqlite:///{database_file}", f"sqlite:///{database_file}")


def test_the_migration_chain_builds_exactly_the_models_on_postgresql(monkeypatch: pytest.MonkeyPatch) -> None:
    server = _server_url()
    if server is None:
        pytest.skip("TEST_DATABASE_URL is not a PostgreSQL server")
    with throwaway_database(server) as url:
        rendered = url.set(drivername="postgresql+asyncpg").render_as_string(hide_password=False)
        sync = url.set(drivername="postgresql+psycopg2").render_as_string(hide_password=False)
        _assert_chain_matches_models(monkeypatch, rendered, sync)


def test_autogenerate_never_compares_otaris_tables() -> None:
    assert include_object(None, "nr_orgs", "table", True, None)
    assert not include_object(None, "nr_alembic_version", "table", True, None)
    assert not include_object(None, "api_keys", "table", True, None)
    assert not include_object(None, "alembic_version", "table", True, None)


# PostgreSQL concurrency


@pytest.mark.asyncio
async def test_pg_concurrent_grants_same_key(pg_session_factory: Any) -> None:
    async with pg_session_factory() as setup_db:
        org_id = await _ensure_otari_org(setup_db)
        await _ensure_org(setup_db, org_id)
        await setup_db.commit()

    async def deliver() -> dict[str, Any]:
        async with pg_session_factory() as session:
            return await _grant(session, org_id, "25.00", "pg-grant-same-key", reason="Concurrent payment")

    results = await asyncio.gather(deliver(), deliver())
    assert sorted(result["granted"] for result in results) == [False, True]
    async with pg_session_factory() as verify_db:
        entries = await verify_db.scalar(
            select(func.count())
            .select_from(NrLedgerEntry)
            .where(NrLedgerEntry.ref == _grant_ledger_ref("pg-grant-same-key"))
        )
        assert entries == 1
        assert (await _nr_org(verify_db, org_id)).balance == Decimal("25.000000")


@pytest.mark.asyncio
async def test_pg_concurrent_holds_on_same_funds(pg_session_factory: Any) -> None:
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

    assert sorted(await asyncio.gather(place_hold(), place_hold())) == [False, True]


@pytest.mark.asyncio
async def test_pg_grants_racing_charges_keep_the_balance_equal_to_the_ledger(pg_session_factory: Any) -> None:
    async with pg_session_factory() as setup_db:
        org_id = await _org_and_credit(setup_db, Decimal("10"))
        await setup_db.commit()

    async def grant(n: int) -> None:
        async with pg_session_factory() as session:
            await _grant(session, org_id, "3", f"race-grant-{n}")

    async def charge() -> None:
        async with pg_session_factory() as session:
            await NorthRouterBillingAdapter(session).charge(
                organization_id=org_id, amount=Decimal("2"), billed=None, tools=NO_TOOLS
            )
            await session.commit()

    await asyncio.gather(*(grant(n) for n in range(5)), *(charge() for _ in range(5)))
    async with pg_session_factory() as verify_db:
        org = await _nr_org(verify_db, org_id)
        assert org.balance == await _ledger_sum(verify_db, org.id) == Decimal("15.000000")
