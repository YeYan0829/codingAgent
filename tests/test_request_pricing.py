"""价格仅用于实际费用记录；不预留额度，不决定能否执行。"""
import subprocess
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
import pytest
from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.base import ModelRequest
from codeagent.runtime.request_executor import RequestExecutor, settlement
from codeagent.runtime.request_pricing import input_envelope
from codeagent.session.store import SessionStore

@pytest.fixture
def store(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "README.md").write_text("known evidence\n")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base"], cwd=root, check=True)
    return SessionStore(root, session_root=tmp_path / "sessions").create()


def budget_for(store, **kwargs):
    config = RuntimeConfig(model_token_prices={"glm/glm-5.3": {"input": 8, "cached_input": 2, "output": 28}}, **kwargs)
    store.append_event("user_message", {"message": "task", "legacy_quota": 1})
    return RequestExecutor(store, config, ModelConfig(provider="glm", model="glm-5.3", reasoning_enabled=True,
                           max_tokens=8192), None, lambda: False)


def reservation(store, rid):
    return next(e.payload for e in store.read_events() if e.type == "model_request_reserved" and e.payload["request_id"] == rid)



def test_differentiated_usage_and_idempotent_settlement(store):
    ledger = budget_for(store)
    rid = ledger.reserve(ModelRequest(messages=[], max_tokens=8192), "main")
    row = reservation(store, rid)
    assert row["fee_reserved_microunits"] == 0 and not row["fee_limit_enabled"]
    usage = {"input_tokens": 1000, "cached_input_tokens": 800, "output_tokens": 100, "total_tokens": 1100}
    ledger.settle(rid, "main", "completed", usage)
    ledger.settle(rid, "main", "completed", usage)
    assert ledger.occupied_fee() == 200 * 8 + 800 * 2 + 100 * 28
    assert sum(e.type == "model_request_settled" for e in store.read_events()) == 1


def test_missing_or_inconsistent_usage_stays_unknown_without_money_reservation(store):
    ledger = budget_for(store)
    rid = ledger.reserve(ModelRequest(messages=[], max_tokens=100), "main")
    row = reservation(store, rid)
    assert settlement(row, None)["fee_state"] == "unknown"
    assert settlement(row, {"input_tokens": 100, "output_tokens": 50, "total_tokens": 160})["fee_state"] == "unknown"
    basic = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
    assert settlement(row, basic)["fee_settled_microunits"] == 100 * 8 + 50 * 28
    assert settlement(row, {**basic, "cache_miss_input_tokens": 20})["fee_settled_microunits"] == 20 * 8 + 80 * 2 + 50 * 28


def test_concurrent_requests_only_record_costs_and_never_reserve_main_slots(store):
    ledger = budget_for(store)
    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(lambda i: ledger.reserve(ModelRequest(messages=[], max_tokens=100), "main" if i % 2 else "child"), range(55)))
    assert len(set(ids)) == 55
    assert len({reservation(store, rid)["ledger_id"] for rid in ids}) == 1
    assert all(reservation(store, rid)["fee_reserved_microunits"] == 0 for rid in ids)


def test_changing_prices_only_affects_later_requests(store):
    ledger = budget_for(store)
    rid = ledger.reserve(ModelRequest(messages=[], max_tokens=100), "main")
    newer = RequestExecutor(store, replace(ledger.config, model_token_prices={"glm/glm-5.3": {"input": 4, "cached_input": 2, "output": 20}}), ledger.model_config, None, lambda: False)
    next_id = newer.reserve(ModelRequest(messages=[], max_tokens=100), "main")
    assert reservation(store, rid)["token_prices"]["input"] == 8
    assert reservation(store, next_id)["token_prices"]["input"] == 4
