from __future__ import annotations

import json
import shutil
from pathlib import Path

from codeagent.runtime.command import CommandArtifactPaths, CommandRequest
from codeagent.session.store import SessionStore


class CommandArtifactStore:
    """命令运行期临时材料；仅失败诊断长期保留。"""

    def __init__(self, session_store: SessionStore) -> None:
        self.root = session_store.diagnostics_dir / "commands"
        # 子进程会继续在 HOME/TMPDIR 下创建任意深度的目录。runtime 与
        # 长期 command artifact 分离，便于后续独立设置清理策略。
        # 使用 session-root 下的短路径，同时保留 session/command 隔离。
        self.runtime_root = session_store.session_root / "_runtime" / session_store.session_id

    def prepare(self, spec: CommandRequest) -> CommandArtifactPaths:
        command_id = spec.execution_id
        command_dir = self.root / command_id
        command_dir.mkdir(parents=True, exist_ok=False)
        command_runtime_dir = self.runtime_root / command_id[:12]
        command_runtime_dir.mkdir(parents=True, exist_ok=False)
        paths = CommandArtifactPaths(
            command_dir=command_dir,
            request_path=command_dir / "request.json",
            result_path=command_dir / "result.json",
            stdout_path=command_dir / "stdout.log",
            stderr_path=command_dir / "stderr.log",
            workspace_change_path=command_dir / "workspace-change.patch",
            runtime_home=command_runtime_dir / "home",
            runtime_temp=command_runtime_dir / "tmp",
        )
        paths.runtime_home.mkdir(parents=True)
        paths.runtime_temp.mkdir(parents=True)
        paths.stdout_path.touch()
        paths.stderr_path.touch()
        paths.workspace_change_path.touch()
        paths.request_path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return paths
