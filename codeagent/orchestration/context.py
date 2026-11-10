"""子会话只负责选择私有历史与角色提示；装配和压缩使用公共 ContextManager。"""
from __future__ import annotations

import json
from importlib.resources import files

from codeagent.session.events import SessionEvent
from codeagent.skills.builtin import render_behavior_contract_skill


class ScopedHistoryStore:
    """把同一日志中的子会话事件映射为公共历史格式，保留原始 seq 供回读。"""

    def __init__(self, store, run, emit):
        self.store, self.run, self.emit = store, run, emit
        self.meta_path, self.session_dir = store.meta_path, store.session_dir
        self.session_id = store.session_id

    def read_meta(self):
        return self.store.read_meta()

    def read_events(self):
        result = []
        for event in self.store.read_events():
            payload = event.payload
            if payload.get("agent_id") != self.run["agent_id"]:
                continue
            if event.type == "agent_history":
                result.append(SessionEvent(seq=event.seq, ts=event.ts, turn_id=payload["run_id"],
                    model_step_id=payload.get("step_id"), type=payload["kind"], payload=payload["data"]))
            elif event.type in {"agent_context_condensed", "agent_context_condensation_attempt", "agent_task_plan_updated"}:
                result.append(SessionEvent(seq=event.seq, ts=event.ts, turn_id=payload.get("run_id"),
                    type=event.type.removeprefix("agent_"), payload=payload))
        return result

    def append_derived_events(self, items, *, turn_id=None):
        return [self.emit("agent_" + kind, {**payload, "agent_id": self.run["agent_id"],
            "run_id": self.run["run_id"]}) for kind, payload in items]


class ChildOperationStore:
    """操作产物沿用Session存储，记录角色归属；不混入主方模型工具交换。"""
    def __init__(self, store, run, emit):
        self.store, self.run, self.emit = store, run, emit

    def __getattr__(self, name):
        return getattr(self.store, name)

    def append_event(self, kind, payload=None):
        return self.emit(kind, {**(payload or {}), "agent_id": self.run["agent_id"],
                                "run_id": self.run["run_id"]})


def child_system_prompt(agent, run, store, *, workspace=None):
    from codeagent.orchestration.profiles import SPECS
    spec = SPECS[agent["spec"]["name"]]
    return (files("codeagent.prompts").joinpath(spec.prompt_file).read_text(encoding="utf-8")
        + render_behavior_contract_skill("readonly_child" if spec.readonly else "worker_child", delegation=spec.readonly)
        + "\n你只收到显式子任务和自己的历史，不继承父对话；不递归委派。"
        + "\n本次角色：" + agent["spec"]["name"] + "。子任务现场：\n"
        + json.dumps({"agent_id": run["agent_id"], "run_id": run["run_id"],
            "workspace": workspace or str(store.workspace_root),
            "history_notice": "先前观察是历史，文件可能已经变化；需要当前事实时重新读取。"}, ensure_ascii=False))
