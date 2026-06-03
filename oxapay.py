# -*- coding: utf-8 -*-
"""Stub OxaPay integration — real API keys not configured; returns safe fallbacks."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Used by deposit UI: currency -> metadata (network label optional)
SUPPORTED_CURRENCIES: dict[str, dict[str, str]] = {
    "USDT": {"network": "TRC20"},
    "BTC": {"network": "BTC"},
    "ETH": {"network": "ERC20"},
    "LTC": {"network": "LTC"},
    "DOGE": {"network": "DOGE"},
}


async def get_crypto_amount_for_usd(usd_amount: float, currency: str) -> float:
    """Placeholder rate: 1 USD worth of crypto (invoice creation may still fail without API)."""
    _ = currency
    return round(float(usd_amount), 8)


async def create_invoice(
    *,
    amount: float,
    currency: str,
    life_time: int = 1800,
    fee_paid_by_payer: int = 1,
    under_paid: float = 0,
    auto_withdrawal: int = 0,
    mixed_payment: int = 0,
) -> dict[str, Any] | None:
    """Without OxaPay merchant credentials, invoice creation is unavailable."""
    _ = (
        amount,
        currency,
        life_time,
        fee_paid_by_payer,
        under_paid,
        auto_withdrawal,
        mixed_payment,
    )
    logger.warning("oxapay.create_invoice: stub — configure OxaPay API for real invoices")
    return None


async def request_static_address(currency: str, network: str) -> dict[str, Any]:
    """Stub static address request."""
    _ = currency, network
    return {"result": 0, "message": "stub"}


async def check_invoice(track_id: str) -> dict[str, Any] | None:
    """Stub: never marks paid."""
    _ = track_id
    return None
