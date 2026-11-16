"""Synchronous OTARI_BOOTSTRAP entry point for the NorthRouter billing overlay."""

import os

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.container import Container, RouterContribution
from gateway.log_config import logger
from gateway.ports.billing_port import BillingPort
from gateway.ports.entitlement_port import EntitlementPort
from northrouter_billing.adapter import NorthRouterBillingAdapter
from northrouter_billing.entitlements import NorthRouterEntitlementAdapter
from northrouter_billing.routes import billing_router
from northrouter_billing.settings import configure, load_settings


def _entitlement_factory(session: AsyncSession | None) -> EntitlementPort:
    return NorthRouterEntitlementAdapter(session)


def register(container: Container) -> None:
    """Validate overlay configuration, bind billing, and expose its HTTP surface."""
    settings = load_settings()
    configure(settings)

    def billing_factory(session: AsyncSession | None) -> BillingPort:
        return NorthRouterBillingAdapter(session, settings)

    container.bind(BillingPort, billing_factory)
    container.bind(EntitlementPort, _entitlement_factory)
    container.contribute_router(RouterContribution(capability="northrouter_billing", router=billing_router))
    if os.getenv("OTARI_DEPLOYMENT_PAID_JOBS", "").strip().lower() not in {"false", "0", "no", "off"}:
        # The overlay cannot read config.yml, so it cannot tell whether the file turns jobs off.
        logger.warning(
            "NorthRouter billing: set OTARI_DEPLOYMENT_PAID_JOBS=false. A batch or video job on a hosted "
            "provider is charged only when it completes, so no balance check bounds what it costs."
        )
    logger.info("NorthRouter billing overlay bound")
