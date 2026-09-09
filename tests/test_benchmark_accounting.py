from decimal import Decimal
from pathlib import Path

from codeagent.benchmark.accounting import PriceSnapshot, aggregate_usage, calculate_cost


def snapshot(input_rate="2"):
    return PriceSnapshot("prices-1", "glm", "glm-5.2", "2026-09-04", "CNY", 1_000_000,
                         {"input": input_rate, "output": "8", "cached_input": "0.4", "reasoning": "8"},
                         "provider public pricing page")


def test_usage_distinguishes_complete_partial_missing_and_zero():
    complete = aggregate_usage([{"input_tokens": 0, "output_tokens": 2, "total_tokens": 2,
                                 "cache_miss_input_tokens": 0, "cached_input_tokens": 0,
                                 "reasoning_tokens": 0}])
    assert complete["coverage"] == "complete" and complete["input_tokens"] == 0
    assert aggregate_usage([{"total_tokens": 2}, None])["coverage"] == "partial"
    missing = aggregate_usage([None, None])
    assert missing["coverage"] == "unavailable" and missing["total_tokens"] is None
    derived = aggregate_usage([{"input_tokens": 10, "cached_input_tokens": 4, "total_tokens": 10}])
    assert derived["cache_miss_input_tokens"] == 6 and derived["cache_miss_input_tokens_derived"]


def test_cost_uses_decimal_and_never_presents_partial_as_exact():
    usage = aggregate_usage([{"input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
                              "cache_miss_input_tokens": 800, "cached_input_tokens": 200,
                              "reasoning_tokens": 10}])
    cost = calculate_cost(usage, snapshot())
    expected = Decimal(800) * Decimal(2) / Decimal(1_000_000)
    expected += Decimal(200) * Decimal("0.4") / Decimal(1_000_000)
    expected += Decimal(200) * Decimal(8) / Decimal(1_000_000)
    expected += Decimal(10) * Decimal(8) / Decimal(1_000_000)
    assert cost["complete"] and Decimal(cost["total"]) == expected
    assert calculate_cost({**usage, "coverage": "partial"}, snapshot())["total"] is None
    assert calculate_cost(usage, snapshot("3"))["known_subtotal"] != cost["known_subtotal"]


def test_unpriced_usage_is_explicit():
    usage = aggregate_usage([{"input_tokens": 10, "output_tokens": 3, "total_tokens": 13,
                              "cache_miss_input_tokens": 10, "cached_input_tokens": None,
                              "reasoning_tokens": None}])
    price = PriceSnapshot("x", "p", "m", "2026-01-01", "USD", 1000, {"input": "1"}, "source")
    result = calculate_cost(usage, price)
    assert not result["complete"] and result["total"] is None


def test_glm_snapshots_price_reasoning_as_part_of_aggregate_output():
    root = Path(__file__).parents[1] / "benchmarks/swebench/prices"
    prices = [PriceSnapshot.from_json(path) for path in (
        root / "glm-5.2-standard-api-2026-09-04.json",
        root / "glm-5.3-standard-api-2026-09-09.json",
    )]
    aggregate = aggregate_usage([{
        "input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
        "cache_miss_input_tokens": 100, "cached_input_tokens": 0,
        "reasoning_tokens": None,
    }])
    for price in prices:
        aggregate_cost = calculate_cost(aggregate, price)
        assert aggregate_cost["complete"] is True
        assert aggregate_cost["unpriced_or_unknown"] == []

        separate = {**aggregate, "reasoning_tokens": 5, "reasoning_tokens_complete": True}
        separate_cost = calculate_cost(separate, price)
        assert separate_cost["complete"] is True
        assert separate_cost["total"] == aggregate_cost["total"]
        assert separate_cost["reasoning_included_in_output"] is True
        assert separate_cost["unpriced_or_unknown"] == []
