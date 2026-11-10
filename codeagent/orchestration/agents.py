"""持久子会话：角色、私有历史、执行事实与当前文件调查报告。"""
from __future__ import annotations

import copy
from contextvars import ContextVar
import json
import threading
import time
import uuid
from dataclasses import asdict, replace
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codeagent.model_gateway.output_recovery import (
    MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS, recovery_reasoning_effort,
    protocol_repair_message, protocol_repair_exhausted, output_truncation_reason,
)
from codeagent.runtime.request_executor import ExecutionInterrupted
from codeagent.runtime.execution_service import ModelToolExecution, UnsafeRecovery, digest
from codeagent.runtime.policy import ChildRolePolicy, PolicyDecision
from codeagent.tools.base import PermissionLevel, ToolResult, ToolSpec
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext
from codeagent.orchestration.reports import normalize_child_report
from codeagent.orchestration.lease import session_lease
from codeagent.runtime.context_preparation import ContextPreparation
from codeagent.orchestration.profiles import AgentSpec, SPECS, task_description
from codeagent.tools.upstream.task import TaskInput


ACTIVE_TOOL_OPERATION = ContextVar("codeagent_agent_tool_operation", default=None)

class SpawnCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    spec: Literal["explore", "review", "code-explorer", "bash-runner", "general-purpose"] = "explore"
    gateway: str = Field(default="parent", min_length=1, max_length=80)
    goal: str = Field(min_length=1, description="自足的子任务及范围；命令/编辑能力以所选角色实际工具为准。")
    done_when: str = Field(default="", max_length=1000, description="需要返回的证据、实际结果与未确认项；不要求未提供的能力。")
    description: str | None = Field(default=None, description="便于显示的短任务名称。")
    context: str = Field(default="", max_length=4000, description="自足背景：原要求、相关路径、已确认事实、待核实假设和父正在做什么；子不继承父对话。")
    request_key: str | None = Field(default=None, min_length=1, max_length=80, description="可选的调用方幂等键；普通模型工具调用由Runtime自动提供。显式重试同一操作时可复用原键。")
    execution: Literal["foreground", "background"] = "foreground"


class SendCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str = Field(min_length=1, max_length=80)
    message: str = Field(min_length=1, description="本次续问及必要背景。保留自身历史，读取当前工作目录。")
    done_when: str = Field(default="", max_length=1000, description="本轮所需证据、实际结果或明确缺口。")
    description: str | None = None
    request_key: str | None = Field(default=None, min_length=1, max_length=80, description="可选的调用方幂等键；普通模型工具调用由Runtime自动提供。显式重试同一操作时可复用原键。")
    execution: Literal["foreground", "background"] = "foreground"


class WaitCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_ids: list[str] = Field(min_length=1, json_schema_extra={"uniqueItems": True}, description="本会话receipt中的run_id，不重复；不限制累计已完成run数量。")
    mode: Literal["all", "any"] = "all"
    timeout: float = Field(default=10, ge=0, le=60)


class CancelCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    run_ids: list[str] = Field(min_length=1, json_schema_extra={"uniqueItems": True}, description="本会话receipt中的run_id，不重复；不限制累计已完成run数量。")
    request_key: str | None = Field(default=None, min_length=1, max_length=80, description="可选的调用方幂等键；普通模型工具调用由Runtime自动提供。显式重试同一操作时可复用原键。")


class CloseCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    agent_id: str = Field(min_length=1, max_length=80)


class AgentSessions:
    def __init__(self, store, executor, legacy, gateway_routes=None):
        self.store, self.executor, self.delegation_adapter = store, executor, legacy
        self.routes = {"parent": executor.model_config, **(gateway_routes or {})}
        self.command_lock = threading.Lock()
        self.scheduler = None
        legacy.sessions = self

    def emit(self, kind, payload):
        with self.executor.lock:
            from codeagent.orchestration.lease import current_execution_epoch, assert_execution_epoch
            epoch = current_execution_epoch(self.store)
            if epoch:
                assert_execution_epoch(self.store, epoch)
            if kind == "agent_run_finished":
                previous = next((e for e in self.store.read_events() if e.type == kind and e.payload["run_id"] == payload["run_id"]), None)
                if previous:
                    return previous
            turn_id = self.executor.turn_id
            run_id = payload.get("run_id")
            if run_id:
                command = next((e.payload for e in self.commands() if e.payload["run"]["run_id"] == run_id), None)
                if command:
                    turn_id = command["run"]["root_turn_id"]
            return self.store.append_derived_events([(kind, payload)], turn_id=turn_id)[0]

    def task_tool(self):
        """普通Runtime只开放同步task，续问复用同一入口的resume。"""
        return ToolSpec.from_model("task", task_description(), PermissionLevel.SESSION_WRITE, TaskInput, self.task)

    def tools(self):
        """旧控制接口兼容定义；普通Runtime不注册后台工具。"""
        return [self.task_tool()] + [ToolSpec(name, desc, PermissionLevel.SESSION_WRITE, schema.model_json_schema(), handler, input_model=schema) for name, desc, schema, handler in (
            ("spawn_agent", "任务会话的高级入口。常规同步子任务优先task；需后台并行时选择角色spec，提供自足goal/context，execution=background。子工具取角色与父权限交集，不继承父对话、不递归；与主方使用当前共享目录，避免重叠修改。收到receipt后继续自己的独立工作，按需wait_agents。gateway默认parent。", SpawnCommand, self.spawn),
            ("send_agent", "向已回收的子会话续问，使用 receipt/report 的 agent_id，保留自身历史并读取当前工作目录；busy 先 wait_agents。", SendCommand, self.send),
            ("wait_agents", "等待 receipt 的 run_ids：all 等全部、any 等任一终态和资源回收；失败也算终态，检查 reports.status。timeout 仅结束本次等待，pending 可继续 wait，不取消、不重启 run，也不请求模型轮询。", WaitCommand, self.wait),
            ("cancel_agents", "幂等请求取消自己的 run。返回 receipt；随后 wait_agents 等待资源回收 terminal ack。", CancelCommand, self.cancel),
            ("close_agent", "仅关闭 idle 子会话，保留历史；busy 必须先取消并等待回收 ack。", CloseCommand, self.close),
        )]

    def task(self, args):
        """OpenHands四字段同步入口；状态和恢复沿用同一个AgentSessions。"""
        try:
            task = TaskInput.model_validate(args)
            if not task.prompt.strip():
                return self.failure("invalid_arguments", "prompt不能为空白")
            if task.resume is not None and not task.resume.strip():
                return self.failure("invalid_arguments", "resume必须是先前返回的非空task_id")
            if task.resume:
                prior = self.latest(task.resume)
                if prior is None:
                    prior = next((e.payload for e in self.commands() if e.payload["run"]["run_id"] == task.resume), None)
                if prior is None:
                    return self.failure("unknown_task", "resume必须是本会话返回的task_id")
                if "subagent_type" in args and task.subagent_type != prior["agent"]["spec"]["name"]:
                    return self.failure("invalid_arguments", "resume不能改变子角色")
                result = self.send({"agent_id": prior["agent"]["agent_id"], "message": task.prompt,
                                    "description": task.description})
            else:
                result = self.spawn({"spec": task.subagent_type, "goal": task.prompt,
                                     "description": task.description})
            if not result.ok:
                return result
            report = json.loads(result.content)
            completed = report["status"] == "completed"
            source = next(e for e in self.store.read_events() if e.type == "agent_run_finished"
                          and e.payload["run_id"] == report["run_id"])
            agent = self.latest(report["agent_id"])["agent"]
            value = {"task_id": report["agent_id"], "subagent": agent["spec"]["name"],
                     "status": "completed" if completed else "error", "text": report.get("findings") or report.get("error", ""),
                     "run_id": report["run_id"], "execution_status": report["status"],
                     "source_ref": "seq:" + str(source.seq)}
            return ToolResult(ok=completed, content=json.dumps(value, ensure_ascii=False),
                              error=None if completed else value["text"],
                              error_code=None if completed else "child_task_failed",
                              metadata={k: v for k, v in value.items() if k != "text"})
        except ValidationError as exc:
            return self.failure("invalid_arguments", str(exc))

    def failure(self, code, detail=None, agent_id=None):
        hints = {
            "agent_busy": "该子会话仍忙或尚未回收；先 wait_agents 等待原 run 的 terminal_ack，再 send_agent；不要重复 spawn。",
            "request_key_conflict": "该 request_key 已绑定不同参数；若是原操作重试须保持参数完全一致，新操作请使用新 request_key。",
            "queue_full": "当前两并发/四排队已满；先 wait_agents 收集已有 run，再提交新任务。",
            "agent_closed": "会话已关闭，历史保留但不能续问；如仍需调查请 spawn_agent 创建新会话。",
            "agent_execution_unavailable": "子角色需要父方已启用的受控命令能力；本次不可用，请选择code-explorer。",
            "unknown_run": "run_ids 中含未知或其他 Session 的 ID；只使用本 Session receipt/report 的 run_id。",
            "unknown_agent": "agent_id 不属于本 Session；请使用已有 receipt 的 agent_id 或创建新会话。",
        }
        metadata = {}
        latest = self.latest(agent_id) if agent_id else None
        if latest:
            metadata = {"agent_id": agent_id, "latest_run_id": latest["run"]["run_id"],
                        "terminal_ack": self.acked(latest["run"]["run_id"])}
        return ToolResult(ok=False, error_code=code, error=hints.get(code, detail or code), metadata=metadata)

    def commands(self):
        return [e for e in self.store.read_events() if e.type == "agent_command_intent"]

    def terminal(self, run_id):
        report = next((e.payload for e in self.store.read_events() if e.type == "agent_run_finished" and e.payload["run_id"] == run_id), None)
        return normalize_child_report(report) if report is not None else None

    def latest(self, agent_id):
        return next((e.payload for e in reversed(self.commands()) if e.payload["agent"]["agent_id"] == agent_id), None)

    def spawn(self, args):
        return self._command("spawn", args, SpawnCommand)

    def send(self, args):
        return self._command("send", args, SendCommand)

    def _command(self, action, args, schema):
        try:
            if isinstance(args, dict):
                args = {k: v for k, v in args.items() if k not in {"revision_mode", "expected_last_run_id"}}
                if not args.get("request_key"):
                    args["request_key"] = ACTIVE_TOOL_OPERATION.get() or uuid.uuid4().hex
            command = schema.model_validate(args)
        except ValidationError as exc:
            return ToolResult(ok=False, error_code="invalid_arguments", error=str(exc)[:1500])
        from codeagent.orchestration.lease import current_execution_epoch
        if command.execution == "background" and current_execution_epoch(self.store) is None:
            return ToolResult(ok=False, error_code="execution_scope_required", error="后台 run 必须在持有 Session lease 的 Runtime 执行范围内提交")
        # 同 agent 忙时先给明确诊断，而不是隐式等待 Session lease。
        if action == "send":
            latest = self.latest(command.agent_id)
            if latest and not self.terminal(latest["run"]["run_id"]) and latest["request_key"] != command.request_key:
                return self.failure("agent_busy", agent_id=command.agent_id)
        if not self.command_lock.acquire(blocking=False):
            return ToolResult(ok=False, error_code="agent_busy", error="前台命令正在执行")
        try:
            with session_lease(self.store) as execution_epoch:
                previous = next((e.payload for e in self.commands() if e.payload["request_key"] == command.request_key), None)
                if not previous or previous["run"]["root_turn_id"] == self.executor.turn_id:
                    self.recover(execution_epoch)
                with self.executor.lock:
                    payload = self._submit(action, command, execution_epoch)
                if isinstance(payload, ToolResult):
                    return payload
                return self._execute_or_report(payload, execution_epoch)
        except (ValueError, RuntimeError, ExecutionInterrupted) as exc:
            return self.failure(getattr(exc, "reason", str(exc)), str(exc)[:1000], args.get("agent_id") if isinstance(args, dict) else None)
        finally:
            self.command_lock.release()

    def _submit(self, action, command, execution_epoch):
        # 新增的可选显示标签缺省时不改变旧命令身份，允许历史操作恢复/去重。
        normalized = {"action": action, **command.model_dump(exclude={"description"} if command.description is None else set())}
        fingerprint = digest(normalized)
        if any(e.type == "agent_cancel_requested" and e.payload["request_key"] == command.request_key for e in self.store.read_events()):
            raise ValueError("request_key_conflict")
        prior = next((e.payload for e in self.commands() if e.payload["request_key"] == command.request_key), None)
        if prior:
            if prior["digest"] != fingerprint:
                return self.failure("request_key_conflict")
            return prior
        self.executor.check()
        if self.stopping_reason():
            raise ExecutionInterrupted(self.stopping_reason())
        scheduler = self._scheduler(execution_epoch)
        if not scheduler.has_capacity():
            raise ValueError("queue_full")
        commands = self.commands()
        if action == "spawn":
            if command.gateway not in self.routes:
                raise ValueError("unknown_gateway_alias")
            spec = SPECS[command.spec]
            agent = {"agent_id": "agent-" + uuid.uuid4().hex[:16], "spec": asdict(spec),
                     "gateway": command.gateway}
            message, done_when, context = command.goal, command.done_when, command.context
        else:
            last = self.latest(command.agent_id)
            if last is None:
                raise ValueError("unknown_agent")
            if command.agent_id in self.closed_agents():
                raise ValueError("agent_closed")
            if not self.acked(last["run"]["run_id"]):
                raise ValueError("agent_busy")
            agent = last["agent"]
            self._check_agent(agent)
            message, done_when, context = command.message, command.done_when, ""
        self._check_agent(agent)
        run = {"run_id": "run-" + uuid.uuid4().hex[:16], "agent_id": agent["agent_id"],
               "root_turn_id": self.executor.turn_id, "message": message,
               "done_when": done_when, "context": context, "execution_epoch": execution_epoch,
               "description": command.description}
        payload = {"command_id": "command-" + uuid.uuid4().hex[:16], "action": action,
                   "request_key": command.request_key, "digest": fingerprint,
                   "agent": agent, "run": run, "execution": command.execution}
        self.emit("agent_command_intent", payload)
        return payload


    def _check_agent(self, agent):
        if agent["spec"]["name"] not in SPECS or agent["gateway"] not in self.routes:
            raise ValueError("unknown_agent_role_or_gateway")
        # 历史角色/提示的版本变化不锁死续问；权限取当前角色与父 Policy 的交集。
        agent["spec"] = asdict(SPECS[agent["spec"]["name"]])
        spec = SPECS[agent["spec"]["name"]]
        parent = self.delegation_adapter.parent_runtime
        if not spec.readonly and (parent is None or parent.command_service is None):
            raise ValueError("agent_execution_unavailable")


    def _scheduler(self, epoch):
        from codeagent.orchestration.scheduler import RunScheduler
        from codeagent.orchestration.lease import execution_resources
        if any(isinstance(resource, RunScheduler) and resource.service is not self and not resource.closed
               for resource in execution_resources(self.store)):
            raise RuntimeError("session_busy")
        if self.scheduler is None or self.scheduler.epoch != epoch or self.scheduler.closed:
            self.scheduler = RunScheduler(self, epoch)
        return self.scheduler

    def acked(self, run_id):
        return self.terminal(run_id) is not None and any(e.type == "agent_run_ack" and e.payload["run_id"] == run_id for e in self.store.read_events())

    def ack(self, run_id, epoch):
        from codeagent.orchestration.lease import assert_execution_epoch
        with self.executor.lock:
            assert_execution_epoch(self.store, epoch)
            if not self.acked(run_id):
                self.emit("agent_run_ack", {"run_id": run_id, "execution_epoch": epoch})

    def _execute_or_report(self, payload, execution_epoch):
        rid = payload["run"]["run_id"]
        scheduler = self._scheduler(execution_epoch)
        if self.terminal(rid):
            if rid not in scheduler.active:
                self.ack(rid, execution_epoch)
            return ToolResult(ok=True, content=json.dumps(self.consume(rid), ensure_ascii=False))
        scheduler.submit(payload)
        if payload.get("execution", "foreground") == "background":
            return ToolResult(ok=True, content=json.dumps({"receipt": "accepted", "command_id": payload["command_id"],
                "agent_id": payload["agent"]["agent_id"], "run_id": rid, "root_turn_id": payload["run"]["root_turn_id"],
                "status": "running" if rid in scheduler.active else "queued", "terminal_ack": False}))
        scheduler.wait([rid], "all", float("inf"))
        self.executor.check()
        return ToolResult(ok=True, content=json.dumps(self.consume(rid), ensure_ascii=False))

    def _execute_run(self, payload, execution_epoch, stop):
        if payload["run"]["root_turn_id"] != self.executor.turn_id:
            return self._failed(payload, "interrupted", "原用户轮次已结束", consume=False)
        try:
            self._check_agent(payload["agent"])
            executor = copy.copy(self.executor)
            executor.model_config = self.routes[payload["agent"]["gateway"]]
            executor.local_cancelled = stop.is_set
            executor.execution_epoch = execution_epoch
            executor.check()
            root = self.delegation_adapter.context_provider().active_root
            adapter = ChildAdapter(self, executor, payload, root, execution_epoch)
            from codeagent.orchestration.loop import AgentLoop
            AgentLoop(adapter).invoke()
        except ExecutionInterrupted as exc:
            reason = self.scheduler.root_reason if stop.is_set() and self.scheduler.root_reason else exc.reason
            self._failed(payload, "cancelled" if stop.is_set() or reason == "cancelled" else "failed", reason, consume=False)
        except Exception as exc:
            self._failed(payload, "interrupted" if isinstance(exc, UnsafeRecovery) else "failed", str(exc), consume=False)

    def _failed(self, payload, status, error, *, consume=True):
        run = payload["run"]
        if not self.terminal(run["run_id"]):
            self._close_failed_history(run, error)
            self.emit("agent_run_finished", {"agent_id": run["agent_id"], "run_id": run["run_id"],
                "status": status, "findings": "", "evidence_refs": [], "error": error,
                "limitations": ["执行未完成"], "usage_refs": [
                    e.payload["request_id"] for e in self.store.read_events() if e.type == "model_request_reserved"
                    and str(e.payload.get("operation_key", "")).startswith(run["run_id"] + ":")],})
        if consume:
            return ToolResult(ok=True, content=json.dumps(self.consume(run["run_id"]), ensure_ascii=False))

    def _close_failed_history(self, run, error):
        records = [e for e in self.store.read_events() if e.type == "agent_history" and e.payload["run_id"] == run["run_id"]]
        closed = {e.payload["data"].get("call_id") for e in records if e.payload["kind"] in {"tool_result", "tool_denied"}}
        for event in records:
            if event.payload["kind"] != "assistant_tool_calls":
                continue
            for call in event.payload["data"]["tool_calls"]:
                if call["call_id"] not in closed:
                    self.emit("agent_history", {"agent_id": run["agent_id"], "run_id": run["run_id"],
                        "step_id": event.payload["step_id"],
                        "kind": "tool_denied", "key": "recovery:" + call["call_id"],
                        "data": {"call_id": call["call_id"], "name": call["name"],
                                 "result": ToolResult(ok=False, error_code="interrupted", error=error).model_dump()}})
                    closed.add(call["call_id"])

    def compat_delegate(self, args):
        """旧批次协议映射到相同的 spawn + wait_all；图模式不启动第二套工具循环。"""
        from codeagent.runtime.delegation import DelegationRequest
        try:
            if isinstance(args, dict) and not args.get("request_key"):
                args = {**args, "request_key": ACTIVE_TOOL_OPERATION.get() or uuid.uuid4().hex}
            command = DelegationRequest.model_validate(args)
            with session_lease(self.store) as epoch:
                fingerprint = digest(command.model_dump())
                bid = "batch-" + digest({"turn": self.executor.turn_id, "key": command.request_key})[:16]
                previous = next((e for e in self.store.read_events() if e.type == "delegation_batch_started"
                                 and e.payload["batch_id"] == bid), None)
                if previous and previous.payload["digest"] != fingerprint:
                    raise ValueError("request_key_conflict")
                if not previous:
                    scheduler = self._scheduler(epoch)
                    with self.executor.lock:
                        capacity = scheduler.running_limit + scheduler.queued_limit - len(scheduler.active) - len(scheduler.queue)
                        if len(command.tasks) > capacity:
                            return ToolResult(ok=False, error_code="queue_full", error="兼容批次超出当前子 run 容量；未提交新任务")
                    self.emit("delegation_batch_started", {"batch_id": bid, "request_key": command.request_key,
                        "digest": fingerprint})
                reports = []
                for index, task in enumerate(command.tasks):
                    result = self.spawn({**task.model_dump(), "request_key": bid + ":" + str(index), "execution": "background"})
                    if not result.ok:
                        return result
                    reports.append(json.loads(result.content))
                self._scheduler(epoch).wait([r["run_id"] for r in reports], "all", float("inf"))
                tasks = []
                for receipt in reports:
                    report = self.consume(receipt["run_id"])
                    task = {"task_id": report["run_id"], "batch_id": bid, "state": report["status"],
                            "report": report["findings"], "error": None if report["status"] == "completed" else report.get("error")}
                    if not any(e.type == "delegation_task_finished" and e.payload["task_id"] == task["task_id"] for e in self.store.read_events()):
                        self.emit("delegation_task_finished", task)
                    tasks.append(task)
                    self.close({"agent_id": report["agent_id"]})
                payload = {"batch_id": bid, "tasks": tasks}
                if not any(e.type == "delegation_batch_finished" and e.payload["batch_id"] == bid for e in self.store.read_events()):
                    self.emit("delegation_batch_finished", payload)
                return ToolResult(ok=True, content=json.dumps(payload, ensure_ascii=False))
        except ValidationError as exc:
            return self.failure("invalid_arguments", str(exc))
        except (ValueError, RuntimeError, ExecutionInterrupted) as exc:
            return self.failure(getattr(exc, "reason", str(exc)), str(exc)[:1000], args.get("agent_id") if isinstance(args, dict) else None)

    def closed_agents(self):
        return {e.payload["agent_id"] for e in self.store.read_events() if e.type == "agent_session_closed"}


    def stopping_reason(self):
        events = [e for e in self.store.read_events() if e.turn_id == self.executor.turn_id]
        stop = next((e for e in reversed(events) if e.type == "agent_root_stopping"), None)
        if stop and stop.payload["reason"] not in {"slice_exhausted", "output_truncation", "interrupted", "steps", "fee", "main_reserve", "budget_limited", "closing_exhausted", "fee_bound_exceeded", "deadline"}:
            return stop.payload["reason"]
        return None


    def _validate_runs(self, run_ids):
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("duplicate_run_id")
        known = {e.payload["run"]["run_id"] for e in self.commands()}
        if any(rid not in known for rid in run_ids):
            raise ValueError("unknown_run")

    def wait(self, args):
        try:
            command = WaitCommand.model_validate(args)
            with session_lease(self.store) as epoch:
                self._validate_runs(command.run_ids)
                self.recover(epoch)
                operation = ACTIVE_TOOL_OPERATION.get()
                fingerprint = digest(command.model_dump())
                prior = next((e for e in self.store.read_events() if e.type == "agent_wait_started"
                              and operation and e.payload.get("operation_key") == operation), None)
                if prior and prior.payload["digest"] != fingerprint:
                    raise UnsafeRecovery("wait_identity_conflict")
                wait_id = prior.payload["wait_id"] if prior else "wait-" + uuid.uuid4().hex[:16]
                finished = next((e for e in self.store.read_events() if e.type == "agent_wait_finished" and e.payload["wait_id"] == wait_id), None)
                if finished:
                    return ToolResult(ok=True, content=json.dumps(finished.payload, ensure_ascii=False))
                deadline_epoch = prior.payload["deadline_epoch"] if prior else time.time() + command.timeout
                if not prior:
                    self.emit("agent_wait_started", {"wait_id": wait_id, **command.model_dump(), "digest": fingerprint,
                                                    "operation_key": operation, "deadline_epoch": deadline_epoch})
                satisfied = self._scheduler(epoch).wait(command.run_ids, command.mode,
                    time.monotonic() + max(0, deadline_epoch - time.time()))
                reports = [self.consume(rid) for rid in command.run_ids if self.acked(rid)]
                result = {"wait_id": wait_id, "run_ids": command.run_ids, "mode": command.mode,
                          "satisfied": satisfied, "timed_out": not satisfied,
                          "reports": reports, "pending": [rid for rid in command.run_ids if not self.acked(rid)]}
                self.emit("agent_wait_finished", result)
                return ToolResult(ok=True, content=json.dumps(result, ensure_ascii=False))
        except ValidationError as exc:
            return self.failure("invalid_arguments", str(exc))
        except (ValueError, RuntimeError, ExecutionInterrupted) as exc:
            return self.failure(getattr(exc, "reason", str(exc)), str(exc)[:1000], args.get("agent_id") if isinstance(args, dict) else None)

    def cancel(self, args):
        try:
            if isinstance(args, dict) and not args.get("request_key"):
                args = {**args, "request_key": ACTIVE_TOOL_OPERATION.get() or uuid.uuid4().hex}
            command = CancelCommand.model_validate(args)
            with session_lease(self.store) as epoch, self.executor.lock:
                self._validate_runs(command.run_ids)
                fingerprint = digest({"action": "cancel", **command.model_dump()})
                if any(e.payload["request_key"] == command.request_key for e in self.commands()):
                    raise ValueError("request_key_conflict")
                previous = next((e for e in self.store.read_events() if e.type == "agent_cancel_requested"
                                 and e.payload["request_key"] == command.request_key), None)
                if previous and previous.payload["digest"] != fingerprint:
                    raise ValueError("request_key_conflict")
                receipt = previous.payload if previous else {"cancel_id": "cancel-" + uuid.uuid4().hex[:16],
                    "digest": fingerprint, **command.model_dump(), "receipt": "cancel_requested"}
                if not previous:
                    self.emit("agent_cancel_requested", receipt)
                self.recover(epoch)
                self._scheduler(epoch).cancel(command.run_ids)
                return ToolResult(ok=True, content=json.dumps(receipt))
        except ValidationError as exc:
            return self.failure("invalid_arguments", str(exc))
        except (ValueError, RuntimeError) as exc:
            return self.failure(str(exc), str(exc)[:1000], args.get("agent_id") if isinstance(args, dict) else None)

    def close(self, args):
        try:
            if isinstance(args, dict):
                args = {k: v for k, v in args.items() if k != "expected_last_run_id"}
            command = CloseCommand.model_validate(args)
            with session_lease(self.store), self.executor.lock:
                latest = self.latest(command.agent_id)
                if latest is None:
                    raise ValueError("unknown_agent")
                if not self.acked(latest["run"]["run_id"]):
                    raise ValueError("agent_busy")
                if command.agent_id not in self.closed_agents():
                    self.emit("agent_session_closed", command.model_dump())
                return ToolResult(ok=True, content=json.dumps({"agent_id": command.agent_id, "status": "closed"}))
        except ValidationError as exc:
            return self.failure("invalid_arguments", str(exc))
        except (ValueError, RuntimeError) as exc:
            return self.failure(str(exc), str(exc)[:1000], args.get("agent_id") if isinstance(args, dict) else None)

    def recover(self, epoch):
        scheduler = self._scheduler(epoch)
        for event in self.commands():
            payload, rid = event.payload, event.payload["run"]["run_id"]
            if rid in scheduler.stops or self.acked(rid):
                continue
            if self.terminal(rid):
                self.ack(rid, epoch)
                continue
            if payload.get("execution") != "background":
                continue
            if self.delegation_adapter.parent_runtime is not None:
                # 普通Runtime已收敛为同步task；恢复旧轮次也不能重新引入后台写入。
                self._failed(payload, "interrupted", "后台委派已停用；保留现场与历史，不自动续跑", consume=False)
                self.ack(rid, epoch)
                continue
            cancelled = any(e.type == "agent_cancel_requested" and rid in e.payload["run_ids"] for e in self.store.read_events())
            if (cancelled or scheduler.root_reason or self.stopping_reason()
                    or payload["run"]["root_turn_id"] != self.executor.turn_id):
                self._failed(payload, "cancelled" if cancelled else "interrupted", "recovered stopping run", consume=False)
                self.ack(rid, epoch)
            else:
                try:
                    self.executor.check()
                    scheduler.submit(payload)
                except ExecutionInterrupted as exc:
                    self._failed(payload, "timed_out" if exc.reason == "deadline" else "interrupted", exc.reason, consume=False)
                    self.ack(rid, epoch)

    def drain(self, epoch, *, cancel=False, reason="cancelled"):
        if self.scheduler is None or self.scheduler.epoch != epoch or self.scheduler.closed:
            return
        scheduler = self.scheduler
        if cancel:
            scheduler.stop_root(reason)
        scheduler.shutdown(cancel=cancel)

    def finish_parent(self, epoch):
        # 模型正常结束就结束；后台工作先取消并回收，不额外向模型索取整合或失败确认。
        self.drain(epoch, cancel=True, reason="parent_finished")


    def consume(self, run_id):
        payload = next((e.payload for e in self.commands() if e.payload["run"]["run_id"] == run_id), None)
        if payload is None:
            raise ValueError("unknown_run")
        report = self.terminal(run_id)
        if report is None:
            raise ValueError("run_pending")
        return {**report, "command_id": payload["command_id"],
                "root_turn_id": payload["run"]["root_turn_id"], "terminal_ack": self.acked(run_id)}

    def validity_message(self):
        states = []
        for event in self.commands():
            run = event.payload["run"]
            if run["root_turn_id"] != self.executor.turn_id:
                continue
            terminal = self.terminal(run["run_id"])
            states.append({"agent_id": run["agent_id"], "run_id": run["run_id"],
                "status": terminal["status"] if terminal else "pending", "terminal_ack": self.acked(run["run_id"])})
        return [{"role": "system", "content": "当前委派现场；子观察来自共享工作目录，文件可能在观察后变化：" + json.dumps(states)}] if states else []


class ChildAdapter(ContextPreparation):
    purpose = "readonly_child"

    def __init__(self, service, executor, payload, root, execution_epoch):
        self.service, self.store, self.executor = service, service.store, executor
        self.agent, self.run = payload["agent"], dict(payload["run"])
        self.agent_id, self.run_id = self.run["agent_id"], self.run["run_id"]
        self.execution_epoch = execution_epoch
        self.iteration = 0
        self.spec = SPECS[self.agent["spec"]["name"]]
        self.purpose = "readonly_child" if self.spec.readonly else "worker_child"
        from dataclasses import replace
        child_context = replace(WorkspaceContext.source(root), denied_paths=self.store.workspace_context().denied_paths)
        parent = service.delegation_adapter.parent_runtime
        backend = getattr(parent, "workspace_backend", None)
        model_root = getattr(backend, "model_root", str(root))
        self.registry = ToolRegistry(child_context, allowed_names=frozenset(self.spec.tool_names),
                                     readonly=self.spec.readonly, workspace_backend=backend)
        self.policy = ChildRolePolicy(self.spec, service.delegation_adapter.parent_policy)
        self.approval_gate = parent.approval_gate if parent else None
        from codeagent.orchestration.context import ScopedHistoryStore, ChildOperationStore, child_system_prompt
        self.session_store = ScopedHistoryStore(self.store, self.run, self.emit)
        operations = ChildOperationStore(self.store, self.run, self.emit)
        from codeagent.tools.git_read import build_git_tools
        candidates = (backend.build_read_tools(child_context) if backend else
                      build_fs_tools(child_context) + build_git_tools(child_context))
        self.command_service = None
        if not self.spec.readonly:
            from codeagent.runtime.command_service import CommandService
            from codeagent.runtime.artifacts import CommandArtifactStore
            from codeagent.tools.command import build_command_tool
            from codeagent.runtime.command import ScopedCommandExecutor
            command_executor = ScopedCommandExecutor(parent.command_service.executor,
                lambda: bool(executor.local_cancelled and executor.local_cancelled()))
            self.command_service = CommandService(child_context, self.approval_gate, command_executor,
                                                  operations, CommandArtifactStore(operations))
            candidates.append(build_command_tool(self.command_service))
            if self.spec.name == "general-purpose":
                from codeagent.runtime.atomic_edit import AtomicEditService
                from codeagent.tools.atomic_edit import build_edit_file_tool, build_atomic_edit_tool
                editor = (backend.edit_service(child_context, operations) if backend else
                          AtomicEditService(child_context, operations))
                candidates += [build_edit_file_tool(editor), build_atomic_edit_tool(editor)]
                if parent.config.task_tracking:
                    from codeagent.runtime.task_plan import TaskPlanService
                    from codeagent.tools.task_plan import build_task_plan_tools
                    candidates += build_task_plan_tools(TaskPlanService(self.session_store))
        from codeagent.tools.observations import build_observation_tool
        candidates.append(build_observation_tool(self.store, agent_id=self.agent_id))
        parent_names = {t.name for t in parent.tools.list_tools()} if parent else None
        for tool in candidates:
            decision = self.policy.evaluate(tool).decision
            if (tool.name in self.spec.tool_names and (parent_names is None or tool.name in parent_names)
                    and (decision == PolicyDecision.ALLOW or
                         decision in {PolicyDecision.ASK, PolicyDecision.REQUIRE_APPROVAL} and self.approval_gate is not None)):
                self.registry.register(tool)
        self.execution = ModelToolExecution(self.store, executor, self.emit)
        from codeagent.context.builder import ContextManager
        from codeagent.context.models import ContextState
        from codeagent.runtime.execution_service import JournaledCondenser
        self.model_config = executor.model_config
        self.max_tokens = self.model_config.resolved_max_tokens
        self.model = JournaledCondenser(self.execution, self.run_id, self.agent_id)
        self.context_manager = ContextManager(self.session_store, self.registry,
            state_provider=lambda: ContextState(model_root, {
                **({"default_cwd_resolves_to": model_root, "project_file_operations": "same_container"} if backend else {}),
                **({"evaluation_rules": backend.evaluation_rules} if getattr(backend, 'evaluation_rules', None) else {}),
                **(self.command_service.environment_snapshot() if self.command_service else {}),
                "readonly": self.spec.readonly, "agent_type": self.spec.name}),
            system_prompt_provider=lambda names: child_system_prompt(self.agent, self.run, self.store,
                                                                     workspace=model_root))
        self.history("user_message", {"message": json.dumps({"goal": self.run["message"], "done_when": self.run["done_when"],
                                                            "context": self.run["context"]}, ensure_ascii=False)}, "anchor")

    def emit(self, kind, payload):
        from codeagent.orchestration.lease import assert_execution_epoch
        assert_execution_epoch(self.store, self.execution_epoch)
        return self.service.emit(kind, {**payload, "execution_epoch": self.execution_epoch})


    def check(self):
        self.executor.check()

    def history(self, kind, data, key, step_id=None):
        prior = next((e for e in self.store.read_events() if e.type == "agent_history" and e.payload["run_id"] == self.run_id and e.payload["key"] == key), None)
        if prior:
            return prior
        return self.emit("agent_history", {"agent_id": self.agent_id, "run_id": self.run_id,
            "kind": kind, "data": data, "key": key, "step_id": step_id})

    def prepare(self):
        from codeagent.context.models import ContextBudgetExceeded
        controls = ({"role": "system", "content": (
            "上一次响应达到输出长度上限，该响应中的工具没有执行，报告也未交付。"
            "请利用已有观察，直接给出简短完整的结论、关键代码位置与未解决项；"
            "不要重写长报告或重复已有调查。确实缺少证据时，只请求一个角色允许的必要操作。"
        )},) if self._consecutive_truncations() else ()
        errors = self._protocol_errors()
        if errors:
            controls += ({"role": "system", "content": protocol_repair_message(errors[0])},)
        prepared = self._prepare_context(self.model_config.context_capabilities(), controls)
        if isinstance(prepared, ContextBudgetExceeded):
            return {"status": "failed", "findings": "", "error": prepared.reason}
        return prepared.messages


    def _protocol_errors(self, before_iteration=None):
        errors = []
        for event in reversed(self.store.read_events()):
            p = event.payload
            if event.type != "agent_history" or p["run_id"] != self.run_id or not p["key"].startswith("response:"):
                continue
            if before_iteration is not None and int(p["key"].split(":")[1]) >= before_iteration:
                continue
            if p["kind"] != "model_protocol_error":
                break
            errors.append(p["data"])
        return errors

    def _consecutive_truncations(self, before_iteration=None):
        count = 0
        for event in reversed(self.store.read_events()):
            p = event.payload
            if event.type != "agent_history" or p["run_id"] != self.run_id or not p["key"].startswith("response:"):
                continue
            if before_iteration is not None and int(p["key"].split(":")[1]) >= before_iteration:
                continue
            if p["kind"] != "model_output_truncated":
                break
            count += 1
        return count

    @property
    def request_reasoning_effort(self):
        return (recovery_reasoning_effort(self.executor.model_config, recovering=bool(self._consecutive_truncations()))
                or self.executor.model_config.resolved_reasoning_effort)


    def record_response(self, response, iteration):
        if output_truncation_reason(response, self.max_tokens):
            # 先保存截断事实；半截正文/调用不投影成已完成的报告或工具交换。
            count = self._consecutive_truncations(before_iteration=iteration) + 1
            self.history("model_output_truncated", {
                "message": response.text or "", "reasoning_content": response.reasoning_content,
                "finish_reason": response.finish_reason, "consecutive_count": count,
                "max_consecutive_output_truncations": MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS,
            }, "response:" + str(iteration))
            if count >= MAX_CONSECUTIVE_OUTPUT_TRUNCATIONS:
                return {"route": "finish", "output": {"status": "failed", "error":
                    f"output truncated {count} consecutive times; no complete report"}}
            return {"route": "next"}
        if response.protocol_error:
            errors = self._protocol_errors(before_iteration=iteration)
            stopped = protocol_repair_exhausted(response.protocol_error, errors)
            self.history("model_protocol_error", {**response.protocol_error, "attempt": len(errors) + 1},
                         "response:" + str(iteration))
            if stopped:
                return {"route": "finish", "output": {"status": "failed", "error":
                    "repeated malformed tool arguments; no calls from invalid responses executed"}}
            return {"route": "next"}
        if len({c.call_id for c in response.tool_calls}) != len(response.tool_calls):
            raise ValueError("invalid child tool call ids/count")
        # 每 run 的所有 call_id 唯一；续问可复用 provider ID，因为 turn_id 不同。
        previous = {c["call_id"] for e in self.store.read_events() if e.type == "agent_history"
                    and e.payload["run_id"] == self.run_id and e.payload["kind"] == "assistant_tool_calls"
                    and e.payload["key"] != "response:" + str(iteration) for c in e.payload["data"]["tool_calls"]}
        if previous.intersection(c.call_id for c in response.tool_calls):
            raise ValueError("duplicate child tool call_id")
        if response.tool_calls:
            self.history("assistant_tool_calls", {"message": response.text or "", "tool_calls": [c.model_dump() for c in response.tool_calls],
                         "reasoning_content": response.reasoning_content}, "response:" + str(iteration), self.run_id + ":" + str(iteration))
            return {"route": "tools"}
        self.history("assistant_message", {"message": response.text or ""}, "response:" + str(iteration))
        return {"route": "finish", "output": {"status": "completed" if response.text else "failed", "findings": response.text or "",
                **({"error": "empty child response"} if not response.text else {})}}

    def execute(self, call, key):
        prior = next((e.payload["data"]["result"] for e in self.store.read_events()
                      if e.type == "agent_history" and e.payload["run_id"] == self.run_id
                      and e.payload["kind"] == "tool_result" and e.payload["key"] == key), None)
        try:
            tool = self.registry.get(call.name)
            safe = tool.permission_level in {PermissionLevel.READ, PermissionLevel.SESSION_WRITE}
        except KeyError:
            safe = True
        try:
            result = self.execution.tool(key, call, lambda c: prior if prior is not None else self._call(c),
                                         readonly=safe or prior is not None)
        except UnsafeRecovery:
            self.store.mark_recovery_required("子工具intent无可靠result，禁止自动重放命令或编辑，请检查现场")
            raise
        self.history("tool_result", {"call_id": call.call_id, "name": call.name, "arguments": call.arguments, "result": result}, key,
                     self.run_id + ":" + key.split(":tool:", 1)[1].split(":")[0])

    def _call(self, call):
        try:
            self.registry.get(call.name)
        except KeyError:
            return ToolResult(ok=False, error_code="policy_denied", error="子角色不提供该工具；只使用实际工具，不能绕过角色权限。").model_dump()
        def record(kind, payload):
            if kind in {"approval_requested", "approval_decision"}:
                self.emit(kind, {**payload, "agent_id": self.agent_id, "run_id": self.run_id})
        _, result = ModelToolExecution.dispatch(self.registry, self.policy, self.approval_gate, call, record)
        return result.model_dump()

    def finish(self, output):
        if self.service.terminal(self.run_id):
            return self.service.terminal(self.run_id)
        evidence = [{"event_ref": "seq:" + str(e.seq)} for e in self.store.read_events()
            if e.type == "agent_history" and e.payload["run_id"] == self.run_id
            and e.payload["kind"] == "tool_result"]
        usages = [e.payload["request_id"] for e in self.store.read_events() if e.type == "model_request_reserved"
                  and str(e.payload.get("operation_key", "")).startswith(self.run_id + ":")]
        report = {"agent_id": self.agent_id, "run_id": self.run_id, "status": output.get("status", "failed"),
                  "findings": output.get("findings", ""), "evidence_refs": evidence,
                  **({"error": output["error"]} if output.get("error") else {}),
                  "subagent": self.spec.name,
                  "limitations": ["只读调查；历史观察可能与当前文件不同" if self.spec.readonly else
                                  "共享当前项目；报告仅描述实际执行与观察，不绑定代码版本"], "usage_refs": usages}
        self.emit("agent_run_finished", report)
        return report
