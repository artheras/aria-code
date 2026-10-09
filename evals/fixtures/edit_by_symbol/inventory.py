def helper_0(value):
    """Unrelated helper 0."""
    return value + 0

def helper_1(value):
    """Unrelated helper 1."""
    return value + 1

def helper_2(value):
    """Unrelated helper 2."""
    return value + 2

def helper_3(value):
    """Unrelated helper 3."""
    return value + 3

def helper_4(value):
    """Unrelated helper 4."""
    return value + 4

def helper_5(value):
    """Unrelated helper 5."""
    return value + 5

def helper_6(value):
    """Unrelated helper 6."""
    return value + 6

def helper_7(value):
    """Unrelated helper 7."""
    return value + 7

def helper_8(value):
    """Unrelated helper 8."""
    return value + 8

def helper_9(value):
    """Unrelated helper 9."""
    return value + 9

def helper_10(value):
    """Unrelated helper 10."""
    return value + 10

def helper_11(value):
    """Unrelated helper 11."""
    return value + 11

def helper_12(value):
    """Unrelated helper 12."""
    return value + 12

def helper_13(value):
    """Unrelated helper 13."""
    return value + 13

def helper_14(value):
    """Unrelated helper 14."""
    return value + 14

def helper_15(value):
    """Unrelated helper 15."""
    return value + 15

def helper_16(value):
    """Unrelated helper 16."""
    return value + 16

def helper_17(value):
    """Unrelated helper 17."""
    return value + 17

def helper_18(value):
    """Unrelated helper 18."""
    return value + 18

def helper_19(value):
    """Unrelated helper 19."""
    return value + 19

def helper_20(value):
    """Unrelated helper 20."""
    return value + 20

def helper_21(value):
    """Unrelated helper 21."""
    return value + 21

def helper_22(value):
    """Unrelated helper 22."""
    return value + 22

def helper_23(value):
    """Unrelated helper 23."""
    return value + 23

def helper_24(value):
    """Unrelated helper 24."""
    return value + 24


class Cart:
    def __init__(self):
        self.lines = []

    def add(self, sku, price, quantity):
        self.lines.append((sku, price, quantity))

    def total(self):
        return sum(price * quantity for _sku, price, quantity in self.lines)

    def count(self):
        return sum(quantity for _sku, _price, quantity in self.lines)
