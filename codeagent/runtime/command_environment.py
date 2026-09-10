from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from codeagent.runtime.permissions import EffectiveSandboxPolicy


COMMAND_ENVIRONMENT_REVISION = "command-environment-v1"
COMMAND_SHELL = ("/bin/bash", "--noprofile", "--norc")
INHERITED_ENV_NAMES = frozenset({"PATH", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"})
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"


def command_shell_options(purpose: str) -> tuple[str, ...]:
    """返回内层命令 shell 选项；仅 validation 启用 pipefail。"""
    return ("-o", "pipefail") if purpose == "validation" else ()


@dataclass(frozen=True)
class EffectiveCommandEnvironment:
    """命令启动配置及其 provenance 的单一事实来源；不解释命令语义。"""

    contract_revision: str
    backend: str
    backend_availability: str
    shell: tuple[str, ...]
    launch_cwd: Path
    variables: tuple[tuple[str, str], ...]
    path_source: str
    fresh_shell: bool
    policy: EffectiveSandboxPolicy

    @property
    def process_environment(self) -> dict[str, str]:
        return dict(self.variables)

    def snapshot(self) -> dict:
        values = self.process_environment
        return {
            "contract_revision": self.contract_revision,
            "backend": self.backend,
            "backend_availability": self.backend_availability,
            "shell": " ".join(self.shell),
            "default_cwd": ".",
            "default_cwd_resolves_to": str(self.launch_cwd),
            "path": values["PATH"].split(":"),
            "path_source": self.path_source,
            "environment_names": sorted(values),
            "home_kind": "private_runtime_home",
            "tilde_is_host_home": False,
            "tmp_kind": "private_tmp",
            "network_mode": self.policy.network_mode.value,
            "commands_are_fresh_processes": self.fresh_shell,
            "shell_state_persists": False,
            "source_workspace_access": "read_only",
            "active_workspace_access": "read_write",
        }

    def provenance(self) -> dict:
        policy = self.policy.summary()
        return {
            "environment_contract_revision": self.contract_revision,
            "backend": self.backend,
            "backend_availability": self.backend_availability,
            "launch_cwd": str(self.launch_cwd),
            "environment_names": tuple(sorted(dict(self.variables))),
            "effective_network_policy": policy["network_mode"],
            "effective_filesystem_policy": {
                key: policy[key] for key in (
                    "policy_revision", "read_grants", "write_grants", "hard_deny_paths",
                    "protected_readonly_paths",
                )
            },
        }


def build_command_environment(
    *, launch_cwd: Path, runtime_home: Path, policy: EffectiveSandboxPolicy,
    backend: str = "bubblewrap", backend_availability: str = "available",
    host_environment: Mapping[str, str] | None = None,
    create_directories: bool = True,
) -> EffectiveCommandEnvironment:
    host = os.environ if host_environment is None else host_environment
    values = {name: value for name, value in host.items() if name in INHERITED_ENV_NAMES}
    values.setdefault("PATH", DEFAULT_PATH)
    cache = runtime_home / "cache"
    if create_directories:
        cache.mkdir(parents=True, exist_ok=True)
    values.update({
        "HOME": str(runtime_home), "TMPDIR": "/tmp", "XDG_CACHE_HOME": str(cache),
        "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
    })
    return EffectiveCommandEnvironment(
        COMMAND_ENVIRONMENT_REVISION, backend, backend_availability, COMMAND_SHELL,
        launch_cwd.resolve(), tuple(sorted(values.items())),
        "filtered_host_environment" if "PATH" in host else "runtime_default", True, policy,
    )
