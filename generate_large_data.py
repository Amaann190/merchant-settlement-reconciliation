"""Generate a seeded month of synthetic merchant, gateway and bank records.

The files imitate what an Indian payment gateway and a current-account statement
look like in practice: gateway payment IDs instead of a shared key, T+2 payout batches
that skip weekends and bank holidays, fee plus 18% GST deductions, refunds netted from
later payouts, failed retries, and bank credits identified only by a UTR inside
free-text narration with Indian digit grouping.

A known set of problems is injected and written to ground_truth*.csv so the
reconciler can be checked. The reconciler never reads those files.
"""
from __future__ import annotations

import argparse
import csv
import random
import string
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from reconcile import (BANK_HOLIDAYS, REFUND_LAG, SETTLEMENT_LAG, add_business_days,
                       contract_charges, is_business_day, rupees)

ROOT = Path(__file__).resolve().parent
FIRST_DAY, LAST_CAPTURE_DAY, AS_OF = date(2026, 10, 1), date(2026, 10, 31), date(2026, 11, 2)
MISSING_PAYOUT_DAY = date(2026, 10, 16)   # gateway says it paid; nothing reached the bank
SHORT_PAYOUT_DAY = date(2026, 10, 22)     # bank credited less than the gateway paid
BANK_DEDUCTION = 1_18_000                 # ₹1,180.00 = ₹1,000 charge + 18% GST, in paise
WRONG_FEE_RATE = Decimal("0.025")         # gateway applied the wrong pricing plan

BASE_COUNTS = {                           # per 10,000 orders; MATCHED takes the remainder
    "IN_TRANSIT": 500, "REFUNDED": 350, "REFUND_MISMATCH": 40, "PAYMENT_FAILED": 400,
    "UNEXPECTED_SETTLEMENT": 50, "ORDER_PAYMENT_MISMATCH": 50, "MISSING_SETTLEMENT": 200,
    "DUPLICATE_SETTLEMENT": 40, "FEE_MISMATCH": 150, "MERCHANT_ORDER_MISSING": 30,
    "PAYMENT_RECORD_MISSING": 20,
}


def indian_format(paise: int) -> str:
    """12345678 paise -> '1,23,456.78' (lakh/crore grouping used on Indian statements)."""
    sign = "-" if paise < 0 else ""
    whole, fraction = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        digits = ",".join(([head] if head else []) + groups) + "," + tail
    return f"{sign}{digits}.{fraction:02d}"


def generate(output: Path, orders: int = 10_000, seed: int = 42):
    rng = random.Random(seed)
    output.mkdir(parents=True, exist_ok=True)
    scale = orders / 10_000
    counts = {k: max(1, round(v * scale)) for k, v in BASE_COUNTS.items()}
    counts["MATCHED"] = orders - sum(counts.values())
    scenarios = [s for s, n in counts.items() for _ in range(n)]
    rng.shuffle(scenarios)

    def token(prefix, n=14):
        return prefix + "".join(rng.choices(string.ascii_letters + string.digits, k=n))

    capture_days = [FIRST_DAY + timedelta(d) for d in range((LAST_CAPTURE_DAY - FIRST_DAY).days + 1)]
    due = {d: add_business_days(d, SETTLEMENT_LAG) for d in capture_days}
    special = {MISSING_PAYOUT_DAY, SHORT_PAYOUT_DAY}
    settled_days = [d for d in capture_days if due[d] <= AS_OF]            # MATCHED may land in special payouts
    clean_days = [d for d in settled_days if due[d] not in special]        # everything else avoids them
    transit_days = [d for d in capture_days if due[d] > AS_OF]
    duplicate_days = [d for d in clean_days
                      if add_business_days(due[d], 1) <= AS_OF and add_business_days(due[d], 1) not in special]

    merchant_orders, merchant_refunds, payments, truth = [], [], [], []
    payout_lines = defaultdict(list)       # payout day -> gateway settlement lines

    def stamp(day):
        moment = datetime.combine(day, time(rng.randrange(24), rng.randrange(60), rng.randrange(60)))
        return moment.isoformat() + "+05:30"

    def receipt(order_id):
        # 3% of gateway records carry the merchant receipt with stray case or spaces.
        roll = rng.random()
        return f" {order_id.lower()}" if roll < 0.015 else f"{order_id} " if roll < 0.03 else order_id

    def add_payment(order_id, day, amount, status):
        pid = token("pay_")
        payments.append({"payment_id": pid, "order_receipt": receipt(order_id), "status": status,
                         "amount": rupees(amount), "method": rng.choice(["upi", "upi", "upi", "card", "netbanking"]),
                         "created_at": stamp(day)})
        return pid

    def settle(pid, amount, payout_day, charges=None):
        charges = contract_charges(amount) if charges is None else charges
        fee = round(charges / Decimal("1.18"))
        payout_lines[payout_day].append({"line_type": "payment", "entity_id": pid, "payment_id": pid,
                                         "amount": amount, "fee": fee, "tax": charges - fee,
                                         "net": amount - charges})
        return amount - charges

    def refund_day(capture):
        for _ in range(50):
            day = capture + timedelta(rng.randint(0, 4))
            # Deduct no earlier than the payment's own payout, so no batch can go negative.
            deduct = max(add_business_days(day, REFUND_LAG), due[capture])
            if deduct <= AS_OF and deduct not in special:
                return day, deduct
        raise RuntimeError("no valid refund date")

    for index, scenario in enumerate(scenarios, start=1):
        order_id = f"O{index:06d}"
        amount = rng.randint(100_00, 10_000_00)
        if scenario == "MATCHED":
            day = rng.choice(settled_days)
        elif scenario == "IN_TRANSIT":
            day = rng.choice(transit_days)
        elif scenario == "DUPLICATE_SETTLEMENT":
            day = rng.choice(duplicate_days)
        elif scenario in ("REFUNDED", "REFUND_MISMATCH"):
            day = rng.choice([d for d in clean_days if d <= date(2026, 10, 25)])
        else:
            day = rng.choice(clean_days)
        net = amount - contract_charges(amount)
        status, difference = scenario, 0

        if scenario != "MERCHANT_ORDER_MISSING":
            merchant_orders.append({"order_id": order_id, "order_date": day.isoformat(),
                                    "order_status": "paid", "order_amount": rupees(amount)})
        if scenario == "PAYMENT_RECORD_MISSING":
            truth.append({"order_id": order_id, "expected_status": status, "expected_difference": rupees(net)})
            continue

        if scenario in ("PAYMENT_FAILED", "UNEXPECTED_SETTLEMENT"):
            pid = add_payment(order_id, day, amount, "failed")
            if scenario == "UNEXPECTED_SETTLEMENT":
                difference = -settle(pid, amount, due[day])
            truth.append({"order_id": order_id, "expected_status": status,
                          "expected_difference": "" if scenario == "PAYMENT_FAILED" else rupees(difference)})
            continue

        if rng.random() < 0.06:                     # customer's first UPI attempt failed, retry succeeded
            add_payment(order_id, day, amount, "failed")
        charged = amount + 10_00 if scenario == "ORDER_PAYMENT_MISMATCH" else amount
        pid = add_payment(order_id, day, charged, "captured")
        net = charged - contract_charges(charged)

        if scenario == "IN_TRANSIT":
            difference = None
        elif scenario == "MISSING_SETTLEMENT":
            difference = net
        elif scenario == "FEE_MISMATCH":
            wrong_fee = int((Decimal(charged) * WRONG_FEE_RATE).quantize(Decimal(1), ROUND_HALF_UP))
            wrong = wrong_fee + int((Decimal(wrong_fee) * Decimal("0.18")).quantize(Decimal(1), ROUND_HALF_UP))
            settle(pid, charged, due[day], wrong)
            difference = wrong - contract_charges(charged)
        else:
            settle(pid, charged, due[day])
            if scenario == "DUPLICATE_SETTLEMENT":
                difference = -settle(pid, charged, add_business_days(due[day], 1))
            elif due[day] == MISSING_PAYOUT_DAY:
                status, difference = "BANK_CREDIT_MISSING", net
            elif due[day] == SHORT_PAYOUT_DAY:
                status, difference = "BATCH_SHORT_CREDITED", None

        if scenario in ("REFUNDED", "REFUND_MISMATCH"):
            partial = scenario == "REFUND_MISMATCH" or rng.random() < 0.6
            refund = rng.randint(10, 90) * charged // 100 // 100 * 100 if partial else charged
            refund_on, deduct_on = refund_day(day)
            merchant_refunds.append({"refund_ref": f"R{index:06d}", "order_id": order_id,
                                     "refund_date": refund_on.isoformat(), "refund_amount": rupees(refund)})
            processed = charged if scenario == "REFUND_MISMATCH" else refund   # gateway refunded in full
            rid = token("rfnd_")
            payout_lines[deduct_on].append({"line_type": "refund", "entity_id": rid, "payment_id": pid,
                                            "amount": processed, "fee": 0, "tax": 0, "net": -processed})
            difference = processed - refund

        truth.append({"order_id": order_id, "expected_status": status,
                      "expected_difference": "" if difference is None else rupees(difference)})

    # Gateway payouts and the bank statement.
    settlements, lines, bank, batch_truth = [], [], [], []
    for payout_day in sorted(payout_lines):
        sid, utr = token("setl_"), f"YESB{rng.randrange(10**12):012d}"
        total = sum(l["net"] for l in payout_lines[payout_day])
        settlements.append({"settlement_id": sid, "settled_on": payout_day.isoformat(),
                            "net_amount": rupees(total), "utr": utr})
        for line in payout_lines[payout_day]:
            lines.append({"settlement_id": sid, **{k: rupees(v) if isinstance(v, int) else v
                                                   for k, v in line.items()}})
        credited, status = total, "CREDITED"
        if payout_day == MISSING_PAYOUT_DAY:
            credited, status = None, "BANK_CREDIT_MISSING"
        elif payout_day == SHORT_PAYOUT_DAY:
            credited, status = total - BANK_DEDUCTION, "SHORT_CREDITED"
        batch_truth.append({"settlement_id": sid, "expected_status": status,
                            "expected_difference": rupees(total - (credited or 0))})
        if credited is not None:
            late = add_business_days(payout_day, 1)
            value_day = late if rng.random() < 0.15 and late <= AS_OF else payout_day
            narration = rng.choice([f"RTGS-{utr}-PAYGATE INDIA PVT LTD-SETL", f"NEFT/CR/{utr}/PAYGATE INDIA",
                                    f"rtgs cr {utr.lower()} paygate settlement"])
            bank.append({"value_date": value_day, "narration": narration, "debit": "",
                         "credit": indian_format(credited)})

    business_days = [FIRST_DAY + timedelta(d) for d in range((AS_OF - FIRST_DAY).days + 1)]
    business_days = [d for d in business_days if is_business_day(d)]
    for _ in range(max(3, round(25 * scale))):     # credits that are not gateway payouts
        bank.append({"value_date": rng.choice(business_days), "debit": "",
                     "narration": rng.choice([f"NEFT/CR/HDFC{rng.randrange(10**12):012d}/SHARMA TRADERS",
                                              f"UPI/CR/{rng.randrange(10**12):012d}/RAHUL K/okaxis",
                                              "INT.CR 01-10-2026 TO 31-10-2026"]),
                     "credit": indian_format(rng.randint(500_00, 2_00_000_00))})
    for _ in range(max(3, round(15 * scale))):     # ordinary debits the reconciler must ignore
        bank.append({"value_date": rng.choice(business_days), "credit": "",
                     "narration": rng.choice(["NEFT/DR/VENDOR PAYMENT", "ACH/DR/RENT OCT",
                                              "CHGS/SMS ALERT"]),
                     "debit": indian_format(rng.randint(100_00, 5_00_000_00))})
    bank.sort(key=lambda r: r["value_date"])
    for row in bank:
        row["value_date"] = row["value_date"].strftime("%d/%m/%Y")

    rng.shuffle(payments)
    files = {
        "merchant_orders.csv": (merchant_orders, ["order_id", "order_date", "order_status", "order_amount"]),
        "merchant_refunds.csv": (merchant_refunds, ["refund_ref", "order_id", "refund_date", "refund_amount"]),
        "gateway_payments.csv": (payments, ["payment_id", "order_receipt", "status", "amount", "method", "created_at"]),
        "gateway_settlement_lines.csv": (lines, ["settlement_id", "line_type", "entity_id", "payment_id",
                                                 "amount", "fee", "tax", "net"]),
        "gateway_settlements.csv": (settlements, ["settlement_id", "settled_on", "net_amount", "utr"]),
        "bank_statement.csv": (bank, ["value_date", "narration", "debit", "credit"]),
        "ground_truth.csv": (truth, ["order_id", "expected_status", "expected_difference"]),
        "ground_truth_batches.csv": (batch_truth, ["settlement_id", "expected_status", "expected_difference"]),
    }
    for name, (rows, columns) in files.items():
        with (output / name).open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
    return Counter(t["expected_status"] for t in truth)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--orders", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output", type=Path, default=ROOT / "data/generated")
    args = parser.parse_args()
    counts = generate(args.output, args.orders, args.seed)
    print(f"Wrote {args.orders:,} orders to {args.output}")
    for status, n in sorted(counts.items()):
        print(f"  {status}: {n}")


if __name__ == "__main__":
    main()
