import io
import os
import sys
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError


sys.path.append(os.path.join(os.path.dirname(__file__), "..", "scripts"))

from http_client import HttpClientError, get_json


class MockResponse:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.body


def test_get_json_uses_browser_headers():
    with patch("http_client.urlopen", return_value=MockResponse(b'{"ok": true}')) as mock_urlopen:
        data = get_json("https://data-api.polymarket.com/activity", params={"limit": "1"})

    request = mock_urlopen.call_args.args[0]
    assert data == {"ok": True}
    assert request.full_url == "https://data-api.polymarket.com/activity?limit=1"
    assert request.get_header("User-agent").startswith("Mozilla/5.0")
    assert request.get_header("Accept") == "application/json, text/plain, */*"
    assert request.get_header("Origin") == "https://polymarket.com"
    assert request.get_header("Referer") == "https://polymarket.com/"


def test_get_json_includes_http_error_body():
    error = HTTPError(
        url="https://data-api.polymarket.com/activity",
        code=403,
        msg="Forbidden",
        hdrs={},
        fp=io.BytesIO(b'{"error":"blocked"}'),
    )

    with patch("http_client.urlopen", MagicMock(side_effect=error)):
        try:
            get_json("https://data-api.polymarket.com/activity")
        except HttpClientError as e:
            assert "HTTP Error 403: Forbidden" in str(e)
            assert '{"error":"blocked"}' in str(e)
        else:
            raise AssertionError("Expected HttpClientError")
