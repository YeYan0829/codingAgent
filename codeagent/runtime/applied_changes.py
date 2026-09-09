from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import Any

from codeagent.session.store import SessionStore
from codeagent.workspace.workspace import WorkspaceContext


class AppliedChangesError(RuntimeError):
    """已应用修改的审查快照无法保存或读取。"""


class AppliedChangesStore:
    """保存 Accept 当时的文件内容，供 Session 之后继续打开同一份 Diff。"""

    SCHEMA_VERSION = 1
    MAX_PREVIEW_BYTES = 512_000

    def __init__(self, store: SessionStore) -> None:
        self.root = store.session_dir / "deliveries"

    def save(
        self,
        manifest: dict[str, Any],
        patch: bytes,
        receipt: dict[str, Any],
        context: WorkspaceContext,
    ) -> dict[str, Any]:
        delivery_id = self._delivery_id(receipt.get("apply_id"))
        target = self.root / delivery_id
        if target.exists():
            raise AppliedChangesError("已应用修改的审查快照已存在")
        temporary = self.root / f".{delivery_id}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.mkdir(parents=True)
            (temporary / "applied.patch").write_bytes(patch)
            file_rows = []
            before_tree = str(manifest["source_snapshot_tree"])
            after_tree = str(manifest["merged_tree"])
            for index, path in enumerate(manifest.get("changed_files", [])):
                relative = str(path)
                before = self._git_file(context.active_root, before_tree, relative)
                after = self._git_file(context.active_root, after_tree, relative)
                additions, deletions = self._numstat(context.active_root, before_tree, after_tree, relative)
                previewable = self._previewable(before) and self._previewable(after)
                before_name = after_name = None
                if previewable:
                    before_name = f"{index}.before"
                    after_name = f"{index}.after"
                    (temporary / before_name).write_bytes(before)
                    (temporary / after_name).write_bytes(after)
                file_rows.append({
                    "path": relative,
                    "additions": additions,
                    "deletions": deletions,
                    "previewAvailable": previewable,
                    "beforeArtifact": before_name,
                    "afterArtifact": after_name,
                })
            artifact = {
                "schemaVersion": self.SCHEMA_VERSION,
                "deliveryId": delivery_id,
                "status": "applied",
                "createdAt": receipt.get("created_at"),
                "patchSha256": manifest.get("patch_sha256"),
                "files": file_rows,
            }
            (temporary / "delivery.json").write_text(
                json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            self.root.mkdir(parents=True, exist_ok=True)
            temporary.rename(target)
            return self.summary(delivery_id)
        except (AppliedChangesError, KeyError, OSError, subprocess.SubprocessError, UnicodeError, ValueError) as exc:
            shutil.rmtree(temporary, ignore_errors=True)
            if isinstance(exc, AppliedChangesError):
                raise
            raise AppliedChangesError(f"无法保存已应用修改的审查快照: {exc}") from exc

    def summary(self, delivery_id: str) -> dict[str, Any]:
        artifact = self._load(delivery_id)
        try:
            rows = [{
                "path": str(item["path"]),
                "additions": item.get("additions"),
                "deletions": item.get("deletions"),
                "previewAvailable": bool(item.get("previewAvailable")),
            } for item in artifact["files"]]
        except (KeyError, TypeError, AttributeError) as exc:
            raise AppliedChangesError("已应用修改的审查快照格式无效") from exc
        return {
            "state": "applied",
            "available": bool(rows),
            "reviewable": any(row["previewAvailable"] for row in rows),
            "deliveryId": artifact["deliveryId"],
            "fileCount": len(rows),
            "files": [row["path"] for row in rows],
            "fileStats": rows,
            "additions": sum(row["additions"] or 0 for row in rows),
            "deletions": sum(row["deletions"] or 0 for row in rows),
        }

    def read_file(self, delivery_id: str, path: str) -> dict[str, str]:
        artifact = self._load(delivery_id)
        item = next((row for row in artifact.get("files", []) if row.get("path") == path), None)
        if item is None:
            raise AppliedChangesError("文件不属于这次已应用修改")
        if not item.get("previewAvailable"):
            raise AppliedChangesError("文件过大或不是文本，无法打开 Diff")
        directory = self.root / artifact["deliveryId"]
        try:
            before = (directory / self._artifact_name(item["beforeArtifact"], "before")).read_bytes()
            after = (directory / self._artifact_name(item["afterArtifact"], "after")).read_bytes()
            return {"path": path, "before": before.decode("utf-8"), "after": after.decode("utf-8")}
        except (KeyError, OSError, UnicodeError) as exc:
            raise AppliedChangesError(f"无法读取已应用修改: {exc}") from exc

    def _load(self, delivery_id: str) -> dict[str, Any]:
        selected = self._delivery_id(delivery_id)
        try:
            artifact = json.loads((self.root / selected / "delivery.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AppliedChangesError(f"已应用修改的审查快照不存在或已损坏: {exc}") from exc
        if artifact.get("schemaVersion") != self.SCHEMA_VERSION or artifact.get("deliveryId") != selected:
            raise AppliedChangesError("已应用修改的审查快照格式无效")
        if artifact.get("status") != "applied" or not isinstance(artifact.get("files"), list):
            raise AppliedChangesError("已应用修改的审查快照格式无效")
        if any(not isinstance(item, dict) for item in artifact["files"]):
            raise AppliedChangesError("已应用修改的审查快照格式无效")
        return artifact

    @staticmethod
    def _delivery_id(value: Any) -> str:
        selected = str(value or "")
        if not re.fullmatch(r"[0-9a-f]{32}", selected):
            raise AppliedChangesError("已应用修改的标识无效")
        return selected

    @staticmethod
    def _artifact_name(value: Any, suffix: str) -> str:
        selected = str(value or "")
        if not re.fullmatch(rf"\d+\.{suffix}", selected):
            raise AppliedChangesError("已应用修改的文件快照标识无效")
        return selected

    @classmethod
    def _previewable(cls, value: bytes) -> bool:
        if len(value) > cls.MAX_PREVIEW_BYTES or b"\0" in value[:8192]:
            return False
        try:
            value.decode("utf-8")
        except UnicodeDecodeError:
            return False
        return True

    @staticmethod
    def _git_file(root: Path, tree: str, path: str) -> bytes:
        proc = subprocess.run(
            ["git", "show", f"{tree}:{path}"], cwd=root, capture_output=True, check=False, timeout=10,
        )
        if proc.returncode == 0:
            return proc.stdout
        if b"does not exist" in proc.stderr or b"exists on disk" in proc.stderr or b"Path '" in proc.stderr:
            return b""
        raise AppliedChangesError("无法读取已应用修改中的文件")

    @staticmethod
    def _numstat(root: Path, before_tree: str, after_tree: str, path: str) -> tuple[int | None, int | None]:
        proc = subprocess.run(
            ["git", "diff", "--numstat", before_tree, after_tree, "--", path],
            cwd=root, text=True, capture_output=True, check=False, timeout=10,
        )
        if proc.returncode or not proc.stdout.strip():
            return None, None
        parts = proc.stdout.splitlines()[0].split("\t", 2)
        if len(parts) != 3:
            return None, None
        return (
            int(parts[0]) if parts[0].isdigit() else None,
            int(parts[1]) if parts[1].isdigit() else None,
        )
