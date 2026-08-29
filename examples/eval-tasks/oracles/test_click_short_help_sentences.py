import pytest

from click.utils import _make_default_short_help


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Deploy now! Additional details follow.", "Deploy now!"),
        ("Continue deployment? Additional details follow.", "Continue deployment?"),
        ("Read docs/example.com before continuing", "Read docs/example.com..."),
    ],
)
def test_sentence_endings_and_embedded_period(value, expected):
    result = _make_default_short_help(value, max_length=24)
    assert result == expected
    assert len(result) <= 24
