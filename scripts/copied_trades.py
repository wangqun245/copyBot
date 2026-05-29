"""
Idempotency ledger for copied trades.

Each source-trader trade we act on is written here keyed on transaction_hash
BEFORE the order hits Polymarket. A duplicate key means the event is a replay
and must not place a second order.
"""
from typing import Optional
from datetime import datetime, timezone

from config import get_config
from db import (
    estimate_close_pnl,
    estimate_settlement_pnl,
    insert_copied_trade,
    sum_trader_exposure,
    total_realized_pnl,
    update_copied_trade,
)
from logger import logger


config = get_config()


def claim_trade(
    transaction_hash: str,
    source_wallet: str,
    asset: str,
    side: str,
    price: float,
    bot_usdc_size: float,
    condition_id: Optional[str] = None,
    bot_units: Optional[float] = None,
    title: Optional[str] = None,
) -> bool:
    """
    Atomically claim a source-trade for copying. Returns True if this process
    now owns the copy, False if another invocation already claimed it.
    """
    if not transaction_hash:
        logger.error("claim_trade refused a row without transaction_hash")
        return False

    row = {
        "transaction_hash": transaction_hash,
        "source_wallet": source_wallet.lower(),
        "asset": asset,
        "condition_id": condition_id,
        "side": side,
        "price": price,
        "bot_usdc_size": bot_usdc_size,
        "bot_units": bot_units,
        "title": title,
        "status": "claimed",
    }
    try:
        if insert_copied_trade(row):
            return True
        logger.info(f"Skipping replay for tx {transaction_hash[:12]}... (already copied)")
        return False
    except Exception as e:
        logger.error(f"claim_trade insert failed for {transaction_hash[:12]}...: {e}")
        return False


def mark_trade(
    transaction_hash: str,
    status: str,
    order_id: Optional[str] = None,
    realized_pnl: Optional[float] = None,
) -> None:
    """Update the ledger row after the order attempt."""
    update = {"status": status}
    if order_id:
        update["order_id"] = order_id
    if realized_pnl is not None:
        update["realized_pnl"] = realized_pnl
        update["closed_at"] = datetime.now(timezone.utc).isoformat()
    try:
        update_copied_trade(transaction_hash, update)
    except Exception as e:
        logger.warning(f"Could not update copied_trades for {transaction_hash[:12]}...: {e}")


def trader_exposure(source_wallet: str) -> float:
    """
    Sum bot_usdc_size of trades copied from this source that are still on the
    book. This remains a conservative upper bound until sell reconciliation.
    """
    try:
        return sum_trader_exposure(source_wallet)
    except Exception as e:
        logger.warning(f"trader_exposure lookup failed for {source_wallet[:10]}...: {e}")
        return 0.0


def close_pnl_estimate(source_wallet: str, asset: str, sell_units: float, sell_price: float) -> dict:
    try:
        return estimate_close_pnl(source_wallet, asset, sell_units, sell_price)
    except Exception as e:
        logger.warning(f"close_pnl_estimate failed for {asset}: {e}")
        return {
            "pnl": 0.0,
            "sell_value": sell_units * sell_price,
            "cost_basis": 0.0,
            "avg_entry_price": 0.0,
            "matched_units": 0.0,
            "has_basis": False,
        }


def settlement_pnl_estimate(source_wallet: str, asset: str, settlement_price: float = 0) -> dict:
    try:
        return estimate_settlement_pnl(source_wallet, asset, settlement_price)
    except Exception as e:
        logger.warning(f"settlement_pnl_estimate failed for {asset}: {e}")
        return {
            "pnl": 0.0,
            "sell_value": 0.0,
            "cost_basis": 0.0,
            "avg_entry_price": 0.0,
            "matched_units": 0.0,
            "has_basis": False,
        }


def realized_pnl_total() -> float:
    try:
        return total_realized_pnl()
    except Exception as e:
        logger.warning(f"realized_pnl_total lookup failed: {e}")
        return 0.0
