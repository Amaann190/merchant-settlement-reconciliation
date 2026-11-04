"""Match merchant, gateway and bank records at their shared reference."""
import csv
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data/generated"
OUTPUT = ROOT / "data/processed"

def load_records(filename):
    records = {}
    with (DATA / filename).open(newline="", encoding="utf-8-sig") as file:
        for row in csv.DictReader(file):
            key = row["order_id"].strip()
            if not key or key in records:
                raise ValueError(f"Missing or duplicate order ID in {filename}: {key}")
            records[key] = row
    return records

def money(value):
    return Decimal(value).quantize(Decimal("0.01"))

def reconcile(orders, payments, settlements):
    for key in sorted(orders.keys() | payments.keys() | settlements.keys()):
        order, payment, settlement = orders.get(key), payments.get(key), settlements.get(key)
        actual = money(settlement["net_settled"]) if settlement else None
        expected = difference = None
        flags = []
        if not order:
            flags.append("MERCHANT_ORDER_MISSING")
        if not payment:
            flags.append("PAYMENT_RECORD_MISSING")
            if settlement:
                flags.append("UNEXPECTED_SETTLEMENT")
        elif payment["status"].strip().lower() == "failed":
            flags.append("UNEXPECTED_SETTLEMENT" if settlement else "PAYMENT_FAILED")
            if settlement:
                flags.append("PAYMENT_FAILED")
        elif payment["status"].strip().lower() == "success":
            charged, fee = money(payment["charged_amount"]), money(payment["fee"])
            if fee < 0 or fee > charged:
                raise ValueError(f"Invalid fee for {key}")
            expected = charged - fee
            if order and money(order["order_amount"]) != charged:
                flags.append("ORDER_PAYMENT_MISMATCH")
            difference = expected - (actual if actual is not None else 0)
            if actual is None:
                flags.append("MISSING_SETTLEMENT")
            elif difference:
                flags.append("AMOUNT_MISMATCH")
        else:
            raise ValueError(f"Unknown payment status for {key}")
        yield dict(order_id=key, status=flags[0] if flags else "MATCHED",
                   expected_net=expected, actual_net=actual, difference=difference,
                   exception_flags=" | ".join(flags))

def main():
    rows = list(reconcile(*(load_records(f) for f in
                ("merchant_orders.csv", "gateway_payments.csv", "bank_settlements.csv"))))
    OUTPUT.mkdir(parents=True, exist_ok=True)
    with (OUTPUT / "reconciliation_report.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=["order_id","status","expected_net","actual_net","difference","exception_flags"])
        writer.writeheader()
        writer.writerows(rows)
    print(len(rows), "references;", sum(max(r["difference"] or 0, 0) for r in rows), "INR potential shortfall")

if __name__ == "__main__":
    main()
