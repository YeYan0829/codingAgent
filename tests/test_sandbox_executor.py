import os
from pathlib import Path

from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.bubblewrap import BubblewrapFeatures
from codeagent.runtime.command import CommandRequest, SandboxExecutionRequest
from codeagent.runtime.permissions import EffectiveSandboxPolicy, NetworkMode
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from test_candidate_loop import execution_session


class HostFixtureBackend:
    def probe(self, candidate_root):
        return BubblewrapFeatures(Path("/fixture/bwrap"), "fixture", False)

    def invocation(self, features, plan, policy, cwd, environment, payload):
        return payload


def request(context, command, timeout=5):
    raw = CommandRequest("exec", command, ".", timeout, "utility", (), context.workspace_revision,
                         context.base_commit, 0, "tree", "hash")
    cache = context.active_root.parent / "cache"
    cache.mkdir(exist_ok=True)
    policy = EffectiveSandboxPolicy("v1", context.active_root, context.source_root, cache, (), (), (), NetworkMode.OFF)
    return SandboxExecutionRequest(raw, policy)


def test_fixed_inner_bash_supports_real_shell_syntax(tmp_path):
    _, store, context = execution_session(tmp_path)
    artifacts = CommandArtifactStore(store).prepare(request(context, "x").command)
    command = "VALUE='hello world'; printf '%s\\n' \"$VALUE\" | tr a-z A-Z > result.txt && cat result.txt"
    result = SandboxedCommandExecutor(HostFixtureBackend()).execute(request(context, command), context, artifacts)
    assert result.exit_code == 0 and result.payload_started
    assert result.stdout.strip() == "HELLO WORLD"
    assert (context.active_root / "result.txt").read_text() == "HELLO WORLD\n"


def test_timeout_terminates_process_group(tmp_path):
    _, store, context = execution_session(tmp_path)
    sandbox_request = request(context, "sleep 30", timeout=1)
    artifacts = CommandArtifactStore(store).prepare(sandbox_request.command)
    result = SandboxedCommandExecutor(HostFixtureBackend()).execute(sandbox_request, context, artifacts)
    assert result.status.value == "execution_timed_out"
    assert result.timed_out and result.process_tree_stopped


def test_output_is_capped_while_pipe_is_drained(tmp_path):
    _, store, context = execution_session(tmp_path)
    sandbox_request = request(context, "python3 -c 'print(\"x\" * 3000000)'", timeout=10)
    artifacts = CommandArtifactStore(store).prepare(sandbox_request.command)
    result = SandboxedCommandExecutor(HostFixtureBackend()).execute(sandbox_request, context, artifacts)
    assert result.exit_code == 0 and result.stdout_truncated
    assert artifacts.stdout_path.stat().st_size == SandboxedCommandExecutor.OUTPUT_LIMIT_BYTES
