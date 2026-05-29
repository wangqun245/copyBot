import asyncio
import time
import traceback
from datetime import datetime, timezone
from typing import Optional

from make_orders import make_order
from get_player_positions import (
    fetch_player_positions,
    get_current_exposures,
    insert_player_positions_batch,
)
from get_player_history_new import (
    fetch_activities as fetch_history_activities,
    insert_activities_batch as insert_history_batch,
)
from constraints.sizing import sizing_constraints
from constraints.risk_manager import check_risk_constraints
from copied_trades import (
    claim_trade,
    close_pnl_estimate,
    mark_trade,
    realized_pnl_total,
    settlement_pnl_estimate,
    trader_exposure,
)
from config import get_config
from db import count_historic_activities, count_positions
from logger import logger
from notifier import copy_result_alert, copy_trade_alert

try:
    from py_clob_client.order_builder.constants import BUY, SELL
except ImportError:
    BUY = "BUY"
    SELL = "SELL"


config = get_config()
STARTUP_EPOCH_SECONDS = int(time.time())
MARKET_STATUS_CACHE_SECONDS = 30
_market_status_cache = {}


def is_target_trader(wallet: str) -> bool:
    """Checks if a wallet is in our target list."""
    if not wallet:
        return False
    return wallet.lower() in config.TRADER_WALLETS


def _payload(record: dict, old_record: Optional[dict] = None) -> dict:
    data = {"record": record}
    if old_record is not None:
        data["old_record"] = old_record
    return {"data": data}


def _order_success(resp: Optional[dict]) -> bool:
    return bool(resp and resp.get("success"))


def _order_id(resp: Optional[dict]) -> Optional[str]:
    return resp.get("orderID") if resp else None


def _parse_timestamp(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_fresh_startup_buy(record: dict) -> bool:
    timestamp = _parse_timestamp(record.get("timestamp"))
    if timestamp is None:
        logger.warning(f"Skipping BUY without timestamp: {record.get('transaction_hash')}")
        return False

    now = time.time()
    if timestamp < STARTUP_EPOCH_SECONDS:
        logger.info(
            f"Skipping pre-startup BUY {record.get('transaction_hash')} "
            f"from {datetime.fromtimestamp(timestamp, timezone.utc).isoformat()}"
        )
        return False

    age = now - timestamp
    if age > config.COPY_BUY_MAX_AGE_SECONDS:
        logger.info(
            f"Skipping stale BUY {record.get('transaction_hash')} "
            f"age={age:.1f}s max={config.COPY_BUY_MAX_AGE_SECONDS}s"
        )
        return False
    return True


def _market_is_open(record: dict) -> bool:
    slug = record.get("slug")
    if not slug:
        logger.warning(f"Skipping trade without market slug: {record.get('transaction_hash')}")
        return False

    now = time.time()
    cached = _market_status_cache.get(slug)
    if cached and now - cached["checked_at"] < MARKET_STATUS_CACHE_SECONDS:
        return cached["is_open"]

    try:
        from http_client import get_json

        markets = get_json("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=5)
        market = markets[0] if markets else None
        is_open = bool(market) and _market_payload_is_open(market)
    except Exception as e:
        logger.warning(f"Market status lookup failed for {slug}: {e}; skipping copy")
        is_open = False

    _market_status_cache[slug] = {"checked_at": now, "is_open": is_open}
    if not is_open:
        logger.info(f"Skipping closed/inactive market: {slug}")
    return is_open


def _market_payload_is_open(market: dict) -> bool:
    if not market.get("active", False):
        return False
    if market.get("closed") or market.get("archived"):
        return False
    if market.get("acceptingOrders") is False or market.get("enableOrderBook") is False:
        return False

    end_date = _parse_datetime(market.get("endDate") or market.get("endDateIso"))
    if end_date:
        if end_date.tzinfo is None:
            end_date = end_date.replace(tzinfo=timezone.utc)
        if end_date <= datetime.now(timezone.utc):
            return False
    return True


def _position_size_for_asset(positions, asset: str) -> float:
    if not positions or not asset:
        return 0.0
    for position in positions:
        if str(position.get("asset")) == str(asset):
            try:
                return max(float(position.get("size") or 0), 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _my_position_size(condition_id: Optional[str], asset: str) -> float:
    positions = fetch_player_positions(user_address=config.POLY_FUNDER, condition_id=condition_id)
    return _position_size_for_asset(positions, asset)


def _notify_copy_trade(
    side: str,
    price: float,
    units: float,
    usdc_value: float,
    title: str,
    source_wallet: str,
    resp: Optional[dict],
) -> None:
    copy_trade_alert(
        side=side,
        price=price,
        units=units,
        usdc_value=usdc_value,
        title=title,
        source_wallet=source_wallet,
        order_id=_order_id(resp),
        success=_order_success(resp),
        dry_run=config.DRY_RUN,
    )


def _notify_sell_result(
    side: str,
    price: float,
    units: float,
    title: str,
    source_wallet: str,
    asset: str,
    resp: Optional[dict],
    result_type: str = "SELL",
) -> Optional[float]:
    if not _order_success(resp):
        return None
    pnl_info = close_pnl_estimate(source_wallet, asset, units, price)
    pnl = float(pnl_info["pnl"])
    total_pnl = realized_pnl_total() + pnl
    copy_result_alert(
        side=side,
        price=price,
        units=units,
        sell_value=float(pnl_info["sell_value"]),
        pnl=pnl,
        total_pnl=total_pnl,
        title=title,
        source_wallet=source_wallet,
        order_id=_order_id(resp),
        has_basis=bool(pnl_info["has_basis"]),
        dry_run=config.DRY_RUN,
        result_type=result_type,
    )
    return pnl


def _notify_settlement_result(title: str, source_wallet: str, asset: str, settlement_price: float = 0) -> float:
    pnl_info = settlement_pnl_estimate(source_wallet, asset, settlement_price)
    pnl = float(pnl_info["pnl"])
    total_pnl = realized_pnl_total() + pnl
    copy_result_alert(
        side="SETTLE",
        price=settlement_price,
        units=float(pnl_info["matched_units"]),
        sell_value=float(pnl_info["sell_value"]),
        pnl=pnl,
        total_pnl=total_pnl,
        title=title,
        source_wallet=source_wallet,
        order_id=None,
        has_basis=bool(pnl_info["has_basis"]),
        dry_run=config.DRY_RUN,
        result_type="SETTLEMENT",
    )
    return pnl


async def handle_new_trade(payload):
    try:
        record = payload.get("data", {}).get("record", {})
        proxy_wallet = (record.get("proxy_wallet") or "").lower()

        if not is_target_trader(proxy_wallet):
            logger.debug(f"Ignoring trade from non-target wallet: {proxy_wallet}")
            return None

        transaction_hash = record.get("transaction_hash")
        usdc_size = float(record.get("usdc_size", 0))
        side = record.get("side")
        token_id = record.get("asset")
        title = record.get("title")
        price = float(record.get("price", 0))
        condition_id = record.get("condition_id")

        if price <= 0:
            logger.warning(f"Skipping trade with invalid price: {transaction_hash}")
            return None
        if not _market_is_open(record):
            return None

        logger.info(f"Copying trade from target: {proxy_wallet[:10]}... | {title} | {side}")

        if side == SELL:
            logger.info("Side is SELL, calculating proportional size...")
            data_trader = fetch_player_positions(user_address=proxy_wallet, condition_id=condition_id)
            my_current_size = _my_position_size(condition_id, token_id)

            if data_trader and my_current_size > 0:
                size_trader = _position_size_for_asset(data_trader, token_id)

                if size_trader > 0:
                    percentage_position = usdc_size / size_trader
                    final_size = min(percentage_position * my_current_size, my_current_size)
                    if final_size <= 0:
                        logger.info(f"Skipping SELL {transaction_hash}; no sellable position for {token_id}")
                        return None
                    logger.info(f"Selling {percentage_position*100:.2f}% of position: {final_size:.2f} units")
                    if not claim_trade(
                        transaction_hash,
                        proxy_wallet,
                        token_id,
                        side,
                        price,
                        final_size * price,
                        condition_id,
                        bot_units=final_size,
                        title=title,
                    ):
                        return None
                    resp = make_order(price=price, size=final_size, side=side, token_id=token_id)
                    _notify_copy_trade(
                        side=side,
                        price=price,
                        units=final_size,
                        usdc_value=final_size * price,
                        title=title,
                        source_wallet=proxy_wallet,
                        resp=resp,
                    )
                    realized_pnl = _notify_sell_result(
                        side=side,
                        price=price,
                        units=final_size,
                        title=title,
                        source_wallet=proxy_wallet,
                        asset=token_id,
                        resp=resp,
                    )
                    mark_trade(
                        transaction_hash,
                        "submitted" if _order_success(resp) else "failed",
                        _order_id(resp),
                        realized_pnl=realized_pnl,
                    )
                    return resp
            logger.info(f"Skipping SELL {transaction_hash}; no matching bot position for {token_id}")
            return None

        if side != BUY:
            logger.info(f"Skipping unsupported trade side {side}: {transaction_hash}")
            return None

        if not _is_fresh_startup_buy(record):
            return None

        bot_usdc_size = sizing_constraints(usdc_size)
        if bot_usdc_size > 0:
            total_exp, market_exps = get_current_exposures(config.POLY_FUNDER)
            t_exp = trader_exposure(proxy_wallet)
            if check_risk_constraints(
                total_exp,
                bot_usdc_size,
                market_exposure=market_exps.get(token_id, 0),
                trader_exposure=t_exp,
            ):
                bot_size_units = bot_usdc_size / price
                if not claim_trade(
                    transaction_hash,
                    proxy_wallet,
                    token_id,
                    side,
                    price,
                    bot_usdc_size,
                    condition_id,
                    bot_units=bot_size_units,
                    title=title,
                ):
                    return None
                resp = make_order(price=price, size=bot_size_units, side=side, token_id=token_id)
                _notify_copy_trade(
                    side=side,
                    price=price,
                    units=bot_size_units,
                    usdc_value=bot_usdc_size,
                    title=title,
                    source_wallet=proxy_wallet,
                    resp=resp,
                )
                mark_trade(
                    transaction_hash,
                    "submitted" if _order_success(resp) else "failed",
                    _order_id(resp),
                )
                return resp
        return None
    except Exception as e:
        logger.error(f"Error in handle_new_trade: {e}")
        return None


async def handle_new_position(payload):
    try:
        record = payload.get("data", {}).get("record", {})
        proxy_wallet = (record.get("proxy_wallet") or "").lower()

        if not is_target_trader(proxy_wallet):
            return None

        asset = record.get("asset")
        initial_value = float(record.get("initial_value", 0))
        avg_price = float(record.get("avg_price", 0))
        title = record.get("title", "N/A")

        if avg_price <= 0:
            logger.warning(f"Skipping position with invalid avg_price: {asset}")
            return None
        if not _market_is_open(record):
            return None

        logger.info(f"New position from target: {proxy_wallet[:10]}... | {title}")
        logger.info("Skipping position-created BUY; BUY copies require fresh history trade timestamps")
        return None
    except Exception as e:
        logger.error(f"Error in handle_new_position: {e}")
        return None


async def handle_update_position(payload):
    try:
        new_record = payload.get("data", {}).get("record", {})
        proxy_wallet = (new_record.get("proxy_wallet") or "").lower()

        if not is_target_trader(proxy_wallet):
            return None

        old_record = payload.get("data", {}).get("old_record", {})
        asset = new_record.get("asset")
        title = new_record.get("title", "N/A")
        old_value = float(old_record.get("current_value", 0))
        new_value = float(new_record.get("current_value", 0))
        cur_price = float(new_record.get("cur_price", 0))

        if old_value > 0 and new_value <= 0 and cur_price <= 0:
            _notify_settlement_result(
                title=title,
                source_wallet=proxy_wallet,
                asset=asset,
                settlement_price=0,
            )
            return None

        if cur_price <= 0:
            logger.warning(f"Skipping position update with invalid cur_price: {asset}")
            return None
        if not _market_is_open(new_record):
            return None

        delta_value = new_value - old_value
        if abs(delta_value) < 1.0:
            return None

        logger.info(f"Update from target: {proxy_wallet[:10]}... | {title} | Delta: ${delta_value:+.2f}")

        if delta_value > 0:
            logger.info("Skipping position-increase BUY; BUY copies require fresh history trade timestamps")
            return None

        old_size_trader = float(old_record.get("size", 1))
        new_size_trader = float(new_record.get("size", 0))
        if old_size_trader <= 0:
            return None
        my_current_size = _my_position_size(new_record.get("condition_id"), asset)
        if my_current_size > 0:
            reduction_pct = (old_size_trader - new_size_trader) / old_size_trader
            my_reduction_size = min(my_current_size * reduction_pct, my_current_size)
            if my_reduction_size <= 0:
                logger.info(f"Skipping position SELL for {asset}; reduction size <= 0")
                return None
            resp = make_order(price=cur_price, size=my_reduction_size, side=SELL, token_id=asset)
            _notify_copy_trade(
                side=SELL,
                price=cur_price,
                units=my_reduction_size,
                usdc_value=my_reduction_size * cur_price,
                title=title,
                source_wallet=proxy_wallet,
                resp=resp,
            )
            _notify_sell_result(
                side=SELL,
                price=cur_price,
                units=my_reduction_size,
                title=title,
                source_wallet=proxy_wallet,
                asset=asset,
                resp=resp,
            )
            return resp
        logger.info(f"Skipping position SELL for {asset}; no matching bot position")
        return None
    except Exception as e:
        logger.error(f"Error in handle_update_position: {e}")
        return None


async def process_new_history(wallet: str) -> None:
    is_initial_history_sync = count_historic_activities(wallet) == 0
    activities = fetch_history_activities(wallet, limit=500, offset=0)
    inserted = insert_history_batch(activities)
    if is_initial_history_sync:
        logger.info(
            f"Seeded {len(inserted)} historic activities for {wallet[:10]}...; "
            "skipping copy actions for baseline history"
        )
        return
    for record in inserted:
        await handle_new_trade(_payload(record))


async def process_position_changes(wallet: str) -> None:
    is_initial_position_sync = count_positions(wallet) == 0
    positions = fetch_player_positions(user_address=wallet, limit=50, offset=0)
    events = insert_player_positions_batch(positions)
    if is_initial_position_sync:
        logger.info(
            f"Seeded {len(events)} positions for {wallet[:10]}...; "
            "skipping copy actions for baseline positions"
        )
        return
    for event in events:
        if event["event"] == "insert":
            await handle_new_position(_payload(event["record"]))
        elif event["event"] == "update":
            await handle_update_position(_payload(event["record"], event["old_record"]))


async def run_polling_loop():
    logger.info("STARTING POLYMARKET MONITORING SYSTEM (SQLite polling mode)")
    next_positions_poll = 0.0

    while True:
        now = time.monotonic()
        for wallet in config.TRADER_WALLETS:
            try:
                await process_new_history(wallet)
                if now >= next_positions_poll:
                    await process_position_changes(wallet)
            except Exception:
                logger.error(f"Error polling wallet {wallet}: {traceback.format_exc()}")

        if now >= next_positions_poll:
            next_positions_poll = time.monotonic() + config.POSITIONS_POLL_INTERVAL_SECONDS

        await asyncio.sleep(config.HISTORY_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    config.print_config_summary()
    asyncio.run(run_polling_loop())
