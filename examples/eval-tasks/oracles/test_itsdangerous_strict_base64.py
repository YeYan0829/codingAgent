import pytest

from itsdangerous.encoding import base64_decode
from itsdangerous.exc import BadData


@pytest.mark.parametrize("value", ["YWJj!", "YWJj💥", b"YW Jj", b"YWJj\x00"])
def test_invalid_characters_are_rejected(value):
    with pytest.raises(BadData, match="Invalid base64-encoded data"):
        base64_decode(value)


def test_valid_unpadded_urlsafe_input_still_decodes():
    assert base64_decode("Pz8-Pg") == b"??>>"
