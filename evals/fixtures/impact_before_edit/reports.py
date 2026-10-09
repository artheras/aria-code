import pricing


def discounted(prices, rate):
    return [pricing.apply_discount(p, rate) for p in prices]
