from pricing import apply_discount


def test_rounds_to_cents():
    assert apply_discount(10, 0.3333) == 6.67


def test_no_discount():
    assert apply_discount(5, 0) == 5
