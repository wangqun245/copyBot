#!/usr/bin/env python3
"""
Config-driven Polymarket weather paper trader using The Weather Company API.

Run:
  python polymarket_weather_paper_trader.py run --config polymarket_weather_config.json
  python polymarket_weather_paper_trader.py once --config polymarket_weather_config.json

This is a simulator. It never submits real orders.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional
from urllib.parse import urljoin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

BASE_POLY = "https://polymarket.com"
DEFAULT_CONFIG_PATH = "polymarket_weather_config.json"
LOGGER = logging.getLogger("weatherbot")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

TEMP_TITLE_RE = re.compile(
    r"^(Highest|Lowest)\s+temperature\s+in\s+(.+?)\s+on\s+([A-Za-z]+\s+\d{1,2})(?:\?)?$",
    re.I,
)
TEMP_NUMBER_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:deg(?:rees?)?|\u00b0)?\s*([FC])?", re.I)
WU_URL_RE = re.compile(r"https?://(?:www\.)?wunderground\.com/[^\s\"'<)]+", re.I)


@dataclass
class TemperatureMarket:
    event_id: str
    market_id: str
    condition_id: str
    city: str
    kind: str
    event_date: str
    event_title: str
    market_question: str
    polymarket_url: str
    yes_price: Optional[float]
    rule_min: Optional[float]
    rule_max: Optional[float]
    unit: str
    closed: bool = False
    raw_market_json: str = ""


@dataclass
class PaperTrade:
    trade_id: str
    created_at: str
    cycle_id: str
    strategy: str
    event_id: str
    market_id: str
    condition_id: str
    city: str
    kind: str
    event_date: str
    event_title: str
    market_question: str
    polymarket_url: str
    wunderground_source_url: str
    forecast_source: str
    forecast_observed_at: str
    forecast_station: str
    forecast_temp: Optional[float]
    forecast_high: Optional[float]
    forecast_low: Optional[float]
    forecast_first_valid_time_local: str
    forecast_last_valid_time_local: str
    forecast_unit: str
    rule_min: Optional[float]
    rule_max: Optional[float]
    market_unit: str
    comparable_rule_min: Optional[float]
    comparable_rule_max: Optional[float]
    comparable_unit: str
    yes_price: Optional[float]
    mispricing_price_threshold: float
    pricing_edge: float
    notional_usdc: float
    shares: float
    taker_fee_rate: float
    buy_fee_usdc: float
    total_cost_usdc: float
    exit_at: str = ""
    exit_reason: str = ""
    exit_yes_price: Optional[float] = None
    exit_fee_usdc: float = 0.0
    exit_proceeds_usdc: float = 0.0
    status: str = "OPEN"
    settlement_source: str = ""
    winning_outcome: str = ""
    payout_usdc: float = 0.0
    pnl_usdc: float = 0.0
    error: str = ""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def default_config() -> dict[str, Any]:
    return {
        "api": {
            "polymarket_gamma_base": "https://gamma-api.polymarket.com",
            "weather_company_base": "https://api.weather.com",
            "twc_api_key_env": "TWC_API_KEY",
            "twc_api_key": "",
            "twc_duration": "2day",
            "twc_units": "e",
            "twc_language": "en-US",
            "request_timeout_seconds": 30,
            "per_request_delay_seconds": 0.25,
        },
        "events": {
            "target_dates": ["today"],
            "city_filter": "",
            "include_closed": False,
            "max_offsets": 1200,
            "city_timezones": {
                "London": "Europe/London",
                "Paris": "Europe/Paris",
                "Sao Paulo": "America/Sao_Paulo",
                "Buenos Aires": "America/Argentina/Buenos_Aires",
                "Seoul": "Asia/Seoul",
                "Toronto": "America/Toronto",
                "Seattle": "America/Los_Angeles",
                "NYC": "America/New_York",
                "Dallas": "America/Chicago",
                "Atlanta": "America/New_York",
                "Miami": "America/New_York",
                "Chicago": "America/Chicago",
                "Ankara": "Europe/Istanbul",
                "Wellington": "Pacific/Auckland",
                "Lucknow": "Asia/Kolkata",
                "Munich": "Europe/Berlin",
                "Tel Aviv": "Asia/Jerusalem",
                "Tokyo": "Asia/Tokyo",
                "Hong Kong": "Asia/Hong_Kong",
                "Shanghai": "Asia/Shanghai",
                "Singapore": "Asia/Singapore",
                "Milan": "Europe/Rome",
                "Madrid": "Europe/Madrid",
                "Warsaw": "Europe/Warsaw",
                "Taipei": "Asia/Taipei",
                "Chongqing": "Asia/Shanghai",
                "Beijing": "Asia/Shanghai",
                "Wuhan": "Asia/Shanghai",
                "Chengdu": "Asia/Shanghai",
                "Shenzhen": "Asia/Shanghai",
                "Austin": "America/Chicago",
                "Denver": "America/Denver",
                "Houston": "America/Chicago",
                "Los Angeles": "America/Los_Angeles",
                "San Francisco": "America/Los_Angeles",
                "Moscow": "Europe/Moscow",
                "Istanbul": "Europe/Istanbul",
                "Mexico City": "America/Mexico_City",
                "Busan": "Asia/Seoul",
                "Amsterdam": "Europe/Amsterdam",
                "Helsinki": "Europe/Helsinki",
                "Panama City": "America/Panama",
                "Kuala Lumpur": "Asia/Kuala_Lumpur",
                "Jeddah": "Asia/Riyadh",
                "Cape Town": "Africa/Johannesburg",
                "Guangzhou": "Asia/Shanghai",
                "Qingdao": "Asia/Shanghai",
                "Karachi": "Asia/Karachi",
                "Manila": "Asia/Manila",
            },
        },
        "trading": {
            "strategy_name": "twc_every_15m_most_likely",
            "strategy_mode": "intraday_reactive",
            "buy_notional_usdc": 5.0,
            "mispricing_price_threshold": 0.5,
            "fee_rate": 0.05,
            "fee_enabled": True,
            "one_trade_per_event_per_cycle": True,
            "time_windows_enabled": True,
            "lowest_local_hour_window": "0-6",
            "highest_local_hour_window": "12-18",
            "forecast_horizon_hours": 6,
            "forecast_scope": "next_hours_plus_observed",
            "include_observed_today": True,
        },
        "scheduler": {
            "poll_interval_minutes": 15,
            "align_to_top_of_hour": False,
            "run_once": False,
            "max_cycles": 0,
            "settle_after_each_cycle": True,
            "stop_when_all_target_events_settled": False,
        },
        "outputs": {
            "trades_csv": "polymarket_weather_trades.csv",
            "snapshots_csv": "polymarket_weather_forecast_snapshots.csv",
            "settled_trades_csv": "polymarket_weather_trades_settled.csv",
            "performance_by_cycle_csv": "polymarket_weather_performance_by_cycle.csv",
            "performance_by_event_csv": "polymarket_weather_performance_by_event.csv",
            "state_json": "polymarket_weather_state.json",
            "log_file": "bot.log",
            "log_level": "INFO",
            "console_log_enabled": False,
        },
        "strategies": [
            {
                "name": "intraday_reactive",
                "enabled": True,
                "run_every_minutes": 15,
                "align_to_top_of_hour": False,
                "events": {"target_dates": ["today"]},
                "trading": {
                    "strategy_name": "intraday_reactive",
                    "strategy_mode": "intraday_reactive",
                    "mispricing_price_threshold": 0.5,
                    "time_windows_enabled": True,
                    "lowest_local_hour_window": "0-6",
                    "highest_local_hour_window": "12-18",
                    "forecast_scope": "next_hours_plus_observed",
                    "forecast_horizon_hours": 6,
                    "include_observed_today": True,
                },
            },
            {
                "name": "tomorrow_mispricing",
                "enabled": True,
                "run_every_minutes": 60,
                "align_to_top_of_hour": True,
                "events": {"target_dates": ["tomorrow"]},
                "trading": {
                    "strategy_name": "tomorrow_mispricing",
                    "strategy_mode": "tomorrow_mispricing",
                    "mispricing_price_threshold": 0.5,
                    "time_windows_enabled": True,
                    "tomorrow_mispricing_local_hour_window": "12-24",
                    "forecast_scope": "event_day_full",
                    "forecast_horizon_hours": 6,
                    "include_observed_today": False,
                },
            },
        ],
    }


def load_config(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        user_config = json.load(f)
    return deep_merge(default_config(), user_config)


def setup_logging(config: dict[str, Any]) -> None:
    level_name = str(config["outputs"].get("log_level", "INFO")).upper()
    level = getattr(logging, level_name, logging.INFO)
    log_file = str(config["outputs"].get("log_file", "bot.log"))
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

    LOGGER.setLevel(level)
    LOGGER.handlers.clear()
    LOGGER.propagate = False

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    LOGGER.addHandler(file_handler)

    if config["outputs"].get("console_log_enabled", False):
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(logging.Formatter("%(message)s"))
        LOGGER.addHandler(console_handler)


def log_info(message: str) -> None:
    LOGGER.info(message)


def redacted_config(config: dict[str, Any]) -> dict[str, Any]:
    def redact(value: Any, key: str = "") -> Any:
        key_lower = key.lower()
        if key_lower.endswith("_env"):
            return value
        if any(secret_word in key_lower for secret_word in ("key", "token", "secret", "password")):
            return "***REDACTED***" if value else ""
        if isinstance(value, dict):
            return {k: redact(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v, key) for v in value]
        return value

    return redact(config)


def resolve_date(value: str) -> date:
    lowered = value.lower().strip()
    if lowered == "today":
        return date.today()
    if lowered == "tomorrow":
        return date.today() + timedelta(days=1)
    return datetime.strptime(value, "%Y-%m-%d").date()


def infer_year(month_day_text: str, today: Optional[date] = None) -> date:
    today = today or date.today()
    parsed = datetime.strptime(f"{month_day_text} {today.year}", "%B %d %Y").date()
    if parsed < today - timedelta(days=2):
        parsed = datetime.strptime(f"{month_day_text} {today.year + 1}", "%B %d %Y").date()
    return parsed


def http_get_json(url: str, params: Optional[dict[str, Any]], timeout: int) -> Any:
    r = requests.get(url, headers=HEADERS, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def http_get_text(url: str, timeout: int) -> str:
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.text


def gamma_get(config: dict[str, Any], path: str, params: Optional[dict[str, Any]] = None) -> Any:
    timeout = int(config["api"]["request_timeout_seconds"])
    return http_get_json(f"{config['api']['polymarket_gamma_base']}{path}", params, timeout)


def twc_get(config: dict[str, Any], path: str, params: dict[str, Any]) -> Any:
    env_name = str(config["api"].get("twc_api_key_env", "TWC_API_KEY")).strip()
    api_key = os.environ.get(env_name, "").strip() if env_name else ""
    if not api_key:
        api_key = str(config["api"].get("twc_api_key", "")).strip()
    if not api_key:
        raise RuntimeError(f"Missing Weather Company API key. Set environment variable {env_name!r} or config api.twc_api_key.")
    timeout = int(config["api"]["request_timeout_seconds"])
    query = {"apiKey": api_key, **params}
    return http_get_json(f"{config['api']['weather_company_base']}{path}", query, timeout)


def parse_jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return default


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes"}


def parse_event_title(title: str) -> Optional[tuple[str, str, str]]:
    m = TEMP_TITLE_RE.match(" ".join(str(title).split()))
    if not m:
        return None
    return m.group(1).title(), m.group(2).strip(), m.group(3).strip()


def poly_url_from_event(event: dict[str, Any]) -> str:
    slug = event.get("slug") or event.get("ticker") or event.get("id", "")
    return urljoin(BASE_POLY, f"/event/{slug}") if slug else BASE_POLY


def discover_temperature_events(config: dict[str, Any], target: date) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    city_filter = str(config["events"].get("city_filter") or "").lower()
    max_offsets = int(config["events"]["max_offsets"])
    queries = [
        {"tag_slug": "weather"},
        {"tag_id": 100215},
        {"q": "Highest temperature"},
        {"q": "Lowest temperature"},
    ]

    for base_params in queries:
        for offset in range(0, max_offsets, 100):
            params = {"limit": 100, "offset": offset, **base_params}
            if not config["events"]["include_closed"]:
                params["closed"] = "false"
                params["archived"] = "false"
            try:
                batch = gamma_get(config, "/events", params)
            except requests.HTTPError:
                if "tag_id" in base_params:
                    break
                raise

            if not isinstance(batch, list) or not batch:
                break

            for event in batch:
                title = event.get("title") or event.get("question") or ""
                parsed = parse_event_title(title)
                if not parsed:
                    continue
                kind, city, md = parsed
                event_date = infer_year(md)
                if event_date != target:
                    continue
                if city_filter and city_filter not in city.lower():
                    continue
                event["_parsed_kind"] = kind
                event["_parsed_city"] = city
                event["_parsed_event_date"] = event_date.isoformat()
                found[str(event.get("id") or event.get("slug"))] = event

            if len(batch) < 100:
                break
            time.sleep(float(config["api"]["per_request_delay_seconds"]))

    return list(found.values())


def extract_wunderground_source(config: dict[str, Any], event_url: str) -> str:
    html = http_get_text(event_url, int(config["api"]["request_timeout_seconds"]))
    urls = [u.rstrip(".,") for u in WU_URL_RE.findall(html)]
    return urls[0] if urls else ""


def station_from_wu_url(url: str) -> str:
    parts = [p for p in url.split("?")[0].rstrip("/").split("/") if p]
    if not parts:
        return ""
    if "date" in parts:
        parts = parts[: parts.index("date")]
    station = parts[-1].upper()
    return station if re.fullmatch(r"[A-Z0-9]{4}", station) else ""


def infer_temperature_unit(text: str, default_unit: str = "F") -> str:
    normalized = text.lower()
    if "celsius" in normalized or "centigrade" in normalized or "°c" in normalized or "℃" in normalized:
        return "C"
    if "fahrenheit" in normalized or "°f" in normalized or "℉" in normalized:
        return "F"
    return default_unit.upper()


def parse_temperature_rule(text: str, default_unit: str = "F") -> tuple[Optional[float], Optional[float], str]:
    normalized = text.replace("\u2013", "-").replace("\u2014", "-")
    low_text = normalized.lower()
    inferred_unit = infer_temperature_unit(normalized, default_unit)
    nums = [(float(n), (u or inferred_unit).upper()) for n, u in TEMP_NUMBER_RE.findall(normalized)]
    unit = nums[0][1] if nums else inferred_unit
    values = [n for n, _ in nums]
    if not values:
        return None, None, unit

    if len(values) >= 2 and (
        re.search(r"\bbetween\b|\bfrom\b|\bto\b|\bthrough\b|\brange\b", low_text)
        or re.search(r"\d\s*-\s*\d", low_text)
    ):
        a, b = values[0], values[1]
        return min(a, b), max(a, b), unit

    v = values[0]
    if re.search(r"\b(at\s+or\s+above|or\s+higher|or\s+more|at\s+least|above|over|greater\s+than)\b", low_text):
        return v, None, unit
    if re.search(r"\b(at\s+or\s+below|or\s+lower|or\s+less|at\s+most|below|under|less\s+than)\b", low_text):
        return None, v, unit
    return v, v, unit


def outcome_price(market: dict[str, Any], outcome_name: str) -> Optional[float]:
    outcomes = parse_jsonish(market.get("outcomes"), [])
    prices = parse_jsonish(market.get("outcomePrices"), [])
    for idx, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() == outcome_name.lower() and idx < len(prices):
            try:
                return float(prices[idx])
            except (TypeError, ValueError):
                return None
    return None


def markets_for_event(config: dict[str, Any], event: dict[str, Any]) -> list[TemperatureMarket]:
    markets = event.get("markets") or []
    if not markets and event.get("slug"):
        detail = gamma_get(config, f"/events/slug/{event['slug']}")
        markets = detail.get("markets") or []
    if not markets and event.get("id"):
        detail = gamma_get(config, f"/events/{event['id']}")
        markets = detail.get("markets") or []

    title = event.get("title") or event.get("question") or ""
    parsed: list[TemperatureMarket] = []
    for market in markets:
        question = market.get("question") or market.get("title") or title
        unit_context = " ".join(
            str(market.get(field) or "")
            for field in ("question", "title", "description", "resolutionSource", "rules")
        )
        market_unit = infer_temperature_unit(unit_context or question)
        rule_min, rule_max, unit = parse_temperature_rule(question, default_unit=market_unit)
        yes_price = outcome_price(market, "Yes")
        parsed.append(
            TemperatureMarket(
                event_id=str(event.get("id") or event.get("slug") or ""),
                market_id=str(market.get("id") or market.get("slug") or ""),
                condition_id=str(market.get("conditionId") or ""),
                city=str(event.get("_parsed_city") or ""),
                kind=str(event.get("_parsed_kind") or ""),
                event_date=str(event.get("_parsed_event_date") or ""),
                event_title=title,
                market_question=question,
                polymarket_url=poly_url_from_event(event),
                yes_price=yes_price,
                rule_min=rule_min,
                rule_max=rule_max,
                unit=unit,
                closed=parse_bool(market.get("closed")),
                raw_market_json=json.dumps(market, ensure_ascii=False, sort_keys=True),
            )
        )
    return parsed


def parse_twc_local_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        if re.search(r"[+-]\d{4}$", value):
            value = value[:-5] + value[-5:-2] + ":" + value[-2:]
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def first_twc_local_time(payload: dict[str, Any]) -> Optional[datetime]:
    for raw_time in payload.get("validTimeLocal") or []:
        parsed = parse_twc_local_time(str(raw_time))
        if parsed:
            return parsed
    return None


def parse_hour_window(value: str) -> tuple[int, int]:
    start_text, end_text = str(value).split("-", 1)
    start, end = int(start_text), int(end_text)
    if not 0 <= start <= 23 or not 0 <= end <= 24:
        raise ValueError(f"Invalid hour window: {value}")
    return start, end


def hour_in_window(hour: int, window: tuple[int, int]) -> bool:
    start, end = window
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def trading_window_status(config: dict[str, Any], kind: str, local_dt: Optional[datetime]) -> tuple[bool, str, str]:
    if not config["trading"].get("time_windows_enabled", True):
        return True, "", "time_windows_disabled"
    if local_dt is None:
        return False, "", "missing_twc_local_time"

    if config["trading"].get("strategy_mode") == "tomorrow_mispricing":
        window_text = str(config["trading"].get("tomorrow_mispricing_local_hour_window", "12-24"))
    elif kind == "Lowest":
        window_text = str(config["trading"]["lowest_local_hour_window"])
    else:
        window_text = str(config["trading"]["highest_local_hour_window"])

    window = parse_hour_window(window_text)
    allowed = hour_in_window(local_dt.hour, window)
    reason = "inside_local_window" if allowed else f"outside_local_window_{window_text}"
    return allowed, window_text, reason


def city_local_now(config: dict[str, Any], city: str) -> tuple[Optional[datetime], str, str]:
    timezone_name = (config["events"].get("city_timezones") or {}).get(city, "")
    if not timezone_name:
        return None, "", "missing_city_timezone"
    try:
        return datetime.now(ZoneInfo(timezone_name)), timezone_name, "city_timezone"
    except ZoneInfoNotFoundError:
        return None, timezone_name, "invalid_city_timezone"


def event_market_unit(markets: list[TemperatureMarket]) -> str:
    counts: dict[str, int] = {}
    for market in markets:
        unit = (market.unit or "").upper()
        if unit in {"F", "C"} and (market.rule_min is not None or market.rule_max is not None):
            counts[unit] = counts.get(unit, 0) + 1
    if not counts:
        return "F"
    return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


def twc_units_for_temperature_unit(unit: str) -> str:
    return "m" if unit.upper() == "C" else "e"


def twc_hourly_forecast_by_icao(config: dict[str, Any], icao_code: str, units: Optional[str] = None) -> dict[str, Any]:
    request_units = units or config["api"]["twc_units"]
    return twc_get(
        config,
        f"/v3/wx/forecast/hourly/{config['api']['twc_duration']}",
        {
            "icaoCode": icao_code,
            "units": request_units,
            "language": config["api"]["twc_language"],
            "format": "json",
        },
    )


def twc_historical_hourly_by_icao(config: dict[str, Any], icao_code: str, units: str) -> dict[str, Any]:
    return twc_get(
        config,
        "/v3/wx/conditions/historical/hourly/1day",
        {
            "icaoCode": icao_code,
            "units": units,
            "language": config["api"]["twc_language"],
            "format": "json",
        },
    )


def summarize_twc_daily_forecast(payload: dict[str, Any], event_date: str) -> tuple[Optional[float], Optional[float], str, str]:
    temps = payload.get("temperature") or []
    times = payload.get("validTimeLocal") or []
    matched: list[tuple[datetime, float]] = []
    for idx, raw_time in enumerate(times):
        if idx >= len(temps) or temps[idx] is None:
            continue
        local_dt = parse_twc_local_time(str(raw_time))
        if local_dt and local_dt.date().isoformat() == event_date:
            matched.append((local_dt, float(temps[idx])))
    if not matched:
        return None, None, "", ""
    return (
        max(temp for _, temp in matched),
        min(temp for _, temp in matched),
        matched[0][0].isoformat(),
        matched[-1][0].isoformat(),
    )


def daily_twc_points(payload: dict[str, Any], event_date: str) -> list[tuple[datetime, float]]:
    temps = payload.get("temperature") or []
    times = payload.get("validTimeLocal") or []
    matched: list[tuple[datetime, float]] = []
    for idx, raw_time in enumerate(times):
        if idx >= len(temps) or temps[idx] is None:
            continue
        local_dt = parse_twc_local_time(str(raw_time))
        if local_dt and local_dt.date().isoformat() == event_date:
            matched.append((local_dt, float(temps[idx])))
    return sorted(matched, key=lambda item: item[0])


def filtered_twc_points(
    payload: dict[str, Any],
    event_date: str,
    horizon_hours: int,
) -> list[tuple[datetime, float]]:
    temps = payload.get("temperature") or []
    times = payload.get("validTimeLocal") or []
    current_local = first_twc_local_time(payload)
    if current_local is None:
        return []
    horizon_end = current_local + timedelta(hours=horizon_hours)
    matched: list[tuple[datetime, float]] = []
    for idx, raw_time in enumerate(times):
        if idx >= len(temps) or temps[idx] is None:
            continue
        local_dt = parse_twc_local_time(str(raw_time))
        if not local_dt:
            continue
        if local_dt.date().isoformat() != event_date:
            continue
        if current_local <= local_dt <= horizon_end:
            matched.append((local_dt, float(temps[idx])))
    return matched


def observed_twc_points(payload: dict[str, Any], event_date: str, current_local: Optional[datetime]) -> list[tuple[datetime, float]]:
    temps = payload.get("temperature") or []
    times = payload.get("validTimeLocal") or []
    matched: list[tuple[datetime, float]] = []
    for idx, raw_time in enumerate(times):
        if idx >= len(temps) or temps[idx] is None:
            continue
        local_dt = parse_twc_local_time(str(raw_time))
        if not local_dt:
            continue
        if local_dt.date().isoformat() != event_date:
            continue
        if current_local is None or local_dt <= current_local:
            matched.append((local_dt, float(temps[idx])))
    return sorted(matched, key=lambda item: item[0])


def summarize_points(points: list[tuple[datetime, float]]) -> tuple[Optional[float], Optional[float], str, str, list[str], list[Any]]:
    if not points:
        return None, None, "", "", [], []
    ordered = sorted(points, key=lambda item: item[0])
    return (
        max(temp for _, temp in ordered),
        min(temp for _, temp in ordered),
        ordered[0][0].isoformat(),
        ordered[-1][0].isoformat(),
        [dt.isoformat() for dt, _ in ordered],
        [temp for _, temp in ordered],
    )


def merge_observed_and_forecast_points(
    observed: list[tuple[datetime, float]],
    forecast: list[tuple[datetime, float]],
) -> list[tuple[datetime, float]]:
    by_time: dict[str, tuple[datetime, float]] = {}
    for item in observed:
        by_time[item[0].isoformat()] = item
    for item in forecast:
        by_time.setdefault(item[0].isoformat(), item)
    return sorted(by_time.values(), key=lambda item: item[0])


def summarize_twc_horizon_forecast(
    payload: dict[str, Any],
    event_date: str,
    horizon_hours: int,
) -> tuple[Optional[float], Optional[float], str, str, list[str], list[Any]]:
    return summarize_points(filtered_twc_points(payload, event_date, horizon_hours))


def twc_daily_series(payload: dict[str, Any], event_date: str) -> tuple[list[str], list[Any]]:
    times = payload.get("validTimeLocal") or []
    temps = payload.get("temperature") or []
    daily_times: list[str] = []
    daily_temps: list[Any] = []
    for idx, raw_time in enumerate(times):
        if idx >= len(temps):
            continue
        local_dt = parse_twc_local_time(str(raw_time))
        if local_dt and local_dt.date().isoformat() == event_date:
            daily_times.append(str(raw_time))
            daily_temps.append(temps[idx])
    return daily_times, daily_temps


def twc_forecast_unit_for_units(units: str) -> str:
    return "F" if units == "e" else "C"


def twc_forecast_unit(config: dict[str, Any]) -> str:
    return twc_forecast_unit_for_units(config["api"]["twc_units"])


def convert_temperature(value: Optional[float], from_unit: str, to_unit: str) -> Optional[float]:
    if value is None:
        return None
    source = (from_unit or to_unit).upper()
    target = (to_unit or source).upper()
    if source == target:
        return value
    if source == "C" and target == "F":
        return value * 9.0 / 5.0 + 32.0
    if source == "F" and target == "C":
        return (value - 32.0) * 5.0 / 9.0
    return value


def comparable_rule_bounds(market: TemperatureMarket, target_unit: str) -> tuple[Optional[float], Optional[float], str]:
    market_unit = (market.unit or target_unit).upper()
    comparable_unit = target_unit.upper()
    return (
        convert_temperature(market.rule_min, market_unit, comparable_unit),
        convert_temperature(market.rule_max, market_unit, comparable_unit),
        comparable_unit,
    )


def market_distance(forecast: float, market: TemperatureMarket, forecast_unit: str) -> tuple[float, float, float]:
    lo, hi, _ = comparable_rule_bounds(market, forecast_unit)
    if lo is not None and forecast < lo:
        outside = lo - forecast
    elif hi is not None and forecast > hi:
        outside = forecast - hi
    else:
        outside = 0.0

    if lo is None and hi is None:
        center = 999.0
        width = 999.0
    elif lo is None:
        center = abs(forecast - hi)
        width = 999.0
    elif hi is None:
        center = abs(forecast - lo)
        width = 999.0
    else:
        center = abs(forecast - ((lo + hi) / 2.0))
        width = abs(hi - lo)
    return outside, center, width


def native_forecast_for_market(
    forecasts_by_unit: dict[str, dict[str, Optional[float]]],
    market: TemperatureMarket,
    kind: str,
) -> tuple[Optional[float], Optional[float], Optional[float], str]:
    unit = (market.unit or "F").upper()
    forecast = forecasts_by_unit.get(unit) or {}
    high = forecast.get("high")
    low = forecast.get("low")
    temp = high if kind == "Highest" else low
    return temp, high, low, unit


def canonical_market_distance(
    forecasts_by_unit: dict[str, dict[str, Optional[float]]],
    market: TemperatureMarket,
    kind: str,
) -> tuple[float, float, float]:
    native_temp, _, _, native_unit = native_forecast_for_market(forecasts_by_unit, market, kind)
    if native_temp is None:
        return 999.0, 999.0, 999.0
    comparable_temp = convert_temperature(native_temp, native_unit, "F")
    return market_distance(comparable_temp or native_temp, market, "F")


def choose_most_likely_market(
    markets: list[TemperatureMarket],
    forecasts_by_unit: dict[str, dict[str, Optional[float]]],
    kind: str,
) -> Optional[TemperatureMarket]:
    usable = [
        m
        for m in markets
        if m.yes_price is not None
        and m.yes_price > 0
        and (m.rule_min is not None or m.rule_max is not None)
        and native_forecast_for_market(forecasts_by_unit, m, kind)[0] is not None
    ]
    return sorted(usable, key=lambda m: canonical_market_distance(forecasts_by_unit, m, kind))[0] if usable else None


def taker_fee_usdc(shares: float, price: float, fee_rate: float, fee_enabled: bool) -> float:
    if not fee_enabled:
        return 0.0
    return shares * fee_rate * price * (1.0 - price)


def build_trade(
    config: dict[str, Any],
    cycle_id: str,
    market: TemperatureMarket,
    wu_source: str,
    station: str,
    forecast_temp: Optional[float],
    forecast_high: Optional[float],
    forecast_low: Optional[float],
    first_valid_time_local: str,
    last_valid_time_local: str,
) -> PaperTrade:
    now = datetime.now().isoformat(timespec="seconds")
    forecast_unit = (market.unit or twc_forecast_unit(config)).upper()
    comparable_min, comparable_max, comparable_unit = comparable_rule_bounds(market, forecast_unit)
    notional = float(config["trading"]["buy_notional_usdc"])
    threshold = float(config["trading"].get("mispricing_price_threshold", 1.0))
    price = float(market.yes_price or 0.0)
    shares = notional / price if price > 0 else 0.0
    fee = taker_fee_usdc(
        shares,
        price,
        float(config["trading"]["fee_rate"]),
        bool(config["trading"]["fee_enabled"]),
    )
    trade_id = f"{cycle_id}:{market.market_id}:{len(str(time.time_ns()))}:{time.time_ns()}"
    return PaperTrade(
        trade_id=trade_id,
        created_at=now,
        cycle_id=cycle_id,
        strategy=str(config["trading"]["strategy_name"]),
        event_id=market.event_id,
        market_id=market.market_id,
        condition_id=market.condition_id,
        city=market.city,
        kind=market.kind,
        event_date=market.event_date,
        event_title=market.event_title,
        market_question=market.market_question,
        polymarket_url=market.polymarket_url,
        wunderground_source_url=wu_source,
        forecast_source="twc_hourly_forecast",
        forecast_observed_at=now,
        forecast_station=station,
        forecast_temp=forecast_temp,
        forecast_high=forecast_high,
        forecast_low=forecast_low,
        forecast_first_valid_time_local=first_valid_time_local,
        forecast_last_valid_time_local=last_valid_time_local,
        forecast_unit=forecast_unit,
        rule_min=market.rule_min,
        rule_max=market.rule_max,
        market_unit=market.unit,
        comparable_rule_min=comparable_min,
        comparable_rule_max=comparable_max,
        comparable_unit=comparable_unit,
        yes_price=market.yes_price,
        mispricing_price_threshold=threshold,
        pricing_edge=round(1.0 - price, 8),
        notional_usdc=round(notional, 6),
        shares=round(shares, 8),
        taker_fee_rate=float(config["trading"]["fee_rate"]),
        buy_fee_usdc=round(fee, 8),
        total_cost_usdc=round(notional + fee, 8),
    )


def append_csv(path: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = sorted({k for row in rows for k in row.keys()})
    exists = os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)


def read_csv_dicts(path: str) -> list[dict[str, str]]:
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def read_trades(path: str) -> list[PaperTrade]:
    trades: list[PaperTrade] = []
    for row in read_csv_dicts(path):
        cleaned: dict[str, Any] = {}
        for field in PaperTrade.__dataclass_fields__:
            value = row.get(field, "")
            if field in {
                "forecast_temp",
                "forecast_high",
                "forecast_low",
                "rule_min",
                "rule_max",
                "comparable_rule_min",
                "comparable_rule_max",
                "yes_price",
                "mispricing_price_threshold",
                "pricing_edge",
                "notional_usdc",
                "shares",
                "taker_fee_rate",
                "buy_fee_usdc",
                "total_cost_usdc",
                "exit_yes_price",
                "exit_fee_usdc",
                "exit_proceeds_usdc",
                "payout_usdc",
                "pnl_usdc",
            }:
                cleaned[field] = float(value) if value not in {"", "None", None} else None
            else:
                cleaned[field] = value
        cleaned.setdefault("forecast_first_valid_time_local", "")
        cleaned.setdefault("forecast_last_valid_time_local", "")
        cleaned.setdefault("comparable_rule_min", None)
        cleaned.setdefault("comparable_rule_max", None)
        cleaned.setdefault("comparable_unit", cleaned.get("forecast_unit", ""))
        cleaned.setdefault("exit_at", "")
        cleaned.setdefault("exit_reason", "")
        for field in {
            "notional_usdc",
            "shares",
            "taker_fee_rate",
            "buy_fee_usdc",
            "total_cost_usdc",
            "exit_fee_usdc",
            "exit_proceeds_usdc",
            "payout_usdc",
            "pnl_usdc",
        }:
            cleaned[field] = float(cleaned[field] or 0.0)
        trades.append(PaperTrade(**cleaned))
    return trades


def write_csv(path: str, rows: Iterable[Any]) -> None:
    materialized = [asdict(r) if hasattr(r, "__dataclass_fields__") else dict(r) for r in rows]
    fieldnames = sorted({k for row in materialized for k in row.keys()})
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(materialized)


def pct(numerator: float, denominator: float) -> float:
    return round(numerator / denominator, 8) if denominator else 0.0


def performance_row(group_name: str, group_value: str, rows: list[PaperTrade]) -> dict[str, Any]:
    settled = [t for t in rows if t.status in {"SETTLED", "SOLD"}]
    open_rows = [t for t in rows if t.status not in {"SETTLED", "SOLD"}]
    total_notional = sum(t.notional_usdc for t in rows)
    total_fees = sum(t.buy_fee_usdc for t in rows)
    total_cost = sum(t.total_cost_usdc for t in rows)
    settled_cost = sum(t.total_cost_usdc for t in settled)
    open_cost = sum(t.total_cost_usdc for t in open_rows)
    total_payout = sum(t.payout_usdc for t in settled)
    wins = [t for t in settled if t.pnl_usdc > 0]
    losses = [t for t in settled if t.pnl_usdc < 0]
    realized_pnl = total_payout - settled_cost
    return {
        group_name: group_value,
        "trade_count": len(rows),
        "settled_count": len(settled),
        "win_count": len(wins),
        "loss_count": len(losses),
        "open_count": len(open_rows),
        "total_notional_usdc": round(total_notional, 8),
        "total_fees_usdc": round(total_fees, 8),
        "total_cost_usdc": round(total_cost, 8),
        "settled_cost_usdc": round(settled_cost, 8),
        "open_cost_usdc": round(open_cost, 8),
        "total_payout_usdc": round(total_payout, 8),
        "realized_pnl_usdc": round(realized_pnl, 8),
        "realized_roi_on_settled_cost": pct(realized_pnl, settled_cost),
        "win_rate_settled": pct(len(wins), len(settled)),
    }


def write_performance_reports(config: dict[str, Any], trades: list[PaperTrade]) -> None:
    by_cycle: dict[str, list[PaperTrade]] = {}
    by_event: dict[str, list[PaperTrade]] = {}
    for trade in trades:
        by_cycle.setdefault(trade.cycle_id, []).append(trade)
        event_key = f"{trade.strategy}|{trade.event_date}|{trade.city}|{trade.kind}"
        by_event.setdefault(event_key, []).append(trade)

    cycle_rows = [performance_row("cycle_id", key, rows) for key, rows in sorted(by_cycle.items())]
    event_rows = []
    for key, rows in sorted(by_event.items()):
        row = performance_row("event_key", key, rows)
        first = rows[0]
        row.update(
            {
                "event_date": first.event_date,
                "strategy": first.strategy,
                "city": first.city,
                "kind": first.kind,
                "event_title": first.event_title,
                "polymarket_url": first.polymarket_url,
            }
        )
        event_rows.append(row)

    write_csv(config["outputs"]["performance_by_cycle_csv"], cycle_rows)
    write_csv(config["outputs"]["performance_by_event_csv"], event_rows)


def fetch_market_by_id(config: dict[str, Any], market_id: str) -> Optional[dict[str, Any]]:
    if not market_id:
        return None
    try:
        return gamma_get(config, f"/markets/{market_id}")
    except requests.HTTPError:
        return None


def fetch_event_by_trade(config: dict[str, Any], trade: PaperTrade) -> Optional[dict[str, Any]]:
    slug = trade.polymarket_url.rstrip("/").split("/")[-1]
    if not slug:
        return None
    try:
        event = gamma_get(config, f"/events/slug/{slug}")
    except requests.HTTPError:
        return None
    event["_parsed_kind"] = trade.kind
    event["_parsed_city"] = trade.city
    event["_parsed_event_date"] = trade.event_date
    return event


def evaluate_trade_forecast_market(config: dict[str, Any], trade: PaperTrade) -> Optional[TemperatureMarket]:
    event = fetch_event_by_trade(config, trade)
    if not event:
        return None
    markets = markets_for_event(config, event)
    unit = (trade.market_unit or event_market_unit(markets)).upper()
    twc_units = twc_units_for_temperature_unit(unit)
    station = trade.forecast_station or station_from_wu_url(trade.wunderground_source_url)
    if not station:
        return None

    payload = twc_hourly_forecast_by_icao(config, station, units=twc_units)
    if config["trading"].get("forecast_scope") == "event_day_full":
        forecast_points = daily_twc_points(payload, trade.event_date)
    else:
        forecast_points = filtered_twc_points(payload, trade.event_date, int(config["trading"]["forecast_horizon_hours"]))

    observed_points: list[tuple[datetime, float]] = []
    if config["trading"].get("include_observed_today", True) and datetime.strptime(trade.event_date, "%Y-%m-%d").date() <= date.today():
        city_local_dt, _, _ = city_local_now(config, trade.city)
        historical_payload = twc_historical_hourly_by_icao(config, station, units=twc_units)
        observed_points = observed_twc_points(historical_payload, trade.event_date, city_local_dt)

    combined_points = merge_observed_and_forecast_points(observed_points, forecast_points)
    high, low, _, _, _, _ = summarize_points(combined_points)
    forecasts_by_unit = {unit: {"high": high, "low": low}}
    return choose_most_likely_market(markets, forecasts_by_unit, trade.kind)


def resolved_outcome_from_market(market: dict[str, Any]) -> str:
    if not market or not parse_bool(market.get("closed")):
        return ""
    yes = outcome_price(market, "Yes")
    no = outcome_price(market, "No")
    if yes is not None and yes >= 0.999:
        return "Yes"
    if no is not None and no >= 0.999:
        return "No"
    return ""


def settle_open_trades(config: dict[str, Any]) -> list[PaperTrade]:
    trades_path = config["outputs"]["trades_csv"]
    trades = read_trades(trades_path)
    if not trades:
        LOGGER.info("settle skipped: no trades found at %s", trades_path)
        return []

    market_cache: dict[str, Optional[dict[str, Any]]] = {}
    settled_now = 0
    for trade in trades:
        if trade.status in {"SETTLED", "SOLD"}:
            continue
        market_cache.setdefault(trade.market_id, fetch_market_by_id(config, trade.market_id))
        outcome = resolved_outcome_from_market(market_cache[trade.market_id] or {})
        if not outcome:
            trade.status = "OPEN"
            continue
        trade.status = "SETTLED"
        trade.settlement_source = "polymarket_closed_market"
        trade.winning_outcome = outcome
        trade.payout_usdc = round(trade.shares if outcome == "Yes" else 0.0, 8)
        trade.pnl_usdc = round(trade.payout_usdc - trade.total_cost_usdc, 8)
        settled_now += 1

    write_csv(config["outputs"]["settled_trades_csv"], trades)
    write_performance_reports(config, trades)
    LOGGER.info("settle complete: trades=%s newly_settled=%s", len(trades), settled_now)
    return trades


def process_strategy_exits(config: dict[str, Any]) -> list[PaperTrade]:
    exit_config = next(
        (
            strategy_config
            for strategy_config in active_strategy_configs(config)
            if strategy_config["trading"].get("strategy_name") == "tomorrow_mispricing"
        ),
        config,
    )
    trades_path = config["outputs"]["trades_csv"]
    trades = read_trades(trades_path)
    if not trades:
        LOGGER.info("exit check skipped: no trades found at %s", trades_path)
        return []

    exits_now = 0
    market_cache: dict[str, Optional[dict[str, Any]]] = {}
    for trade in trades:
        if trade.status != "OPEN" or trade.strategy != "tomorrow_mispricing":
            continue
        try:
            event_dt = datetime.strptime(trade.event_date, "%Y-%m-%d").date()
        except ValueError:
            continue
        if event_dt > date.today():
            continue

        market_cache.setdefault(trade.market_id, fetch_market_by_id(config, trade.market_id))
        market = market_cache[trade.market_id] or {}
        current_price = outcome_price(market, "Yes")
        threshold = float(trade.mispricing_price_threshold or config["trading"].get("mispricing_price_threshold", 0.5))

        exit_reason = ""
        current_choice_id = ""
        if current_price is not None and current_price >= threshold:
            exit_reason = "price_reached_threshold"
        else:
            current_choice = evaluate_trade_forecast_market(exit_config, trade)
            current_choice_id = current_choice.market_id if current_choice else ""
            if current_choice and current_choice.market_id != trade.market_id:
                exit_reason = f"forecast_invalidated_now_{current_choice.market_id}"

        if not exit_reason or current_price is None:
            continue

        exit_fee = taker_fee_usdc(
            trade.shares,
            float(current_price),
            float(trade.taker_fee_rate),
            bool(exit_config["trading"]["fee_enabled"]),
        )
        proceeds = trade.shares * float(current_price) - exit_fee
        trade.status = "SOLD"
        trade.exit_at = datetime.now().isoformat(timespec="seconds")
        trade.exit_reason = exit_reason
        trade.exit_yes_price = float(current_price)
        trade.exit_fee_usdc = round(exit_fee, 8)
        trade.exit_proceeds_usdc = round(proceeds, 8)
        trade.payout_usdc = round(proceeds, 8)
        trade.pnl_usdc = round(proceeds - trade.total_cost_usdc, 8)
        exits_now += 1
        LOGGER.info(
            "exit strategy=tomorrow_mispricing trade=%s city=%s kind=%s bought_market=%s current_choice=%s exit_price=%s threshold=%s reason=%s shares=%s exit_fee=%s proceeds=%s total_cost=%s pnl=%s",
            trade.trade_id,
            trade.city,
            trade.kind,
            trade.market_id,
            current_choice_id,
            current_price,
            threshold,
            exit_reason,
            trade.shares,
            trade.exit_fee_usdc,
            trade.exit_proceeds_usdc,
            trade.total_cost_usdc,
            trade.pnl_usdc,
        )

    write_csv(trades_path, trades)
    write_csv(config["outputs"]["settled_trades_csv"], trades)
    write_performance_reports(config, trades)
    LOGGER.info("exit check complete: trades=%s exits=%s", len(trades), exits_now)
    return trades


def all_events_settled(events: list[dict[str, Any]]) -> bool:
    if not events:
        return False
    return all(parse_bool(e.get("closed")) for e in events)


def active_strategy_configs(config: dict[str, Any]) -> list[dict[str, Any]]:
    strategies = config.get("strategies") or []
    if not strategies:
        return [config]

    effective_configs: list[dict[str, Any]] = []
    for strategy in strategies:
        if not strategy.get("enabled", True):
            continue
        overrides = {k: v for k, v in strategy.items() if k not in {"name", "enabled", "run_every_minutes", "align_to_top_of_hour"}}
        effective = deep_merge(config, overrides)
        effective["strategies"] = []
        effective["_strategy_meta"] = {
            "name": strategy.get("name") or effective["trading"].get("strategy_name", "strategy"),
            "run_every_minutes": int(strategy.get("run_every_minutes", config["scheduler"]["poll_interval_minutes"])),
            "align_to_top_of_hour": bool(strategy.get("align_to_top_of_hour", False)),
        }
        effective["trading"]["strategy_name"] = effective["_strategy_meta"]["name"]
        effective_configs.append(effective)
    return effective_configs


def strategy_due(strategy_config: dict[str, Any], now: datetime) -> bool:
    meta = strategy_config.get("_strategy_meta", {})
    interval = int(meta.get("run_every_minutes", 15))
    if meta.get("align_to_top_of_hour", False) and now.minute != 0:
        return False
    return (now.minute % interval) == 0


def run_strategy_cycle(config: dict[str, Any], cycle_id: str) -> int:
    target_dates = [resolve_date(str(v)) for v in config["events"]["target_dates"]]
    all_new_trades: list[PaperTrade] = []
    snapshot_rows: list[dict[str, Any]] = []
    strategy_name = str(config["trading"]["strategy_name"])

    for target in target_dates:
        events = discover_temperature_events(config, target)
        log_info(f"[{cycle_id}] strategy={strategy_name} target={target.isoformat()} events={len(events)}")

        for event in events:
            markets = markets_for_event(config, event)
            event_url = poly_url_from_event(event)
            try:
                event_unit = event_market_unit(markets)
                twc_units = twc_units_for_temperature_unit(event_unit)
                city_local_dt, city_timezone, city_time_source = city_local_now(config, event["_parsed_city"])
                window_allowed, window_text, window_reason = trading_window_status(
                    config,
                    event["_parsed_kind"],
                    city_local_dt,
                )
                observed_at = datetime.now().isoformat(timespec="seconds")
                if not window_allowed:
                    snapshot_rows.append(
                        {
                            "cycle_id": cycle_id,
                            "strategy": strategy_name,
                            "observed_at": observed_at,
                            "target_date": target.isoformat(),
                            "city": event["_parsed_city"],
                            "kind": event["_parsed_kind"],
                            "station": "",
                            "event_market_unit": event_unit,
                            "twc_units_requested": "",
                            "forecast_temp": "",
                            "forecast_high": "",
                            "forecast_low": "",
                            "forecast_unit": event_unit,
                            "forecast_high_f": "",
                            "forecast_low_f": "",
                            "forecast_high_c": "",
                            "forecast_low_c": "",
                            "forecast_horizon_hours": "",
                            "include_observed_today": config["trading"].get("include_observed_today", True),
                            "observed_point_count_f": "",
                            "forecast_point_count_f": "",
                            "combined_point_count_f": "",
                            "observed_point_count_c": "",
                            "forecast_point_count_c": "",
                            "combined_point_count_c": "",
                            "city_local_time": city_local_dt.isoformat() if city_local_dt else "",
                            "city_timezone": city_timezone,
                            "city_time_source": city_time_source,
                            "trade_window": window_text,
                            "trade_window_allowed": False,
                            "trade_window_reason": window_reason,
                            "first_valid_time_local": "",
                            "last_valid_time_local": "",
                            "chosen_market_id": "",
                            "chosen_condition_id": "",
                            "chosen_question": "",
                            "chosen_yes_price": "",
                            "mispricing_price_threshold": "",
                            "pricing_edge": "",
                            "should_buy": False,
                            "chosen_rule_min": "",
                            "chosen_rule_max": "",
                            "chosen_market_unit": "",
                            "chosen_comparable_rule_min": "",
                            "chosen_comparable_rule_max": "",
                            "chosen_comparable_unit": "",
                            "trade_notional_usdc": "",
                            "polymarket_url": event_url,
                            "wunderground_source_url": "",
                            "twc_valid_time_local_f_json": "",
                            "twc_temperature_f_json": "",
                            "twc_raw_payload_f_json": "",
                            "twc_observed_time_local_f_json": "",
                            "twc_observed_temperature_f_json": "",
                            "twc_raw_historical_payload_f_json": "",
                            "twc_valid_time_local_c_json": "",
                            "twc_temperature_c_json": "",
                            "twc_raw_payload_c_json": "",
                            "twc_observed_time_local_c_json": "",
                            "twc_observed_temperature_c_json": "",
                            "twc_raw_historical_payload_c_json": "",
                            "error": "",
                        }
                    )
                    LOGGER.info(
                        "[%s] skip city=%s kind=%s reason=%s local_time=%s timezone=%s",
                        cycle_id,
                        event["_parsed_city"],
                        event["_parsed_kind"],
                        window_reason,
                        city_local_dt.isoformat() if city_local_dt else "",
                        city_timezone,
                    )
                    continue

                wu_source = extract_wunderground_source(config, event_url)
                station = station_from_wu_url(wu_source)
                if not station:
                    raise RuntimeError("No ICAO station code found in Wunderground source URL.")

                horizon_hours = int(config["trading"]["forecast_horizon_hours"])
                payload = twc_hourly_forecast_by_icao(config, station, units=twc_units)
                if config["trading"].get("forecast_scope") == "event_day_full":
                    forecast_points = daily_twc_points(payload, event["_parsed_event_date"])
                else:
                    forecast_points = filtered_twc_points(payload, event["_parsed_event_date"], horizon_hours)
                observed_points: list[tuple[datetime, float]] = []
                historical_payload: dict[str, Any] = {}
                if config["trading"].get("include_observed_today", True) and target == date.today():
                    historical_payload = twc_historical_hourly_by_icao(config, station, units=twc_units)
                    observed_points = observed_twc_points(historical_payload, event["_parsed_event_date"], city_local_dt)
                combined_points = merge_observed_and_forecast_points(observed_points, forecast_points)
                high, low, first_local, last_local, daily_times, daily_temps = summarize_points(combined_points)
                high_f = high if event_unit == "F" else ""
                low_f = low if event_unit == "F" else ""
                high_c = high if event_unit == "C" else ""
                low_c = low if event_unit == "C" else ""
                daily_times_f = daily_times if event_unit == "F" else []
                daily_temps_f = daily_temps if event_unit == "F" else []
                daily_times_c = daily_times if event_unit == "C" else []
                daily_temps_c = daily_temps if event_unit == "C" else []
                observed_points_f = observed_points if event_unit == "F" else []
                observed_points_c = observed_points if event_unit == "C" else []
                forecast_points_f = forecast_points if event_unit == "F" else []
                forecast_points_c = forecast_points if event_unit == "C" else []
                combined_points_f = combined_points if event_unit == "F" else []
                combined_points_c = combined_points if event_unit == "C" else []
                historical_payload_f = historical_payload if event_unit == "F" else {}
                historical_payload_c = historical_payload if event_unit == "C" else {}
                payload_f = payload if event_unit == "F" else {}
                payload_c = payload if event_unit == "C" else {}
                forecasts_by_unit = {
                    event_unit: {"high": high, "low": low},
                }
                forecast_temp = high if event["_parsed_kind"] == "Highest" else low
                forecast_unit = event_unit
                chosen = choose_most_likely_market(markets, forecasts_by_unit, event["_parsed_kind"])
                price_threshold = float(config["trading"].get("mispricing_price_threshold", 1.0))
                should_buy = bool(chosen and chosen.yes_price is not None and chosen.yes_price < price_threshold)
                if chosen:
                    forecast_temp, forecast_high, forecast_low, forecast_unit = native_forecast_for_market(
                        forecasts_by_unit,
                        chosen,
                        event["_parsed_kind"],
                    )
                else:
                    forecast_high, forecast_low = high_f, low_f
                comparable_min, comparable_max, comparable_unit = (
                    comparable_rule_bounds(chosen, forecast_unit) if chosen else (None, None, forecast_unit)
                )
                observed_at = datetime.now().isoformat(timespec="seconds")

                snapshot_rows.append(
                    {
                        "cycle_id": cycle_id,
                        "strategy": strategy_name,
                        "observed_at": observed_at,
                        "target_date": target.isoformat(),
                        "city": event["_parsed_city"],
                        "kind": event["_parsed_kind"],
                        "station": station,
                        "event_market_unit": event_unit,
                        "twc_units_requested": twc_units,
                        "forecast_temp": forecast_temp,
                        "forecast_high": forecast_high,
                        "forecast_low": forecast_low,
                        "forecast_unit": forecast_unit,
                        "forecast_high_f": high_f,
                        "forecast_low_f": low_f,
                        "forecast_high_c": high_c,
                        "forecast_low_c": low_c,
                        "forecast_horizon_hours": horizon_hours,
                        "include_observed_today": config["trading"].get("include_observed_today", True),
                        "observed_point_count_f": len(observed_points_f),
                        "forecast_point_count_f": len(forecast_points_f),
                        "combined_point_count_f": len(combined_points_f),
                        "observed_point_count_c": len(observed_points_c),
                        "forecast_point_count_c": len(forecast_points_c),
                        "combined_point_count_c": len(combined_points_c),
                        "city_local_time": city_local_dt.isoformat() if city_local_dt else "",
                        "city_timezone": city_timezone,
                        "city_time_source": city_time_source,
                        "trade_window": window_text,
                        "trade_window_allowed": window_allowed,
                        "trade_window_reason": window_reason,
                        "first_valid_time_local": first_local,
                        "last_valid_time_local": last_local,
                        "chosen_market_id": chosen.market_id if chosen else "",
                        "chosen_condition_id": chosen.condition_id if chosen else "",
                        "chosen_question": chosen.market_question if chosen else "",
                        "chosen_yes_price": chosen.yes_price if chosen else "",
                        "mispricing_price_threshold": price_threshold,
                        "pricing_edge": round(1.0 - float(chosen.yes_price), 8) if chosen and chosen.yes_price is not None else "",
                        "should_buy": should_buy,
                        "chosen_rule_min": chosen.rule_min if chosen else "",
                        "chosen_rule_max": chosen.rule_max if chosen else "",
                        "chosen_market_unit": chosen.unit if chosen else "",
                        "chosen_comparable_rule_min": comparable_min if chosen else "",
                        "chosen_comparable_rule_max": comparable_max if chosen else "",
                        "chosen_comparable_unit": comparable_unit if chosen else "",
                        "trade_notional_usdc": config["trading"]["buy_notional_usdc"] if chosen else "",
                        "polymarket_url": event_url,
                        "wunderground_source_url": wu_source,
                        "twc_valid_time_local_f_json": json.dumps(daily_times_f, ensure_ascii=False),
                        "twc_temperature_f_json": json.dumps(daily_temps_f, ensure_ascii=False),
                        "twc_raw_payload_f_json": json.dumps(payload_f, ensure_ascii=False, sort_keys=True),
                        "twc_observed_time_local_f_json": json.dumps([dt.isoformat() for dt, _ in observed_points_f], ensure_ascii=False),
                        "twc_observed_temperature_f_json": json.dumps([temp for _, temp in observed_points_f], ensure_ascii=False),
                        "twc_raw_historical_payload_f_json": json.dumps(historical_payload_f, ensure_ascii=False, sort_keys=True),
                        "twc_valid_time_local_c_json": json.dumps(daily_times_c, ensure_ascii=False),
                        "twc_temperature_c_json": json.dumps(daily_temps_c, ensure_ascii=False),
                        "twc_raw_payload_c_json": json.dumps(payload_c, ensure_ascii=False, sort_keys=True),
                        "twc_observed_time_local_c_json": json.dumps([dt.isoformat() for dt, _ in observed_points_c], ensure_ascii=False),
                        "twc_observed_temperature_c_json": json.dumps([temp for _, temp in observed_points_c], ensure_ascii=False),
                        "twc_raw_historical_payload_c_json": json.dumps(historical_payload_c, ensure_ascii=False, sort_keys=True),
                        "error": "",
                    }
                )

                if should_buy and chosen:
                    new_trade = build_trade(
                        config,
                        cycle_id,
                        chosen,
                        wu_source,
                        station,
                        forecast_temp,
                        forecast_high,
                        forecast_low,
                        first_local,
                        last_local,
                    )
                    all_new_trades.append(new_trade)
                    LOGGER.info(
                        "[%s] buy strategy=%s trade=%s city=%s kind=%s station=%s forecast=%s%s market=%s price=%s threshold=%s edge=%s notional=%s shares=%s buy_fee=%s total_cost=%s market_unit=%s comparable_rule=%s-%s%s",
                        cycle_id,
                        strategy_name,
                        new_trade.trade_id,
                        event["_parsed_city"],
                        event["_parsed_kind"],
                        station,
                        forecast_temp,
                        forecast_unit,
                        chosen.market_id,
                        chosen.yes_price,
                        price_threshold,
                        new_trade.pricing_edge,
                        new_trade.notional_usdc,
                        new_trade.shares,
                        new_trade.buy_fee_usdc,
                        new_trade.total_cost_usdc,
                        chosen.unit,
                        comparable_min,
                        comparable_max,
                        comparable_unit,
                    )
                else:
                    log_method = LOGGER.info if not window_allowed else LOGGER.warning
                    log_method(
                        "[%s] skip city=%s kind=%s forecast=%s reason=%s local_time=%s price=%s threshold=%s",
                        cycle_id,
                        event["_parsed_city"],
                        event["_parsed_kind"],
                        forecast_temp,
                        window_reason if not window_allowed else ("price_above_threshold" if chosen else "no_usable_market"),
                        city_local_dt.isoformat() if city_local_dt else "",
                        chosen.yes_price if chosen else "",
                        price_threshold,
                    )
            except Exception as exc:
                LOGGER.exception(
                    "[%s] event failed city=%s kind=%s url=%s",
                    cycle_id,
                    event.get("_parsed_city", ""),
                    event.get("_parsed_kind", ""),
                    event_url,
                )
                snapshot_rows.append(
                    {
                        "cycle_id": cycle_id,
                        "strategy": strategy_name,
                        "observed_at": datetime.now().isoformat(timespec="seconds"),
                        "target_date": target.isoformat(),
                        "city": event.get("_parsed_city", ""),
                        "kind": event.get("_parsed_kind", ""),
                        "station": "",
                        "event_market_unit": "",
                        "twc_units_requested": "",
                        "forecast_temp": "",
                        "forecast_high": "",
                        "forecast_low": "",
                        "forecast_unit": "",
                        "forecast_high_f": "",
                        "forecast_low_f": "",
                        "forecast_high_c": "",
                        "forecast_low_c": "",
                        "forecast_horizon_hours": "",
                        "include_observed_today": "",
                        "observed_point_count_f": "",
                        "forecast_point_count_f": "",
                        "combined_point_count_f": "",
                        "observed_point_count_c": "",
                        "forecast_point_count_c": "",
                        "combined_point_count_c": "",
                        "city_local_time": "",
                        "city_timezone": "",
                        "city_time_source": "",
                        "trade_window": "",
                        "trade_window_allowed": "",
                        "trade_window_reason": "event_error",
                        "first_valid_time_local": "",
                        "last_valid_time_local": "",
                        "chosen_market_id": "",
                        "chosen_condition_id": "",
                        "chosen_question": "",
                        "chosen_yes_price": "",
                        "mispricing_price_threshold": "",
                        "pricing_edge": "",
                        "should_buy": "",
                        "chosen_rule_min": "",
                        "chosen_rule_max": "",
                        "chosen_market_unit": "",
                        "chosen_comparable_rule_min": "",
                        "chosen_comparable_rule_max": "",
                        "chosen_comparable_unit": "",
                        "trade_notional_usdc": "",
                        "polymarket_url": event_url,
                        "wunderground_source_url": "",
                        "twc_valid_time_local_f_json": "",
                        "twc_temperature_f_json": "",
                        "twc_raw_payload_f_json": "",
                        "twc_observed_time_local_f_json": "",
                        "twc_observed_temperature_f_json": "",
                        "twc_raw_historical_payload_f_json": "",
                        "twc_valid_time_local_c_json": "",
                        "twc_temperature_c_json": "",
                        "twc_raw_payload_c_json": "",
                        "twc_observed_time_local_c_json": "",
                        "twc_observed_temperature_c_json": "",
                        "twc_raw_historical_payload_c_json": "",
                        "error": repr(exc),
                    }
                )
            time.sleep(float(config["api"]["per_request_delay_seconds"]))

    append_csv(config["outputs"]["snapshots_csv"], snapshot_rows)
    append_csv(config["outputs"]["trades_csv"], [asdict(t) for t in all_new_trades])
    log_info(f"[{cycle_id}] strategy={strategy_name} snapshots={len(snapshot_rows)} new_trades={len(all_new_trades)}")
    return len(all_new_trades)


def run_cycle(config: dict[str, Any], cycle_num: int) -> int:
    now = datetime.now()
    base_cycle_id = now.strftime("%Y%m%dT%H%M%S") + f"-{cycle_num}"
    total_new_trades = 0
    for strategy_config in active_strategy_configs(config):
        strategy_name = strategy_config["trading"]["strategy_name"]
        if not strategy_due(strategy_config, now):
            LOGGER.info("[%s] strategy=%s not due", base_cycle_id, strategy_name)
            continue
        total_new_trades += run_strategy_cycle(strategy_config, f"{base_cycle_id}:{strategy_name}")
    return total_new_trades


def write_state(config: dict[str, Any], cycle_num: int) -> None:
    state = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "cycle_num": cycle_num,
        "config_target_dates": config["events"]["target_dates"],
        "trades_csv": config["outputs"]["trades_csv"],
        "snapshots_csv": config["outputs"]["snapshots_csv"],
        "settled_trades_csv": config["outputs"]["settled_trades_csv"],
        "performance_by_cycle_csv": config["outputs"]["performance_by_cycle_csv"],
        "performance_by_event_csv": config["outputs"]["performance_by_event_csv"],
        "log_file": config["outputs"]["log_file"],
    }
    with open(config["outputs"]["state_json"], "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def summarize_settled(config: dict[str, Any]) -> None:
    trades = read_trades(config["outputs"]["settled_trades_csv"])
    if not trades:
        return
    settled = [t for t in trades if t.status in {"SETTLED", "SOLD"}]
    open_trades = [t for t in trades if t.status not in {"SETTLED", "SOLD"}]
    settled_cost = sum(t.total_cost_usdc for t in settled)
    open_cost = sum(t.total_cost_usdc for t in open_trades)
    total_cost = settled_cost + open_cost
    total_payout = sum(t.payout_usdc for t in settled)
    total_fee = sum(t.buy_fee_usdc for t in trades)
    realized_pnl = total_payout - settled_cost
    log_info(
        f"trades={len(trades)} settled={len(settled)} open={len(open_trades)} "
        f"total_cost=${total_cost:.2f} open_cost=${open_cost:.2f} fees=${total_fee:.2f} "
        f"settled_payout=${total_payout:.2f} realized_pnl=${realized_pnl:.2f}"
    )


def run(config: dict[str, Any]) -> None:
    cycle_num = 0
    max_cycles = int(config["scheduler"]["max_cycles"])
    LOGGER.info("bot started config=%s", json.dumps(redacted_config(config), ensure_ascii=False, sort_keys=True))
    while True:
        cycle_num += 1
        run_cycle(config, cycle_num)
        process_strategy_exits(config)
        if config["scheduler"]["settle_after_each_cycle"]:
            settle_open_trades(config)
            summarize_settled(config)
        write_state(config, cycle_num)

        if config["scheduler"]["stop_when_all_target_events_settled"]:
            settled_trades = read_trades(config["outputs"]["settled_trades_csv"])
            if settled_trades and all(t.status == "SETTLED" for t in settled_trades):
                log_info("all known paper trades are settled; stopping")
                break

        if config["scheduler"]["run_once"] or (max_cycles and cycle_num >= max_cycles):
            break
        if config["scheduler"].get("align_to_top_of_hour", False):
            now = datetime.now()
            next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
            sleep_seconds = max(1, int((next_hour - now).total_seconds()))
        else:
            sleep_seconds = int(config["scheduler"]["poll_interval_minutes"]) * 60
        log_info(f"sleeping {sleep_seconds} seconds")
        time.sleep(sleep_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="Run continuously using the config file.")
    run_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    once_parser = sub.add_parser("once", help="Run one polling cycle using the config file.")
    once_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    settle_parser = sub.add_parser("settle", help="Settle existing paper trades using Polymarket closed market data.")
    settle_parser.add_argument("--config", default=DEFAULT_CONFIG_PATH)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    setup_logging(config)
    if args.command == "once":
        config["scheduler"]["run_once"] = True
        run(config)
    elif args.command == "settle":
        settle_open_trades(config)
        summarize_settled(config)
    else:
        run(config)


if __name__ == "__main__":
    main()
