from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

from codeagent import __version__
from codeagent.product.service import ProductApplicationService, ProductServiceError


PROTOCOL_VERSION = "1.0"


class JsonRpcServer:
    """newline-delimited stdio JSON-RPC 2.0 产品服务。"""

    def __init__(self, service: ProductApplicationService) -> None:
        self.service = service

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0" or not isinstance(request.get("method"), str):
            return _error(request_id, -32600, "Invalid Request")
        if "id" not in request:
            return None
        params = request.get("params", {})
        if not isinstance(params, dict):
            return _error(request_id, -32602, "Invalid params")
        try:
            result = self._call(request["method"], params)
        except RpcMethodNotFound as exc:
            return _error(request_id, -32601, str(exc))
        except ProductServiceError as exc:
            return _error(request_id, -32004, str(exc))
        except (KeyError, TypeError, ValueError) as exc:
            return _error(request_id, -32602, str(exc))
        except Exception:
            return _error(request_id, -32603, "Internal error")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        if method == "initialize":
            requested = params.get("protocolVersion", PROTOCOL_VERSION)
            if requested != PROTOCOL_VERSION:
                raise ProductServiceError(f"unsupported protocolVersion: {requested}")
            return {
                "protocolVersion": PROTOCOL_VERSION,
                "runtimeVersion": __version__,
                "capabilities": {
                    "readOnlyProductShell": False,
                    "sessionList": True,
                    "sessionDetail": True,
                    "sessionEventsAfterSeq": True,
                    "liveNotifications": True,
                    "execution": True,
                    "changesReview": True,
                    "changesResolution": True,
                    "interactiveApproval": True,
                    "protectedOperations": True,
                    "stop": True,
                    "increaseTurnBudget": True,
                },
            }
        if method == "session/list":
            return {"sessions": self.service.list_sessions(params.get("workspace"))}
        if method == "session/get":
            return self.service.get_session(str(params["sessionId"]))
        if method == "session/events":
            return self.service.get_events(
                str(params["sessionId"]),
                after_seq=int(params.get("afterSeq", 0)),
                limit=int(params.get("limit", 200)),
            )
        if method == "session/create":
            return self.service.create_session(
                params["workspace"], provider=str(params.get("provider", "fake")),
                model=params.get("model"), title=params.get("title"),
                temperature=_optional_float(params.get("temperature")),
                max_tokens=_optional_int(params.get("maxTokens")),
                max_steps_per_turn=int(params.get("maxStepsPerTurn", 12)),
                max_model_steps_per_user_turn=int(params.get("maxModelStepsPerUserTurn", 48)),
            )
        if method == "session/run":
            return self.service.run_session(str(params["sessionId"]), str(params["message"]))
        if method == "session/continue":
            return self.service.continue_session(str(params["sessionId"]))
        if method == "session/stop":
            return self.service.stop_session(str(params["sessionId"]))
        if method == "session/increaseBudget":
            return self.service.increase_budget(
                str(params["sessionId"]), turn_id=params["turnId"],
                expected_limit=params["expectedLimit"], new_limit=params.get("newLimit"),
                additional_steps=params.get("additionalSteps"),
            )
        if method == "approval/resolve":
            return self.service.resolve_approval(
                str(params["sessionId"]), str(params["approvalId"]), params["allow"],
            )
        if method == "changes/get":
            return self.service.get_changes(str(params["sessionId"]))
        if method == "changes/file":
            return self.service.get_change_file(str(params["sessionId"]), str(params["path"]))
        if method == "changes/accept":
            return self.service.accept_changes(str(params["sessionId"]))
        if method == "changes/discard":
            return self.service.discard_changes(str(params["sessionId"]))
        raise RpcMethodNotFound(f"method not found: {method}")


def serve(
    *, session_root: Path | str | None = None, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout,
) -> None:
    output_lock = threading.Lock()

    def write_message(message: dict[str, Any]) -> None:
        with output_lock:
            output_stream.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
            output_stream.flush()

    from codeagent.product.execution import ExecutionSupervisor

    supervisor = ExecutionSupervisor(lambda method, params: write_message({
        "jsonrpc": "2.0", "method": method, "params": params,
    }))
    server = JsonRpcServer(ProductApplicationService(session_root, supervisor=supervisor))
    for raw in input_stream:
        try:
            value = json.loads(raw)
            response = server.dispatch(value) if isinstance(value, dict) else _error(None, -32600, "Invalid Request")
        except json.JSONDecodeError:
            response = _error(None, -32700, "Parse error")
        if response is not None:
            write_message(response)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="CodeAgent VS Code product RPC server")
    parser.add_argument("--session-root", type=Path)
    args = parser.parse_args()
    serve(session_root=args.session_root)


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


class RpcMethodNotFound(ProductServiceError):
    pass


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


if __name__ == "__main__":
    main()
