import json
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class HttpClientError(Exception):
    pass


def get_json(url: str, params: Optional[dict] = None, timeout: int = 10):
    full_url = url
    if params:
        full_url = f"{url}?{urlencode(params)}"
    try:
        with urlopen(full_url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HttpClientError(str(e)) from e


def post_json(url: str, payload: dict, timeout: int = 10):
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HttpClientError(str(e)) from e
