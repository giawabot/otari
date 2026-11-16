"""Tests for the caller's self-scoped personal budget read (GET /organizations/me/my-budget)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

import gateway.models  # noqa: F401
from gateway.api.deps import get_api_key_service, get_unit_of_work, get_user_access
from gateway.api.routes.organizations import get_caller_organization_budget
from gateway.exceptions.budget_exceptions import OrganizationMemberBudgetNotFoundError
from gateway.models.budgets import SCOPE_ORG_MEMBER, SCOPE_WORKSPACE_MEMBER, Budget, ScopedBudget
from gateway.models.tenancy import Organization, OrganizationMember, User, Workspace, WorkspaceMember
from gateway.models.users import User as AttributionUser
from gateway.repositories.budgets import BudgetRepositories, CeilingUsage, LimitUsage
from gateway.services.budgets import BudgetService, reserve_budget
from gateway.services.budgets._scoped_enforcement import BudgetScopeRequest
from gateway.services.tenancy.authorization import WorkspaceAccess
from gateway.services.tenancy.organization_service import OrganizationService


def _limit(
    *,
    max_budget: str | None,
    spent: str = "0",
    reserved: str = "0",
    scope: str = SCOPE_WORKSPACE_MEMBER,
    alignment: str | None = "calendar_month",
    duration: int | None = None,
    period_end: datetime | None = None,
) -> LimitUsage:
    return LimitUsage(
        scope=scope,
        provider_key_id=None,
        budget_duration_sec=duration,
        reset_alignment=alignment,
        usage=CeilingUsage(
            max_budget=None if max_budget is None else Decimal(max_budget),
            current_spend=Decimal(spent),
            reserved_spend=Decimal(reserved),
            token_limit=None,
            current_tokens=0,
            reserved_tokens=0,
            request_limit=None,
            current_requests=0,
            reserved_requests=0,
            period_end=period_end,
        ),
    )


def _service_with_limits(
    ceilings: list[LimitUsage],
    *,
    org_member_id: uuid.UUID | None = None,
    workspace_member_ids: list[uuid.UUID] | None = None,
) -> BudgetService:
    uow = MagicMock()
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    repositories = MagicMock()
    repositories.ceilings.usage_in_scopes = AsyncMock(return_value=ceilings)
    organizations = MagicMock(spec=OrganizationService)
    organizations.get_caller_member_scope_ids = AsyncMock(
        return_value=(
            org_member_id or uuid.uuid4(),
            workspace_member_ids if workspace_member_ids is not None else [uuid.uuid4()],
        )
    )
    users = MagicMock()
    users.budget_use = AsyncMock(return_value=None)
    return BudgetService(
        uow,
        repositories,
        organizations,
        MagicMock(),
        MagicMock(),
        users=users,
    )


@pytest.mark.asyncio
async def test_member_with_workspace_member_ceiling_sees_remaining_including_reserved() -> None:
    reset_at = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    user = User(id=uuid.uuid4(), email="member@example.com")
    service = _service_with_limits(
        [
            _limit(
                max_budget="50.00",
                spent="12.25",
                reserved="2.75",
                scope=SCOPE_WORKSPACE_MEMBER,
                alignment="calendar_month",
                period_end=reset_at,
            )
        ]
    )

    body = await get_caller_organization_budget(service=service, current_identity=user)

    assert body.max_budget == 50.0
    assert body.spend == 12.25
    assert body.reserved == 2.75
    assert body.remaining == 35.0
    assert body.next_reset_at == reset_at.isoformat()
    assert body.cadence == "calendar_month"


@pytest.mark.asyncio
async def test_member_with_two_applicable_ceilings_gets_the_binding_smallest_one() -> None:
    org_reset = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    ws_reset = datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
    user = User(id=uuid.uuid4(), email="member@example.com")
    service = _service_with_limits(
        [
            _limit(
                max_budget="100.00",
                spent="20.00",
                reserved="5.00",
                scope=SCOPE_ORG_MEMBER,
                alignment="calendar_month",
                period_end=org_reset,
            ),
            _limit(
                max_budget="30.00",
                spent="15.00",
                reserved="3.50",
                scope=SCOPE_WORKSPACE_MEMBER,
                alignment="calendar_day",
                period_end=ws_reset,
            ),
        ]
    )

    body = await get_caller_organization_budget(service=service, current_identity=user)

    assert body.max_budget == 30.0
    assert body.spend == 15.0
    assert body.reserved == 3.5
    assert body.remaining == 11.5
    assert body.next_reset_at == ws_reset.isoformat()
    assert body.cadence == "calendar_day"


@pytest.mark.asyncio
async def test_member_with_no_applicable_ceiling_raises_404() -> None:
    user = User(id=uuid.uuid4(), email="member@example.com")
    service = _service_with_limits([])

    with pytest.raises(OrganizationMemberBudgetNotFoundError) as exc_info:
        await get_caller_organization_budget(service=service, current_identity=user)

    assert exc_info.value.status_code == status.HTTP_404_NOT_FOUND


@pytest.mark.asyncio
async def test_my_budget_matches_enforcement_gate_and_isolates_members() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            org = Organization(name="Acme", slug="acme")
            ws = Workspace(name="Default", organization_id=org.id)
            alice = User(email="alice@example.com", active_organization_id=org.id)
            bob = User(email="bob@example.com", active_organization_id=org.id)
            session.add_all([org, ws, alice, bob])
            await session.flush()

            alice_org_member = OrganizationMember(organization_id=org.id, user_id=alice.id, role="member")
            bob_org_member = OrganizationMember(organization_id=org.id, user_id=bob.id, role="member")
            alice_ws_member = WorkspaceMember(workspace_id=ws.id, user_id=alice.id, role="member")
            bob_ws_member = WorkspaceMember(workspace_id=ws.id, user_id=bob.id, role="member")
            session.add_all([alice_org_member, bob_org_member, alice_ws_member, bob_ws_member])
            await session.flush()

            session.add_all(
                [
                    AttributionUser(user_id=str(alice.id), alias="Alice"),
                    AttributionUser(user_id=str(bob.id), alias="Bob"),
                ]
            )
            reset_at = datetime.now(UTC) + timedelta(days=7)
            budget = Budget(
                budget_id="member-cap",
                organization_id=org.id,
                max_budget=Decimal("10.00"),
                reset_alignment="calendar_week",
            )
            session.add(budget)
            await session.flush()

            ceiling = ScopedBudget(
                scope_type=SCOPE_WORKSPACE_MEMBER,
                scope_id=str(alice_ws_member.id),
                budget_id=budget.budget_id,
                current_spend=Decimal("4.00"),
                reserved_spend=Decimal("2.50"),
                period_start=datetime.now(UTC) - timedelta(days=1),
                period_end=reset_at,
            )
            session.add(ceiling)
            await session.commit()
            session.expunge_all()

            def build_service(db: AsyncSession) -> BudgetService:
                uow = get_unit_of_work(db)
                organizations = OrganizationService(db, membership_listener=None)
                return BudgetService(
                    uow,
                    BudgetRepositories.on(uow),
                    organizations,
                    get_api_key_service(uow),
                    WorkspaceAccess(db, organizations),
                    users=get_user_access(uow),
                )

            service = build_service(session)

            # Bob has no ceiling of his own and cannot see Alice's.
            with pytest.raises(OrganizationMemberBudgetNotFoundError) as bob_exc:
                await service.get_my_organization_budget(user=bob)
            assert bob_exc.value.status_code == status.HTTP_404_NOT_FOUND

            # Alice sees her exact remaining headroom (10.00 - 4.00 - 2.50 = 3.50).
            alice_budget = await service.get_my_organization_budget(user=alice)
            assert alice_budget.max_budget == 10.0
            assert alice_budget.spend == 4.0
            assert alice_budget.reserved == 2.5
            assert alice_budget.remaining == 3.5
            assert alice_budget.cadence == "calendar_week"

            # The enforcement gate refuses an estimate of remaining + $0.01 (3.51)
            # and admits an estimate of exactly remaining (3.50).
            scope_req = BudgetScopeRequest(api_key=None, workspace_id=ws.id)
            with pytest.raises(HTTPException) as gate_refusal:
                await reserve_budget(
                    session,
                    str(alice.id),
                    Decimal("3.51"),
                    scope=scope_req,
                )
            assert gate_refusal.value.status_code == status.HTTP_403_FORBIDDEN

            handle = await reserve_budget(
                session,
                str(alice.id),
                Decimal(str(alice_budget.remaining)),
                scope=scope_req,
            )
            assert handle.scoped_estimate == Decimal("3.50")

            # After reserving the remaining 3.50, my-budget reports 0.0 remaining and 6.0 reserved.
            after_hold = await build_service(session).get_my_organization_budget(user=alice)
            assert after_hold.remaining == 0.0
            assert after_hold.reserved == 6.0
    finally:
        await engine.dispose()
