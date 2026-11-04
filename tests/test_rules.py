"""Hand-calculated cases. None of these use the data generator, so they check the
reconciliation rules independently of how the synthetic month was built."""
from datetime import date

import pytest

from reconcile import (Inputs, add_business_days, contract_charges, extract_utr, load_inputs,
                       reconcile, to_paise)

OCT = lambda day: date(2026, 10, day)  # noqa: E731
UTR = "YESB000000000001"


# ---------------------------------------------------------------- helpers

def test_contract_charges_round_fee_then_gst_half_up():
    # ₹1,000: fee ₹20.00, GST 18% of fee ₹3.60 -> ₹23.60
    assert contract_charges(1_000_00) == 23_60
    # ₹999.99: fee 19.9998 -> ₹20.00, GST ₹3.60
    assert contract_charges(999_99) == 23_60
    # ₹123.45: fee 2.469 -> ₹2.47, GST 0.4446 -> ₹0.44
    assert contract_charges(123_45) == 2_91


@pytest.mark.parametrize("text, paise", [
    ("1,23,456.78", 1_23_456_78), ("₹ 500", 500_00), ("  -12.5 ", -12_50), ("INR 0.01", 1)])
def test_amount_parsing(text, paise):
    assert to_paise(text) == paise


def test_amount_with_fractional_paise_is_rejected():
    with pytest.raises(ValueError):
        to_paise("10.005")


def test_t_plus_two_skips_gandhi_jayanti_and_weekend():
    # Thu 1 Oct + 2 business days: Fri 2 Oct is a bank holiday, then Sat/Sun -> Tue 6 Oct
    assert add_business_days(OCT(1), 2) == OCT(6)
    # Fri 30 Oct -> Tue 3 Nov
    assert add_business_days(OCT(30), 2) == date(2026, 11, 3)


@pytest.mark.parametrize("narration, utr", [
    (f"RTGS-{UTR}-PAYGATE INDIA PVT LTD-SETL", UTR),
    (f"neft cr {UTR.lower()} paygate", UTR),
    ("UPI/CR/612345678901/RAHUL K/okaxis", None),   # 12-digit RRN, no bank prefix
    (f"REF{UTR}9", None),                          # embedded in a longer token
    ("INT.CR 01-10-2026 TO 31-10-2026", None),
])
def test_utr_extraction(narration, utr):
    assert extract_utr(narration) == utr


# ---------------------------------------------------------------- scenarios

def build(*, order=1_000_00, payments=(("pay_1", "captured", 1_000_00),), lines=None,
          bank_credit=976_40, refunds=0, receipt="O1"):
    """One ₹1,000 order captured 1 Oct, paid out in batch S1 on 6 Oct (T+2)."""
    if lines is None:
        lines = [("payment", "pay_1", 1_000_00, 23_60)]
    gateway_lines = [{"settlement_id": "S1", "type": kind, "entity_id": pid, "payment_id": pid,
                      "amount": amount, "charges": charges,
                      "net": amount - charges if kind == "payment" else -amount}
                     for kind, pid, amount, charges in lines]
    total = sum(l["net"] for l in gateway_lines)
    bank = [] if bank_credit is None else [
        {"value_date": OCT(6), "narration": f"RTGS-{UTR}-PAYGATE", "credit": bank_credit, "debit": None}]
    return Inputs(
        orders={"O1": {"amount": order, "date": OCT(1)}} if order else {},
        refunds={"O1": refunds} if refunds else {},
        payments=[{"payment_id": pid, "order_id": receipt, "status": status, "amount": amount,
                   "captured_on": OCT(1)} for pid, status, amount in payments],
        lines=gateway_lines,
        settlements={"S1": {"settled_on": OCT(6), "net": total, "utr": UTR}} if gateway_lines else {},
        bank=bank)


def run(inputs, as_of=OCT(9)):
    rows, batches, unmatched, orphans = reconcile(inputs, as_of)
    return rows[0], (batches[0] if batches else None), unmatched


def test_clean_order_matches():
    row, batch, _ = run(build())
    assert (row["status"], row["expected_net"], row["actual_net"], row["difference"]) == \
        ("MATCHED", 976_40, 976_40, 0)
    assert batch["status"] == "CREDITED"


def test_failed_attempt_then_successful_retry_is_matched():
    row, _, _ = run(build(payments=(("pay_0", "failed", 1_000_00), ("pay_1", "captured", 1_000_00))))
    assert row["status"] == "MATCHED"


def test_gateway_receipts_with_stray_case_and_spaces_match_merchant_ids(tmp_path):
    files = {
        "merchant_orders.csv": "order_id,order_date,order_status,order_amount\nO1,2026-10-01,paid,1000.00\n",
        "gateway_payments.csv": "payment_id,order_receipt,status,amount,method,created_at\n"
                                "pay_1, o1 ,captured,1000.00,upi,2026-10-01T10:00:00+05:30\n",
        "gateway_settlement_lines.csv": "settlement_id,line_type,entity_id,payment_id,amount,fee,tax,net\n"
                                        "S1,payment,pay_1,pay_1,1000.00,20.00,3.60,976.40\n",
        "gateway_settlements.csv": f"settlement_id,settled_on,net_amount,utr\nS1,2026-10-06,976.40,{UTR}\n",
        "bank_statement.csv": f'value_date,narration,debit,credit\n06/10/2026,rtgs cr {UTR.lower()},,"976.40"\n',
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    row, _, _ = run(load_inputs(tmp_path))
    assert (row["order_id"], row["status"]) == ("O1", "MATCHED")


def test_missing_payout_is_in_transit_until_due_then_overdue():
    inputs = build(lines=[], bank_credit=None)
    assert run(inputs, as_of=OCT(5))[0]["status"] == "IN_TRANSIT"
    assert run(inputs, as_of=OCT(5))[0]["difference"] is None
    overdue, _, _ = run(inputs, as_of=OCT(6))
    assert (overdue["status"], overdue["difference"]) == ("MISSING_SETTLEMENT", 976_40)


def test_payout_never_reaching_bank():
    pending, batch, _ = run(build(bank_credit=None), as_of=OCT(6))
    assert batch["status"] == "CREDIT_PENDING" and pending["status"] == "IN_TRANSIT"
    row, batch, _ = run(build(bank_credit=None), as_of=OCT(7))
    assert batch["status"] == "BANK_CREDIT_MISSING" and batch["difference"] == 976_40
    assert (row["status"], row["actual_net"], row["difference"]) == ("BANK_CREDIT_MISSING", 0, 976_40)


def test_short_credited_batch_is_reported_at_batch_level_only():
    row, batch, _ = run(build(bank_credit=900_00))
    assert (batch["status"], batch["difference"]) == ("SHORT_CREDITED", 76_40)
    assert (row["status"], row["difference"]) == ("BATCH_SHORT_CREDITED", None)


def test_wrong_pricing_plan_is_a_fee_mismatch():
    # 2.5% fee ₹25.00 + GST ₹4.50 = ₹29.50 deducted; contract allows ₹23.60
    row, _, _ = run(build(lines=[("payment", "pay_1", 1_000_00, 29_50)], bank_credit=970_50))
    assert (row["status"], row["difference"]) == ("FEE_MISMATCH", 5_90)


def test_partial_refund_processed_in_full():
    lines = [("payment", "pay_1", 1_000_00, 23_60), ("refund", "pay_1", 1_000_00, 0)]
    row, _, _ = run(build(lines=lines, refunds=200_00, bank_credit=-23_60))
    # expected 976.40 - 200.00 = 776.40; received 976.40 - 1,000.00 = -23.60
    assert (row["status"], row["expected_net"], row["actual_net"], row["difference"]) == \
        ("REFUND_MISMATCH", 776_40, -23_60, 800_00)


def test_correct_partial_refund():
    lines = [("payment", "pay_1", 1_000_00, 23_60), ("refund", "pay_1", 200_00, 0)]
    row, _, _ = run(build(lines=lines, refunds=200_00, bank_credit=776_40))
    assert (row["status"], row["difference"]) == ("REFUNDED", 0)


def test_same_payment_in_two_payout_lines():
    lines = [("payment", "pay_1", 1_000_00, 23_60)] * 2
    row, _, _ = run(build(lines=lines, bank_credit=1_952_80))
    assert (row["status"], row["difference"]) == ("DUPLICATE_SETTLEMENT", -976_40)


def test_failed_payment_that_was_paid_out():
    row, _, _ = run(build(payments=(("pay_1", "failed", 1_000_00),)))
    assert (row["status"], row["expected_net"], row["difference"]) == ("UNEXPECTED_SETTLEMENT", 0, -976_40)


def test_customer_charged_twice():
    row, _, _ = run(build(payments=(("pay_1", "captured", 1_000_00), ("pay_2", "captured", 1_000_00))),
                    as_of=OCT(5))
    assert row["status"] == "DOUBLE_CHARGED"


def test_merchant_paid_order_with_no_gateway_record():
    row, _, _ = run(build(payments=(), lines=[], bank_credit=None))
    assert (row["status"], row["difference"]) == ("PAYMENT_RECORD_MISSING", 976_40)


def test_gateway_payment_without_merchant_order_keeps_all_flags():
    row, _, _ = run(build(order=None, payments=(("pay_1", "captured", 1_000_00),)))
    assert row["status"] == "MERCHANT_ORDER_MISSING" and row["difference"] == 0


def test_order_amount_differs_from_charge_and_payout_is_late():
    row, _, _ = run(build(order=990_00, lines=[], bank_credit=None), as_of=OCT(6))
    assert row["exception_flags"].split(" | ") == ["ORDER_PAYMENT_MISMATCH", "MISSING_SETTLEMENT"]
    assert row["difference"] == 976_40


def test_unrelated_bank_credits_are_listed_not_matched():
    inputs = build()
    inputs.bank.append({"value_date": OCT(6), "narration": "NEFT/CR/HDFC123456789012/SHARMA TRADERS",
                        "credit": 5_000_00, "debit": None})
    _, _, unmatched = run(inputs)
    assert [u["reason"] for u in unmatched] == ["UTR NOT IN GATEWAY PAYOUTS"]
