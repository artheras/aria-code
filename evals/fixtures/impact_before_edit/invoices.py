from pricing import apply_discount


def invoice_line(description, price, rate):
    return f"{description}: {apply_discount(price, rate):.2f}"
