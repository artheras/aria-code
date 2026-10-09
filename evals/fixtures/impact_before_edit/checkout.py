from pricing import apply_discount


def checkout_total(items, rate=0.0):
    return sum(apply_discount(price, rate) for price in items)
