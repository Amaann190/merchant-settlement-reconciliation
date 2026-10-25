
from pathlib import Path
import csv

# Folder where the simulated data will be stored
output = Path("data/sample")
output.mkdir(parents=True, exist_ok=True)

# These are fictional financial transactions.
datasets = {
    "merchant_orders.csv": [
        ["order_id", "order_amount"],
        ["O1001", "500.00"],
        ["O1002", "850.00"],
        ["O1003", "1200.00"],
        ["O1004", "650.00"],
        ["O1005", "400.00"],
    ],

    "gateway_payments.csv": [
        ["order_id", "status", "charged_amount", "fee"],
        ["O1001", "success", "500.00", "10.00"],
        ["O1002", "success", "850.00", "17.00"],
        ["O1003", "success", "1200.00", "24.00"],
        ["O1004", "success", "650.00", "13.00"],
        ["O1005", "failed", "400.00", "0.00"],
    ],

    "bank_settlements.csv": [
        ["order_id", "net_settled"],
        ["O1001", "490.00"],
        ["O1002", "833.00"],
        ["O1003", "1170.00"],
    ],
}

for filename, rows in datasets.items():
    file_path = output / filename

    with file_path.open(
        "w", newline="", encoding="utf-8"
    ) as file:
        writer = csv.writer(file)
        writer.writerows(rows)

    print(f"Created: {filename}")

print("Synthetic reconciliation data is ready.")
