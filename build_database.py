
from pathlib import Path
from decimal import Decimal
import sqlite3
import csv

# ==========================================
# 1. DEFINE FILE LOCATIONS
# ==========================================

ROOT = Path(__file__).resolve().parent

REPORT = (
    ROOT / "data/processed/reconciliation_report.csv"
)

DATABASE = (
    ROOT / "data/processed/reconciliation.db"
)

if not REPORT.exists():
    raise FileNotFoundError(
        "Run reconcile.py before building the database."
    )


# ==========================================
# 2. CONVERT RUPEES TO INTEGER PAISE
# ==========================================

def to_paise(value):

    if value is None or str(value).strip() == "":
        return None

    amount = Decimal(value)

    if not amount.is_finite():
        raise ValueError("Invalid monetary amount")

    paise = amount * 100

    if paise != paise.to_integral_value():
        raise ValueError(
            f"Amount contains fractional paise: {value}"
        )

    return int(paise)


# ==========================================
# 3. READ RECONCILIATION RESULTS
# ==========================================

records = []

with REPORT.open(
    newline="", encoding="utf-8-sig"
) as file:

    reader = csv.DictReader(file)

    for row in reader:

        records.append((
            row["order_id"],
            row["status"],
            to_paise(row["expected_net"]),
            to_paise(row["actual_net"]),
            to_paise(row["difference"]), row["exception_flags"]
        ))

print("Records loaded:", len(records))


# ==========================================
# 4. CREATE SQLITE DATABASE
# ==========================================

DATABASE.parent.mkdir(
    parents=True, exist_ok=True
)

with sqlite3.connect(DATABASE) as connection:

    connection.execute("""
        CREATE TABLE IF NOT EXISTS reconciliation_results (
            order_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            expected_paise INTEGER,
            actual_paise INTEGER,
            difference_paise INTEGER
        )
    """)

    if "exception_flags" not in {row[1] for row in connection.execute("PRAGMA table_info(reconciliation_results)")}:
        connection.execute("ALTER TABLE reconciliation_results ADD COLUMN exception_flags TEXT")

    # Rebuild local analytical results on each run.
    connection.execute(
        "DELETE FROM reconciliation_results"
    )

    connection.executemany("""
        INSERT INTO reconciliation_results (
            order_id,
            status,
            expected_paise,
            actual_paise,
            difference_paise, exception_flags
        )
        VALUES (?, ?, ?, ?, ?, ?)
    """, records)

    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_status
        ON reconciliation_results(status)
    """)

    # ======================================
    # 5. QUERY EXCEPTION CATEGORIES
    # ======================================

    print("\n=== EXCEPTION SUMMARY ===")

    summary = connection.execute("""
        SELECT status, COUNT(*)
        FROM reconciliation_results
        GROUP BY status
        ORDER BY status
    """).fetchall()

    for status, count in summary:
        print(f"{status}: {count}")

    # ======================================
    # 6. CALCULATE POTENTIAL SHORTFALL
    # ======================================

    shortfall_paise = connection.execute("""
        SELECT COALESCE(
            SUM(
                CASE
                    WHEN difference_paise > 0
                    THEN difference_paise
                    ELSE 0
                END
            ),
            0
        )
        FROM reconciliation_results
    """).fetchone()[0]

    shortfall_rupees = (
        Decimal(shortfall_paise) / Decimal(100)
    )

    print("\n=== FINANCIAL SUMMARY ===")
    print(
        f"Potential shortfall: INR {shortfall_rupees:.2f}"
    )

    # ======================================
    # 7. FIND THE BIGGEST SHORTFALLS
    # ======================================

    print("\n=== TOP 5 POTENTIAL SHORTFALLS ===")

    top_cases = connection.execute("""
        SELECT
            order_id,
            status,
            difference_paise
        FROM reconciliation_results

        WHERE difference_paise > 0

        ORDER BY difference_paise DESC
        LIMIT 5
    """).fetchall()

    for order_id, status, difference in top_cases:

        amount = Decimal(difference) / 100

        print(
            f"{order_id} | {status} | INR {amount:.2f}"
        )


print("\nDatabase created successfully!")
print("Location:", DATABASE)
