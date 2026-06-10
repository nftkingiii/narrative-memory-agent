"""
memory.py — Narrative Memory Agent
SQLite-backed narrative pattern memory.
Stores historical narrative cycles, queries for matches,
and writes back outcomes after trades close.
"""

import os
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
DB_PATH = DATA_DIR / "memory.db"


# ─────────────────────────────────────────────
# Schema
# ─────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS narrative_memory (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    narrative_tag               TEXT NOT NULL,
    first_detected              TEXT NOT NULL,
    peak_date                   TEXT,
    days_to_peak                INTEGER,
    optimal_entry_day           INTEGER,
    avg_return_pct              REAL,
    sentiment_at_detection      REAL,
    news_volume_at_detection    INTEGER,
    funding_rate_at_detection   REAL,
    fear_greed_at_detection     INTEGER,
    btc_dominance_at_detection  REAL,
    outcome                     TEXT CHECK(outcome IN ('played_out','fizzled','running','unknown')),
    confidence_score            REAL,
    notes                       TEXT,
    created_at                  TEXT NOT NULL,
    updated_at                  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trade_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    narrative_tag       TEXT NOT NULL,
    memory_id           INTEGER REFERENCES narrative_memory(id),
    entry_date          TEXT NOT NULL,
    exit_date           TEXT,
    symbol              TEXT NOT NULL,
    side                TEXT CHECK(side IN ('long','short')),
    entry_price         REAL,
    exit_price          REAL,
    position_size       TEXT CHECK(position_size IN ('small','medium','full')),
    pnl_pct             REAL,
    current_price       REAL,
    unrealized_pnl_pct  REAL,
    stop_loss_price     REAL,
    take_profit_price   REAL,
    initial_risk_pct    REAL,
    last_price_at       TEXT,
    trade_type          TEXT CHECK(trade_type IN ('narrative','fallback')),
    exit_reason         TEXT CHECK(exit_reason IN ('take_profit','stop_loss','memory_exit','manual')),
    memory_informed     INTEGER DEFAULT 0,
    status              TEXT CHECK(status IN ('open','closed')) DEFAULT 'open',
    notes               TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
"""

# ─────────────────────────────────────────────
# Seed Data — 5 historical narrative cycles
# ─────────────────────────────────────────────

SEED_NARRATIVES = [
    {
        "narrative_tag": "ai_coins",
        "first_detected": "2024-01-10",
        "peak_date": "2024-03-18",
        "days_to_peak": 68,
        "optimal_entry_day": 3,
        "avg_return_pct": 312.5,
        "sentiment_at_detection": 62.0,
        "news_volume_at_detection": 45,
        "funding_rate_at_detection": 0.01,
        "fear_greed_at_detection": 72,
        "btc_dominance_at_detection": 52.3,
        "outcome": "played_out",
        "confidence_score": 0.92,
        "notes": "FET, AGIX, RNDR led. Narrative emerged from ChatGPT mainstream adoption + crypto AI token buzz. Best entry was day 2-3 after CoinTelegraph first major coverage. Peak coincided with BTC ATH run.",
    },
    {
        "narrative_tag": "rwa_tokenization",
        "first_detected": "2024-02-15",
        "peak_date": "2024-04-02",
        "days_to_peak": 46,
        "optimal_entry_day": 5,
        "avg_return_pct": 187.3,
        "sentiment_at_detection": 58.0,
        "news_volume_at_detection": 28,
        "funding_rate_at_detection": 0.008,
        "fear_greed_at_detection": 65,
        "btc_dominance_at_detection": 53.1,
        "outcome": "played_out",
        "confidence_score": 0.85,
        "notes": "ONDO, POLYX, CPOOL led. BlackRock BUIDL fund announcement was the catalyst. Slower build than AI coins — needed 5 days to confirm real narrative vs news cycle. TradFi legitimacy signal was key.",
    },
    {
        "narrative_tag": "btc_etf_approval",
        "first_detected": "2023-10-16",
        "peak_date": "2024-01-11",
        "days_to_peak": 87,
        "optimal_entry_day": 2,
        "avg_return_pct": 68.4,
        "sentiment_at_detection": 55.0,
        "news_volume_at_detection": 112,
        "funding_rate_at_detection": 0.015,
        "fear_greed_at_detection": 60,
        "btc_dominance_at_detection": 51.8,
        "outcome": "played_out",
        "confidence_score": 0.95,
        "notes": "Pure BTC play. BlackRock ETF filing leak drove initial spike. Slow grind up with multiple fake-out dips. Best strategy was to enter BTC directly on day 2 of initial news burst and hold. Sold the news on approval day.",
    },
    {
        "narrative_tag": "meme_supercycle",
        "first_detected": "2024-02-26",
        "peak_date": "2024-03-31",
        "days_to_peak": 34,
        "optimal_entry_day": 2,
        "avg_return_pct": 425.0,
        "sentiment_at_detection": 75.0,
        "news_volume_at_detection": 89,
        "funding_rate_at_detection": 0.03,
        "fear_greed_at_detection": 80,
        "btc_dominance_at_detection": 50.2,
        "outcome": "played_out",
        "confidence_score": 0.78,
        "notes": "DOGE, SHIB, WIF, PEPE led. Greed index above 75 was the trigger signal. Very fast cycle — 34 days total. High funding rate at detection was a warning sign but momentum carried it. Exit before funding rate hits 0.05.",
    },
    {
        "narrative_tag": "depin",
        "first_detected": "2024-01-22",
        "peak_date": "2024-03-25",
        "days_to_peak": 63,
        "optimal_entry_day": 7,
        "avg_return_pct": 156.8,
        "sentiment_at_detection": 50.0,
        "news_volume_at_detection": 22,
        "funding_rate_at_detection": 0.005,
        "fear_greed_at_detection": 58,
        "btc_dominance_at_detection": 52.7,
        "outcome": "played_out",
        "confidence_score": 0.80,
        "notes": "HNT, IOTX, MOBILE led. Slower narrative build — needed more confirmation days than AI or meme. Low initial news volume meant waiting for day 7 before confident entry. Steady grind, less volatile than meme plays.",
    },
]


# ─────────────────────────────────────────────
# Database Setup
# ─────────────────────────────────────────────

def get_connection() -> sqlite3.Connection:
    """Return a SQLite connection with row factory enabled."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _migrate_trade_log(conn: sqlite3.Connection):
    """Add live-tracking fields to existing deployments without losing trades."""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(trade_log)").fetchall()
    }
    additions = {
        "current_price": "REAL",
        "unrealized_pnl_pct": "REAL",
        "stop_loss_price": "REAL",
        "take_profit_price": "REAL",
        "initial_risk_pct": "REAL",
        "last_price_at": "TEXT",
        "trade_type": "TEXT",
    }
    for name, column_type in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE trade_log ADD COLUMN {name} {column_type}")

    conn.execute("""
        UPDATE trade_log
        SET trade_type = CASE
            WHEN narrative_tag LIKE 'fallback_%' THEN 'fallback'
            ELSE 'narrative'
        END
        WHERE trade_type IS NULL
    """)
    conn.execute("""
        UPDATE trade_log
        SET current_price = entry_price,
            unrealized_pnl_pct = COALESCE(unrealized_pnl_pct, 0),
            stop_loss_price = COALESCE(
                stop_loss_price,
                CASE
                    WHEN side = 'short' THEN entry_price * 1.03
                    ELSE entry_price * 0.97
                END
            ),
            take_profit_price = COALESCE(
                take_profit_price,
                CASE
                    WHEN side = 'short' THEN entry_price * 0.80
                    ELSE entry_price * 1.20
                END
            ),
            initial_risk_pct = COALESCE(initial_risk_pct, 3.0),
            last_price_at = COALESCE(last_price_at, updated_at)
        WHERE status = 'open' AND current_price IS NULL
    """)
    conn.execute("""
        UPDATE trade_log
        SET stop_loss_price = COALESCE(
                stop_loss_price,
                CASE WHEN side = 'short' THEN entry_price * 1.03 ELSE entry_price * 0.97 END
            ),
            take_profit_price = COALESCE(
                take_profit_price,
                CASE WHEN side = 'short' THEN entry_price * 0.80 ELSE entry_price * 1.20 END
            ),
            initial_risk_pct = COALESCE(
                initial_risk_pct,
                ABS(entry_price - stop_loss_price) / entry_price * 100,
                3.0
            )
        WHERE status = 'open'
          AND (
              stop_loss_price IS NULL
              OR take_profit_price IS NULL
              OR initial_risk_pct IS NULL
          )
    """)
    conn.commit()


def init_db():
    """Create tables and seed historical data if DB is empty."""
    conn = get_connection()
    conn.executescript(SCHEMA)
    _migrate_trade_log(conn)
    conn.commit()

    # Only seed if empty
    count = conn.execute("SELECT COUNT(*) FROM narrative_memory").fetchone()[0]
    if count == 0:
        print("[memory] Seeding historical narrative data...")
        now = datetime.now(timezone.utc).isoformat()
        for n in SEED_NARRATIVES:
            conn.execute("""
                INSERT INTO narrative_memory (
                    narrative_tag, first_detected, peak_date, days_to_peak,
                    optimal_entry_day, avg_return_pct, sentiment_at_detection,
                    news_volume_at_detection, funding_rate_at_detection,
                    fear_greed_at_detection, btc_dominance_at_detection,
                    outcome, confidence_score, notes, created_at, updated_at
                ) VALUES (
                    :narrative_tag, :first_detected, :peak_date, :days_to_peak,
                    :optimal_entry_day, :avg_return_pct, :sentiment_at_detection,
                    :news_volume_at_detection, :funding_rate_at_detection,
                    :fear_greed_at_detection, :btc_dominance_at_detection,
                    :outcome, :confidence_score, :notes, :created_at, :updated_at
                )
            """, {**n, "created_at": now, "updated_at": now})
        conn.commit()
        print(f"[memory] Seeded {len(SEED_NARRATIVES)} historical narratives")
    else:
        print(f"[memory] DB already has {count} narrative records")

    conn.close()


# ─────────────────────────────────────────────
# Query Memory
# ─────────────────────────────────────────────

def query_narrative(tag: str) -> dict | None:
    """
    Query memory for a narrative tag.
    Returns the most recent played_out record for that tag,
    or None if never seen before.
    """
    conn = get_connection()
    row = conn.execute("""
        SELECT * FROM narrative_memory
        WHERE narrative_tag = ?
        AND outcome = 'played_out'
        ORDER BY first_detected DESC
        LIMIT 1
    """, (tag,)).fetchone()
    conn.close()

    if row:
        result = dict(row)
        print(f"[memory] Found match: {tag} — "
              f"avg_return={result['avg_return_pct']}%, "
              f"days_to_peak={result['days_to_peak']}, "
              f"optimal_entry_day={result['optimal_entry_day']}")
        return result

    print(f"[memory] No prior record for: {tag}")
    return None


def get_all_narratives() -> list[dict]:
    """Return all narrative records ordered by most recent."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM narrative_memory ORDER BY first_detected DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_running_narratives() -> list[dict]:
    """Return all narratives currently marked as running."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM narrative_memory WHERE outcome = 'running'
        ORDER BY first_detected DESC
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# Write New Narrative (when agent detects a new one)
# ─────────────────────────────────────────────

def record_new_narrative(
    tag: str,
    sentiment_score: float = None,
    news_volume: int = None,
    funding_rate: float = None,
    fear_greed: int = None,
    btc_dominance: float = None,
    notes: str = "",
) -> int:
    """
    Write a newly detected narrative to memory.
    Returns the new record's ID.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    existing = conn.execute("""
        SELECT id FROM narrative_memory
        WHERE narrative_tag = ? AND outcome = 'running'
        ORDER BY first_detected DESC LIMIT 1
    """, (tag,)).fetchone()
    if existing:
        conn.execute("""
            UPDATE narrative_memory SET
                sentiment_at_detection = COALESCE(?, sentiment_at_detection),
                news_volume_at_detection = COALESCE(?, news_volume_at_detection),
                funding_rate_at_detection = COALESCE(?, funding_rate_at_detection),
                fear_greed_at_detection = COALESCE(?, fear_greed_at_detection),
                btc_dominance_at_detection = COALESCE(?, btc_dominance_at_detection),
                notes = COALESCE(NULLIF(?, ''), notes),
                updated_at = ?
            WHERE id = ?
        """, (sentiment_score, news_volume, funding_rate, fear_greed,
              btc_dominance, notes, now, existing["id"]))
        conn.commit()
        conn.close()
        print(f"[memory] Running narrative already exists: {tag} (id={existing['id']})")
        return existing["id"]

    cursor = conn.execute("""
        INSERT INTO narrative_memory (
            narrative_tag, first_detected, outcome, confidence_score,
            sentiment_at_detection, news_volume_at_detection,
            funding_rate_at_detection, fear_greed_at_detection,
            btc_dominance_at_detection, notes, created_at, updated_at
        ) VALUES (?, ?, 'running', 0.5, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (tag, now, sentiment_score, news_volume, funding_rate,
          fear_greed, btc_dominance, notes, now, now))
    conn.commit()
    record_id = cursor.lastrowid
    conn.close()
    print(f"[memory] New narrative recorded: {tag} (id={record_id})")
    return record_id


# ─────────────────────────────────────────────
# Write Back Outcome (after trade closes)
# ─────────────────────────────────────────────

def update_narrative_outcome(
    record_id: int,
    outcome: str,
    peak_date: str = None,
    days_to_peak: int = None,
    optimal_entry_day: int = None,
    avg_return_pct: float = None,
    notes: str = None,
):
    """
    Update a narrative record with the actual outcome after a trade closes.
    This is the memory write-back — the agent learns from each cycle.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute("""
        UPDATE narrative_memory SET
            outcome = ?,
            peak_date = COALESCE(?, peak_date),
            days_to_peak = COALESCE(?, days_to_peak),
            optimal_entry_day = COALESCE(?, optimal_entry_day),
            avg_return_pct = COALESCE(?, avg_return_pct),
            notes = COALESCE(?, notes),
            updated_at = ?
        WHERE id = ?
    """, (outcome, peak_date, days_to_peak, optimal_entry_day,
          avg_return_pct, notes, now, record_id))
    conn.commit()
    conn.close()
    print(f"[memory] Updated narrative id={record_id} → outcome={outcome}")


# ─────────────────────────────────────────────
# Trade Log
# ─────────────────────────────────────────────

def log_trade(
    narrative_tag: str,
    symbol: str,
    side: str,
    entry_price: float,
    position_size: str,
    memory_id: int = None,
    memory_informed: bool = False,
    notes: str = "",
    stop_loss_price: float = None,
    take_profit_price: float = None,
    initial_risk_pct: float = None,
    trade_type: str = None,
) -> int:
    """Log a paper trade entry. Returns trade ID."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    resolved_trade_type = trade_type or (
        "fallback" if narrative_tag.startswith("fallback_") else "narrative"
    )
    cursor = conn.execute("""
        INSERT INTO trade_log (
            narrative_tag, memory_id, entry_date, symbol, side,
            entry_price, position_size, memory_informed,
            current_price, unrealized_pnl_pct, stop_loss_price,
            take_profit_price, initial_risk_pct, last_price_at, trade_type,
            status, notes, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, 'open', ?, ?, ?)
    """, (narrative_tag, memory_id, now, symbol, side,
          entry_price, position_size, int(memory_informed), entry_price,
          stop_loss_price, take_profit_price, initial_risk_pct, now,
          resolved_trade_type,
          notes, now, now))
    conn.commit()
    trade_id = cursor.lastrowid
    conn.close()
    print(f"[memory] Trade logged: {symbol} {side} @ {entry_price} "
          f"(size={position_size}, memory_informed={memory_informed})")
    return trade_id


def update_trade_mark(
    trade_id: int,
    current_price: float,
    unrealized_pnl_pct: float,
):
    """Persist the latest mark-to-market values for an open trade."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute("""
        UPDATE trade_log SET
            current_price = ?,
            unrealized_pnl_pct = ?,
            last_price_at = ?,
            updated_at = ?
        WHERE id = ? AND status = 'open'
    """, (current_price, unrealized_pnl_pct, now, now, trade_id))
    conn.commit()
    conn.close()


def update_trade_stop(trade_id: int, stop_loss_price: float):
    """Raise or lower the stored protective stop for an open trade."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()
    conn.execute("""
        UPDATE trade_log SET
            stop_loss_price = ?,
            updated_at = ?
        WHERE id = ? AND status = 'open'
    """, (stop_loss_price, now, trade_id))
    conn.commit()
    conn.close()


def close_trade(
    trade_id: int,
    exit_price: float,
    exit_reason: str,
    pnl_pct: float = None,
):
    """Close an open paper trade and calculate PnL."""
    now = datetime.now(timezone.utc).isoformat()
    conn = get_connection()

    # Auto-calculate PnL if not provided
    if pnl_pct is None:
        trade = conn.execute(
            "SELECT entry_price, side FROM trade_log WHERE id = ?", (trade_id,)
        ).fetchone()
        if trade:
            entry = trade["entry_price"]
            side = trade["side"]
            if entry and exit_price:
                raw = (exit_price - entry) / entry * 100
                pnl_pct = raw if side == "long" else -raw

    conn.execute("""
        UPDATE trade_log SET
            exit_date = ?,
            exit_price = ?,
            exit_reason = ?,
            pnl_pct = ?,
            status = 'closed',
            updated_at = ?
        WHERE id = ?
    """, (now, exit_price, exit_reason, pnl_pct, now, trade_id))
    conn.commit()
    conn.close()
    print(f"[memory] Trade {trade_id} closed @ {exit_price} "
          f"({exit_reason}) PnL={pnl_pct:.2f}%" if pnl_pct else
          f"[memory] Trade {trade_id} closed @ {exit_price}")


def get_trade_log(status: str = None) -> list[dict]:
    """Return trade log, optionally filtered by status."""
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM trade_log WHERE status = ? ORDER BY created_at DESC",
            (status,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM trade_log ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────────
# Test
# ─────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Initializing Memory DB ===")
    init_db()

    print("\n=== All Narratives ===")
    for n in get_all_narratives():
        print(f"  [{n['id']}] {n['narrative_tag']:25s} | "
              f"return={n['avg_return_pct']:6.1f}% | "
              f"days_to_peak={n['days_to_peak']:3} | "
              f"outcome={n['outcome']}")

    print("\n=== Query Test: ai_coins ===")
    result = query_narrative("ai_coins")
    if result:
        print(f"  Found: {result['narrative_tag']} — "
              f"optimal entry day {result['optimal_entry_day']}, "
              f"avg return {result['avg_return_pct']}%")

    print("\n=== Query Test: unknown_narrative ===")
    result = query_narrative("unknown_narrative")
    print(f"  Result: {result}")

    print("\n=== Trade Log Test ===")
    trade_id = log_trade(
        narrative_tag="ai_coins",
        symbol="FETUSDT",
        side="long",
        entry_price=2.45,
        position_size="medium",
        memory_informed=True,
        notes="Test trade — memory informed entry"
    )
    close_trade(trade_id, exit_price=2.89, exit_reason="take_profit")

    print("\n=== Trade Log ===")
    for t in get_trade_log():
        print(f"  [{t['id']}] {t['symbol']:12s} {t['side']:5s} | "
              f"entry={t['entry_price']} exit={t['exit_price']} | "
              f"pnl={t['pnl_pct']}% | status={t['status']}")

    print("\n✅ memory.py working correctly")
