"""Small Stripe SDK boundary for routes that handle payment lifecycle events."""

import os
from typing import Any

import stripe


class StripeClient:
    """Read Stripe credentials on demand and keep SDK calls out of inference."""

    @property
    def secret_key(self) -> str:
        value = os.getenv("NORTHROUTER_STRIPE_SECRET_KEY", "")
        if not value:
            raise RuntimeError("NORTHROUTER_STRIPE_SECRET_KEY is not configured")
        return value

    @property
    def webhook_secret(self) -> str:
        value = os.getenv("NORTHROUTER_STRIPE_WEBHOOK_SECRET", "")
        if not value:
            raise RuntimeError("NORTHROUTER_STRIPE_WEBHOOK_SECRET is not configured")
        return value

    def create_customer(
        self, *, organization_id: str, email: str | None = None, idempotency_key: str | None = None
    ) -> Any:
        return stripe.Customer.create(
            api_key=self.secret_key,
            metadata={"organization_id": organization_id},
            email=email,
            idempotency_key=idempotency_key,
        )

    def create_payment_intent(
        self, *, customer_id: str, organization_id: str, amount_minor: int, currency: str = "usd"
    ) -> Any:
        return stripe.PaymentIntent.create(
            api_key=self.secret_key,
            customer=customer_id,
            amount=amount_minor,
            currency=currency,
            metadata={"organization_id": organization_id},
        )

    def create_subscription(
        self, *, customer_id: str, price_id: str, organization_id: str, plan: str, credit_amount: str
    ) -> Any:
        return stripe.Subscription.create(
            api_key=self.secret_key,
            customer=customer_id,
            items=[{"price": price_id}],
            expand=["latest_invoice.payment_intent"],
            metadata={
                "organization_id": organization_id,
                "plan": plan,
                "credit_amount": credit_amount,
            },
        )

    def list_subscriptions(self, *, customer_id: str) -> Any:
        return stripe.Subscription.list(api_key=self.secret_key, customer=customer_id, status="all")

    def construct_event(self, payload: bytes, signature: str) -> Any:
        return stripe.Webhook.construct_event(payload, signature, self.webhook_secret)
