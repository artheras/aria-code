from checkout import checkout_total
from invoices import invoice_line
from reports import discounted


def test_checkout():
    assert checkout_total([10, 20], 0.5) == 15


def test_invoice():
    assert invoice_line("Pen", 3, 0.1) == "Pen: 2.70"


def test_reports():
    assert discounted([1, 2], 0) == [1, 2]
