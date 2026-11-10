from __future__ import annotations

import json
import re
from typing import Any

from codeagent.session.store import SessionStore


class AppliedChangesError(RuntimeError):
    """已应用修改的审查快照无法保存或读取。"""


class AppliedChangesStore:
    """只读历史Accept产物；新编辑不产生交付快照。"""

    SCHEMA_VERSION = 1
    MAX_PREVIEW_BYTES = 512_000

    def __init__(self, store: SessionStore) -> None:
        self.root = store.session_dir / "deliveries"


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
