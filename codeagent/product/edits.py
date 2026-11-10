"""当前项目的受控编辑读取投影；历史交付继续使用原 changes 接口。"""
from __future__ import annotations

import json
import re

from codeagent.runtime.atomic_edit import AtomicEditError, AtomicEditService


def active_edits(store):
    service = AtomicEditService(store.workspace_context(), store)
    events = [e for e in store.read_events() if e.type == "edit_transaction"]
    undone = {tx for e in events for tx in e.payload.get("undo_of", [])}
    active = []
    for event in events:
        tx = event.payload.get("transaction_id", "")
        if not re.fullmatch(r"[a-f0-9]{32}", tx) or tx in undone or event.payload.get("undo_of"):
            continue
        path = service.root / tx / "plan.json"
        if path.is_file():
            record = json.loads(path.read_text())
            if record.get("workspace") == str(service.context.active_root):
                active.append((event, record))
    return service, events, active


def edits_summary(store):
    _, events, active = active_edits(store)
    files = sorted({path for _, record in active for path in record.get("before", {})})
    latest_turn = active[-1][0].turn_id if active else None
    undo_files = sorted({path for event, record in active if event.turn_id == latest_turn for path in record["before"]})
    # 从已提交的撤销事务投影反馈，失败或取消不显示成功；新的编辑会替换此提示。
    last_edit = events[-1] if events else None
    last_undo = None
    if last_edit and last_edit.payload.get("undo_of"):
        last_undo = {"turnId": last_edit.turn_id, "fileCount": len(last_edit.payload.get("file_changes", []))}
    return {"available": bool(files), "files": files, "fileCount": len(files),
            "turnId": latest_turn, "undoFiles": undo_files,
            "lastUndo": last_undo,
            "lastEditSeq": max((e.seq or 0 for e in events), default=0),
            "scope": "controlled_edits", "state": "current_project"}


def edit_file(store, path):
    service, _, active = active_edits(store)
    selected = next(((event, record) for event, record in active if path in record.get("before", {})), None)
    if selected is None:
        raise AtomicEditError("unknown_edit_file", "路径不属于当前受控编辑")
    event, _ = selected
    # 使用编辑服务已有路径检查、备份完整性与文本大小边界。
    return service.preview_file(path, event.payload["transaction_id"])
