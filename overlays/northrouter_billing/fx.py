"""Bank of Canada FXUSDCAD exchange rate lookup and CAD-to-USD conversion."""

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from typing import Any

import httpx

BOC_FXUSDCAD_URL = "https://www.bankofcanada.ca/valet/observations/FXUSDCAD/json?recent=1"
FX_SOURCE = "boc:FXUSDCAD"
FX_TIMEOUT_SECONDS = 5.0
MIN_FX_RATE = Decimal("0.5")
MAX_FX_RATE = Decimal("3.0")
MAX_FX_AGE_DAYS = 7
MAX_FX_MARGIN = Decimal("0.2")
CENT_SCALE = Decimal("0.01")


class FxRateError(RuntimeError):
    """Raised when the Bank of Canada exchange rate cannot be fetched or fails validation."""


@dataclass(frozen=True, slots=True)
class FxQuote:
    """Validated Bank of Canada daily CAD-per-USD rate observation."""

    rate: Decimal
    rate_date: str
    source: str = FX_SOURCE


def get_fx_margin() -> Decimal:
    """Read and validate NORTHROUTER_ETRANSFER_FX_MARGIN from the environment.

    Validated at overlay registration time so an invalid margin prevents server
    startup rather than failing only when an operator approves an e-transfer.
    """
    raw = os.getenv("NORTHROUTER_ETRANSFER_FX_MARGIN", "0").strip()
    if not raw:
        raw = "0"
    try:
        margin = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("NORTHROUTER_ETRANSFER_FX_MARGIN must be a finite decimal between 0 and 0.2") from exc
    if not margin.is_finite() or margin < Decimal("0") or margin > MAX_FX_MARGIN:
        raise ValueError("NORTHROUTER_ETRANSFER_FX_MARGIN must be a finite decimal between 0 and 0.2")
    return margin


def parse_boc_usdcad_payload(payload: Any, *, now: datetime | None = None) -> FxQuote:
    """Extract and validate the latest FXUSDCAD observation from a Valet JSON payload."""
    if not isinstance(payload, dict):
        raise FxRateError("Bank of Canada response must be a JSON object")
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise FxRateError("Bank of Canada response contains no observations")
    latest = observations[-1]
    if not isinstance(latest, dict):
        raise FxRateError("Bank of Canada observation entry is malformed")
    raw_date = latest.get("d")
    if not isinstance(raw_date, str) or not raw_date:
        raise FxRateError("Bank of Canada observation is missing its date")
    try:
        obs_date = date.fromisoformat(raw_date)
    except ValueError as exc:
        raise FxRateError("Bank of Canada observation date is invalid") from exc

    reference_date = (now or datetime.now(UTC)).astimezone(UTC).date()
    if reference_date - obs_date > timedelta(days=MAX_FX_AGE_DAYS) or obs_date > reference_date + timedelta(days=1):
        raise FxRateError(f"Bank of Canada FXUSDCAD observation from {raw_date} is stale")

    series = latest.get("FXUSDCAD")
    if not isinstance(series, dict):
        raise FxRateError("Bank of Canada observation is missing FXUSDCAD series data")
    raw_value = series.get("v")
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise FxRateError("Bank of Canada FXUSDCAD rate must be a decimal string")
    try:
        rate = Decimal(raw_value)
    except InvalidOperation as exc:
        raise FxRateError("Bank of Canada FXUSDCAD rate is not a valid decimal") from exc
    if not rate.is_finite() or rate < MIN_FX_RATE or rate > MAX_FX_RATE:
        raise FxRateError("Bank of Canada FXUSDCAD rate is outside the allowed [0.5, 3.0] range")
    return FxQuote(rate=rate, rate_date=raw_date, source=FX_SOURCE)


async def fetch_boc_usdcad_rate(*, now: datetime | None = None) -> FxQuote:
    """Fetch the most recent daily FXUSDCAD rate from the Bank of Canada Valet API."""
    try:
        async with httpx.AsyncClient(timeout=FX_TIMEOUT_SECONDS) as client:
            response = await client.get(BOC_FXUSDCAD_URL)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise FxRateError("Failed to fetch Bank of Canada FXUSDCAD rate") from exc
    return parse_boc_usdcad_payload(payload, now=now)


def convert_cad_to_usd(cad_amount: Decimal, *, rate: Decimal, margin: Decimal = Decimal("0")) -> Decimal:
    """Convert a CAD amount to USD using ``cad / rate * (1 - margin)``, rounded down to cents."""
    if not cad_amount.is_finite() or cad_amount <= Decimal("0"):
        raise ValueError("CAD amount must be a positive finite decimal")
    if not rate.is_finite() or rate < MIN_FX_RATE or rate > MAX_FX_RATE:
        raise FxRateError("FX rate must be finite and between 0.5 and 3.0")
    if not margin.is_finite() or margin < Decimal("0") or margin > MAX_FX_MARGIN:
        raise ValueError("FX margin must be finite and between 0 and 0.2")
    usd = (cad_amount / rate) * (Decimal("1") - margin)
    return usd.quantize(CENT_SCALE, rounding=ROUND_DOWN)
