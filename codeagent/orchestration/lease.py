"""同 Session 单执行者；flock 不依靠 PID 猜测所有权。"""
import fcntl
from contextvars import ContextVar
import uuid
from contextlib import contextmanager

from codeagent.session.store import _store_lock

_RESOURCES = ContextVar("codeagent_execution_resources", default={})

_OWNED = ContextVar("codeagent_execution_leases", default={})


@contextmanager
def session_lease(store):
    key = str(store.session_dir)
    owned = _OWNED.get()
    if key in owned:
        yield owned[key]
        return
    # 同进程不同线程同样 fail closed，不等待另一个执行者结束后隐式接管。
    lock = _store_lock(store.session_dir / "execution-thread")
    if not lock.acquire(blocking=False):
        raise RuntimeError("session_busy")
    try:
        with (store.session_dir / "execution.lock").open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError("session_busy") from exc
            store.reconcile_event_frontier()
            epoch = "exec-" + uuid.uuid4().hex
            token = _OWNED.set({**owned, key: epoch})
            resources = []
            resource_token = _RESOURCES.set({**_RESOURCES.get(), key: resources})
            try:
                reconcile_requests(store)
                store.append_event("execution_acquired", {"execution_epoch": epoch})
                yield epoch
            finally:
                try:
                    for resource in resources:
                        resource.shutdown()
                finally:
                    _RESOURCES.reset(resource_token)
                    _OWNED.reset(token)
                fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        lock.release()


def assert_execution_epoch(store, epoch):
    latest = next((e.payload["execution_epoch"] for e in reversed(store.read_events())
                   if e.type == "execution_acquired"), None)
    if latest != epoch or _OWNED.get().get(str(store.session_dir)) != epoch:
        raise RuntimeError("stale_execution_epoch")


def reconcile_requests(store):
    from codeagent.runtime.request_executor import settlement
    events = store.read_events()
    settled = {e.payload["request_id"] for e in events if e.type == "model_request_settled"}
    for event in events:
        if event.type != "model_request_reserved" or event.payload["request_id"] in settled:
            continue
        response = next((e for e in events if e.type == "model_response" and e.payload["request_id"] == event.payload["request_id"]), None)
        value = response.payload["response"] if response else None
        store.append_derived_events([("model_request_settled", {
            "request_id": event.payload["request_id"], "agent_id": event.payload["agent_id"],
            "outcome": ("protocol_error" if value.get("protocol_error") else "completed") if value else "interrupted",
            "usage": value["usage"] if value else None,
            "provider_request_id": value.get("provider_request_id") if value else None,
            **settlement(event.payload, value["usage"] if value else None), "recovered": True,
        })], turn_id=event.turn_id)


def current_execution_epoch(store):
    return _OWNED.get().get(str(store.session_dir))


def execution_owner_busy(store):
    """只探测执行权，不对账、不新增执行世代。"""
    if current_execution_epoch(store):
        return False
    path = store.session_dir / "execution.lock"
    if not path.exists():
        return False
    lock = _store_lock(store.session_dir / "execution-thread")
    if not lock.acquire(blocking=False):
        return True
    try:
        with path.open("r") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        lock.release()


def register_execution_resource(store, resource):
    resources = _RESOURCES.get().get(str(store.session_dir))
    if resources is None:
        raise RuntimeError("execution_scope_required")
    resources.append(resource)


def execution_resources(store):
    return tuple(_RESOURCES.get().get(str(store.session_dir), ()))
