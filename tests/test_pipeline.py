"""End-to-end checks on generated data, plus tampering tests: corrupt the inputs and
confirm the reconciler notices, without relying on the generator's labels."""
import csv
import json

import pytest

from generate_large_data import generate
from reconcile import run
from verify_reconciliation import verify


def read(path):
    with path.open(newline="", encoding="utf-8") as file:
        return list(csv.DictReader(file))


def write(path, rows):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def month(tmp_path):
    generated, processed = tmp_path / "generated", tmp_path / "processed"
    generate(generated, orders=2_000, seed=7)
    return generated, processed


def test_every_order_and_payout_matches_ground_truth(month):
    generated, processed = month
    run(generated, processed)
    assert verify(generated, processed) == []


def test_default_month_headline_numbers():
    """Regression lock on the published 10,000-order month (seed 42)."""
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as temp:
        generated, processed = Path(temp) / "g", Path(temp) / "p"
        generate(generated)
        summary = run(generated, processed)
        assert verify(generated, processed) == []
    assert summary["orders"] == 10_000
    assert summary["total_recoverable_inr"] == "2536060.65"
    assert summary["batch_level_shortfall_inr"] == "1180.00"


def test_deleting_a_bank_credit_is_detected(month):
    generated, processed = month
    bank = read(generated / "bank_statement.csv")
    payout = next(i for i, row in enumerate(bank) if "PAYGATE" in row["narration"].upper())
    del bank[payout]
    write(generated / "bank_statement.csv", bank)
    run(generated, processed)
    batches = read(processed / "settlement_batches.csv")
    assert sum(b["status"] == "BANK_CREDIT_MISSING" for b in batches) == 2   # injected one + ours
    assert verify(generated, processed)


def test_removing_a_gateway_line_breaks_the_payout_total(month):
    generated, processed = month
    lines = read(generated / "gateway_settlement_lines.csv")
    removed = next(l for l in lines if l["line_type"] == "payment")
    write(generated / "gateway_settlement_lines.csv", [l for l in lines if l is not removed])
    run(generated, processed)
    batch = next(b for b in read(processed / "settlement_batches.csv")
                 if b["settlement_id"] == removed["settlement_id"])
    assert "GATEWAY_TOTAL_MISMATCH" in batch["status"]
    payments = {p["payment_id"]: p for p in read(generated / "gateway_payments.csv")}
    order_id = payments[removed["payment_id"]]["order_receipt"].strip().upper()
    order = next(r for r in read(processed / "reconciliation_report.csv") if r["order_id"] == order_id)
    assert "MISSING_SETTLEMENT" in order["exception_flags"]


def test_summary_is_written(month):
    generated, processed = month
    run(generated, processed)
    summary = json.loads((processed / "summary.json").read_text())
    assert summary["orders"] == 2_000 and summary["unmatched_bank_credits"] > 0
