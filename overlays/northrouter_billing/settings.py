"""The overlay's environment configuration, validated once when the overlay is bound."""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from northrouter_billing.models import MONEY_MAX, _money

MIN_SERVICE_TOKEN_LENGTH = 32
DEFAULT_CONCURRENCY_LIMIT = 8
DEFAULT_MIN_AVAILABLE_USD = Decimal("1.00")
DEFAULT_MAX_GRANT_USD = Decimal("10000")

SERVICE = "service"
SERVICE_PREVIOUS = "service_previous"
MASTER = "master"


@dataclass(frozen=True)
class BillingSettings:
    """What the overlay reads from its environment."""

    service_tokens: tuple[tuple[str, str], ...]
    """Each accepted billing service token, with the principal it authenticates as."""
    oauth_client_id: str | None
    """The registered OAuth app whose keys may read billing, or ``None`` for none."""
    concurrency_limit: int
    min_available_usd: Decimal
    max_grant_usd: Decimal

    def __repr__(self) -> str:
        # Never render the tokens, wherever a settings object ends up printed.
        return (
            f"BillingSettings(service_tokens=<{len(self.service_tokens)} hidden>, "
            f"oauth_client_id={self.oauth_client_id!r}, concurrency_limit={self.concurrency_limit}, "
            f"min_available_usd={self.min_available_usd}, max_grant_usd={self.max_grant_usd})"
        )


def _decimal(environ: Mapping[str, str], name: str, default: Decimal) -> Decimal:
    raw = environ.get(name, "").strip()
    if not raw:
        return _money(default)
    try:
        return _money(Decimal(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc


def _token(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name, "").strip()
    if not value:
        return None
    if len(value) < MIN_SERVICE_TOKEN_LENGTH:
        raise ValueError(f"{name} must be at least {MIN_SERVICE_TOKEN_LENGTH} characters")
    return value


def load_settings(environ: Mapping[str, str] | None = None) -> BillingSettings:
    """Read and validate the overlay's environment.

    Raises:
        ValueError: a variable is missing or malformed. The message names the
            variable and never its value.

    """
    env = os.environ if environ is None else environ

    current = _token(env, "NORTHROUTER_BILLING_SERVICE_TOKEN")
    if current is None:
        raise ValueError("NORTHROUTER_BILLING_SERVICE_TOKEN must be set")
    previous = _token(env, "NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS")
    if previous is not None and previous == current:
        raise ValueError(
            "NORTHROUTER_BILLING_SERVICE_TOKEN_PREVIOUS must differ from NORTHROUTER_BILLING_SERVICE_TOKEN"
        )
    tokens = ((SERVICE, current),) + (((SERVICE_PREVIOUS, previous),) if previous is not None else ())

    raw_limit = env.get("NORTHROUTER_ORG_CONCURRENCY_LIMIT", "").strip()
    try:
        limit = int(raw_limit) if raw_limit else DEFAULT_CONCURRENCY_LIMIT
    except ValueError as exc:
        raise ValueError("NORTHROUTER_ORG_CONCURRENCY_LIMIT must be a positive integer") from exc
    if limit < 1:
        raise ValueError("NORTHROUTER_ORG_CONCURRENCY_LIMIT must be a positive integer")

    minimum = _decimal(env, "NORTHROUTER_MIN_AVAILABLE_USD", DEFAULT_MIN_AVAILABLE_USD)
    if minimum < 0:
        raise ValueError("NORTHROUTER_MIN_AVAILABLE_USD must not be negative")

    max_grant = _decimal(env, "NORTHROUTER_MAX_GRANT_USD", DEFAULT_MAX_GRANT_USD)
    if max_grant <= 0 or max_grant > MONEY_MAX:
        raise ValueError(f"NORTHROUTER_MAX_GRANT_USD must be positive and at most {MONEY_MAX}")

    client_id = env.get("NORTHROUTER_BILLING_OAUTH_CLIENT_ID", "").strip() or None
    return BillingSettings(
        service_tokens=tokens,
        oauth_client_id=client_id,
        concurrency_limit=limit,
        min_available_usd=minimum,
        max_grant_usd=max_grant,
    )


_current: BillingSettings | None = None


def configure(settings: BillingSettings) -> None:
    """Make ``settings`` what the adapter and routes read."""
    global _current
    _current = settings


def current_settings() -> BillingSettings:
    """The settings the overlay was bound with.

    Raises:
        RuntimeError: the overlay was never bound through ``register``.

    """
    if _current is None:
        raise RuntimeError("NorthRouter billing settings are not configured; bind the overlay through register()")
    return _current
