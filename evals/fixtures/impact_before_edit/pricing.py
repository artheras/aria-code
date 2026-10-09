def apply_discount(price, rate):
    """Price after a fractional discount, e.g. rate=0.2 for 20% off."""
    return price * (1 - rate)
