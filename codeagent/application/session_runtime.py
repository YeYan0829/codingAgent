from __future__ import annotations

from codeagent.config import ModelConfig
from codeagent.config import RuntimeConfig
from codeagent.model_gateway.factory import build_model_client
from codeagent.runtime.approval import ApprovalGate
from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.runner import AgentRunner
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from codeagent.session.store import SessionStore
from codeagent.tools.fs_read import build_fs_tools
from codeagent.tools.git_read import build_git_tools
from codeagent.tools.registry import ToolRegistry
from codeagent.workspace.workspace import WorkspaceContext


def build_registry_for_context(context: WorkspaceContext) -> ToolRegistry:
    registry = ToolRegistry(workspace_context=context)
    for tool in build_fs_tools(context) + build_git_tools(context):
        registry.register(tool)
    return registry


def build_agent_runner(
    store: SessionStore,
    *,
    model_config: ModelConfig,
    approval_gate: ApprovalGate,
    execution_allowed: bool = True,
    progress_callback=None,
    model_factory=None,
    runtime_config: RuntimeConfig | None = None,
    cancellation_callback=None,
    gateway_routes=None,
) -> AgentRunner:
    """在当前项目组装核心Runtime；文件编辑与受控命令可直接使用。"""
    from codeagent.safety.repository_access import assert_session_access
    assert_session_access(store)
    from codeagent.orchestration.lease import session_lease
    with session_lease(store):
        recover_lifecycle_transition(store)
        context = store.workspace_context()
        execution = execution_allowed
        return AgentRunner(
            session_store=store,
            model=(model_factory or build_model_client)(model_config),
            tools=build_registry_for_context(context),
            approval_gate=approval_gate,
            model_config=model_config,
            config=runtime_config,
            workspace_context=context,
            command_executor=SandboxedCommandExecutor() if execution else None,
            command_artifact_store=CommandArtifactStore(store) if execution else None,
            execution_allowed=execution_allowed,
            progress_callback=progress_callback,
            cancellation_callback=cancellation_callback,
            model_factory=model_factory or build_model_client,
            gateway_routes=gateway_routes,
        )


def recover_lifecycle_transition(store: SessionStore) -> None:
    """运行时打开当前项目；旧隔离目录和其中尚未采纳的修改保持原样。"""
    if not store.workspace_root.is_dir():
        return  # 读取历史不依赖任何旧工作目录存在。
    store.open_current_workspace()
    from codeagent.runtime.atomic_edit import AtomicEditService, AtomicEditError
    try:
        AtomicEditService(store.workspace_context(), store).ensure_recovered()
    except AtomicEditError:
        pass  # 具体事务已标记recovery_required；只读调查仍可使用。
