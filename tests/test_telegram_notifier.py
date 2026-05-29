import io
import json
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

from telegram_notifier import TelegramNotifier


def test_send_uses_single_queue_worker():
    notifier = TelegramNotifier(bot_token="token", chat_id="chat")

    with patch("telegram_notifier.threading.Thread") as mock_thread:
        thread = MagicMock()
        mock_thread.return_value = thread

        notifier.send("one")
        notifier.send("two")

    assert notifier._queue.qsize() == 2
    mock_thread.assert_called_once()
    thread.start.assert_called_once()


def test_retry_after_reads_telegram_response_body():
    body = json.dumps({"parameters": {"retry_after": 7}}).encode("utf-8")
    error = HTTPError(
        url="https://api.telegram.org/bot/sendMessage",
        code=429,
        msg="Too Many Requests",
        hdrs={},
        fp=io.BytesIO(body),
    )
    notifier = TelegramNotifier(bot_token="token", chat_id="chat")

    assert notifier._retry_after_seconds(error) == 7
