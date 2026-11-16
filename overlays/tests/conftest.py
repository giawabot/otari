"""Database and environment fixtures for the NorthRouter billing overlay tests."""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import gateway.models  # noqa: F401 - register core models on Base.metadata
from gateway.models.api_keys import APIKey
from gateway.models.base import Base
from gateway.models.tenancy import Organization, OrganizationMember, Workspace
from gateway.models.tenancy import User as TenancyUser
from northrouter_billing.models import BillingBase


def _async_database_url(raw_url: str) -> str:
    url = make_url(raw_url)
    if url.drivername in {"postgresql", "postgresql+psycopg2", "postgresql+psycopg"}:
        url = url.set(drivername="postgresql+asyncpg")
    elif url.drivername == "sqlite":
        url = url.set(drivername="sqlite+aiosqlite")
    return url.render_as_string(hide_password=False)


@pytest.fixture(autouse=True)
def default_billing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide default overlay environment variables for tests unless overridden."""
    monkeypatch.setenv("NORTHROUTER_BILLING_OAUTH_CLIENT_ID", "northrouter-web")
    monkeypatch.setenv("NORTHROUTER_STRIPE_PRICE_STARTER", "price_starter")
    monkeypatch.setenv("NORTHROUTER_STRIPE_PRICE_GROWTH", "price_growth")
    monkeypatch.setenv("NORTHROUTER_STRIPE_PRICE_SCALE", "price_scale")
    monkeypatch.setenv("NORTHROUTER_STRIPE_SECRET_KEY", "sk_test_northrouter")
    monkeypatch.setenv("NORTHROUTER_STRIPE_WEBHOOK_SECRET", "whsec_test_northrouter")
    monkeypatch.delenv("NORTHROUTER_ETRANSFER_FX_MARGIN", raising=False)
    monkeypatch.delenv("NORTHROUTER_ORG_CONCURRENCY_LIMIT", raising=False)
    monkeypatch.delenv("NORTHROUTER_MIN_AVAILABLE_USD", raising=False)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Create core and overlay tables in SQLite (or TEST_DATABASE_URL when set)."""
    configured_url = os.getenv("TEST_DATABASE_URL", "").strip()
    db_url = _async_database_url(configured_url) if configured_url else "sqlite+aiosqlite:///:memory:"
    engine = create_async_engine(db_url)
    async with engine.begin() as connection:
        if connection.dialect.name == "postgresql":
            await connection.run_sync(BillingBase.metadata.drop_all)
            await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(BillingBase.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Provide a PostgreSQL session factory for row-lock concurrency tests, skipping when unset."""
    configured_url = os.getenv("TEST_DATABASE_URL", "").strip()
    if not configured_url:
        pytest.skip("TEST_DATABASE_URL is not set; skipping PostgreSQL concurrency test")
    db_url = _async_database_url(configured_url)
    if not db_url.startswith("postgresql"):
        pytest.skip("TEST_DATABASE_URL is not a PostgreSQL URL; skipping PostgreSQL concurrency test")
    engine = create_async_engine(db_url, pool_size=5, max_overflow=5)
    async with engine.begin() as connection:
        for table in reversed(BillingBase.metadata.sorted_tables):
            await connection.execute(text(f'TRUNCATE TABLE "{table.name}" CASCADE'))
        await connection.run_sync(Base.metadata.create_all)
        await connection.run_sync(BillingBase.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def seed_tenancy_org(
    db: AsyncSession,
    *,
    org_id: uuid.UUID | None = None,
    role: str = "owner",
    oauth_client_id: str | None = "northrouter-web",
    self_service: bool = True,
    user_active: bool = True,
    member_status: str = "active",
) -> tuple[uuid.UUID, TenancyUser, Workspace, APIKey]:
    """Create an Otari organization, workspace, user, membership, and API key for testing."""
    resolved_org_id = org_id or uuid.uuid4()
    org = await db.get(Organization, resolved_org_id)
    if org is None:
        org = Organization(
            id=resolved_org_id,
            name=f"Org {resolved_org_id.hex[:8]}",
            slug=f"org-{resolved_org_id.hex[:12]}",
        )
        db.add(org)
        await db.flush()

    workspace = Workspace(
        id=uuid.uuid4(),
        name=f"ws-{uuid.uuid4().hex[:8]}",
        organization_id=resolved_org_id,
    )
    user = TenancyUser(
        id=uuid.uuid4(),
        email=f"user-{uuid.uuid4().hex[:8]}@example.com",
        is_active=user_active,
        active_organization_id=resolved_org_id,
    )
    db.add_all([workspace, user])
    await db.flush()

    if member_status != "none":
        membership = OrganizationMember(
            id=uuid.uuid4(),
            organization_id=resolved_org_id,
            user_id=user.id,
            role=role,
            status=member_status,
        )
        db.add(membership)

    metadata = {"oauth_client_id": oauth_client_id} if oauth_client_id is not None else {}
    api_key = APIKey(
        id=str(uuid.uuid4()),
        workspace_id=workspace.id,
        key_hash=uuid.uuid4().hex + uuid.uuid4().hex,
        key_prefix="otari_",
        key_suffix="1234",
        key_name="test-key",
        user_id=str(user.id),
        self_service=self_service,
        exclude_from_budget=False,
        last_used_at=datetime.now(UTC),
        metadata_=metadata,
    )
    db.add(api_key)
    await db.commit()
    return resolved_org_id, user, workspace, api_key
