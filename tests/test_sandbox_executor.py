import os
from dataclasses import replace
from pathlib import Path

from codeagent.runtime.artifacts import CommandArtifactStore
from codeagent.runtime.bubblewrap import BubblewrapFeatures
from codeagent.runtime.command import CommandRequest, SandboxExecutionRequest
from codeagent.runtime.command_environment import COMMAND_ENVIRONMENT_REVISION, build_command_environment
from codeagent.runtime.bubblewrap import SandboxUnavailable
from codeagent.runtime.permissions import EffectiveSandboxPolicy, NetworkMode
from codeagent.runtime.sandbox_executor import SandboxedCommandExecutor
from test_candidate_loop import execution_session


class HostFixtureBackend:
    def __init__(self):
        self.environments = []

    def probe(self, candidate_root):
        return BubblewrapFeatures(Path("/fixture/bwrap"), "fixture", False)

    def invocation(self, features, plan, policy, cwd, environment, payload):
        self.environments.append((cwd, dict(environment), policy))
        return ["/usr/bin/env", "-i", *(f"{name}={value}" for name, value in environment.items()), *payload]


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


def test_environment_filters_host_values_and_applies_runtime_overrides(tmp_path):
    _, store, context = execution_session(tmp_path)
    sandbox_request = request(context, "true")
    artifacts = CommandArtifactStore(store).prepare(sandbox_request.command)
    environment = build_command_environment(
        launch_cwd=context.active_root, runtime_home=artifacts.runtime_home,
        policy=sandbox_request.policy,
        host_environment={"PATH": "/custom/bin:/usr/bin", "LANG": "C.UTF-8", "API_KEY": "secret", "UNKNOWN": "x"},
    )

    values = environment.process_environment
    assert values["PATH"] == "/custom/bin:/usr/bin"
    assert values["LANG"] == "C.UTF-8"
    assert "API_KEY" not in values and "UNKNOWN" not in values
    assert values["HOME"] == str(artifacts.runtime_home)
    assert values["TMPDIR"] == "/tmp"
    assert values["XDG_CACHE_HOME"] == str(artifacts.runtime_home / "cache")
    assert environment.contract_revision == COMMAND_ENVIRONMENT_REVISION


def test_executor_launch_and_provenance_use_same_effective_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/contract/bin:/usr/bin")
    monkeypatch.setenv("CODEAGENT_SECRET", "must-not-enter")
    _, store, context = execution_session(tmp_path)
    backend = HostFixtureBackend()
    sandbox_request = request(context, "printf '%s' \"$PATH\"")
    artifacts = CommandArtifactStore(store).prepare(sandbox_request.command)

    result = SandboxedCommandExecutor(backend).execute(sandbox_request, context, artifacts)

    launch_cwd, launched, _ = backend.environments[-1]
    assert result.stdout == "/contract/bin:/usr/bin"
    assert result.launch_cwd == str(launch_cwd) == str(context.active_root)
    assert result.environment_contract_revision == COMMAND_ENVIRONMENT_REVISION
    assert tuple(result.environment_names) == tuple(sorted(launched))
    assert "CODEAGENT_SECRET" not in launched
    assert result.backend == "bubblewrap"
    assert result.effective_network_policy == "off"


def test_each_command_uses_a_fresh_shell(tmp_path):
    _, store, context = execution_session(tmp_path)
    executor = SandboxedCommandExecutor(HostFixtureBackend())
    first = request(context, "export CODEAGENT_EPHEMERAL=1; cd /; alias ephemeral=true; ephemeral() { :; }")
    second = request(context, "printf '%s|%s|%s|%s' \"${CODEAGENT_EPHEMERAL-unset}\" \"$PWD\" \"$(type -t ephemeral || true)\" \"$(alias ephemeral 2>/dev/null || true)\"")
    second = replace(second, command=replace(second.command, execution_id="exec-2"))

    first_result = executor.execute(first, context, CommandArtifactStore(store).prepare(first.command))
    second_result = executor.execute(second, context, CommandArtifactStore(store).prepare(second.command))

    assert first_result.exit_code == 0
    assert second_result.stdout == f"unset|{context.active_root}||"


def test_command_not_found_keeps_generic_stderr_and_environment_provenance(tmp_path):
    _, store, context = execution_session(tmp_path)
    sandbox_request = request(context, "codeagent-command-that-does-not-exist")
    artifacts = CommandArtifactStore(store).prepare(sandbox_request.command)

    result = SandboxedCommandExecutor(HostFixtureBackend()).execute(sandbox_request, context, artifacts)

    assert result.exit_code == 127
    assert "command not found" in result.stderr
    assert result.environment_contract_revision == COMMAND_ENVIRONMENT_REVISION
    assert result.effective_filesystem_policy["policy_revision"] == "v1"


def test_environment_snapshot_reports_unavailable_backend_without_creating_runtime_dirs(tmp_path):
    class MissingBackend:
        def discover(self, _candidate_root):
            raise SandboxUnavailable("missing")

    _, store, context = execution_session(tmp_path)
    artifacts = CommandArtifactStore(store)
    policy = request(context, "true").policy
    snapshot = SandboxedCommandExecutor(MissingBackend()).environment_snapshot(context, artifacts, policy)

    assert snapshot["backend"] == "bubblewrap"
    assert snapshot["backend_availability"] == "unavailable"
    assert snapshot["default_cwd_resolves_to"] == str(context.active_root)
    assert not artifacts.runtime_root.exists()
