from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable


TOKEN_FIELDS = (
    "input_tokens", "output_tokens", "total_tokens", "cached_input_tokens",
    "cache_miss_input_tokens", "reasoning_tokens",
)


def aggregate_usage(items: Iterable[dict | None]) -> dict[str, object]:
    values = list(items)
    known = [item for item in values if isinstance(item, dict)]
    result: dict[str, object] = {
        "requests": len(values), "requests_with_usage": len(known),
        "coverage": "unavailable" if not values or not known else
                    ("complete" if len(known) == len(values) else "partial"),
        "coverage_ratio": None if not values else str(Decimal(len(known)) / Decimal(len(values))),
    }
    for field in TOKEN_FIELDS:
        present = [item[field] for item in known if isinstance(item.get(field), int)]
        result[field] = sum(present) if present else None
        result[field + "_complete"] = bool(known) and len(present) == len(values)
    if (result["cache_miss_input_tokens"] is None and result["input_tokens_complete"] and
            result["cached_input_tokens_complete"]):
        result["cache_miss_input_tokens"] = int(result["input_tokens"]) - int(result["cached_input_tokens"])
        result["cache_miss_input_tokens_complete"] = True
        result["cache_miss_input_tokens_derived"] = True
    else:
        result["cache_miss_input_tokens_derived"] = False
    return result


@dataclass(frozen=True)
class PriceSnapshot:
    snapshot_id: str
    provider: str
    model: str
    effective_date: str
    currency: str
    unit_tokens: int
    rates: dict[str, str]
    source: str

    @classmethod
    def from_json(cls, path: str | Path) -> "PriceSnapshot":
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(**value)

    @property
    def fingerprint(self) -> str:
        raw = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


def calculate_cost(usage: dict[str, object], snapshot: PriceSnapshot) -> dict[str, object]:
    mapping = {
        "input": "cache_miss_input_tokens",
        "output": "output_tokens",
        "cached_input": "cached_input_tokens",
        "reasoning": "reasoning_tokens",
    }
    components: dict[str, str] = {}
    missing: list[str] = []
    total = Decimal(0)
    for rate_name, token_name in mapping.items():
        rate = snapshot.rates.get(rate_name)
        tokens = usage.get(token_name)
        # 无独立 cache-miss 计量时，仅在 cached token 不存在时允许使用完整 input。
        if token_name == "cache_miss_input_tokens" and tokens is None and usage.get("cached_input_tokens") is None:
            tokens = usage.get("input_tokens")
        if rate is None:
            if isinstance(tokens, int) and tokens:
                missing.append(rate_name)
            continue
        if tokens is None:
            missing.append(token_name)
            continue
        amount = Decimal(tokens) * Decimal(rate) / Decimal(snapshot.unit_tokens)
        components[rate_name] = str(amount)
        total += amount
    complete = usage.get("coverage") == "complete" and not missing
    return {
        "snapshot_id": snapshot.snapshot_id,
        "snapshot_fingerprint": snapshot.fingerprint,
        "currency": snapshot.currency,
        "complete": complete,
        "total": str(total) if complete else None,
        "known_subtotal": str(total),
        "components": components,
        "unpriced_or_unknown": sorted(set(missing)),
    }
