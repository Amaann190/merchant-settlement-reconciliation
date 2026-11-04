# Merchant Settlement Reconciliation

**[Download the populated Power BI report](powerbi/Settlement_Review_Updated.pbix)** and open it in Power BI Desktop. The charts work offline.


Trace a merchant order from gateway charge to bank settlement. Open [Settlement Review in Power BI Desktop](powerbi/Settlement_Review.pbip) and choose **Home → Refresh**. Select an outcome to filter the KPIs and order ledger. The ledger sorts the largest positive shortfalls first. The shared order ID is the reference across all three synthetic sources.

## Rebuild

Run these commands from this directory with Python 3. The reporting pipeline uses the standard library.

    python generate_large_data.py
    python reconcile.py
    python build_database.py
    python verify_reconciliation.py

Optional test runner: install pytest and run `python -m pytest -q`.

## Interpretation

The seeded ledger contains 10,000 orders. Expected net is the successful gateway charge less its fee. A positive difference is the expected net less the amount settled. For missing settlements the full expected net is outstanding. Overpayments are reported separately within amount mismatches and do not offset the shortfall. Source amount conflicts and failed payments have their own statuses. SQLite stores amounts in integer paise.

The generator assumes at most one payment and one settlement per merchant order. Every reference present in any source is included. The primary outcome supports grouping and the exception flags retain simultaneous issues. A successful charge with a missing payout contributes its expected net even if the merchant amount differs. Split payouts, refunds and timing cutoffs remain outside the demonstration. The verifier compares every order with the generator's expected status and shortfall.

## Power BI refresh on another computer

In Power BI Desktop, choose **Transform data → Edit parameters**. Set **DataFolder** to the absolute path of this repository’s `data/processed` directory, then select **Refresh**. The saved PBIX uses its imported snapshot until refreshed.
