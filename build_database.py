"""Load the reconciliation outputs into SQLite (amounts as integer paise) and print the review queue."""
import csv
import sqlite3
from pathlib import Path

from reconcile import optional_paise, rupees

ROOT = Path(__file__).resolve().parent
PROCESSED = ROOT / "data/processed"
DATABASE = PROCESSED / "reconciliation.db"

ORDER_MONEY = ["expected_net", "actual_net", "difference", "contract_charges", "deducted_charges",
               "merchant_refunds", "gateway_refunds"]
BATCH_MONEY = ["gateway_net", "bank_credit", "difference"]

SCHEMA = """
DROP TABLE IF EXISTS reconciliation_results;
DROP TABLE IF EXISTS settlement_batches;
CREATE TABLE reconciliation_results (
    order_id TEXT PRIMARY KEY, status TEXT NOT NULL,
    expected_net_paise INTEGER, actual_net_paise INTEGER, difference_paise INTEGER,
    exception_flags TEXT, payment_id TEXT, captured_on TEXT, settlement_id TEXT, settled_on TEXT,
    utr TEXT, contract_charges_paise INTEGER, deducted_charges_paise INTEGER,
    merchant_refunds_paise INTEGER, gateway_refunds_paise INTEGER);
CREATE TABLE settlement_batches (
    settlement_id TEXT PRIMARY KEY, settled_on TEXT NOT NULL, utr TEXT NOT NULL, line_count INTEGER,
    gateway_net_paise INTEGER, bank_credit_paise INTEGER, difference_paise INTEGER,
    status TEXT NOT NULL, bank_value_date TEXT);
CREATE INDEX idx_results_status ON reconciliation_results(status);
"""

QUERIES = {
    "Outcomes": """
        SELECT status, COUNT(*), SUM(CASE WHEN difference_paise > 0 THEN difference_paise ELSE 0 END)
        FROM reconciliation_results GROUP BY status ORDER BY 3 DESC, 2 DESC""",
    "Payouts needing follow-up": """
        SELECT settled_on, utr, status, difference_paise FROM settlement_batches
        WHERE status <> 'CREDITED' ORDER BY settled_on""",
    "Largest recoverable orders": """
        SELECT order_id, status, difference_paise FROM reconciliation_results
        WHERE difference_paise > 0 ORDER BY difference_paise DESC LIMIT 5""",
}


def load(path, money):
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = list(csv.DictReader(file))
    for row in rows:
        for column in money:
            row[column] = optional_paise(row[column])
    return rows


def main():
    report = PROCESSED / "reconciliation_report.csv"
    if not report.exists():
        raise SystemExit("Run reconcile.py first.")
    orders = load(report, ORDER_MONEY)
    batches = load(PROCESSED / "settlement_batches.csv", BATCH_MONEY)
    with sqlite3.connect(DATABASE) as db:
        db.executescript(SCHEMA)
        db.executemany(f"INSERT INTO reconciliation_results VALUES ({','.join('?' * 15)})",
                       [tuple(r.values()) for r in orders])
        db.executemany(f"INSERT INTO settlement_batches VALUES ({','.join('?' * 9)})",
                       [tuple(b.values()) for b in batches])
        print(f"Loaded {len(orders):,} orders and {len(batches)} payouts into {DATABASE.name}")
        for title, sql in QUERIES.items():
            print(f"\n=== {title.upper()} ===")
            for *fields, amount in db.execute(sql):   # last column is always paise
                print(" | ".join(map(str, fields)), "| INR", rupees(amount) or "-")


if __name__ == "__main__":
    main()
