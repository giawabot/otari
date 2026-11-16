"""Entitlement adapter that extends, rather than replaces, Otari's base set."""

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.adapters.entitlement_adapter import BaseEntitlementAdapter
from gateway.ports.entitlement_port import EntitlementPort


class NorthRouterEntitlementAdapter(EntitlementPort):
    """Preserve core capabilities and add the billing router capability."""

    def __init__(self, session: AsyncSession | None) -> None:
        self._base = BaseEntitlementAdapter(session)

    async def entitlements(self) -> set[str]:
        return await self._base.entitlements() | {"northrouter_billing"}
