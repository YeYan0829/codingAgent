"""子执行调度；只负责并发容量、通知与资源回收，业务由公共 AgentLoop 推进。"""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from collections import deque
import threading
import time

from codeagent.orchestration.lease import assert_execution_epoch, register_execution_resource
from codeagent.runtime.request_executor import ExecutionInterrupted


class RunScheduler:
    running_limit = 2
    queued_limit = 4

    def __init__(self, service, epoch):
        self.service, self.epoch = service, epoch
        self.condition = threading.Condition(service.executor.lock)
        self.queue, self.active, self.futures = deque(), {}, {}
        self.stops = {}
        self.root_reason = None
        self.closed = False
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="codeagent-child")
        self.monitor_stop = threading.Event()
        register_execution_resource(service.store, self)
        self.monitor = threading.Thread(target=copy_context().run, args=(self._monitor,), name="codeagent-root-watch", daemon=True)
        self.monitor.start()

    def assert_owned(self):
        assert_execution_epoch(self.service.store, self.epoch)

    def has_capacity(self):
        return len(self.active) < self.running_limit or len(self.queue) < self.queued_limit

    def submit(self, payload):
        rid = payload["run"]["run_id"]
        with self.condition:
            self.assert_owned()
            if rid in self.stops or self.service.acked(rid):
                return
            if self.closed or self.root_reason:
                raise ExecutionInterrupted(self.root_reason or "cancelled")
            if not self.has_capacity():
                raise ValueError("queue_full")
            self.stops[rid] = threading.Event()
            self.queue.append(payload)
            self._pump()

    def _pump(self):
        while self.queue and len(self.active) < self.running_limit and not self.root_reason:
            payload = self.queue.popleft()
            rid = payload["run"]["run_id"]
            self.service.emit("agent_run_running", {"run_id": rid, "execution_epoch": self.epoch})
            future = self.executor.submit(copy_context().run, self.service._execute_run, payload, self.epoch, self.stops[rid])
            self.active[rid] = future
            self.futures[rid] = future
            callback_context = copy_context()
            future.add_done_callback(lambda f, rid=rid, context=callback_context: context.run(self._done, rid, f))

    def _done(self, rid, future):
        # future 完成意味着 AgentLoop 与 transport 的 finally 均已返回。
        with self.condition:
            self.assert_owned()
            self.active.pop(rid, None)
            if self.service.terminal(rid):
                self.service.ack(rid, self.epoch)
            self._pump()
            self.condition.notify_all()

    def cancel(self, run_ids, reason="cancelled"):
        with self.condition:
            self.assert_owned()
            queued = {p["run"]["run_id"]: p for p in self.queue}
            for rid in run_ids:
                if rid in self.stops:
                    self.stops[rid].set()
                if rid in queued:
                    self.queue.remove(queued[rid])
                    self.service._failed(queued[rid], "cancelled", reason, consume=False)
                    self.service.ack(rid, self.epoch)
            self.condition.notify_all()

    def stop_root(self, reason):
        with self.condition:
            if not self.root_reason:
                self.root_reason = reason
                if reason != "parent_finished":
                    self.service.emit("agent_root_stopping", {"reason": reason, "execution_epoch": self.epoch})
            self.cancel(list(self.stops), reason)


    def _monitor(self):
        # 仅 supervisor 检查外部 Stop callback；WaitSet 自身只由 condition 通知／期限唤醒。
        while not self.monitor_stop.wait(0.025):
            try:
                self.service.executor.check()
            except ExecutionInterrupted as exc:
                with self.condition:
                    if self.active or self.queue:
                        self.stop_root(exc.reason)
                return

    def shutdown(self, cancel=True):
        if self.closed:
            return
        if cancel and (self.active or self.queue):
            self.stop_root("cancelled")
        self.monitor_stop.set()
        self.monitor.join()
        self.executor.shutdown(wait=True)
        self.closed = True

    def wait(self, run_ids, mode, deadline, *, check_root=True):
        with self.condition:
            while True:
                # BaseException 故障注入即使发生在 terminal fsync 后也向调用者报告中断。
                for rid in run_ids:
                    future = self.futures.get(rid)
                    if future is not None and future.done() and future.exception() is not None:
                        future.result()
                complete = [rid for rid in run_ids if self.service.acked(rid)]
                satisfied = len(complete) == len(run_ids) if mode == "all" else bool(complete)
                if satisfied:
                    return True
                if check_root:
                    self.service.executor.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(None if remaining == float("inf") else remaining)
