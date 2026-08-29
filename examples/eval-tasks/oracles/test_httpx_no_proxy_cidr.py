from unittest.mock import patch

import httpx
from httpx._utils import URLPattern, get_environment_proxies


def test_ipv4_cidr_pattern_matches_only_network_members():
    pattern = URLPattern("all://192.168.0.0/16")
    assert pattern.matches(httpx.URL("http://192.168.10.20"))
    assert not pattern.matches(httpx.URL("http://192.169.0.1"))


def test_ipv6_cidr_pattern_matches_only_network_members():
    pattern = URLPattern("all://[2001:db8::]/32")
    assert pattern.matches(httpx.URL("https://[2001:db8:1::1]"))
    assert not pattern.matches(httpx.URL("https://[2001:db9::1]"))


def test_environment_ipv6_cidr_uses_parseable_pattern():
    with patch("httpx._utils.getproxies", return_value={"no": "2001:db8::/32"}):
        assert get_environment_proxies() == {"all://[2001:db8::]/32": None}
