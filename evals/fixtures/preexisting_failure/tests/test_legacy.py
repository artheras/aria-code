from legacy import legacy_rate


def test_legacy_rate():
    assert legacy_rate() == 0.2
