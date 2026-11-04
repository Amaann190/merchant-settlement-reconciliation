"""Three-way reconciliation: merchant books -> payment gateway -> bank statement.

The merchant knows what it sold and refunded. The gateway reports what it captured,
what it deducted and which settlement batch paid each item out. The bank statement
shows what actually arrived, identified only by a UTR inside free-text narration.

All money is handled as integer paise. Nothing here reads the generator's ground truth.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = ROOT / "data/generated"
DEFAULT_OUTPUT = ROOT / "data/processed"

# Contract terms for this demonstration merchant.
FEE_RATE = Decimal("0.02")        # 2% gateway fee (MDR) on every captured payment
GST_RATE = Decimal("0.18")        # 18% GST charged on the gateway fee
SETTLEMENT_LAG = 2                # T+2 business days from capture to payout
REFUND_LAG = 1                    # refunds are deducted from the next business day's payout
BANK_CREDIT_LAG = 1               # a payout may reach the bank one business day late
BANK_HOLIDAYS = {date(2026, 10, 2)}   # Gandhi Jayanti; extend for other months or states

UTR_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z]{4}[0-9]{12})(?![A-Z0-9])")

# Primary status = first flag present in this order. Exceptions that cost money first,
# informational outcomes (in transit, refunded) last.
PRIORITY = [
    "MERCHANT_ORDER_MISSING", "PAYMENT_RECORD_MISSING", "UNEXPECTED_SETTLEMENT",
    "DOUBLE_CHARGED", "PAYMENT_FAILED", "ORDER_PAYMENT_MISMATCH", "MISSING_SETTLEMENT",
    "DUPLICATE_SETTLEMENT", "BANK_CREDIT_MISSING", "FEE_MISMATCH", "REFUND_MISMATCH",
    "BATCH_SHORT_CREDITED", "IN_TRANSIT", "REFUNDED",
]
# Outcomes where no per-order amount can be stated yet (not due, or not allocatable).
NO_DIFFERENCE = {"IN_TRANSIT", "BATCH_SHORT_CREDITED", "PAYMENT_FAILED"}

REPORT_COLUMNS = [
    # The first six columns feed the Power BI model; keep their names stable.
    "order_id", "status", "expected_net", "actual_net", "difference", "exception_flags",
    "payment_id", "captured_on", "settlement_id", "settled_on", "utr",
    "contract_charges", "deducted_charges", "merchant_refunds", "gateway_refunds",
]
BATCH_COLUMNS = ["settlement_id", "settled_on", "utr", "line_count", "gateway_net",
                 "bank_credit", "difference", "status", "bank_value_date"]


# ---------------------------------------------------------------- parsing helpers

def to_paise(value) -> int:
    """Parse '1,23,456.78', ' ₹500 ', '-12.5' or a Decimal into integer paise."""
    text = str(value).replace(",", "").replace("₹", "").replace("INR", "").strip()
    if not text:
        raise ValueError("empty amount")
    amount = Decimal(text) * 100
    if amount != amount.to_integral_value():
        raise ValueError(f"amount has fractional paise: {value!r}")
    return int(amount)


def optional_paise(value):
    return None if value is None or str(value).strip() == "" else to_paise(value)


def rupees(paise):
    if paise is None:
        return ""
    sign = "-" if paise < 0 else ""
    return f"{sign}{abs(paise) // 100}.{abs(paise) % 100:02d}"


def normalize_id(value: str) -> str:
    return value.strip().upper()


def parse_date(value: str) -> date:
    text = value.strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    return datetime.fromisoformat(text).date()


def extract_utr(narration: str):
    match = UTR_PATTERN.search(narration.upper())
    return match.group(1) if match else None


def contract_charges(amount_paise: int) -> int:
    """Fee + GST the contract allows, each rounded half-up to the paisa."""
    fee = (Decimal(amount_paise) * FEE_RATE).quantize(Decimal("1"), ROUND_HALF_UP)
    gst = (fee * GST_RATE).quantize(Decimal("1"), ROUND_HALF_UP)
    return int(fee + gst)


def is_business_day(day: date, holidays=BANK_HOLIDAYS) -> bool:
    return day.weekday() < 5 and day not in holidays


def add_business_days(start: date, days: int, holidays=BANK_HOLIDAYS) -> date:
    current = start
    while days > 0:
        current += timedelta(days=1)
        if is_business_day(current, holidays):
            days -= 1
    return current


# ---------------------------------------------------------------- loading

def read_csv(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as file:
        return list(csv.DictReader(file))


@dataclass
class Inputs:
    orders: dict                 # order_id -> {"amount", "date"}
    refunds: dict                # order_id -> total merchant refunds (paise)
    payments: list               # [{"payment_id", "order_id", "status", "amount", "captured_on"}]
    lines: list                  # gateway settlement report lines
    settlements: dict            # settlement_id -> {"settled_on", "net", "utr"}
    bank: list                   # [{"value_date", "narration", "credit", "debit"}]
    problems: list = field(default_factory=list)


def load_inputs(folder: Path) -> Inputs:
    orders = {}
    for row in read_csv(folder / "merchant_orders.csv"):
        key = normalize_id(row["order_id"])
        if not key or key in orders:
            raise ValueError(f"missing or duplicate merchant order ID: {row['order_id']!r}")
        orders[key] = {"amount": to_paise(row["order_amount"]), "date": parse_date(row["order_date"])}

    refunds = Counter()
    refund_file = folder / "merchant_refunds.csv"
    if refund_file.exists():
        for row in read_csv(refund_file):
            refunds[normalize_id(row["order_id"])] += to_paise(row["refund_amount"])

    payments, seen = [], set()
    for row in read_csv(folder / "gateway_payments.csv"):
        pid = row["payment_id"].strip()
        if pid in seen:
            raise ValueError(f"duplicate gateway payment ID: {pid}")
        seen.add(pid)
        status = row["status"].strip().lower()
        if status not in {"captured", "failed"}:
            raise ValueError(f"unknown payment status {status!r} for {pid}")
        payments.append({"payment_id": pid, "order_id": normalize_id(row["order_receipt"]),
                         "status": status, "amount": to_paise(row["amount"]),
                         "captured_on": parse_date(row["created_at"])})

    lines = []
    for row in read_csv(folder / "gateway_settlement_lines.csv"):
        line = {"settlement_id": row["settlement_id"].strip(), "type": row["line_type"].strip().lower(),
                "entity_id": row["entity_id"].strip(), "payment_id": row["payment_id"].strip(),
                "amount": to_paise(row["amount"]), "charges": to_paise(row["fee"]) + to_paise(row["tax"]),
                "net": to_paise(row["net"])}
        expected_net = line["amount"] - line["charges"] if line["type"] == "payment" else -line["amount"]
        if line["net"] != expected_net:
            raise ValueError(f"gateway line {line['entity_id']} does not add up")
        lines.append(line)

    settlements = {}
    for row in read_csv(folder / "gateway_settlements.csv"):
        sid = row["settlement_id"].strip()
        settlements[sid] = {"settled_on": parse_date(row["settled_on"]), "net": to_paise(row["net_amount"]),
                            "utr": normalize_id(row["utr"])}

    bank = [{"value_date": parse_date(r["value_date"]), "narration": r["narration"],
             "credit": optional_paise(r["credit"]), "debit": optional_paise(r["debit"])}
            for r in read_csv(folder / "bank_statement.csv")]
    return Inputs(orders, dict(refunds), payments, lines, settlements, bank)


# ---------------------------------------------------------------- reconciliation

def reconcile_batches(settlements, lines, bank, as_of, holidays=BANK_HOLIDAYS):
    """Match each gateway payout to a bank credit by UTR. Returns (batches, unmatched_bank)."""
    line_totals, line_counts = Counter(), Counter()
    for line in lines:
        line_totals[line["settlement_id"]] += line["net"]
        line_counts[line["settlement_id"]] += 1

    credits_by_utr, unmatched = defaultdict(list), []
    known_utrs = {s["utr"] for s in settlements.values()}
    for row in bank:
        if not row["credit"]:
            continue
        utr = extract_utr(row["narration"])
        if utr in known_utrs:
            credits_by_utr[utr].append(row)
        else:
            unmatched.append({**row, "utr": utr or "",
                              "reason": "UTR NOT IN GATEWAY PAYOUTS" if utr else "NO UTR IN NARRATION"})

    batches = {}
    for sid, s in sorted(settlements.items(), key=lambda item: (item[1]["settled_on"], item[0])):
        flags = []
        if line_totals[sid] != s["net"]:
            flags.append("GATEWAY_TOTAL_MISMATCH")
        credits = credits_by_utr.get(s["utr"], [])
        credited = sum(c["credit"] for c in credits) if credits else None
        if len(credits) > 1:
            flags.append("DUPLICATE_BANK_CREDIT")
        if not credits:
            due = add_business_days(s["settled_on"], BANK_CREDIT_LAG, holidays)
            flags.append("CREDIT_PENDING" if due > as_of else "BANK_CREDIT_MISSING")
        elif credited < s["net"]:
            flags.append("SHORT_CREDITED")
        elif credited > s["net"]:
            flags.append("OVER_CREDITED")
        batches[sid] = {
            "settlement_id": sid, "settled_on": s["settled_on"], "utr": s["utr"],
            "line_count": line_counts[sid], "gateway_net": s["net"], "bank_credit": credited,
            "difference": s["net"] - (credited or 0) if "CREDIT_PENDING" not in flags else None,
            "status": " | ".join(flags) or "CREDITED",
            "bank_value_date": credits[0]["value_date"] if credits else None,
        }
    return batches, unmatched


def reconcile_orders(inputs: Inputs, batches, as_of, holidays=BANK_HOLIDAYS):
    payments_by_order = defaultdict(list)
    for p in inputs.payments:
        payments_by_order[p["order_id"]].append(p)
    lines_by_payment = defaultdict(list)
    for line in inputs.lines:
        lines_by_payment[line["payment_id"]].append(line)

    def batch_status(sid):
        return batches[sid]["status"] if sid in batches else "UNKNOWN_SETTLEMENT"

    for key in sorted(inputs.orders.keys() | payments_by_order.keys()):
        order = inputs.orders.get(key)
        pays = payments_by_order.get(key, [])
        captured = [p for p in pays if p["status"] == "captured"]
        flags = set()
        expected = actual = 0
        allowed = deducted = gateway_refunds = 0
        used_lines = []

        if not order:
            flags.add("MERCHANT_ORDER_MISSING")
        if not pays:
            flags.add("PAYMENT_RECORD_MISSING")
            expected = order["amount"] - contract_charges(order["amount"])

        for p in pays:
            p_lines = lines_by_payment.get(p["payment_id"], [])
            payment_lines = [l for l in p_lines if l["type"] == "payment"]
            if p["status"] == "failed":
                if payment_lines:
                    flags.add("UNEXPECTED_SETTLEMENT")
                for line in payment_lines:
                    used_lines.append(line)
                    actual += 0 if "BANK_CREDIT_MISSING" in batch_status(line["settlement_id"]) else line["net"]
                continue

            if order and order["amount"] != p["amount"]:
                flags.add("ORDER_PAYMENT_MISMATCH")
            charges = contract_charges(p["amount"])
            allowed += charges
            expected += p["amount"] - charges
            if not payment_lines:
                due = add_business_days(p["captured_on"], SETTLEMENT_LAG, holidays)
                flags.add("IN_TRANSIT" if due > as_of else "MISSING_SETTLEMENT")
            if len(payment_lines) > 1:
                flags.add("DUPLICATE_SETTLEMENT")
            for line in p_lines:
                used_lines.append(line)
                status = batch_status(line["settlement_id"])
                if line["type"] == "payment":
                    deducted += line["charges"]
                    if line["charges"] != charges:
                        flags.add("FEE_MISMATCH")
                else:
                    gateway_refunds += line["amount"]
                if "BANK_CREDIT_MISSING" in status:
                    flags.add("BANK_CREDIT_MISSING")
                    continue
                if "CREDIT_PENDING" in status:
                    flags.add("IN_TRANSIT")
                if "SHORT_CREDITED" in status:
                    flags.add("BATCH_SHORT_CREDITED")
                actual += line["net"]

        if len(captured) > 1:
            flags.add("DOUBLE_CHARGED")
        if pays and not captured and "UNEXPECTED_SETTLEMENT" not in flags:
            flags.add("PAYMENT_FAILED")

        merchant_refunds = inputs.refunds.get(key, 0)
        expected -= merchant_refunds
        if merchant_refunds or gateway_refunds:
            flags.add("REFUNDED")
            if merchant_refunds != gateway_refunds:
                flags.add("REFUND_MISMATCH")

        ordered = [f for f in PRIORITY if f in flags]
        status = ordered[0] if ordered else "MATCHED"
        payment_failed_only = status == "PAYMENT_FAILED"
        # Nothing is owed yet for money that is not due, and a short-credited batch's loss
        # cannot be pinned to one order; both are reported without a per-order difference.
        no_amount = status in NO_DIFFERENCE or ("IN_TRANSIT" in flags and not used_lines)
        payment_lines = [l for l in used_lines if l["type"] == "payment"]
        sids = sorted({l["settlement_id"] for l in payment_lines})
        yield {
            "order_id": key,
            "status": status,
            "expected_net": None if payment_failed_only else expected,
            "actual_net": None if payment_failed_only or (status == "IN_TRANSIT" and not used_lines) else actual,
            "difference": None if no_amount else expected - actual,
            "exception_flags": " | ".join(ordered),
            "payment_id": " | ".join(p["payment_id"] for p in (captured or pays)),
            "captured_on": min((p["captured_on"] for p in captured), default=None),
            "settlement_id": " | ".join(sids),
            "settled_on": " | ".join(str(batches[s]["settled_on"]) for s in sids if s in batches),
            "utr": " | ".join(batches[s]["utr"] for s in sids if s in batches),
            "contract_charges": allowed if captured else None,
            "deducted_charges": deducted if payment_lines else None,
            "merchant_refunds": merchant_refunds or None,
            "gateway_refunds": gateway_refunds or None,
        }


def reconcile(inputs: Inputs, as_of: date, holidays=BANK_HOLIDAYS):
    known_payments = {p["payment_id"] for p in inputs.payments}
    orphan_lines = [l for l in inputs.lines if l["payment_id"] not in known_payments]
    batches, unmatched_bank = reconcile_batches(inputs.settlements, inputs.lines, inputs.bank, as_of, holidays)
    rows = list(reconcile_orders(inputs, batches, as_of, holidays))
    return rows, list(batches.values()), unmatched_bank, orphan_lines


def summarize(rows, batches, unmatched_bank, orphan_lines, as_of):
    positive = sum(r["difference"] for r in rows if r["difference"] and r["difference"] > 0)
    negative = -sum(r["difference"] for r in rows if r["difference"] and r["difference"] < 0)
    # Batch-level losses that cannot be pinned to a single order (e.g. bank deducted charges).
    unallocated = sum(b["difference"] for b in batches
                      if b["difference"] and b["difference"] > 0 and "SHORT_CREDITED" in b["status"])
    in_transit = sum(r["expected_net"] or 0 for r in rows if r["status"] == "IN_TRANSIT")
    return {
        "as_of": str(as_of),
        "orders": len(rows),
        "status_counts": dict(sorted(Counter(r["status"] for r in rows).items())),
        "batch_status_counts": dict(sorted(Counter(b["status"] for b in batches).items())),
        "order_level_shortfall_inr": rupees(positive),
        "batch_level_shortfall_inr": rupees(unallocated),
        "total_recoverable_inr": rupees(positive + unallocated),
        "overpaid_to_merchant_inr": rupees(negative),
        "in_transit_inr": rupees(in_transit),
        "unmatched_bank_credits": len(unmatched_bank),
        "orphan_gateway_lines": len(orphan_lines),
    }


# ---------------------------------------------------------------- output

def write_csv(path: Path, rows, columns):
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (rupees(v) if isinstance(v, int) and k not in {"line_count"} else
                                 "" if v is None else v) for k, v in row.items()})


def run(input_dir: Path, output_dir: Path, as_of: date | None = None):
    inputs = load_inputs(input_dir)
    if as_of is None:
        as_of = max(row["value_date"] for row in inputs.bank)
    rows, batches, unmatched_bank, orphan_lines = reconcile(inputs, as_of)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "reconciliation_report.csv", rows, REPORT_COLUMNS)
    write_csv(output_dir / "settlement_batches.csv", batches, BATCH_COLUMNS)
    write_csv(output_dir / "unmatched_bank_lines.csv", unmatched_bank,
              ["value_date", "narration", "credit", "utr", "reason"])
    summary = summarize(rows, batches, unmatched_bank, orphan_lines, as_of)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--as-of", type=date.fromisoformat,
                        help="reconciliation date (default: last bank statement date)")
    args = parser.parse_args()
    print(json.dumps(run(args.input, args.output, args.as_of), indent=2))


if __name__ == "__main__":
    main()
