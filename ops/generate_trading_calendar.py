#!/usr/bin/env python3
"""
Finanzioso Trading Calendar Generator

Generates trading calendar data for NYSE and NASDAQ exchanges (2024-2030).
Uses pandas_market_calendars for accurate holiday/early-close detection.

Usage:
    DATABASE_URL="postgresql://mainrag:xxx@localhost/mainrag" python generate_trading_calendar.py

Or with individual parameters:
    PGHOST=localhost PGUSER=mainrag PGPASSWORD=xxx PGDATABASE=mainrag python generate_trading_calendar.py

Requirements:
    pip install pandas pandas_market_calendars psycopg2-binary
"""

import os
import sys
from datetime import date
from typing import Generator, NamedTuple

try:
    import pandas as pd
    import pandas_market_calendars as mcal
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install pandas pandas_market_calendars psycopg2-binary")
    sys.exit(1)


# Configuration
START_YEAR = 2024
END_YEAR = 2030
EXCHANGES = ["NYSE", "NASDAQ"]
BATCH_SIZE = 1000


class TradingDay(NamedTuple):
    """Represents a single trading calendar entry."""
    exchange: str
    date: date
    is_trading_day: bool
    early_close: bool
    holiday_name: str | None


def get_db_connection():
    """Create database connection from environment variables."""
    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        return psycopg2.connect(database_url)

    # Fall back to individual parameters
    return psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        port=os.environ.get("PGPORT", "5432"),
        user=os.environ.get("PGUSER", "mainrag"),
        password=os.environ.get("PGPASSWORD"),
        database=os.environ.get("PGDATABASE", "mainrag"),
    )


def generate_calendar_entries(
    exchange: str,
    start_date: date,
    end_date: date
) -> Generator[TradingDay, None, None]:
    """
    Generate trading calendar entries for an exchange.

    Yields TradingDay entries for every calendar day in range,
    marking trading days, early closes, and holidays.
    """
    # Get the market calendar
    calendar = mcal.get_calendar(exchange)

    # Get trading schedule (includes early close info)
    schedule = calendar.schedule(
        start_date=pd.Timestamp(start_date),
        end_date=pd.Timestamp(end_date)
    )

    # Convert schedule index to set for fast lookup
    trading_dates = set(schedule.index.date)

    # Get early close dates
    early_close_dates = set()
    if "market_close" in schedule.columns:
        # Early close is typically before 16:00 (4 PM ET)
        normal_close = pd.Timestamp("16:00").time()
        for idx, row in schedule.iterrows():
            close_time = row["market_close"]
            if hasattr(close_time, "time") and close_time.time() < normal_close:
                early_close_dates.add(idx.date())

    # Get holidays with names
    holidays = {}
    try:
        # pandas_market_calendars provides holiday info
        cal_holidays = calendar.holidays()
        if hasattr(cal_holidays, "holidays"):
            for h in cal_holidays.holidays:
                if hasattr(h, "dates") and hasattr(h, "name"):
                    for d in h.dates(start_date, end_date):
                        holidays[d.date() if hasattr(d, "date") else d] = h.name
    except Exception:
        # Holiday names are nice-to-have, not critical
        pass

    # Generate entries for every calendar day
    current = start_date
    while current <= end_date:
        is_trading = current in trading_dates
        is_early_close = current in early_close_dates
        holiday_name = holidays.get(current) if not is_trading else None

        yield TradingDay(
            exchange=exchange,
            date=current,
            is_trading_day=is_trading,
            early_close=is_early_close,
            holiday_name=holiday_name,
        )

        current = current + pd.Timedelta(days=1)
        current = current.date() if hasattr(current, "date") else current


def insert_calendar_batch(conn, entries: list[TradingDay]) -> int:
    """Insert a batch of calendar entries using UPSERT."""
    if not entries:
        return 0

    sql = """
        INSERT INTO finanzioso.trading_calendar
            (exchange, date, is_trading_day, early_close, holiday_name)
        VALUES %s
        ON CONFLICT (exchange, date)
        DO UPDATE SET
            is_trading_day = EXCLUDED.is_trading_day,
            early_close = EXCLUDED.early_close,
            holiday_name = EXCLUDED.holiday_name
    """

    values = [
        (e.exchange, e.date, e.is_trading_day, e.early_close, e.holiday_name)
        for e in entries
    ]

    with conn.cursor() as cur:
        execute_values(cur, sql, values)

    return len(values)


def main():
    """Generate and insert trading calendar data."""
    print("=" * 60)
    print("Finanzioso Trading Calendar Generator")
    print("=" * 60)

    start_date = date(START_YEAR, 1, 1)
    end_date = date(END_YEAR, 12, 31)

    print(f"Date range: {start_date} to {end_date}")
    print(f"Exchanges: {', '.join(EXCHANGES)}")
    print()

    # Connect to database
    try:
        conn = get_db_connection()
        conn.autocommit = False
        print("✓ Connected to database")
    except Exception as e:
        print(f"✗ Database connection failed: {e}")
        print()
        print("Set DATABASE_URL or PGHOST/PGUSER/PGPASSWORD/PGDATABASE environment variables")
        sys.exit(1)

    total_inserted = 0

    try:
        for exchange in EXCHANGES:
            print(f"\nProcessing {exchange}...")

            batch: list[TradingDay] = []
            exchange_count = 0
            trading_days = 0
            early_closes = 0

            for entry in generate_calendar_entries(exchange, start_date, end_date):
                batch.append(entry)

                if entry.is_trading_day:
                    trading_days += 1
                if entry.early_close:
                    early_closes += 1

                if len(batch) >= BATCH_SIZE:
                    inserted = insert_calendar_batch(conn, batch)
                    exchange_count += inserted
                    batch = []
                    print(f"  Inserted {exchange_count} entries...", end="\r")

            # Insert remaining entries
            if batch:
                inserted = insert_calendar_batch(conn, batch)
                exchange_count += inserted

            total_inserted += exchange_count
            print(f"  ✓ {exchange}: {exchange_count} entries ({trading_days} trading days, {early_closes} early closes)")

        # Commit all changes
        conn.commit()
        print()
        print(f"✓ Total: {total_inserted} calendar entries inserted")

        # Verify
        with conn.cursor() as cur:
            cur.execute("""
                SELECT exchange,
                       COUNT(*) as total,
                       COUNT(*) FILTER (WHERE is_trading_day) as trading_days,
                       MIN(date) as min_date,
                       MAX(date) as max_date
                FROM finanzioso.trading_calendar
                GROUP BY exchange
                ORDER BY exchange
            """)

            print()
            print("Verification:")
            print("-" * 60)
            for row in cur.fetchall():
                print(f"  {row[0]}: {row[1]} total, {row[2]} trading days ({row[3]} to {row[4]})")

    except Exception as e:
        conn.rollback()
        print(f"\n✗ Error: {e}")
        sys.exit(1)
    finally:
        conn.close()

    print()
    print("✓ Done!")


if __name__ == "__main__":
    main()
