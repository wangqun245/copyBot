import asyncio
import time
from unittest.mock import MagicMock, patch
import sys
import os

# Add scripts to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'scripts'))

# Create a mock config object for testing
mock_config_obj = MagicMock()
mock_config_obj.LOG_LEVEL = "INFO"
mock_config_obj.SQLITE_DB_PATH = ":memory:"
mock_config_obj.STAKE_WHALE_PCT = 0.01  # 1%
mock_config_obj.STAKE_MIN = 5.0
mock_config_obj.STAKE_MAX = 50.0
mock_config_obj.BANKROLL = 1000.0
mock_config_obj.DRY_RUN = True
mock_config_obj.TRADER_WALLETS = ["0xtrader1"]
mock_config_obj.POLY_FUNDER = "0xbotwallet"
mock_config_obj.DEFAULT_SLIPPAGE = 0.01
mock_config_obj.MAX_RETRY_ATTEMPTS = 1
mock_config_obj.COPY_BUY_MAX_AGE_SECONDS = 600
mock_config_obj.HISTORY_POLL_INTERVAL_SECONDS = 1
mock_config_obj.POSITIONS_POLL_INTERVAL_SECONDS = 300

# Patch BEFORE importing main
with patch('config.get_config', return_value=mock_config_obj):
    from main import (
        handle_new_trade,
        handle_new_position,
        process_new_history,
        process_position_changes,
        _market_payload_is_open,
    )

def test_full_trade_flow():
    """
    Test the flow from a new trade event through sizing, risk checks, and mock execution.
    """
    # 1. Prepare a mock trade payload (similar to what the polling loop sends)
    payload = {
        "data": {
            "record": {
                "proxy_wallet": "0xtrader1",
                "usdc_size": 1000,  # Trader bet 1000
                "side": "BUY",
                "asset": "0xtoken123",
                "title": "Test Market",
                "price": 0.5,
                "transaction_hash": "0xabc123",
                "timestamp": int(time.time()),
                "slug": "test-market",
            }
        }
    }

    # 2. Mock external dependencies inside main.py
    with patch('main.get_current_exposures', return_value=(100, {"0xtoken123": 0})), \
         patch('main.trader_exposure', return_value=0.0), \
         patch('main._market_is_open', return_value=True), \
         patch('main.claim_trade', return_value=True), \
         patch('main.mark_trade'), \
         patch('main.make_order', return_value={"success": True, "orderID": "MOCK_ID"}) as mock_make_order:

        # 3. Trigger the handler
        result = asyncio.run(handle_new_trade(payload))

        # 4. Verify sizing: 1% of 1000 is 10.0
        # 5. Verify the order was attempted
        mock_make_order.assert_called_once()
        args, kwargs = mock_make_order.call_args
        assert kwargs['size'] == 20.0  # 10 USDC / 0.5 price = 20 units
        assert kwargs['price'] == 0.5
        assert kwargs['token_id'] == "0xtoken123"
        assert result["success"] is True

    print("\n✅ Integration Test Passed: Flow from Event -> Sizing -> Risk -> Order confirmed!")


def test_replay_is_skipped():
    """A duplicate event must not place a second order."""
    payload = {
        "data": {
            "record": {
                "proxy_wallet": "0xtrader1",
                "usdc_size": 1000,
                "side": "BUY",
                "asset": "0xtoken123",
                "title": "Test Market",
                "price": 0.5,
                "transaction_hash": "0xreplay",
                "timestamp": int(time.time()),
                "slug": "test-market",
            }
        }
    }

    with patch('main.get_current_exposures', return_value=(100, {"0xtoken123": 0})), \
         patch('main.trader_exposure', return_value=0.0), \
         patch('main._market_is_open', return_value=True), \
         patch('main.claim_trade', return_value=False), \
         patch('main.mark_trade'), \
         patch('main.make_order') as mock_make_order:

        result = asyncio.run(handle_new_trade(payload))
        mock_make_order.assert_not_called()
        assert result is None


def test_position_handler_does_not_copy_buy_without_trade_timestamp():
    """Position inserts are snapshot data; BUY copies require history trade timestamps."""
    payload = {
        "data": {
            "record": {
                "proxy_wallet": "0xtrader1",
                "asset": "0xtokenABC",
                "initial_value": 500,
                "avg_price": 0.25,
                "title": "Test Position",
                "slug": "test-market",
            }
        }
    }

    with patch('main._market_is_open', return_value=True), \
         patch('main.make_order', return_value={"success": True, "orderID": "POS_ID"}) as mock_make_order:
        result = asyncio.run(handle_new_position(payload))
        mock_make_order.assert_not_called()
        assert result is None


def test_buy_before_startup_is_skipped():
    payload = {
        "data": {
            "record": {
                "proxy_wallet": "0xtrader1",
                "usdc_size": 1000,
                "side": "BUY",
                "asset": "0xtoken123",
                "title": "Test Market",
                "price": 0.5,
                "transaction_hash": "0xoldbuy",
                "timestamp": 1,
                "slug": "test-market",
            }
        }
    }

    with patch('main._market_is_open', return_value=True), \
         patch('main.make_order') as mock_make_order:
        result = asyncio.run(handle_new_trade(payload))

    mock_make_order.assert_not_called()
    assert result is None


def test_sell_without_matching_bot_position_is_skipped():
    payload = {
        "data": {
            "record": {
                "proxy_wallet": "0xtrader1",
                "usdc_size": 100,
                "side": "SELL",
                "asset": "0xtoken123",
                "condition_id": "0xcondition",
                "title": "Test Market",
                "price": 0.5,
                "transaction_hash": "0xsell",
                "timestamp": int(time.time()),
                "slug": "test-market",
            }
        }
    }

    with patch('main._market_is_open', return_value=True), \
         patch('main.fetch_player_positions', return_value=[{"asset": "0xtoken123", "size": 50}]), \
         patch('main._my_position_size', return_value=0), \
         patch('main.make_order') as mock_make_order:
        result = asyncio.run(handle_new_trade(payload))

    mock_make_order.assert_not_called()
    assert result is None


def test_closed_market_payload_is_not_open():
    assert _market_payload_is_open({"active": True, "closed": True}) is False
    assert _market_payload_is_open({"active": False, "closed": False}) is False
    assert _market_payload_is_open({"active": True, "closed": False, "acceptingOrders": False}) is False


def test_initial_history_sync_seeds_without_copying():
    """A fresh DB baseline must not copy the latest 500 historical trades."""
    activities = [
        {
            "proxy_wallet": "0xtrader1",
            "transaction_hash": "0xold",
            "asset": "0xtoken123",
            "side": "BUY",
            "price": 0.5,
            "usdc_size": 1000,
        }
    ]

    with patch('main.count_historic_activities', return_value=0), \
         patch('main.fetch_history_activities', return_value=activities), \
         patch('main.insert_history_batch', return_value=activities), \
         patch('main.handle_new_trade') as mock_handle_new_trade:
        asyncio.run(process_new_history("0xtrader1"))

    mock_handle_new_trade.assert_not_called()


def test_initial_position_sync_seeds_without_copying():
    """A fresh DB position snapshot must not copy already-open positions."""
    events = [
        {
            "event": "insert",
            "record": {
                "proxy_wallet": "0xtrader1",
                "asset": "0xtokenABC",
                "initial_value": 500,
                "avg_price": 0.25,
            },
        }
    ]

    with patch('main.count_positions', return_value=0), \
         patch('main.fetch_player_positions', return_value=[events[0]["record"]]), \
         patch('main.insert_player_positions_batch', return_value=events), \
         patch('main.handle_new_position') as mock_handle_new_position, \
         patch('main.handle_update_position') as mock_handle_update_position:
        asyncio.run(process_position_changes("0xtrader1"))

    mock_handle_new_position.assert_not_called()
    mock_handle_update_position.assert_not_called()


if __name__ == "__main__":
    asyncio.run(test_full_trade_flow())
