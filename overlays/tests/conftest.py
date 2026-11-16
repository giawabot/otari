"""Database and environment fixtures for the NorthRouter billing overlay tests.

``TEST_DATABASE_URL`` names a PostgreSQL *server*, as it does for Otari's own
suite: the tests create a throwaway database on it and drop it afterwards, and
never touch the database the URL itself names. Unset, they run on SQLite.
"""

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import gateway.models  # noqa: F401 - register core models on Base.metadata
from gateway.models.api_keys import APIKey
from gateway.models.base import Base
from gateway.models.tenancy import Organization, OrganizationMember, Workspace
from gateway.models.tenancy import User as TenancyUser
from northrouter_billing.models import BillingBase
from northrouter_billing.settings import configure, load_settings

SERVICE_TOKEN = "billing-service-test-token-0123456789abcdef"
PREVIOUS_TOKEN = "billing-service-old-token-0123456789abcdefgh"
OAUTH_CLIENT_ID = "northrouter-web"


def _server_url() -> URL | None:
    configured = os.getenv("TEST_DATABASE_URL", "").strip()
    if not configured:
        return None
    url = make_url(configured)
    return url if url.get_backend_name() == "postgresql" else None


def _sync(url: URL) -> URL:
    return url.set(drivername="postgresql+psycopg2")


def _async(url: URL) -> URL:
    return url.set(drivername="postgresql+asyncpg")


@contextmanager
def throwaway_database(server: URL) -> Iterator[URL]:
    """Create an empty database on ``server`` for the duration of the block, then drop it."""
    name = f"nr_overlay_{uuid.uuid4().hex[:12]}"
    admin = create_engine(_sync(server), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        try:
            yield server.set(database=name)
        finally:
            with admin.connect() as connection:
                connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def pg_schema_url() -> Iterator[URL | None]:
    """A throwaway PostgreSQL database holding Otari's and the overlay's tables, or ``None`` on SQLite."""
    server = _server_url()
    if server is None:
        yield None
        return
    with throwaway_database(server) as url:
        engine = create_engine(_sync(url))
        try:
            with engine.begin() as connection:
                Base.metadata.create_all(connection)
                BillingBase.metadata.create_all(connection)
        finally:
            engine.dispose()
        yield url


def configure_from_environment() -> None:
    """Re-read the overlay's settings after a test changes its environment."""
    configure(load_settings())


@pytest.fixture(autouse=True)
def default_billing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide valid overlay settings unless a test overrides them."""
    monkeypatch.setenv("NORTHROUTER_BILLING_OAUTH_CLIENT_ID", OAUTH_CLIENT_ID)
    monkeypatch.setenv("NORTHROUTER_BILLING_SERVICE_TOKEN", SERVICE_TOKEN)
    for name in (
        "NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS",
        "NORTHROUTER_ORG_CONCURRENCY_LIMIT",
        "NORTHROUTER_MIN_AVAILABLE_USD",
        "NORTHROUTER_MAX_GRANT_USD",
        "OTARI_DEPLOYMENT_PAID_JOBS",
    ):
        monkeypatch.delenv(name, raising=False)
    configure_from_environment()


@pytest_asyncio.fixture
async def session_factory(pg_schema_url: URL | None) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on a clean schema: SQLite in memory, or the throwaway PostgreSQL database truncated."""
    if pg_schema_url is None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.run_sync(BillingBase.metadata.create_all)
    else:
        engine = create_async_engine(_async(pg_schema_url), pool_size=5, max_overflow=5)
        tables = ", ".join(
            f'"{table.name}"' for table in [*Base.metadata.sorted_tables, *BillingBase.metadata.sorted_tables]
        )
        async with engine.begin() as connection:
            await connection.execute(text(f"TRUNCATE TABLE {tables} CASCADE"))
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_session_factory(
    pg_schema_url: URL | None, session_factory: async_sessionmaker[AsyncSession]
) -> async_sessionmaker[AsyncSession]:
    """``session_factory`` on PostgreSQL, for row-lock concurrency tests; skipped on SQLite."""
    if pg_schema_url is None:
        pytest.skip("TEST_DATABASE_URL is not a PostgreSQL server; skipping PostgreSQL concurrency test")
    return session_factory


@pytest_asyncio.fixture
async def db(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


async def seed_tenancy_org(
    db: AsyncSession,
    *,
    org_id: uuid.UUID | None = None,
    role: str = "owner",
    oauth_client_id: str | None = OAUTH_CLIENT_ID,
    metadata: dict[str, str] | None = None,
    self_service: bool = True,
    user_active: bool = True,
    member_status: str = "active",
) -> tuple[uuid.UUID, TenancyUser, Workspace, APIKey]:
    """Create an Otari organization, workspace, user, membership, and API key for testing.

    ``oauth_client_id`` is the app the key was issued to, as the app grant
    exchange records it; ``metadata`` is what the key's owner wrote.
    """
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
        db.add(
            OrganizationMember(
                id=uuid.uuid4(),
                organization_id=resolved_org_id,
                user_id=user.id,
                role=role,
                status=member_status,
            )
        )

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
        oauth_client_id=oauth_client_id,
        metadata_=metadata or {},
    )
    db.add(api_key)
    await db.commit()
    return resolved_org_id, user, workspace, api_key
