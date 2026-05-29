import os
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from config import get_config


config = get_config()

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _db_path():
    raw_path = getattr(config, "SQLITE_DB_PATH", "data/polymarket_bot.sqlite3")
    if not isinstance(raw_path, (str, bytes, os.PathLike)):
        raw_path = "data/polymarket_bot.sqlite3"
    if raw_path == ":memory:":
        return raw_path
    path = Path(raw_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS historic_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy_wallet TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                activity_datetime TEXT,
                condition_id TEXT,
                type TEXT NOT NULL,
                size REAL,
                usdc_size REAL,
                transaction_hash TEXT,
                price REAL,
                asset TEXT,
                side TEXT,
                outcome_index INTEGER,
                title TEXT,
                slug TEXT,
                icon TEXT,
                event_slug TEXT,
                outcome TEXT,
                trader_name TEXT,
                pseudonym TEXT,
                bio TEXT,
                profile_image TEXT,
                profile_image_optimized TEXT,
                unique_activity_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS polymarket_positions (
                proxy_wallet TEXT NOT NULL,
                asset TEXT NOT NULL,
                condition_id TEXT,
                size REAL,
                avg_price REAL,
                initial_value REAL,
                current_value REAL,
                cash_pnl REAL,
                percent_pnl REAL,
                total_bought REAL,
                realized_pnl REAL,
                percent_realized_pnl REAL,
                cur_price REAL,
                redeemable INTEGER,
                mergeable INTEGER,
                title TEXT,
                slug TEXT,
                icon TEXT,
                event_id INTEGER,
                event_slug TEXT,
                outcome TEXT,
                outcome_index INTEGER,
                opposite_outcome TEXT,
                opposite_asset TEXT,
                end_date TEXT,
                negative_risk INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (proxy_wallet, asset)
            );

            CREATE TABLE IF NOT EXISTS copied_trades (
                transaction_hash TEXT PRIMARY KEY,
                source_wallet TEXT NOT NULL,
                asset TEXT NOT NULL,
                condition_id TEXT,
                side TEXT NOT NULL,
                price REAL,
                bot_usdc_size REAL,
                order_id TEXT,
                status TEXT NOT NULL DEFAULT 'submitted',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_copied_trades_source_wallet
                ON copied_trades (source_wallet);
            CREATE INDEX IF NOT EXISTS idx_copied_trades_asset
                ON copied_trades (asset);
            """
        )


def activity_key(activity: dict) -> str:
    return "_".join(
        [
            str(activity.get("transaction_hash") or ""),
            str(activity.get("condition_id") or "null"),
            str(activity.get("price") or "null"),
        ]
    )


def insert_historic_activities(activities: Iterable[dict]) -> list[dict]:
    inserted = []
    with connect() as conn:
        for activity in activities:
            row = dict(activity)
            row["unique_activity_key"] = activity_key(row)
            columns = list(row.keys())
            placeholders = ", ".join("?" for _ in columns)
            sql = (
                f"INSERT OR IGNORE INTO historic_trades "
                f"({', '.join(columns)}) VALUES ({placeholders})"
            )
            cursor = conn.execute(sql, [row[column] for column in columns])
            if cursor.rowcount:
                inserted.append(row)
    return inserted


def _get_position(conn: sqlite3.Connection, proxy_wallet: str, asset: str) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM polymarket_positions WHERE proxy_wallet = ? AND asset = ?",
        (proxy_wallet, asset),
    ).fetchone()
    return dict(row) if row else None


def _values_differ(old_value, new_value) -> bool:
    if old_value is None and new_value is None:
        return False
    try:
        return abs(float(old_value or 0) - float(new_value or 0)) > 1e-9
    except (TypeError, ValueError):
        return str(old_value or "") != str(new_value or "")


def upsert_positions(positions: Iterable[dict]) -> list[dict]:
    events = []
    watched_columns = ("size", "avg_price", "current_value", "cur_price")

    with connect() as conn:
        for position in positions:
            row = dict(position)
            old = _get_position(conn, row["proxy_wallet"], row["asset"])
            columns = list(row.keys())
            assignments = ", ".join(
                f"{column} = excluded.{column}"
                for column in columns
                if column not in ("proxy_wallet", "asset", "created_at")
            )
            assignments += ", updated_at = CURRENT_TIMESTAMP"
            sql = (
                f"INSERT INTO polymarket_positions ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(proxy_wallet, asset) DO UPDATE SET {assignments}"
            )
            conn.execute(sql, [row[column] for column in columns])

            if old is None:
                events.append({"event": "insert", "record": row, "old_record": None})
            elif any(_values_differ(old.get(column), row.get(column)) for column in watched_columns):
                events.append({"event": "update", "record": row, "old_record": old})

    return events


def insert_copied_trade(row: dict) -> bool:
    columns = list(row.keys())
    with connect() as conn:
        cursor = conn.execute(
            f"INSERT OR IGNORE INTO copied_trades ({', '.join(columns)}) "
            f"VALUES ({', '.join('?' for _ in columns)})",
            [row[column] for column in columns],
        )
        return cursor.rowcount == 1


def update_copied_trade(transaction_hash: str, update: dict) -> None:
    if not update:
        return
    assignments = ", ".join(f"{column} = ?" for column in update.keys())
    values = list(update.values()) + [transaction_hash]
    with connect() as conn:
        conn.execute(
            f"UPDATE copied_trades SET {assignments} WHERE transaction_hash = ?",
            values,
        )


def sum_trader_exposure(source_wallet: str) -> float:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(SUM(bot_usdc_size), 0) AS exposure
            FROM copied_trades
            WHERE source_wallet = ?
              AND status IN ('claimed', 'submitted', 'filled')
            """,
            (source_wallet.lower(),),
        ).fetchone()
    return float(row["exposure"] or 0)


init_db()
