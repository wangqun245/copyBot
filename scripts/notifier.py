from config import get_config
from http_client import post_json
from logger import logger

config = get_config()

def send_notification(message: str):
    """
    Sends a message via Telegram if configured.
    """
    token = config.TELEGRAM_BOT_TOKEN
    chat_id = config.TELEGRAM_CHAT_ID
    
    if not token or not chat_id:
        logger.debug("Telegram notifications not configured, skipping.")
        return
        
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": f"🤖 *Polymarket Bot*\n\n{message}",
        "parse_mode": "Markdown"
    }
    
    try:
        post_json(url, payload, timeout=10)
        logger.debug("Telegram notification sent successfully.")
    except Exception as e:
        logger.error(f"Failed to send Telegram notification: {e}")

if __name__ == "__main__":
    # Test notification
    send_notification("Test notification from Polymarket Bot! 🚀")
