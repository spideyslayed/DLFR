from decimal import Decimal, ROUND_HALF_UP

def bill(qty, rate, extra=Decimal("0"), deductions=Decimal("0")):
    return (qty * rate + extra - deductions).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def test_wheat_example():
    assert bill(Decimal("50"), Decimal("2300")) == Decimal("115000.00")

def test_charges_and_deductions():
    assert bill(Decimal("10.500"), Decimal("2300"), Decimal("250"), Decimal("100")) == Decimal("24300.00")

def test_fractional_quantity():
    assert bill(Decimal("0.125"), Decimal("2400")) == Decimal("300.00")
