"""显式用户子会话命令的 Runtime／CLI 共用入口，不依赖交互式模型猜测 ID。"""
import json

from codeagent.config import ModelConfig, RuntimeConfig
from codeagent.model_gateway.factory import build_model_client
from codeagent.orchestration.agents import AgentSessions, SpawnCommand, SendCommand, WaitCommand, CancelCommand, CloseCommand
from codeagent.orchestration.lease import session_lease
from codeagent.runtime.delegation import DelegationService
from codeagent.runtime.request_executor import RequestExecutor
from codeagent.runtime.execution_service import digest
from codeagent.runtime.turn_state import current_turn_state


def execute_agent_command(store, action, arguments, *, model_factory=None):
    schema = {"spawn": SpawnCommand, "send": SendCommand, "wait": WaitCommand, "cancel": CancelCommand, "close": CloseCommand}.get(action)
    if schema is None:
        raise ValueError("action must be spawn, send, wait, cancel or close")
    command = schema.model_validate(arguments)
    with session_lease(store) as epoch:
        meta = store.read_meta()
        config = RuntimeConfig.from_options(meta.get("runtime_options") or {})
        normalized = {"action": action, **command.model_dump()}
        key = getattr(command, "request_key", None) or digest(normalized)
        if hasattr(command, "request_key") and command.request_key is None:
            command = command.model_copy(update={"request_key": key})
        previous = next((e for e in store.read_events() if e.type == "user_message"
                         and e.payload.get("cli_agent_command", {}).get("request_key") == key), None)
        if previous and previous.payload["cli_agent_command"]["digest"] != digest(normalized):
            raise ValueError("request_key_conflict")
        if not previous:
            if not current_turn_state(store.read_events()).closed:
                raise ValueError("请先继续或终止未完成 UserTurn")
            started = store.append_event("user_message", {"message": json.dumps(normalized, ensure_ascii=False),
                "cli_agent_command": {"request_key": key, "digest": digest(normalized)}})
        owned_turn_id = previous.turn_id if previous else started.turn_id
        options = meta.get("model_options") or {}
        model_config = ModelConfig(provider=meta.get("provider", "fake"), model=meta.get("model"),
            temperature=options.get("temperature"), max_tokens=options.get("max_tokens"),
            summary_max_tokens=options.get("summary_max_tokens"),
            reasoning_enabled=options.get("reasoning_enabled", False), reasoning_effort=options.get("reasoning_effort"))
        executor = RequestExecutor(store, config, model_config, model_factory or build_model_client, lambda: False)
        legacy = DelegationService(store, executor, store.workspace_context)
        sessions = AgentSessions(store, executor, legacy)
        try:
            if current_turn_state(store.read_events()).turn_id == owned_turn_id:
                sessions.recover(epoch)
            result = getattr(sessions, action)(command.model_dump())
            if current_turn_state(store.read_events()).turn_id == owned_turn_id:
                sessions.finish_parent(epoch)
        finally:
            if current_turn_state(store.read_events()).turn_id == owned_turn_id:
                sessions.drain(epoch, cancel=True)
        current = current_turn_state(store.read_events())
        if current.turn_id == owned_turn_id and not current.closed:
            store.append_event("assistant_message", {"message": result.model_dump_json(), "cli_agent_command_result": key})
        return result
