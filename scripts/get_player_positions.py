from config import get_config
from db import upsert_positions
from http_client import HttpClientError, get_json
from logger import logger


config = get_config()

API_URL = "https://data-api.polymarket.com/positions"
TABLE_NAME = config.TABLE_NAME_POSITIONS


def fetch_player_positions(user_address: str, limit: int = 500, offset: int = 0, condition_id: str = None):
    try:
        params = {
            "user": user_address,
            "limit": str(limit),
            "offset": str(offset),
            "sortBy": "INITIAL",
            "sortDirection": "DESC",
        }
        if condition_id is not None:
            params["conditionId"] = condition_id

        data = get_json(API_URL, params=params, timeout=5)
        logger.debug(f"Fetched {len(data)} positions for {user_address}")
        return data
    except HttpClientError as e:
        logger.error(f"Request error fetching positions for {user_address}: {e}")
        return None


def get_current_exposures(user_address: str):
    """
    Calculates current exposures for a given wallet.
    Returns: (total_exposure, market_exposures_dict)
    """
    positions = fetch_player_positions(user_address)
    if not positions:
        return 0, {}

    total_exposure = 0
    market_exposures = {}

    for pos in positions:
        val = float(pos.get("currentValue", 0))
        asset = pos.get("asset")
        total_exposure += val
        market_exposures[asset] = market_exposures.get(asset, 0) + val

    return total_exposure, market_exposures


def transform_position_to_db_format(position: dict) -> dict:
    """
    Transforms Polymarket API format to SQLite row format.
    """
    end_date = position.get("endDate")
    if not end_date:
        end_date = None

    event_id = position.get("eventId")
    if event_id:
        try:
            event_id = int(event_id)
        except (ValueError, TypeError):
            event_id = None
    else:
        event_id = None

    return {
        "proxy_wallet": position.get("proxyWallet"),
        "asset": position.get("asset"),
        "condition_id": position.get("conditionId"),
        "size": position.get("size"),
        "avg_price": position.get("avgPrice"),
        "initial_value": position.get("initialValue"),
        "current_value": position.get("currentValue"),
        "cash_pnl": position.get("cashPnl"),
        "percent_pnl": position.get("percentPnl"),
        "total_bought": position.get("totalBought"),
        "realized_pnl": position.get("realizedPnl"),
        "percent_realized_pnl": position.get("percentRealizedPnl"),
        "cur_price": position.get("curPrice"),
        "redeemable": int(bool(position.get("redeemable"))),
        "mergeable": int(bool(position.get("mergeable"))),
        "title": position.get("title"),
        "slug": position.get("slug"),
        "icon": position.get("icon"),
        "event_id": event_id,
        "event_slug": position.get("eventSlug"),
        "outcome": position.get("outcome"),
        "outcome_index": position.get("outcomeIndex"),
        "opposite_outcome": position.get("oppositeOutcome"),
        "opposite_asset": position.get("oppositeAsset"),
        "end_date": end_date,
        "negative_risk": int(bool(position.get("negativeRisk"))),
    }


def insert_player_positions_batch(positions: list):
    """
    Bulk-upserts positions into SQLite.
    Returns insert/update events so the main loop can copy each change once.
    """
    if not positions:
        logger.info("No positions to insert")
        return []

    rows = []
    for position in positions:
        try:
            rows.append(transform_position_to_db_format(position))
        except Exception as e:
            logger.error(f"Error transforming position: {e}")

    if not rows:
        return []

    try:
        events = upsert_positions(rows)
        logger.info(f"Upserted {len(rows)} positions ({len(events)} changed)")
        return events
    except Exception as e:
        logger.error(f"Bulk upsert failed ({len(rows)} rows): {e}")
        return []


def print_positions_readable(positions: list):
    if not positions:
        logger.warning("No positions found.")
        return
    for idx, pos in enumerate(positions, 1):
        logger.info(
            f"Position #{idx}: {pos.get('title')} | Outcome: {pos.get('outcome')} | "
            f"Value: ${pos.get('currentValue', 0)}"
        )


if __name__ == "__main__":
    user = config.TRADER_WALLETS[0] if config.TRADER_WALLETS else ""
    positions = fetch_player_positions(user_address=user)
    if positions:
        insert_player_positions_batch(positions)
        print_positions_readable(positions)
