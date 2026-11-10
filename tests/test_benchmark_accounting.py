from decimal import Decimal
from pathlib import Path

from codeagent.benchmark.accounting import PriceSnapshot, aggregate_usage, calculate_cost, zero_usage


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
    zero = zero_usage()
    assert zero["coverage"] == "complete" and zero["requests"] == 0
    assert all(zero[field] == 0 for field in (
        "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
        "cache_miss_input_tokens", "reasoning_tokens",
    ))
    assert Decimal(calculate_cost(zero, snapshot())["total"]) == 0


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


def test_partial_batch_preserves_known_usage_and_cost_without_claiming_total():
    from codeagent.benchmark.batch import _aggregate_entry_usage

    known = aggregate_usage([{"input_tokens": 1000, "output_tokens": 200,
                             "total_tokens": 1200, "cache_miss_input_tokens": 800,
                             "cached_input_tokens": 200, "reasoning_tokens": 10}])
    unknown = aggregate_usage([None])
    combined = _aggregate_entry_usage([
        {"token_usage": known}, {"token_usage": unknown}, {"token_usage": zero_usage()},
    ])
    assert combined["requests"] == 2 and combined["requests_with_usage"] == 1
    assert combined["coverage"] == "partial"
    assert combined["input_tokens"] == 1000 and combined["output_tokens"] == 200
    assert not combined["input_tokens_complete"]
    cost = calculate_cost(combined, snapshot())
    assert cost["total"] is None and cost["complete"] is False
    assert cost["known_subtotal"] == calculate_cost(known, snapshot())["total"]
    assert "input_tokens" not in cost["unpriced_or_unknown"]
    assert "cache_miss_input_tokens" in cost["unpriced_or_unknown"]
    assert _aggregate_entry_usage([{"token_usage": unknown}])["input_tokens"] is None


def test_known_subtotal_is_not_exact_when_a_priced_field_is_missing():
    # Both requests returned usage, but the second omitted output tokens.
    usage = aggregate_usage([
        {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 3, "reasoning_tokens": 0},
        {"input_tokens": 20, "cached_input_tokens": 0, "reasoning_tokens": 0},
    ])
    assert usage["coverage"] == "complete" and usage["output_tokens"] == 3
    cost = calculate_cost(usage, snapshot())
    assert cost["total"] is None and cost["complete"] is False
    assert "output_tokens" in cost["unpriced_or_unknown"]
    assert Decimal(cost["known_subtotal"]) > 0


def test_glm_snapshots_price_reasoning_as_part_of_aggregate_output():
    root = Path(__file__).parents[1] / "benchmarks/swebench/prices"
    prices = [PriceSnapshot.from_json(path) for path in (
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


def test_partial_usage_keeps_every_known_cache_miss_fee_without_mutating_source():
    raw = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100,
           "total_tokens": 1100, "reasoning_tokens": 0}
    price = PriceSnapshot('regression', 'glm', 'glm-5.3', '2026-11-07', 'CNY', 1_000_000,
                          {'input': '8', 'cached_input': '2', 'output': '28'}, 'regression fixture', True)
    original = dict(raw)
    complete = aggregate_usage([raw])
    partial = aggregate_usage([raw, None])
    assert raw == original and 'cache_miss_input_tokens' not in raw
    assert partial['cache_miss_input_tokens'] == 200
    assert partial['cache_miss_input_tokens_derived'] is True
    assert partial['cache_miss_input_tokens_complete'] is False
    cost = calculate_cost(partial, price)
    assert cost['known_subtotal'] == calculate_cost(complete, price)['total']
    assert Decimal(cost['known_subtotal']) == Decimal('0.0060')
    assert cost['total'] is None and not cost['complete']

    # 混用显式和推导的计量时，也必须逐条相加，而非仅保留显式部分。
    explicit = {**raw, 'cache_miss_input_tokens': 200}
    combined = aggregate_usage([explicit, raw, None])
    assert combined['cache_miss_input_tokens'] == 400
    assert Decimal(calculate_cost(combined, price)['known_subtotal']) == Decimal(cost['known_subtotal']) * 2
    from codeagent.benchmark.batch import _aggregate_entry_usage
    batch = _aggregate_entry_usage([{'token_usage': partial}, {'token_usage': aggregate_usage([explicit])}])
    assert batch['cache_miss_input_tokens'] == 400
    assert batch['cache_miss_input_tokens_derived'] is True
    assert calculate_cost(batch, price)['known_subtotal'] == calculate_cost(combined, price)['known_subtotal']


def test_cache_miss_derivation_requires_valid_counts_in_the_same_request():
    for raw in ({'input_tokens': 1, 'cached_input_tokens': 2},
                {'input_tokens': 1, 'cached_input_tokens': -1},
                {'input_tokens': 1, 'cached_input_tokens': True},
                {'input_tokens': 1}, {'cached_input_tokens': 0}):
        result = aggregate_usage([raw, None])
        assert result['cache_miss_input_tokens'] is None
        assert not result['cache_miss_input_tokens_derived']
    result = aggregate_usage([{'input_tokens': 0, 'cached_input_tokens': 0}, None])
    assert result['cache_miss_input_tokens'] == 0
