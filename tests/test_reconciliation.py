
from pathlib import Path
from contextlib import closing
from decimal import Decimal
import sqlite3
import csv



# ==========================================
# 1. LOCATE PROJECT FILES
# ==========================================

ROOT = Path(__file__).resolve().parents[1]

DATABASE = ROOT / "data/processed/reconciliation.db"
GROUND_TRUTH = ROOT / "data/generated/ground_truth.csv"


def run_query(sql):
    """Execute a SQL query and return its results."""

    assert DATABASE.exists(), (
        "Database not found. Run build_database.py first."
    )

    with closing(sqlite3.connect(DATABASE)) as conn:
        return conn.execute(sql).fetchall()


# ==========================================
# TEST 1: DATABASE RECORD COUNT
# ==========================================

def test_record_count():

    count = run_query("""
        SELECT COUNT(*)
        FROM reconciliation_results
    """)[0][0]

    assert count == 10000


# ==========================================
# TEST 2: UNIQUE ORDER IDENTIFIERS
# ==========================================

def test_unique_order_ids():

    total, unique = run_query("""
        SELECT
            COUNT(*),
            COUNT(DISTINCT order_id)
        FROM reconciliation_results
    """)[0]

    assert total == unique


# ==========================================
# TEST 3: EXPECTED CLASSIFICATIONS
# ==========================================

def test_status_counts():

    results = dict(run_query("""
        SELECT status, COUNT(*)
        FROM reconciliation_results
        GROUP BY status
    """))

    expected = {
        "MATCHED": 8500,
        "AMOUNT_MISMATCH": 550,
        "MISSING_SETTLEMENT": 400,
        "PAYMENT_FAILED": 400,
        "UNEXPECTED_SETTLEMENT": 100,
        "ORDER_PAYMENT_MISMATCH": 50
    }

    assert results == expected


# ==========================================
# TEST 4: FINANCIAL ARITHMETIC
# ==========================================

def test_settlement_calculations():

    records = run_query("""
        SELECT
            status,
            expected_paise,
            actual_paise,
            difference_paise
        FROM reconciliation_results
    """)

    for status, expected, actual, difference in records:

        if status == "MATCHED":
            assert expected == actual
            assert difference == 0

        elif status == "AMOUNT_MISMATCH":
            assert expected is not None
            assert actual is not None
            assert difference == expected - actual
            assert difference != 0

        elif status == "MISSING_SETTLEMENT":
            assert expected is not None
            assert actual is None
            assert difference == expected

        elif status == "PAYMENT_FAILED":
            assert expected is None
            assert actual is None


# ==========================================
# TEST 5: EXACT POTENTIAL SHORTFALL
# ==========================================

def test_total_shortfall():

    result = run_query("""
        SELECT SUM(
            CASE
                WHEN status IN (
                    'AMOUNT_MISMATCH',
                    'MISSING_SETTLEMENT'
                )
                AND difference_paise > 0
                THEN difference_paise
                ELSE 0
            END
        )
        FROM reconciliation_results
    """)[0][0]

    # 2,019,509.26 rupees = 201,950,926 paise
    assert result == 201950926


# ==========================================
# TEST 6: COMPARE EVERY ORDER TO GROUND TRUTH
# ==========================================

def test_all_orders_against_ground_truth():

    assert GROUND_TRUTH.exists()

    actual = {
        order_id: (status, difference)
        for order_id, status, difference in run_query("""
            SELECT order_id, status, difference_paise
            FROM reconciliation_results
        """)
    }

    with GROUND_TRUTH.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        expected_rows = list(csv.DictReader(file))

    assert len(expected_rows) == len(actual)

    for row in expected_rows:

        order_id = row["order_id"]
        expected_status = row["expected_status"]

        assert order_id in actual

        actual_status, difference = actual[order_id]

        assert actual_status == expected_status

        expected_shortfall = int(
            Decimal(row["expected_shortfall"]) * 100
        )

        actual_shortfall = (
            max(difference or 0, 0)
            if actual_status in (
                "AMOUNT_MISMATCH",
                "MISSING_SETTLEMENT"
            )
            else 0
        )

        assert actual_shortfall == expected_shortfall
