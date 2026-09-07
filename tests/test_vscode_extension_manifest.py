import json
from pathlib import Path


EXTENSION_ROOT = Path(__file__).resolve().parents[1] / "vscode-extension"


def test_extension_manifest_exposes_phase_two_product_shell():
    manifest = json.loads((EXTENSION_ROOT / "package.json").read_text(encoding="utf-8"))

    assert manifest["main"] == "./extension.js"
    assert manifest["engines"]["vscode"] == "^1.106.0"
    containers = manifest["contributes"]["viewsContainers"]
    assert "activitybar" not in containers
    assert [item["id"] for item in containers["secondarySidebar"]] == ["codeagent"]
    views = {item["id"]: item for item in manifest["contributes"]["views"]["codeagent"]}
    assert set(views) == {"codeagent.chat"}
    assert views["codeagent.chat"]["type"] == "webview"
    assert "menus" not in manifest["contributes"]
    commands = {item["command"] for item in manifest["contributes"]["commands"]}
    assert commands == {"codeagent.newTask", "codeagent.refreshSessions"}
    assert "codeagent.rpcCommand" in manifest["contributes"]["configuration"]["properties"]
    assert "codeagent.sessionRoot" in manifest["contributes"]["configuration"]["properties"]
    assert "codeagent.provider" not in manifest["contributes"]["configuration"]["properties"]
    assert "SecretStorage" not in json.dumps(manifest)
    launch = json.loads((EXTENSION_ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8"))
    assert launch["configurations"][0]["type"] == "extensionHost"


def test_extension_entry_uses_phase_two_execution_rpc_methods():
    source = (EXTENSION_ROOT / "extension.js").read_text(encoding="utf-8")

    assert '"session/list"' in source
    assert '"session/get"' in source
    assert '"session/create"' in source
    assert '"session/run"' in source
    assert '"session/continue"' in source
    assert "onNotification" in source
    assert "Ask CodeAgent" in source
    assert "productProfile" in source
    assert "context.secrets.store" in source
    assert 'provider: "deepseek"' in source
    assert '"changes/file"' in source
    assert '"changes/accept"' in source
    assert '"changes/discard"' in source
    assert 'registerTextDocumentContentProvider("codeagent-change"' in source
    assert 'executeCommand("vscode.diff"' in source
    assert 'config.get("rpcCommand", "auto")' in source
    assert '["-m", "codeagent.product.rpc"]' in source
    assert 'event.affectsConfiguration("codeagent.sessionRoot")' in source
    assert "setStatusBarMessage" in source
    assert 'registerWebviewViewProvider("codeagent.chat"' in source
    assert "createWebviewPanel" not in source
    assert "Chat history" in source
    assert "user-bubble" in source
    assert "vscode.env.clipboard.writeText" in source
    assert 'message?.type === "copyText"' in source
    assert "submissionRejected" in source
    assert "e.isComposing" in source
    assert "Allow command permissions?" in source
    assert "recentActivity" not in source
    assert (EXTENSION_ROOT / "rpcClient.js").is_file()
    assert (EXTENSION_ROOT / "media" / "codeagent.svg").is_file()
