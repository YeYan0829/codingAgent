from calculator import divide


def test_divide():
    assert divide(8, 2) == 4


def test_zero_count_returns_zero():
    assert divide(8, 0) == 0
