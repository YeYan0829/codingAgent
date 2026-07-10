from sort_utils import sort_numbers


def test_sort_numbers_returns_sorted_list():
    assert sort_numbers([3, 1, 2]) == [1, 2, 3]
