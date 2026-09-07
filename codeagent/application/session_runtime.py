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
from codeagent.workspace.workspace import SessionWorkspaceState, WorkspaceContext


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
) -> AgentRunner:
    """复用现有 Runner 装配，不改变 Session/Workspace/Candidate 语义。"""
    context = store.workspace_context()
    execution = context.workspace_kind == "git_worktree" and execution_allowed
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
        dynamic_workspace=not execution and store.workspace_state() == SessionWorkspaceState.SOURCE_ONLY,
        progress_callback=progress_callback,
        cancellation_callback=cancellation_callback,
    )
