import sys
from pathlib import Path

from config import get_config
from logger import logger


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from telegram_notifier import TelegramNotifier
except ImportError:
    TelegramNotifier = None


config = get_config()
_notifier = None


def _get_notifier():
    global _notifier
    if not TelegramNotifier:
        return None
    if _notifier is None:
        _notifier = TelegramNotifier()
    return _notifier


def _send(message: str, silent: bool = False) -> None:
    notifier = _get_notifier()
    if not notifier:
        logger.debug("TelegramNotifier not available, skipping notification.")
        return
    notifier.send(message, silent=silent)


def send_notification(message: str):
    """Backwards-compatible generic notification helper."""
    _send(f"*Polymarket Bot*\n\n{message}")


def copy_trade_alert(
    side: str,
    price: float,
    units: float,
    usdc_value: float,
    title: str,
    source_wallet: str,
    order_id: str = None,
    success: bool = True,
    dry_run: bool = True,
) -> None:
    mode = "PAPER" if dry_run else "LIVE"
    status = "submitted" if success else "failed"
    _send(
        f"*{mode} COPY TRADE {status.upper()}*\n"
        f"Side: *{side}*\n"
        f"Value: ${usdc_value:.2f}\n"
        f"Units: {units:.2f}\n"
        f"Price: ${price:.4f}\n"
        f"Source: `{source_wallet[:12]}...`\n"
        f"Order: `{order_id or 'N/A'}`\n"
        f"Market: `{(title or 'N/A')[:120]}`"
    )


def copy_result_alert(
    side: str,
    price: float,
    units: float,
    sell_value: float,
    pnl: float,
    total_pnl: float,
    title: str,
    source_wallet: str,
    order_id: str = None,
    has_basis: bool = True,
    dry_run: bool = True,
    result_type: str = "SELL",
) -> None:
    mode = "PAPER" if dry_run else "LIVE"
    label = "PROFIT" if pnl >= 0 else "LOSS"
    basis_note = "" if has_basis else "\nNote: no matching copied BUY cost basis found; P&L shown as estimate."
    _send(
        f"*{mode} COPY TRADE {result_type} RESULT - {label}*\n"
        f"Close side: *{side}*\n"
        f"Sell value: ${sell_value:.2f}\n"
        f"Units: {units:.2f}\n"
        f"Exit price: ${price:.4f}\n"
        f"Trade P&L: ${pnl:+.2f}\n"
        f"Total realized P&L: ${total_pnl:+.2f}\n"
        f"Source: `{source_wallet[:12]}...`\n"
        f"Order: `{order_id or 'N/A'}`\n"
        f"Market: `{(title or 'N/A')[:120]}`"
        f"{basis_note}"
    )


if __name__ == "__main__":
    send_notification("Test notification from Polymarket Bot")
