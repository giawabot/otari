"""The sovereignty domain's service: residency bars and provider attestations.

Every method is pure and synchronous except ``attestations``, which only
reads rows; the routing resolver stays side-effect free so the CLI
``explain`` path can call it without a database.
"""

import uuid
from typing import Any

from gateway.core.unit_of_work import UnitOfWork
from gateway.models.sovereignty import ProviderSovereignty
from gateway.repositories.sovereignty import list_sovereignty_rows
from gateway.services.sovereignty.access import (
    SovereigntyMap,
    build_sovereignty_map,
    combine_residency_bars,
    is_sovereign_enough,
    parse_residency_bar,
    residency_refusal_detail,
)

__all__ = ["SovereigntyService"]


class SovereigntyService:
    """Answers residency questions for routing and loads attestation rows."""

    @staticmethod
    def parse_bar(value: str) -> int:
        """Minimum level a wire bar name requires; raises ``ValueError`` on unknown bars."""
        return parse_residency_bar(value)

    @staticmethod
    def map_for(config: Any, rows: list[Any]) -> SovereigntyMap:
        """Precedence-folded attestation map from config baseline plus DB rows."""
        return build_sovereignty_map(config, rows)

    @staticmethod
    def is_enough(
        sovereignty: SovereigntyMap, provider: str | None, model: str | None, required_level: int
    ) -> tuple[bool, str]:
        """Whether one candidate clears the required level under the map's precedence."""
        return is_sovereign_enough(sovereignty, provider, model, required_level)

    @staticmethod
    def combine_bars(request_bar: str | None, organization_floor: str | None) -> tuple[int, str]:
        """Fold a request bar and organization floor into their effective level and source."""
        return combine_residency_bars(request_bar, organization_floor)

    @staticmethod
    def refusal_detail(residency: str | None, required_level: int, source: str = "request") -> str:
        """The 403 detail naming the bar and what it means."""
        return residency_refusal_detail(residency, required_level, source)

    @staticmethod
    async def attestations(
        uow: UnitOfWork,
        *,
        organization_id: uuid.UUID | None = None,
    ) -> list[ProviderSovereignty]:
        """Attestation rows visible to one organization (deployment-wide + own).

        Opens a Unit of Work block: repositories read through the block's
        session, and a bare Unit of Work with no open block refuses it.
        """
        async with uow:
            return await list_sovereignty_rows(uow, organization_id=organization_id)
