"""Synchronous OTARI_BOOTSTRAP entry point for the NorthRouter billing overlay."""

from sqlalchemy.ext.asyncio import AsyncSession

from gateway.container import Container, RouterContribution
from gateway.log_config import logger
from gateway.ports.billing_port import BillingPort
from gateway.ports.entitlement_port import EntitlementPort
from northrouter_billing.adapter import NorthRouterBillingAdapter, validate_billing_env
from northrouter_billing.entitlements import NorthRouterEntitlementAdapter
from northrouter_billing.routes import billing_router


def _billing_factory(session: AsyncSession | None) -> BillingPort:
    return NorthRouterBillingAdapter(session)


def _entitlement_factory(session: AsyncSession | None) -> EntitlementPort:
    return NorthRouterEntitlementAdapter(session)


def register(container: Container) -> None:
    """Validate overlay configuration, bind billing, and expose its HTTP surface."""
    validate_billing_env()
    container.bind(BillingPort, _billing_factory)
    container.bind(EntitlementPort, _entitlement_factory)
    container.contribute_router(RouterContribution(capability="northrouter_billing", router=billing_router))
    logger.info("NorthRouter billing overlay bound")
