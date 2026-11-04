"""Compare every reconciled order and payout batch with the generator's injected ground truth."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load(path: Path, key: str):
    with path.open(newline="", encoding="utf-8-sig") as file:
        rows = {}
        for row in csv.DictReader(file):
            if row[key] in rows:
                raise ValueError(f"duplicate {key} {row[key]} in {path.name}")
            rows[row[key]] = row
    return rows


def compare(truth, actual, label):
    errors = [f"{label} {k}: missing from report" for k in truth.keys() - actual.keys()]
    errors += [f"{label} {k}: not in ground truth" for k in actual.keys() - truth.keys()]
    for key in sorted(truth.keys() & actual.keys()):
        want, got = truth[key], actual[key]
        got_status = got["status"]
        if want["expected_status"] != got_status:
            errors.append(f"{label} {key}: status {got_status}, expected {want['expected_status']}")
        if want["expected_difference"] != got["difference"]:
            errors.append(f"{label} {key}: difference {got['difference'] or 'blank'}, "
                          f"expected {want['expected_difference'] or 'blank'}")
    return errors


def verify(generated: Path, processed: Path):
    errors = compare(load(generated / "ground_truth.csv", "order_id"),
                     load(processed / "reconciliation_report.csv", "order_id"), "order")
    errors += compare(load(generated / "ground_truth_batches.csv", "settlement_id"),
                      load(processed / "settlement_batches.csv", "settlement_id"), "payout")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generated", type=Path, default=ROOT / "data/generated")
    parser.add_argument("--processed", type=Path, default=ROOT / "data/processed")
    args = parser.parse_args()
    errors = verify(args.generated, args.processed)
    if errors:
        print(f"VALIDATION FAILED: {len(errors)} discrepancies")
        print("\n".join(errors[:20]))
        sys.exit(1)
    orders = sum(1 for _ in open(args.generated / "ground_truth.csv", encoding="utf-8")) - 1
    batches = sum(1 for _ in open(args.generated / "ground_truth_batches.csv", encoding="utf-8")) - 1
    print(f"ALL CHECKS PASSED: {orders:,} orders and {batches} payout batches match ground truth "
          "(status and amount, to the paisa).")


if __name__ == "__main__":
    main()
