from reconcile import reconcile

def test_orphan_settlement():
    row, = reconcile({}, {}, {"X": {"net_settled":"80"}})
    assert row["exception_flags"].split(" | ") == ["MERCHANT_ORDER_MISSING","PAYMENT_RECORD_MISSING","UNEXPECTED_SETTLEMENT"]
    assert row["actual_net"] == 80 and row["difference"] is None

def test_charge_conflict_and_missing_payout():
    row, = reconcile({"X":{"order_amount":"90"}}, {"X":{"status":"success","charged_amount":"100","fee":"2"}}, {})
    assert row["exception_flags"].split(" | ") == ["ORDER_PAYMENT_MISMATCH","MISSING_SETTLEMENT"]
    assert row["difference"] == 98
