
from pathlib import Path
from decimal import Decimal
import csv

ROOT = Path(__file__).resolve().parent

TRUTH_FILE = ROOT / "data/generated/ground_truth.csv"
REPORT_FILE = ROOT / "data/processed/reconciliation_report.csv"


# Read a CSV and organize rows by order ID.
def load_by_order_id(path):
    records = {}

    with path.open(
        newline="", encoding="utf-8-sig"
    ) as file:
        reader = csv.DictReader(file)

        for row in reader:
            order_id = row["order_id"]

            if order_id in records:
                raise ValueError(
                    f"Duplicate order ID: {order_id}"
                )

            records[order_id] = row

    return records


truth = load_by_order_id(TRUTH_FILE)
report = load_by_order_id(REPORT_FILE)

print("\n=== GROUND TRUTH VALIDATION ===")
print("Ground truth orders:", len(truth))
print("Reconciliation results:", len(report))

# Check for orders missing from either dataset.
missing = truth.keys() - report.keys()
unexpected = report.keys() - truth.keys()

print("Missing result IDs:", len(missing))
print("Unexpected result IDs:", len(unexpected))

# Compare classifications and shortfall amounts.
correct_statuses = 0
status_errors = []
amount_errors = []

expected_total = Decimal("0.00")
actual_total = Decimal("0.00")

for order_id, expected in truth.items():

    if order_id not in report:
        continue

    actual = report[order_id]

    # Compare the classification.
    if expected["expected_status"] == actual["status"]:
        correct_statuses += 1
    else:
        status_errors.append(order_id)

    # Read the known expected shortfall.
    expected_shortfall = Decimal(
        expected["expected_shortfall"]
    )

    # Empty differences are treated as zero
    # for statuses without a shortfall.
    difference = Decimal(
        actual["difference"]
    ) if actual["difference"] else Decimal("0.00")

    # Only positive shortfalls are counted.
    if difference > 0:
        actual_shortfall = max(
            difference, Decimal("0.00")
        )
    else:
        actual_shortfall = Decimal("0.00")

    expected_total += expected_shortfall
    actual_total += actual_shortfall

    if expected_shortfall != actual_shortfall:
        amount_errors.append(order_id)


# Calculate classification accuracy.
accuracy = (
    correct_statuses / len(truth) * 100
    if truth else 0
)

print("\n=== CLASSIFICATION RESULTS ===")
print("Correct classifications:", correct_statuses)
print("Classification accuracy:", f"{accuracy:.2f}%")
print("Incorrect classifications:", len(status_errors))

print("\n=== FINANCIAL RESULTS ===")
print("Expected shortfall: INR", expected_total)
print("Detected shortfall: INR", actual_total)
print("Orders with incorrect shortfall:", len(amount_errors))

if missing or unexpected or status_errors or amount_errors:
    print("\nVALIDATION FAILED")

    print("Sample classification errors:", status_errors[:5])
    print("Sample amount errors:", amount_errors[:5])

    raise AssertionError("Reconciliation discrepancies found.")

print("\nALL VALIDATION CHECKS PASSED!")
