
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP
from collections import Counter
import csv
import random

# ==========================================
# 1. CONFIGURATION
# ==========================================

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "data" / "generated"
OUTPUT.mkdir(parents=True, exist_ok=True)

# A fixed seed makes our results reproducible.
rng = random.Random(42)

# Every scenario has a known expected outcome.
scenarios = (
    ["MATCHED"] * 8500
    + ["MISSING_SETTLEMENT"] * 400
    + ["UNDERPAID"] * 350
    + ["OVERPAID"] * 200
    + ["PAYMENT_FAILED"] * 400
    + ["UNEXPECTED_SETTLEMENT"] * 100
    + ["ORDER_PAYMENT_MISMATCH"] * 50
)

rng.shuffle(scenarios)

orders = []
payments = []
settlements = []
ground_truth = []


# ==========================================
# 2. MONEY HELPER
# ==========================================

def money(value):
    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP
    )


# ==========================================
# 3. GENERATE 10,000 ORDERS
# ==========================================

for index, scenario in enumerate(scenarios, start=1):

    order_id = f"O{index:06d}"

    # Generate an amount between Rs 100 and Rs 10,000.
    amount = Decimal(
        rng.randint(10000, 1000000)
    ) / Decimal("100")

    # Fictional 2% gateway fee, inclusive of
    # all deductions for this demonstration.
    charged = amount

    if scenario == "ORDER_PAYMENT_MISMATCH":
        charged += Decimal("10.00")

    fee = money(charged * Decimal("0.02"))
    expected_net = money(charged - fee)

    orders.append({
        "order_id": order_id,
        "order_amount": str(amount)
    })

    status = (
        "failed"
        if scenario in ("PAYMENT_FAILED", "UNEXPECTED_SETTLEMENT")
        else "success"
    )

    payments.append({
        "order_id": order_id,
        "status": status,
        "charged_amount": str(charged),
        "fee": str(fee)
    })

    actual_net = None
    shortfall = Decimal("0.00")

    if scenario == "MATCHED":
        actual_net = expected_net
        expected_status = "MATCHED"

    elif scenario == "MISSING_SETTLEMENT":
        shortfall = expected_net
        expected_status = "MISSING_SETTLEMENT"

    elif scenario == "UNDERPAID":
        shortfall = Decimal(
            rng.randint(100, 2500)
        ) / Decimal("100")

        actual_net = expected_net - shortfall
        expected_status = "AMOUNT_MISMATCH"

    elif scenario == "OVERPAID":
        extra = Decimal(
            rng.randint(100, 2500)
        ) / Decimal("100")

        actual_net = expected_net + extra
        expected_status = "AMOUNT_MISMATCH"

    elif scenario == "PAYMENT_FAILED":
        expected_status = "PAYMENT_FAILED"

    elif scenario == "UNEXPECTED_SETTLEMENT":
        actual_net = Decimal("25.00")
        expected_status = "UNEXPECTED_SETTLEMENT"

    elif scenario == "ORDER_PAYMENT_MISMATCH":
        actual_net = expected_net
        expected_status = "ORDER_PAYMENT_MISMATCH"

    if actual_net is not None:
        settlements.append({
            "order_id": order_id,
            "net_settled": str(money(actual_net))
        })

    ground_truth.append({
        "order_id": order_id,
        "expected_status": expected_status,
        "expected_shortfall": str(money(shortfall))
    })


# ==========================================
# 4. SAVE THE CSV FILES
# ==========================================

def save_csv(filename, rows, columns):
    path = OUTPUT / filename

    with path.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.DictWriter(
            file, fieldnames=columns
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Created: {filename} ({len(rows)} rows)")


save_csv(
    "merchant_orders.csv",
    orders,
    ["order_id", "order_amount"]
)

save_csv(
    "gateway_payments.csv",
    payments,
    ["order_id", "status", "charged_amount", "fee"]
)

save_csv(
    "bank_settlements.csv",
    settlements,
    ["order_id", "net_settled"]
)

save_csv(
    "ground_truth.csv",
    ground_truth,
    ["order_id", "expected_status", "expected_shortfall"]
)


# ==========================================
# 5. DISPLAY EXPECTED RESULTS
# ==========================================

counts = Counter(
    row["expected_status"] for row in ground_truth
)

total_shortfall = sum(
    (
        Decimal(row["expected_shortfall"])
        for row in ground_truth
    ),
    Decimal("0.00")
)

print("\n=== EXPECTED RECONCILIATION RESULTS ===")

for label, count in sorted(counts.items()):
    print(f"{label}: {count}")

print("Expected potential shortfall:", total_shortfall)
print("Total orders:", len(orders))
