from __future__ import annotations

import json
from pathlib import Path

from codeagent.runtime.command import CommandArtifactPaths, CommandReceipt, CommandSpec
from codeagent.session.store import SessionStore


class CommandArtifactStore:
    """将命令 artifacts 固定保存在 session 内，永不写入 worktree。"""

    def __init__(self, session_store: SessionStore) -> None:
        self.root = session_store.session_dir / "artifacts" / "commands"

    def prepare(self, spec: CommandSpec) -> CommandArtifactPaths:
        command_dir = self.root / spec.command_id
        command_dir.mkdir(parents=True, exist_ok=False)
        paths = CommandArtifactPaths(
            command_dir=command_dir,
            request_path=command_dir / "request.json",
            result_path=command_dir / "result.json",
            stdout_path=command_dir / "stdout.log",
            stderr_path=command_dir / "stderr.log",
            workspace_change_path=command_dir / "workspace-change.patch",
            runtime_home=command_dir / "runtime" / "home",
            runtime_temp=command_dir / "runtime" / "tmp",
        )
        paths.runtime_home.mkdir(parents=True)
        paths.runtime_temp.mkdir(parents=True)
        paths.stdout_path.touch()
        paths.stderr_path.touch()
        paths.workspace_change_path.touch()
        paths.request_path.write_text(json.dumps(spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        return paths

    def finalize(self, receipt: CommandReceipt) -> None:
        command_dir = self.root / receipt.command_spec.command_id
        command_dir.mkdir(parents=True, exist_ok=True)
        request_path = command_dir / "request.json"
        if not request_path.exists():
            request_path.write_text(
                json.dumps(receipt.command_spec.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        (command_dir / "stdout.log").touch(exist_ok=True)
        (command_dir / "stderr.log").touch(exist_ok=True)
        (command_dir / "workspace-change.patch").touch(exist_ok=True)
        (command_dir / "result.json").write_text(
            json.dumps(receipt.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
