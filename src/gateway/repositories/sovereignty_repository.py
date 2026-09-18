"""Data access for provider sovereignty attestations.

Pure data access: no business logic, no authorization, no commits (the
service layer owns the transaction boundary). The resolver that routing
consults lives in ``services/sovereignty_access.py``; this module only
loads rows.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.models.sovereignty import ProviderSovereignty


async def list_sovereignty_rows(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID | None = None,
) -> list[ProviderSovereignty]:
    """All attestation rows visible to one organization.

    Deployment-wide rows (``organization_id IS NULL``) plus the
    organization's own rows. Ordering is stable (provider, then
    model-null-first) so a caller building a precedence map reads rows in
    a deterministic order; precedence itself is the resolver's rule, not
    this query's.
    """
    stmt = select(ProviderSovereignty).order_by(
        ProviderSovereignty.provider,
        ProviderSovereignty.model.asc().nullsfirst(),
    )
    if organization_id is not None:
        stmt = stmt.where(
            (ProviderSovereignty.organization_id.is_(None))
            | (ProviderSovereignty.organization_id == organization_id)
        )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def upsert_sovereignty(
    db: AsyncSession,
    *,
    provider: str,
    model: str | None,
    organization_id: uuid.UUID | None,
    sovereignty_level: int,
    hosted_in: str,
    operator_jurisdiction: str,
    model_origin: str | None = None,
    vetted: bool = False,
    evidence: dict | None = None,
) -> ProviderSovereignty:
    """Insert or update the one row for (organization, provider, model).

    Flushes, never commits. The unique constraint on the triple is the
    arbiter: a concurrent writer wins the insert and this call updates the
    winner's row on the next pass rather than raising.
    """
    stmt = select(ProviderSovereignty).where(
        ProviderSovereignty.provider == provider,
        ProviderSovereignty.model == model,
        ProviderSovereignty.organization_id == organization_id
        if organization_id is not None
        else ProviderSovereignty.organization_id.is_(None),
    )
    existing = (await db.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        existing.sovereignty_level = sovereignty_level
        existing.hosted_in = hosted_in
        existing.operator_jurisdiction = operator_jurisdiction
        existing.model_origin = model_origin
        existing.vetted = vetted
        existing.evidence = evidence
        await db.flush()
        return existing
    row = ProviderSovereignty(
        provider=provider,
        model=model,
        organization_id=organization_id,
        sovereignty_level=sovereignty_level,
        hosted_in=hosted_in,
        operator_jurisdiction=operator_jurisdiction,
        model_origin=model_origin,
        vetted=vetted,
        evidence=evidence,
    )
    db.add(row)
    await db.flush()
    return row


__all__ = ["list_sovereignty_rows", "upsert_sovereignty"]
