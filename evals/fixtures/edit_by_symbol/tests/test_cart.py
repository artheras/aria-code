from inventory import Cart, helper_3


def test_total_ignores_negative_quantities():
    cart = Cart()
    cart.add("a", 2.0, 3)
    cart.add("b", 5.0, -1)
    assert cart.total() == 6.0


def test_helpers_untouched():
    assert helper_3(1) == 4
