import json
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class HttpClientError(Exception):
    pass


DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://polymarket.com",
    "Referer": "https://polymarket.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    ),
}


def _format_http_error(e: HTTPError) -> str:
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        body = ""

    message = f"HTTP Error {e.code}: {e.reason}"
    if body:
        message = f"{message} - {body[:500]}"
    return message


def get_json(url: str, params: Optional[dict] = None, timeout: int = 10):
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    try:
        request = Request(full_url, headers=DEFAULT_HEADERS)
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as e:
        raise HttpClientError(_format_http_error(e)) from e
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HttpClientError(str(e)) from e


def post_json(url: str, payload: dict, timeout: int = 10):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={**DEFAULT_HEADERS, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except HTTPError as e:
        raise HttpClientError(_format_http_error(e)) from e
    except (URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HttpClientError(str(e)) from e
