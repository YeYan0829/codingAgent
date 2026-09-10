"use strict";

const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const crypto = require("crypto");
const { RpcClient } = require("./rpcClient");

function canonicalWorkspace(value) {
  if (typeof value !== "string" || !value.trim()) return "";
  let resolved = path.resolve(value);
  try { resolved = fs.realpathSync.native(resolved); } catch (_) { /* Missing workspaces remain searchable by saved path. */ }
  return process.platform === "win32" ? resolved.toLowerCase() : resolved;
}

function sameWorkspace(left, right) {
  const a = canonicalWorkspace(left);
  return Boolean(a) && a === canonicalWorkspace(right);
}

function validationLabel(validation, historical = false) {
  if (!validation) return "○ Not validated yet";
  if (validation.status === "failed") return "× Validation command failed";
  if (historical || validation.historical) return "✓ Validation command succeeded at that time";
  return validation.appliesToCurrentChanges
    ? "✓ Current revision has validation evidence"
    : "○ Previous validation evidence is out of date";
}

function renderToolFailureHtml(tool, key, esc) {
  const failure = tool.failure;
  if (!failure) return tool.result ? '<div class="muted small">' + esc(tool.result) + '</div>' : "";
  const output = failure.output
    ? '<details data-disclosure="' + esc(key) + ':output"><summary>Show output</summary>'
      + '<pre class="tool-output">' + esc(failure.output) + '</pre>'
      + (failure.outputTruncated ? '<small class="output-truncated">Long output was truncated.</small>' : "")
      + '</details>'
    : "";
  return '<div class="tool-failure ' + esc(failure.kind) + '"><strong>' + esc(failure.title) + '</strong>'
    + (failure.summary ? '<span>' + esc(failure.summary) + '</span>' : "") + output + '</div>';
}

function renderChangesHtml(value, detail, esc) {
  if (!value?.available) {
    const receipt = detail?.deliveryReceipt;
    return receipt
      ? '<section class="delivery-receipt ' + esc(receipt.kind) + '">✓ ' + esc(receipt.message) + '</section>'
      : "";
  }
  const actions = detail.availableActions || {};
  const reasons = detail.actionReasons || {};
  const applied = value.state === "applied";
  const validation = value.validation || detail.validationSummary;
  const stat = value.additions !== undefined
    ? ' <span class="muted">+' + value.additions + ' −' + value.deletions + '</span>' : "";
  const rows = value.fileStats || value.files.map(path => ({path, previewAvailable: true}));
  const fileRows = rows.map(file => '<button class="change-file" data-change-file="' + esc(file.path) + '" '
    + (file.previewAvailable === false ? 'disabled title="Diff preview is unavailable for this file"' : "") + '><span>'
    + esc(file.path) + (file.additions !== undefined ? ' <small class="muted">+' + (file.additions ?? '–')
      + ' −' + (file.deletions ?? '–') + '</small>' : "") + '</span><span>'
    + (file.previewAvailable === false ? 'Preview unavailable' : 'Review Diff') + '</span></button>').join("");
  const acceptReason = reasons.acceptChanges || reasons.canAcceptChanges || "";
  const discardReason = reasons.discardChanges || reasons.canDiscardChanges || "";
  const controls = applied ? "" : '<div class="change-actions"><button id="discard-changes" class="secondary-action" '
    + (actions.canDiscardChanges ? "" : 'disabled title="' + esc(discardReason) + '"') + '>Discard</button>'
    + '<button id="accept-changes" class="primary-action" '
    + (actions.canAcceptChanges ? "" : 'disabled title="' + esc(acceptReason) + '"') + '>Accept</button></div>';
  return '<section class="current-changes ' + (applied ? 'applied' : 'pending') + '"><div class="result-title">'
    + (applied ? 'Applied' : 'Changes ready for review') + ' · ' + value.fileCount + ' file'
    + (value.fileCount === 1 ? "" : "s") + stat + '</div>' + fileRows
    + '<div class="validation ' + esc(validation?.status || 'none') + '">' + validationLabel(validation, applied)
    + (validation?.command ? '<details data-disclosure="current-validation"><summary>Validation details</summary><code>'
      + esc(validation.command) + '</code></details>' : "") + '</div>' + controls + '</section>';
}

const DEFAULT_PROFILE = Object.freeze({
  provider: "deepseek", model: "deepseek-v4-flash", temperature: 0.2,
  reasoningEnabled: false, reasoningEffort: "high",
  maxTokens: 4000, maxStepsPerTurn: 12, maxModelStepsPerUserTurn: 48
});

class ChatViewProvider {
  constructor(runtime, workspacePath, context, changeDocuments) {
    this.runtime = runtime;
    this.workspacePath = workspacePath;
    this.context = context;
    this.changeDocuments = changeDocuments;
    const storedProfile = context.globalState.get("productProfile") || {};
    this.profile = {...DEFAULT_PROFILE, ...storedProfile};
    if (!storedProfile.reasoningEffort) this.profile.reasoningEffort = "high";
    if (this.profile.temperature === null) this.profile.temperature = DEFAULT_PROFILE.temperature;
    this.secretState = {deepseek: false, glm: false};
    this.view = undefined;
    this.sessions = [];
    this.current = undefined;
    this.refreshTimer = undefined;
    this.rendered = false;
    this.runtimeError = undefined;
    this.operationError = undefined;
  }

  async resolveWebviewView(view) {
    this.view = view;
    this.rendered = false;
    view.webview.options = { enableScripts: true };
    view.webview.onDidReceiveMessage(message => this._handleMessage(message));
    await this._refreshSecretState();
    this._render();
    await this.refresh();
  }

  async refresh(showResult = false) {
    await this.runtime.ensure();
    this.runtimeError = undefined;
    const result = await this.runtime.client.request("session/list", {});
    this.sessions = result.sessions;
    const localSessions = this.sessions.filter(item => sameWorkspace(item.workspace, this.workspacePath));
    if (this.current && (!sameWorkspace(this.current.workspace, this.workspacePath) ||
        !this.sessions.some(item => item.sessionId === this.current.sessionId))) this.current = undefined;
    if (!this.current && localSessions.length) {
      this.current = await this.runtime.client.request("session/get", { sessionId: localSessions[0].sessionId });
    } else if (this.current) {
      this.current = await this.runtime.client.request("session/get", { sessionId: this.current.sessionId });
    }
    await this._hydrateChanges();
    this.operationError = undefined;
    this._render();
    if (showResult) vscode.window.setStatusBarMessage(`CodeAgent: found ${this.sessions.length} session${this.sessions.length === 1 ? "" : "s"}`, 3000);
  }

  async openSession(sessionId) {
    const summary = this.sessions.find(item => item.sessionId === sessionId);
    if (!summary || !sameWorkspace(summary.workspace, this.workspacePath)) {
      throw new Error("Open this session's workspace before continuing it");
    }
    await this.runtime.ensure();
    this.current = await this.runtime.client.request("session/get", { sessionId });
    await this._hydrateChanges();
    this._render();
  }

  async _hydrateChanges() {
    if (!this.current?.changesSummary?.available) return;
    this.current.changesSummary = await this.runtime.client.request("changes/get", {
      sessionId: this.current.sessionId
    });
  }

  async _handleMessage(message) {
    try {
      if (message?.type === "selectSession" && typeof message.sessionId === "string") {
        await this.openSession(message.sessionId);
      } else if (message?.type === "refresh") {
        await this.refresh(true);
      } else if (message?.type === "newTask") {
        await this.newSession();
      } else if (message?.type === "retryRuntime") {
        this.runtime.restart();
        await this.refresh(true);
      } else if (message?.type === "openOutput") {
        this.runtime.output.show(true);
      } else if (message?.type === "copyText" && typeof message.text === "string") {
        await vscode.env.clipboard.writeText(message.text);
        this.view?.webview.postMessage({type: "copyResult", copyId: message.copyId, ok: true});
      } else if (message?.type === "saveSettings") {
        await this.saveSettings(message.profile, message.apiKey);
      } else if (message?.type === "clearApiKey" && ["deepseek", "glm"].includes(message.provider)) {
        if (this.current?.executionState && this.current.executionState !== "idle") {
          throw new Error("Stop the current task before changing API credentials");
        }
        await this.context.secrets.delete(`codeagent.${message.provider}.apiKey`);
        this.runtime.restart();
        await this._refreshSecretState();
        this._render();
      } else if (message?.type === "run" && typeof message.message === "string" && this.current) {
        await this.runtime.ensure();
        await this.runtime.client.request("session/run", {sessionId: this.current.sessionId, message: message.message});
        await this.refresh();
      } else if (message?.type === "continue" && this.current) {
        await this.runtime.ensure();
        await this.runtime.client.request("session/continue", {sessionId: this.current.sessionId});
        await this.refresh();
      } else if (message?.type === "increaseBudget" && this.current) {
        await this.increaseBudget(message);
      } else if (message?.type === "stop" && this.current) {
        await this.runtime.client.request("session/stop", {sessionId: this.current.sessionId});
        await this.refresh();
      } else if (message?.type === "resolveApproval" && typeof message.allow === "boolean" && this.current?.pendingApproval) {
        await this.runtime.client.request("approval/resolve", {
          sessionId: this.current.sessionId,
          approvalId: this.current.pendingApproval.approvalId,
          allow: message.allow
        });
        await this.refresh();
      } else if (message?.type === "openDiff" && typeof message.path === "string" && this.current) {
        await this.openDiff(message.path);
      } else if (message?.type === "acceptChanges" && this.current) {
        const choice = await vscode.window.showWarningMessage(
          "Apply the reviewed CodeAgent changes to the source repository?", {modal: true}, "Accept Changes"
        );
        if (choice === "Accept Changes") {
          await this.runtime.client.request("changes/accept", {sessionId: this.current.sessionId});
          await this.refresh();
        }
      } else if (message?.type === "discardChanges" && this.current) {
        const choice = await vscode.window.showWarningMessage(
          "Discard all current CodeAgent workspace changes? The source repository will not be changed.",
          {modal: true}, "Discard Changes"
        );
        if (choice === "Discard Changes") {
          await this.runtime.client.request("changes/discard", {sessionId: this.current.sessionId});
          await this.refresh();
        }
      }
    } catch (error) {
      this.operationError = error.message;
      this._render();
      if (message?.type === "run") {
        this.view?.webview.postMessage({type: "submissionRejected", message: message.message});
      } else if (message?.type === "copyText") {
        this.view?.webview.postMessage({type: "copyResult", copyId: message.copyId, ok: false});
      }
      vscode.window.showErrorMessage(`CodeAgent：${error.message}`);
    }
  }

  async increaseBudget(message) {
    const budget = this.current?.turnBudget;
    if (!this.current?.availableActions?.canIncreaseBudget || message.sessionId !== this.current.sessionId ||
        message.turnId !== budget?.turnId || message.expectedLimit !== budget?.limit) {
      throw new Error("The request or budget changed. Refresh before increasing it.");
    }
    const sessionId = this.current.sessionId;
    const {turnId, limit, used, maxLimit} = budget;
    const parseBudget = text => {
      const value = String(text).trim();
      if (/^\+[1-9]\d*$/.test(value)) {
        const additionalSteps = Number(value.slice(1));
        return Number.isSafeInteger(additionalSteps) && limit + additionalSteps <= maxLimit
          ? {additionalSteps, newLimit: limit + additionalSteps} : null;
      }
      if (/^[1-9]\d*$/.test(value)) {
        const newLimit = Number(value);
        return Number.isSafeInteger(newLimit) && newLimit > limit && newLimit <= maxLimit
          ? {newLimit} : null;
      }
      return null;
    };
    const value = await vscode.window.showInputBox({
      title: "Increase budget and continue this request",
      prompt: `${used}/${limit} model steps used. Enter +N to add N steps, or N to set the new total limit (maximum ${maxLimit}). This request only; additional model calls may incur API costs.`,
      placeHolder: `For example: +24 or ${Math.min(limit + 24, maxLimit)}`,
      value: "",
      validateInput: text => parseBudget(text) ? undefined
        : `Enter +N to add steps, or a total limit greater than ${limit} and at most ${maxLimit}.`,
    });
    if (value === undefined) return;
    const requested = parseBudget(value);
    if (!requested) throw new Error("Invalid budget increase.");
    await this.runtime.ensure();
    await this.runtime.client.request("session/increaseBudget", {
      sessionId, turnId, expectedLimit: limit,
      ...(requested.additionalSteps ? {additionalSteps: requested.additionalSteps} : {newLimit: requested.newLimit}),
    });
    await this.refresh();
  }

  async openDiff(filePath) {
    await this.runtime.ensure();
    const change = await this.runtime.client.request("changes/file", {
      sessionId: this.current.sessionId, path: filePath
    });
    const left = this.changeDocuments.put(this.current.sessionId, filePath, "before", change.before);
    const right = this.changeDocuments.put(this.current.sessionId, filePath, "after", change.after);
    await vscode.commands.executeCommand("vscode.diff", left, right, `${filePath} (CodeAgent Changes)`);
  }

  async newSession() {
    if (!this.workspacePath) throw new Error("Open a workspace before creating a Session");
    if (this.current?.executionState && this.current.executionState !== "idle") {
      throw new Error("Stop the current task before creating another session");
    }
    if (!this.secretState[this.profile.provider]) throw new Error("Configure the selected provider API key first");
    const profile = validateProfile(this.profile);
    await this.runtime.ensure();
    this.current = await this.runtime.client.request("session/create", {
      workspace: this.workspacePath, ...profile,
    });
    if (this.current.provider !== profile.provider || this.current.model !== profile.model) {
      throw new Error(`Session configuration mismatch: expected ${profile.provider}/${profile.model}, got ${this.current.provider}/${this.current.model}`);
    }
    await this.refresh();
  }

  async saveSettings(value, apiKey) {
    if (typeof apiKey === "string" && apiKey.trim() && this.current?.executionState && this.current.executionState !== "idle") {
      throw new Error("Stop the current task before changing API credentials");
    }
    this.profile = validateProfile(value);
    await this.context.globalState.update("productProfile", this.profile);
    if (typeof apiKey === "string" && apiKey.trim()) {
      await this.context.secrets.store(`codeagent.${this.profile.provider}.apiKey`, apiKey.trim());
      this.runtime.restart();
    }
    await this._refreshSecretState();
    this._render();
    vscode.window.setStatusBarMessage("CodeAgent: configuration saved for new sessions", 3000);
  }

  async _refreshSecretState() {
    this.secretState = {
      deepseek: Boolean(await this.context.secrets.get("codeagent.deepseek.apiKey")),
      glm: Boolean(await this.context.secrets.get("codeagent.glm.apiKey"))
    };
  }

  handleNotification(method, params) {
    if (!method.startsWith("session/") && method !== "approval/requested") return;
    if (this.current && params.sessionId !== this.current.sessionId && method !== "session/executionStateChanged") return;
    clearTimeout(this.refreshTimer);
    this.refreshTimer = setTimeout(() => this.refresh().catch(error => {
      vscode.window.showErrorMessage(`CodeAgent live update failed: ${error.message}`);
    }), 80);
  }

  handleConnectionState(state, error) {
    this.runtimeError = state === "unavailable" ? (error?.message || "CodeAgent Runtime is unavailable") : undefined;
    this._render();
  }

  _render() {
    if (!this.view) return;
    const sessions = this.sessions.map(session => ({
      ...session, isCurrentWorkspace: sameWorkspace(session.workspace, this.workspacePath)
    }));
    const productState = {
      detail: this.current, sessions, profile: this.profile,
      secretState: this.secretState, runtimeError: this.runtimeError,
      operationError: this.operationError, workspacePath: this.workspacePath
    };
    if (!this.rendered) {
      this.view.webview.html = renderChat(
        this.current, sessions, this.profile, this.secretState, this.runtimeError,
        this.operationError, this.workspacePath
      );
      this.rendered = true;
    } else {
      this.view.webview.postMessage({type: "productState", state: productState});
    }
  }
}

class RuntimeConnection {
  constructor(context, workspacePath, output) {
    this.context = context;
    this.workspacePath = workspacePath;
    this.output = output;
    this.client = undefined;
    this.signature = undefined;
  }

  async ensure() {
    const desired = await this._configuration();
    if (!this.client || desired.signature !== this.signature) {
      this.client?.dispose();
      this.client = new RpcClient(desired.command, desired.args, {
        cwd: desired.cwd,
        env: desired.env,
        onLog: line => this.output.append(line),
        onNotification: (method, params) => this.onNotification?.(method, params),
        onConnectionState: (state, error) => this.onConnectionState?.(state, error)
      });
      this.signature = desired.signature;
    }
    await this.client.start();
  }

  dispose() {
    this.client?.dispose();
  }

  restart() {
    this.client?.dispose();
    this.client = undefined;
    this.signature = undefined;
  }

  async _configuration() {
    const config = vscode.workspace.getConfiguration("codeagent");
    const configuredCommand = config.get("rpcCommand", "auto");
    const projectRoot = path.resolve(this.context.extensionPath, "..");
    const developmentPython = path.join(projectRoot, ".venv", "bin", "python");
    const useDevelopmentPython = configuredCommand === "auto" && fs.existsSync(developmentPython);
    const command = useDevelopmentPython ? developmentPython : (configuredCommand === "auto" ? "codeagent-rpc" : configuredCommand);
    const sessionRoot = config.get("sessionRoot", "").trim();
    const args = [
      ...(useDevelopmentPython ? ["-m", "codeagent.product.rpc"] : []),
      ...(sessionRoot ? ["--session-root", sessionRoot] : [])
    ];
    const deepseekKey = await this.context.secrets.get("codeagent.deepseek.apiKey");
    const glmKey = await this.context.secrets.get("codeagent.glm.apiKey");
    const env = {};
    if (deepseekKey) env.DEEPSEEK_API_KEY = deepseekKey;
    if (glmKey) env.GLM_API_KEY = glmKey;
    return {
      command, args, env, cwd: useDevelopmentPython ? projectRoot : this.workspacePath,
      signature: JSON.stringify({ command, args, deepseekKey: Boolean(deepseekKey), glmKey: Boolean(glmKey) })
    };
  }
}

class ChangeDocumentProvider {
  constructor() { this.documents = new Map(); }
  provideTextDocumentContent(uri) { return this.documents.get(uri.toString()) ?? ""; }
  put(sessionId, filePath, side, content) {
    const uri = vscode.Uri.from({
      scheme: "codeagent-change", authority: sessionId,
      path: `/${side}/${filePath}`, query: String(Date.now())
    });
    this.documents.set(uri.toString(), content);
    return uri;
  }
}

async function activate(context) {
  const output = vscode.window.createOutputChannel("CodeAgent Runtime");
  const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath;
  const runtime = new RuntimeConnection(context, workspacePath, output);
  const changeDocuments = new ChangeDocumentProvider();
  const provider = new ChatViewProvider(runtime, workspacePath, context, changeDocuments);
  runtime.onNotification = (method, params) => provider.handleNotification(method, params);
  runtime.onConnectionState = (state, error) => provider.handleConnectionState(state, error);

  context.subscriptions.push(
    output,
    { dispose: () => runtime.dispose() },
    vscode.workspace.registerTextDocumentContentProvider("codeagent-change", changeDocuments),
    vscode.window.registerWebviewViewProvider("codeagent.chat", provider, {
      webviewOptions: { retainContextWhenHidden: true }
    }),
    vscode.commands.registerCommand("codeagent.refreshSessions", () => provider.refresh(true)),
    vscode.commands.registerCommand("codeagent.newTask", () => {
      provider.newSession().catch(error => vscode.window.showErrorMessage(`CodeAgent：${error.message}`));
    }),
    vscode.workspace.onDidChangeConfiguration(event => {
      if (event.affectsConfiguration("codeagent.rpcCommand") || event.affectsConfiguration("codeagent.sessionRoot")) {
        provider.refresh(true);
      }
    })
  );
}

function renderChat(detail, sessions, profile = DEFAULT_PROFILE, secretState = {}, runtimeError = undefined, operationError = undefined, workspacePath = undefined) {
  const nonce = crypto.randomBytes(16).toString("hex");
  const state = safeJson({ detail, sessions, profile, secretState, runtimeError, operationError, workspacePath });
  return String.raw`<!doctype html><html><head><meta charset="utf-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}'">
  <style>${styles()}</style><style>
  .current-changes{margin:14px 0 74px;border:1px solid var(--vscode-sideBar-border);border-radius:7px;overflow:hidden}
  .change-file{display:flex;width:100%;justify-content:space-between;gap:8px;padding:7px 9px;border:0;border-top:1px solid var(--vscode-sideBar-border);background:transparent;text-align:left;cursor:pointer}
  .change-file:hover{background:var(--vscode-list-hoverBackground)}.change-file span{color:var(--vscode-textLink-foreground)}
  .change-actions{display:flex;justify-content:flex-end;gap:7px;padding:9px;border-top:1px solid var(--vscode-sideBar-border)}
  .change-actions button{padding:5px 12px;border-radius:4px;cursor:pointer}.change-actions button:disabled{opacity:.5;cursor:not-allowed}
  .primary-action{border:1px solid var(--vscode-button-border,transparent);background:var(--vscode-button-background);color:var(--vscode-button-foreground)}
  .secondary-action{border:1px solid var(--vscode-button-border,transparent);background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground)}
  .approval-card{margin:12px 0;padding:11px;border:1px solid var(--vscode-editorWarning-foreground);border-radius:7px;background:var(--vscode-textBlockQuote-background)}
  .approval-card p{margin:7px 0}.approval-card pre{padding:7px;overflow:auto;background:var(--vscode-textCodeBlock-background)}
  .permission-row{display:grid;grid-template-columns:1fr auto;gap:3px 8px;margin:8px 0;padding:8px;border-radius:5px;background:var(--vscode-editor-background)}.permission-row small{grid-column:1/-1;color:var(--vscode-descriptionForeground)}.permission-scope{align-self:start;padding:1px 6px;border-radius:8px;background:var(--vscode-badge-background);color:var(--vscode-badge-foreground);font-size:.8em}
  .link-action{border:0;background:transparent;color:var(--vscode-textLink-foreground);cursor:pointer;padding:5px 0}.delivery-area{padding:0 8px}.delivery-receipt{margin:10px 0 78px;padding:9px;border:1px solid var(--vscode-sideBar-border);border-radius:6px;color:var(--vscode-testing-iconPassed)}
  .global-attention{margin:8px;padding:10px;border:1px solid var(--vscode-editorWarning-foreground);border-radius:7px;background:var(--vscode-textBlockQuote-background)}.global-attention.error,.global-attention.execution_error,.global-attention.recovery{border-color:var(--vscode-editorError-foreground)}.global-attention p{margin:6px 0}.global-attention div{margin-top:8px}.header-status,.session-badge{display:inline-block;margin-left:6px;padding:1px 5px;border-radius:8px;background:var(--vscode-badge-background);color:var(--vscode-badge-foreground);font-size:.8em;font-weight:400}.session-title{display:block;overflow:hidden;text-overflow:ellipsis}.session-badge{margin:3px 0 0}.composer-message{width:100%;padding:7px;text-align:center;color:var(--vscode-descriptionForeground)}.continue-copy{display:flex;width:100%;flex-direction:column;gap:5px}.continue-copy small{color:var(--vscode-descriptionForeground);text-align:center}
  .message-shell{position:relative}.message-copy{position:absolute;top:2px;right:2px;display:flex;align-items:center;justify-content:center;width:26px;height:26px;padding:0;border:0;border-radius:5px;background:var(--vscode-toolbar-hoverBackground);color:var(--vscode-descriptionForeground);cursor:pointer;opacity:0;transition:opacity .1s}.message-shell:hover>.message-copy,.message-copy:focus-visible{opacity:1}.message-copy:hover{color:var(--vscode-foreground)}.message-copy.copied{opacity:1;color:var(--vscode-testing-iconPassed)}.user-message{display:flex;justify-content:flex-end}.user-message .message-shell{max-width:82%}.user-message .user-bubble{max-width:none;padding-right:34px}.agent-message{padding-right:30px}.copy-status{position:fixed;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}
  .tool-failure{margin-top:7px;padding:8px;border-left:2px solid var(--vscode-editorError-foreground);background:var(--vscode-textBlockQuote-background)}.tool-failure strong,.tool-failure span{display:block}.tool-failure span{margin-top:4px}.tool-failure details{margin:5px 0 0}.tool-output{max-height:260px;margin:4px 0 0;padding:7px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--vscode-textCodeBlock-background);color:var(--vscode-foreground);font-family:var(--vscode-editor-font-family);font-size:.9em}.output-truncated{display:block;margin-top:4px;color:var(--vscode-descriptionForeground)}
  .change-file:disabled{cursor:not-allowed;opacity:.65}.change-file:disabled:hover{background:transparent}.current-changes.applied{border-color:var(--vscode-testing-iconPassed)}
  .context-summary{margin:8px 0;padding:7px 9px;border-left:2px solid var(--vscode-descriptionForeground);background:var(--vscode-textBlockQuote-background);color:var(--vscode-descriptionForeground)}.context-summary strong,.context-summary span{display:block}.context-summary strong{color:var(--vscode-foreground);font-size:.92em}.context-summary span{margin-top:3px;font-size:.9em}.context-summary pre{max-height:220px;overflow:auto;white-space:pre-wrap;color:var(--vscode-foreground)}
  </style></head><body>
  <div id="app"><div class="empty">Loading CodeAgent…</div></div>
  <script nonce="${nonce}">
    const vscode = acquireVsCodeApi();
    let state = ${state};
    let historyOpen = false;
    let historyScope = 'workspace';
    let historyStatus = 'all';
    let historyQuery = '';
    let settingsOpen = false;
    let draft = '';
    let submissionPending = false;
    let copyPayloads = [];
    let savedScrollY = 0;
    let followBottom = true;
    const openDisclosures = new Set();
    const app = document.getElementById('app');
    function captureUiState(){const prompt=document.getElementById('prompt');if(prompt)draft=prompt.value;document.querySelectorAll('details[data-disclosure]').forEach(el=>{if(el.open)openDisclosures.add(el.dataset.disclosure);else openDisclosures.delete(el.dataset.disclosure)});savedScrollY=window.scrollY;followBottom=window.innerHeight+window.scrollY>=document.body.scrollHeight-24;}
    function restoreUiState(){const prompt=document.getElementById('prompt');if(prompt)prompt.value=draft;document.querySelectorAll('details[data-disclosure]').forEach(el=>{el.open=openDisclosures.has(el.dataset.disclosure)});requestAnimationFrame(()=>window.scrollTo(0,followBottom?document.body.scrollHeight:savedScrollY));}
    window.addEventListener('message',event=>{const data=event.data;if(data?.type==='productState'){captureUiState();submissionPending=false;state=data.state;render(true);}else if(data?.type==='submissionRejected'){submissionPending=false;if(!draft)draft=String(data.message||'');render(true);requestAnimationFrame(()=>document.getElementById('prompt')?.focus());}else if(data?.type==='copyResult'){const button=document.querySelector('[data-copy="'+String(data.copyId).replace(/[^0-9]/g,'')+'"]');const status=document.getElementById('copy-status');if(button){button.textContent=data.ok?'✓':'×';button.classList.toggle('copied',Boolean(data.ok));button.title=data.ok?'Copied':'Copy failed';}if(status)status.textContent=data.ok?'Copied to clipboard':'Could not copy to clipboard';}});
    const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const markdown = value => {
      let text = esc(value);
      const blocks = [];
      text = text.replace(/\x60\x60\x60([^\n]*)\n([\s\S]*?)\x60\x60\x60/g, (_, lang, code) => {
        const index = blocks.push('<pre><code>' + code + '</code></pre>') - 1;
        return '@@BLOCK' + index + '@@';
      });
      text = text.replace(/^### (.+)$/gm, '<h4>$1</h4>').replace(/^## (.+)$/gm, '<h3>$1</h3>').replace(/^# (.+)$/gm, '<h2>$1</h2>');
      text = text.replace(/\x60([^\x60]+)\x60/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
      text = text.split(/\n{2,}/).map(part => {
        if (part.startsWith('@@BLOCK')) return part;
        const lines = part.split('\n');
        if (lines.every(line => /^[-*] /.test(line))) return '<ul>' + lines.map(line => '<li>' + line.slice(2) + '</li>').join('') + '</ul>';
        if (lines.every(line => /^\d+\. /.test(line))) return '<ol>' + lines.map(line => '<li>' + line.replace(/^\d+\. /, '') + '</li>').join('') + '</ol>';
        return '<p>' + part.replace(/\n/g, '<br>') + '</p>';
      }).join('');
      return text.replace(/@@BLOCK(\d+)@@/g, (_, i) => blocks[Number(i)]);
    };
    const renderToolFailureHtml = ${renderToolFailureHtml.toString()};
    const toolFailure = (tool,key) => renderToolFailureHtml(tool,key,esc);
    const activity = (item,key) => '<details data-disclosure="'+esc(key)+'" ' + (item.defaultExpanded ? 'open' : '') + '><summary><span class="status ' + esc(item.status) + '"></span>' + esc(item.summary) + '</summary><div class="tools">' + item.tools.map(tool => '<div class="tool"><span class="tool-icon">' + (tool.status === 'denied' ? '⊘' : tool.status === 'cancelled' ? '■' : tool.status === 'failed' ? '×' : tool.status === 'running' ? '●' : '✓') + '</span><div class="tool-body"><code class="tool-name">' + esc(tool.tool) + '</code>' + (tool.argumentSummary ? '<div class="tool-args">' + esc(tool.argumentSummary) + '</div>' : '') + toolFailure(tool,key+':'+tool.callId) + '</div></div>').join('') + '</div></details>';
    const copyButton = (text,label) => {const id=copyPayloads.push(String(text??''))-1;return '<button type="button" class="message-copy" data-copy="'+id+'" aria-label="'+esc(label)+'" title="'+esc(label)+'">⧉</button>';};
    const contextSummary = (item,key) => '<div class="context-summary"><strong>↳ ' + esc(item.title) + '</strong><span>' + esc(item.message) + '</span><details data-disclosure="' + esc(key) + '"><summary>Context details</summary>' + (item.reason ? '<div>Trigger: ' + esc(item.reason) + '</div>' : '') + (item.requestEstimatedTokensBefore != null ? '<div>Estimated request: ' + esc(item.requestEstimatedTokensBefore) + (item.rebuildRequestEstimatedTokens != null ? ' → ' + esc(item.rebuildRequestEstimatedTokens) : '') + ' tokens</div>' : '') + (item.retryCount ? '<div>Recovered retries: ' + esc(item.retryCount) + '</div>' : '') + (item.condenser?.model ? '<div>Condenser: ' + esc(item.condenser.model) + '</div>' : '') + (item.condenser?.usage?.total_tokens != null ? '<div>Condenser usage: ' + esc(item.condenser.usage.total_tokens) + ' tokens</div>' : '') + (item.summary ? '<pre>' + esc(item.summary) + '</pre>' : '') + '</details></div>';
    const truncationNotice = (item,key) => '<div class="notice ' + esc(item.tone) + '">' + esc(item.message) + (item.records?.length ? '<details data-disclosure="' + esc(key) + '"><summary>Recovery details</summary><pre>' + esc(JSON.stringify(item.records,null,2)) + '</pre></details>' : '') + '</div>';
    const turn = value => '<section class="turn"><div class="user-row user-message"><div class="message-shell"><div class="user-bubble">' + esc(value.userMessage) + '</div>'+copyButton(value.userMessage,'Copy user message')+'</div></div>' + value.items.map(item => item.type === 'markdown' ? '<div class="message-shell agent-message"><div class="agent-markdown">' + markdown(item.text) + '</div>'+copyButton(item.text,'Copy agent response')+'</div>' : item.type === 'activityGroup' ? activity(item,value.turnId+':'+item.eventSeq) : item.type === 'contextSummary' ? contextSummary(item,value.turnId+':context:'+item.eventSeq) : item.kind === 'truncationRecovery' ? truncationNotice(item,value.turnId+':truncation:'+item.eventSeq) : '<div class="notice ' + esc(item.tone) + '">' + esc(item.message) + '</div>').join('') + (value.changedFiles.length ? '<div class="result-card"><div class="result-title">Changed in this turn · ' + value.changedFiles.length + ' file' + (value.changedFiles.length === 1 ? '' : 's') + '</div>' + value.changedFiles.slice(0,5).map(file => '<div class="file">' + esc(file) + '</div>').join('') + (value.changedFiles.length > 5 ? '<div class="muted">+' + (value.changedFiles.length - 5) + ' more</div>' : '') + '</div>' : '') + (value.validation ? '<div class="validation ' + esc(value.validation.status) + '">' + validationLabel(value.validation,true) + (value.validation.command ? ' · ' + esc(value.validation.command) : '') + (value.validation.appliesToCurrentChanges ? '' : ' · now out of date') + '</div>' : '') + value.attention.map(item => '<div class="notice">' + esc(item.message) + '</div>').join('') + '</section>';
    const validationLabel = ${validationLabel.toString()};
    const renderChangesHtml = ${renderChangesHtml.toString()};
    const currentChanges = value => renderChangesHtml(value,state.detail,esc);
    const approval = value => {if(!value)return '';const summary=value.summary||{};const details=value.details||value.request||{};const permissions=Array.isArray(summary.permissions)?summary.permissions:[];const workspace=value.kind==='workspace_upgrade';const command=value.kind==='command_permissions';const title=workspace?'Create isolated workspace?':command?'Allow command permissions?':'Approval required';const explanation=workspace?'CodeAgent needs a separate worktree before it can run protected operations. Your source files are not changed by this approval.':command?'Review the exact command and each requested capability before allowing it.':summary.message||((summary.tool||value.kind)+' requires permission');const permissionRows=permissions.map(item=>'<div class="permission-row"><strong>'+esc(item.capability==='network'?'Network access':item.capability||'Resource access')+'</strong><span class="permission-scope">'+esc(item.scope||'once')+'</span>'+(item.reason?'<small>'+esc(item.reason)+'</small>':'')+'</div>').join('');const technical=JSON.stringify(details,null,2);const allowLabel=workspace?'Create worktree':permissions.length===1&&permissions[0].scope==='once'?'Allow once':'Allow';return '<section class="approval-card"><strong>'+esc(title)+'</strong><p>'+esc(explanation)+'</p>'+(summary.tool?'<div><strong>Tool</strong> <code>'+esc(summary.tool)+'</code></div>':'')+(summary.command?'<div><strong>Command</strong><pre>'+esc(summary.command)+'</pre></div>':'')+permissionRows+(summary.path?'<div><strong>Path</strong> <code>'+esc(summary.path)+'</code></div>':'')+(technical&&technical!=='{}'?'<details data-disclosure="approval-technical"><summary>Technical details</summary><pre>'+esc(technical)+'</pre></details>':'')+'<div class="change-actions"><button id="reject-approval" class="secondary-action">Reject</button><button id="allow-approval" class="primary-action">'+esc(allowLabel)+'</button></div><button id="stop-approval" class="link-action">Stop task</button></section>';};
    const sessionBadge = session => {const text=session.executionState==='awaiting_approval'?'Approval required':session.executionState==='running'?'Running':session.executionState==='stopping'?'Stopping':session.changesState==='applied'?'Applied':session.changedFileCount?'Changes to review':session.attentionSummary&&session.attentionSummary!=='Ready'?session.attentionSummary:'';return text?'<span class="session-badge">'+esc(text)+'</span>':'';};
    const currentWorkspace = session => Boolean(session?.isCurrentWorkspace);
    const sessionStatusKey = session => session.executionState!=='idle'?'active':session.changedFileCount?'changes':session.attentionSummary&&session.attentionSummary!=='Ready'?'attention':'ready';
    const sessionButton = session => {const local=currentWorkspace(session);const disabled=local?'':' disabled aria-disabled="true" title="Open '+esc(session.workspaceLabel||'this repository')+' as the current workspace to continue"';return '<button class="session' + (state.detail?.sessionId === session.sessionId ? ' active' : '') + (local?'':' remote-session') + '" data-session="' + esc(session.sessionId) + '"'+disabled+'><span><span class="session-title">' + esc(session.title) + '</span><span class="session-meta">'+esc(session.workspaceLabel||'Unknown workspace')+(session.changedFileCount?' · '+esc(session.changedFileCount)+' changed':'')+'</span>'+sessionBadge(session)+'</span><small>' + esc(relativeTime(session.lastActiveAt)) + (local?'':'<span class="workspace-lock" aria-hidden="true">↗</span>') + '</small></button>';};
    const timeGroups = values => {const groups=[['Today',[]],['Previous 7 days',[]],['Older',[]]];values.forEach(session=>{const age=Date.now()-new Date(session.lastActiveAt).getTime();const index=!Number.isFinite(age)||age>=7*86400000?2:age>=86400000?1:0;groups[index][1].push(session)});return groups;};
    const historyResults = () => {
      const query=historyQuery.trim().toLowerCase();
      const values=state.sessions.filter(session=>(historyScope==='all'||currentWorkspace(session))&&(historyStatus==='all'||sessionStatusKey(session)===historyStatus)&&(!query||[session.title,session.workspaceLabel,session.workspace,session.provider,session.model,session.attentionSummary].some(value=>String(value||'').toLowerCase().includes(query))));
      const groups=historyScope==='all'?[...new Map(values.map(session=>[session.workspace||'Unknown workspace',[]])).entries()].map(([workspace])=>[values.find(item=>(item.workspace||'Unknown workspace')===workspace)?.workspaceLabel||workspace,values.filter(item=>(item.workspace||'Unknown workspace')===workspace)]):timeGroups(values);
      const body=groups.filter(group=>group[1].length).map(group=>'<section class="session-group"><div class="group-title">'+esc(group[0])+'<span>'+group[1].length+'</span></div>'+group[1].map(sessionButton).join('')+'</section>').join('');
      return body||(state.sessions.length?'<div class="history-empty"><strong>No matching sessions</strong><span>Try another search or filter.</span></div>':'<div class="history-empty"><strong>No sessions in this Session Root</strong><span>Sessions created with another storage root are kept separate.</span></div>');
    };
    const renderHistoryResults = () => {const target=document.getElementById('sessions');if(target){target.innerHTML=historyResults();document.querySelectorAll('[data-session]:not([disabled])').forEach(el=>el.onclick=()=>{historyOpen=false;vscode.postMessage({type:'selectSession',sessionId:el.dataset.session});});}};
    function relativeTime(value) { const ms = Date.now() - new Date(value).getTime(); if (!Number.isFinite(ms)) return ''; const m=Math.max(0,Math.floor(ms/60000)); return m<1?'now':m<60?m+'m':m<1440?Math.floor(m/60)+'h':Math.floor(m/1440)+'d'; }
    const welcomeView = configured => {
      const workspace = String(state.workspacePath || '').split(/[\\/]/).pop();
      const repository = workspace
        ? '<div class="welcome-context"><span>Repository</span><strong>'+esc(workspace)+'</strong></div>'
        : '<div class="welcome-context warning"><span>Repository</span><strong>Open a Git workspace to begin</strong></div>';
      const model = '<div class="welcome-model"><span class="model-dot"></span>'+esc(state.profile.provider)+' · '+esc(state.profile.model)+' · '+(state.profile.reasoningEnabled?'reasoning '+esc(state.profile.reasoningEffort):'reasoning off')+'</div>';
      if (state.runtimeError) return '<section class="welcome"><div class="welcome-mark" aria-hidden="true">&lt;/&gt;</div><h1>CodeAgent could not start</h1><p class="welcome-copy">The local Runtime is unavailable. Retry it or inspect the Runtime output for details.</p>'+repository+'<div class="welcome-actions"><button id="retry-runtime" class="welcome-primary">Retry Runtime</button><button id="open-output" class="welcome-secondary">Open Runtime output</button></div><p class="welcome-error">'+esc(state.runtimeError)+'</p></section>';
      if (!configured) return '<section class="welcome"><div class="welcome-mark" aria-hidden="true">&lt;/&gt;</div><h1>Start coding with CodeAgent</h1><p class="welcome-copy">Inspect this repository, make isolated changes, and validate the result from one place.</p>'+repository+'<div class="welcome-actions"><button id="configure-empty" class="welcome-primary">Configure model</button></div><p class="welcome-trust">Add a '+esc(state.profile.provider)+' API key before starting your first task.</p></section>';
      return '<section class="welcome"><div class="welcome-mark" aria-hidden="true">&lt;/&gt;</div><h1>Start coding with CodeAgent</h1><p class="welcome-copy">Inspect this repository, make isolated changes, and validate the result from one place.</p>'+repository+model+'<div class="welcome-actions"><button id="new-empty" class="welcome-primary" '+(workspace?'':'disabled')+'>Start a new task</button><button id="configure-empty" class="welcome-secondary">Model settings</button></div><p class="welcome-trust"><span aria-hidden="true">◇</span> Changes stay in an isolated worktree until you accept them.</p></section>';
    };
    function render(skipCapture=false) {
      if(!skipCapture)captureUiState();
      copyPayloads = [];
      if (settingsOpen) {
        const p=state.profile; const configured=Boolean(state.secretState[p.provider]);
        const effortOptions=provider=>provider==='glm'?['high','max']:['low','high','max'];
        const optionHtml=provider=>effortOptions(provider).map(value=>'<option value="'+value+'" '+(p.reasoningEffort===value?'selected':'')+'>'+value+'</option>').join('');
        app.innerHTML='<style>.settings{display:flex;flex-direction:column;gap:14px;padding:16px 12px}.settings label{display:flex;flex-direction:column;gap:6px;font-weight:600}.settings input,.settings select{display:block;width:100%;height:30px;padding:4px 8px;border:1px solid var(--vscode-input-border,transparent);border-radius:4px;background:var(--vscode-input-background);color:var(--vscode-input-foreground);outline:none}.settings input:focus,.settings select:focus{border-color:var(--vscode-focusBorder)}.settings .toggle{flex-direction:row;align-items:center}.settings .toggle input{width:auto;height:auto}.settings .secret-row{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-top:-8px}.settings .save{width:100%;min-height:32px;padding:6px 12px;border:1px solid var(--vscode-button-border,transparent);border-radius:4px;background:var(--vscode-button-background);color:var(--vscode-button-foreground);cursor:pointer}.settings .save:hover{background:var(--vscode-button-hoverBackground)}.settings .secondary{padding:4px 10px;border:1px solid var(--vscode-button-border,transparent);border-radius:4px;background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground);cursor:pointer}.settings .secondary:hover{background:var(--vscode-button-secondaryHoverBackground)}.settings .hint{margin:0;line-height:1.45}</style><header><button id="settings-back" class="icon" aria-label="Back">←</button><strong>Configuration</strong></header><form id="settings-form" class="settings"><label>Provider<select id="provider"><option value="deepseek" '+(p.provider==='deepseek'?'selected':'')+'>DeepSeek</option><option value="glm" '+(p.provider==='glm'?'selected':'')+'>GLM</option></select></label><label>Model<input id="model" value="'+esc(p.model)+'"></label><label class="toggle"><input id="reasoning-enabled" type="checkbox" '+(p.reasoningEnabled?'checked':'')+'>Enable reasoning</label><label>Reasoning effort<select id="reasoning-effort">'+optionHtml(p.provider)+'</select></label><label>API Key<input id="api-key" type="password" autocomplete="off" placeholder="'+(configured?'Configured — leave blank to keep':'Not configured')+'"></label><div class="secret-row"><span class="muted small">'+(configured?'✓ API key configured':'API key not configured')+'</span>'+(configured?'<button id="clear-key" type="button" class="secondary">Clear</button>':'')+'</div><label>Temperature<input id="temperature" type="number" min="0" max="2" step="0.1" value="'+esc(p.temperature)+'"></label><label>Max output tokens<input id="max-tokens" type="number" min="1" max="131072" value="'+esc(p.maxTokens ?? '')+'"></label><label>Steps per slice<input id="slice-steps" type="number" min="1" max="100" value="'+esc(p.maxStepsPerTurn)+'"></label><label>Steps per user turn<input id="turn-steps" type="number" min="1" max="1000" value="'+esc(p.maxModelStepsPerUserTurn)+'"></label><p class="muted small hint">Saved values apply only to newly created sessions.</p><button type="submit" class="save">Save configuration</button></form>';
        document.getElementById('settings-back').onclick=()=>{settingsOpen=false;render()};
        const syncReasoning=(reset=false)=>{const provider=document.getElementById('provider').value;const effort=document.getElementById('reasoning-effort');const current=reset?'':effort.value;effort.innerHTML=effortOptions(provider).map(value=>'<option value="'+value+'">'+value+'</option>').join('');effort.value=effortOptions(provider).includes(current)?current:'high';effort.disabled=!document.getElementById('reasoning-enabled').checked;};
        document.getElementById('provider').onchange=e=>{document.getElementById('model').value=e.target.value==='glm'?'glm-5.2':'deepseek-v4-flash';syncReasoning(true);};
        document.getElementById('reasoning-enabled').onchange=syncReasoning; syncReasoning();
        document.getElementById('clear-key')?.addEventListener('click',()=>vscode.postMessage({type:'clearApiKey',provider:document.getElementById('provider').value}));
        document.getElementById('settings-form').onsubmit=e=>{e.preventDefault();const optional=id=>{const value=document.getElementById(id).value.trim();return value===''?null:Number(value)};vscode.postMessage({type:'saveSettings',profile:{provider:document.getElementById('provider').value,model:document.getElementById('model').value.trim(),reasoningEnabled:document.getElementById('reasoning-enabled').checked,reasoningEffort:document.getElementById('reasoning-effort').value,temperature:optional('temperature'),maxTokens:optional('max-tokens'),maxStepsPerTurn:Number(document.getElementById('slice-steps').value),maxModelStepsPerUserTurn:Number(document.getElementById('turn-steps').value)},apiKey:document.getElementById('api-key').value});};
        restoreUiState(); return;
      }
      if (historyOpen) {
        const localCount=state.sessions.filter(currentWorkspace).length;
        app.innerHTML = '<header><button id="back" class="icon" aria-label="Back">←</button><strong>Session history</strong><button id="refresh" class="icon" aria-label="Refresh" title="Refresh">↻</button></header><div class="history"><div class="history-search"><span aria-hidden="true">⌕</span><input id="search" value="'+esc(historyQuery)+'" placeholder="Search tasks and repositories" aria-label="Search tasks and repositories"></div><div class="scope-tabs" role="group" aria-label="Session scope"><button id="scope-workspace" class="'+(historyScope==='workspace'?'selected':'')+'">Current <span>'+localCount+'</span></button><button id="scope-all" class="'+(historyScope==='all'?'selected':'')+'">All workspaces <span>'+state.sessions.length+'</span></button></div><label class="history-filter"><span>Status</span><select id="status-filter"><option value="all">All statuses</option><option value="active">Active</option><option value="attention">Needs attention</option><option value="changes">Changes</option></select></label><div id="sessions">' + historyResults() + '</div><p class="history-hint">Sessions from another repository are searchable here. Open that workspace to continue them.</p></div>';
        document.getElementById('back').onclick=()=>{historyOpen=false;render()}; document.getElementById('refresh').onclick=()=>vscode.postMessage({type:'refresh'});
        document.getElementById('status-filter').value=historyStatus;document.getElementById('search').oninput=e=>{historyQuery=e.target.value;renderHistoryResults()};document.getElementById('status-filter').onchange=e=>{historyStatus=e.target.value;renderHistoryResults()};document.getElementById('scope-workspace').onclick=()=>{historyScope='workspace';render()};document.getElementById('scope-all').onclick=()=>{historyScope='all';render()};renderHistoryResults();
        restoreUiState(); return;
      }
      if (!state.detail) { const configured=Boolean(state.secretState[state.profile.provider]);app.innerHTML='<header class="empty-toolbar"><span class="toolbar-spacer"></span><button id="settings" class="icon" aria-label="Configuration" title="Configuration">⚙</button><button id="history" class="icon" aria-label="Chat history" title="Chat history">◷</button></header>'+welcomeView(configured); document.getElementById('history').onclick=()=>{historyOpen=true;render()}; document.getElementById('settings').onclick=()=>{settingsOpen=true;render()}; document.getElementById('configure-empty')?.addEventListener('click',()=>{settingsOpen=true;render()});document.getElementById('new-empty')?.addEventListener('click',()=>vscode.postMessage({type:'newTask'}));document.getElementById('retry-runtime')?.addEventListener('click',()=>vscode.postMessage({type:'retryRuntime'}));document.getElementById('open-output')?.addEventListener('click',()=>vscode.postMessage({type:'openOutput'}));restoreUiState();return; }
      const executionState=state.detail.executionState;const running=executionState!=='idle'; const canContinue=state.detail.availableActions?.canContinue;const canStop=state.detail.availableActions?.canStop;const budget=state.detail.turnBudget;const budgetWaiting=!running&&budget?.waiting;
      const status=state.runtimeError?'Runtime unavailable':executionState==='awaiting_approval'?'Waiting for your approval':executionState==='stopping'?'Stopping after current operation':executionState==='running'?'Working':'';const workspace=state.detail.workspaceLabel||String(state.workspacePath||'').split(/[\\/]/).pop();const attention=state.runtimeError?{title:'Runtime unavailable',message:state.runtimeError,action:'Retry the Runtime or inspect its output.'}:state.detail.globalAttention;const banner=attention?'<section class="global-attention '+esc(attention.kind||'error')+'"><strong>'+esc(attention.title)+'</strong><p>'+esc(attention.message)+'</p>'+(attention.action?'<small>'+esc(attention.action)+'</small>':'')+'<div><button id="open-output">Open Output</button>'+(state.runtimeError?' <button id="retry-runtime">Retry Runtime</button>':'')+'</div></section>':state.operationError?'<section class="global-attention error"><strong>Action could not be completed</strong><p>'+esc(state.operationError)+'</p></section>':'';const composer=budgetWaiting?'<div class="continue-copy"><strong>Step budget reached: '+esc(budget.used)+' / '+esc(budget.limit)+'</strong><small>The request and changes are preserved. Increase the limit to continue this request.</small>'+(state.detail.availableActions?.canIncreaseBudget?'<button id="increase-budget">Increase budget and continue</button>':'<small>No further increase is available. Review the current result or stop this request.</small>')+'<button id="stop">Stop this request</button></div>':canContinue?'<div class="continue-copy"><button id="continue" class="continue">Continue current task</button><small>Resume the unfinished request within its remaining budget.</small><button id="stop">Stop this request</button></div>':executionState==='awaiting_approval'?'<div class="composer-message">Resolve the approval request above to continue.</div>':executionState==='stopping'?'<div class="composer-message">Stopping after the current operation…</div>':'<textarea id="prompt" rows="2" placeholder="'+(running?'CodeAgent is working…':'Ask CodeAgent')+'" '+(running||state.runtimeError||submissionPending?'disabled':'')+'></textarea>'+(canStop?'<button id="stop" aria-label="Stop">■</button>':'<button id="send" aria-label="Send" disabled>↑</button>');
      app.innerHTML='<header><div class="title"><div>' + esc(state.detail.title) + (status?' <span class="header-status">'+esc(status)+'</span>':'')+'</div><small class="muted">'+esc(workspace)+(workspace?' · ':'')+esc(state.detail.provider)+' · '+esc(state.detail.model)+(state.detail.reasoningEnabled?' · reasoning '+esc(state.detail.reasoningEffort):' · reasoning off')+'</small></div><button id="new" class="icon" aria-label="New task" title="New task">＋</button><button id="settings" class="icon" aria-label="Configuration">⚙</button><button id="history" class="icon" aria-label="Chat history" title="Chat history">◷</button></header>'+banner+'<main>' + (state.detail.interruptedControl?'<div class="notice error">'+esc(state.detail.interruptedControl)+'</div>':'') + state.detail.turns.map(turn).join('') + approval(state.detail.pendingApproval) + (running&&executionState!=='awaiting_approval'?'<div class="running-indicator">● '+esc(executionState==='stopping'?'Stopping…':'Agent is working…')+'</div>':'') + '</main><div class="delivery-area">'+currentChanges(state.detail.changesSummary)+'</div><div class="composer">'+composer+'</div><div id="copy-status" class="copy-status" role="status" aria-live="polite"></div>';
      document.getElementById('history').onclick=()=>{historyOpen=true;render()}; document.getElementById('settings').onclick=()=>{settingsOpen=true;render()}; document.getElementById('new').onclick=()=>vscode.postMessage({type:'newTask'});
      document.querySelectorAll('[data-change-file]').forEach(el=>el.onclick=()=>vscode.postMessage({type:'openDiff',path:el.dataset.changeFile}));
      document.querySelectorAll('[data-copy]').forEach(el=>el.onclick=()=>{const copyId=Number(el.dataset.copy);vscode.postMessage({type:'copyText',copyId,text:copyPayloads[copyId]});});
      document.getElementById('accept-changes')?.addEventListener('click',()=>vscode.postMessage({type:'acceptChanges'})); document.getElementById('discard-changes')?.addEventListener('click',()=>vscode.postMessage({type:'discardChanges'}));
      document.getElementById('allow-approval')?.addEventListener('click',()=>vscode.postMessage({type:'resolveApproval',allow:true})); document.getElementById('reject-approval')?.addEventListener('click',()=>vscode.postMessage({type:'resolveApproval',allow:false}));
      document.getElementById('stop-approval')?.addEventListener('click',()=>vscode.postMessage({type:'stop'}));document.getElementById('open-output')?.addEventListener('click',()=>vscode.postMessage({type:'openOutput'}));document.getElementById('retry-runtime')?.addEventListener('click',()=>vscode.postMessage({type:'retryRuntime'}));
      document.getElementById('stop')?.addEventListener('click',()=>vscode.postMessage({type:'stop'}));document.getElementById('continue')?.addEventListener('click',()=>vscode.postMessage({type:'continue'}));document.getElementById('increase-budget')?.addEventListener('click',()=>vscode.postMessage({type:'increaseBudget',sessionId:state.detail.sessionId,turnId:budget.turnId,expectedLimit:budget.limit}));if(!running&&!canContinue&&!budgetWaiting&&!state.runtimeError&&!submissionPending){const prompt=document.getElementById('prompt');const sendButton=document.getElementById('send');const syncSendState=()=>{sendButton.disabled=!prompt.value.trim()||submissionPending;};const send=()=>{const message=prompt.value.trim();if(!message||submissionPending)return;submissionPending=true;draft='';prompt.value='';prompt.disabled=true;sendButton.disabled=true;vscode.postMessage({type:'run',message});};sendButton.onclick=send;prompt.oninput=()=>{draft=prompt.value;syncSendState();};prompt.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();send();}};syncSendState();}
      restoreUiState();
    }
    render();
  </script></body></html>`;
}

function safeJson(value) {
  return JSON.stringify(value).replace(/</g, "\\u003c").replace(/>/g, "\\u003e").replace(/&/g, "\\u0026");
}

function validateProfile(value) {
  const profile = {...DEFAULT_PROFILE, ...(value || {})};
  if (!["deepseek", "glm"].includes(profile.provider)) throw new Error("Provider must be DeepSeek or GLM");
  if (typeof profile.model !== "string" || !profile.model.trim()) throw new Error("Model is required");
  if (typeof profile.reasoningEnabled !== "boolean") throw new Error("Reasoning enabled must be a boolean");
  const efforts = profile.provider === "glm" ? ["high", "max"] : ["low", "high", "max"];
  if (!efforts.includes(profile.reasoningEffort)) throw new Error(`Reasoning effort for ${profile.provider} must be ${efforts.join(", ")}`);
  const supportedModels = profile.provider === "glm" ? ["glm-5.2"] : ["deepseek-v4-flash", "deepseek-v4-pro"];
  if (profile.reasoningEnabled && !supportedModels.includes(profile.model.trim())) throw new Error(`Reasoning is not supported for ${profile.model.trim()}`);
  if (profile.temperature !== null && (!Number.isFinite(profile.temperature) || profile.temperature < 0 || profile.temperature > 2)) throw new Error("Temperature must be between 0 and 2");
  if (profile.maxTokens !== null && (!Number.isInteger(profile.maxTokens) || profile.maxTokens < 1 || profile.maxTokens > 131072)) throw new Error("Max output tokens must be between 1 and 131072");
  if (!Number.isInteger(profile.maxStepsPerTurn) || profile.maxStepsPerTurn < 1 || profile.maxStepsPerTurn > 100) throw new Error("Steps per slice must be between 1 and 100");
  if (!Number.isInteger(profile.maxModelStepsPerUserTurn) || profile.maxModelStepsPerUserTurn < profile.maxStepsPerTurn || profile.maxModelStepsPerUserTurn > 1000) throw new Error("Steps per user turn must be at least the slice steps and at most 1000");
  profile.model = profile.model.trim();
  return profile;
}

function styles() {
  return `:root{color-scheme:light dark}*{box-sizing:border-box}body{font-family:var(--vscode-font-family);font-size:var(--vscode-font-size);color:var(--vscode-foreground);padding:0;margin:0 0 70px}button,input,textarea,select{font:inherit;color:inherit}header{position:sticky;top:0;z-index:3;display:flex;align-items:center;gap:4px;min-height:42px;padding:6px 8px;background:var(--vscode-sideBar-background);border-bottom:1px solid var(--vscode-sideBar-border)}.toolbar-spacer{flex:1}.empty-toolbar{min-height:38px;border-bottom-color:transparent}.title{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-weight:600}.icon{border:0;background:transparent;padding:5px 8px;border-radius:5px;cursor:pointer}.icon:hover{background:var(--vscode-toolbar-hoverBackground)}.welcome{display:flex;min-height:calc(100vh - 110px);max-width:360px;margin:0 auto;padding:72px 24px 28px;flex-direction:column;align-items:center;text-align:center}.welcome-mark{display:flex;width:48px;height:48px;align-items:center;justify-content:center;margin-bottom:18px;border:1px solid var(--vscode-widget-border,var(--vscode-sideBar-border));border-radius:14px;background:var(--vscode-editorWidget-background,var(--vscode-editor-background));color:var(--vscode-textLink-foreground);font-family:var(--vscode-editor-font-family);font-size:17px;font-weight:700;box-shadow:0 5px 18px rgba(0,0,0,.12)}.welcome h1{margin:0 0 10px;font-size:20px;line-height:1.25}.welcome-copy{max-width:310px;margin:0 0 24px;color:var(--vscode-descriptionForeground);line-height:1.5}.welcome-context{width:100%;padding:11px 12px;border:1px solid var(--vscode-widget-border,var(--vscode-sideBar-border));border-radius:8px;background:var(--vscode-editorWidget-background,var(--vscode-editor-background));text-align:left}.welcome-context span{display:block;margin-bottom:3px;color:var(--vscode-descriptionForeground);font-size:11px;text-transform:uppercase;letter-spacing:.04em}.welcome-context strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-family:var(--vscode-editor-font-family);font-size:12px}.welcome-context.warning strong{white-space:normal;color:var(--vscode-editorWarning-foreground)}.welcome-model{display:flex;width:100%;align-items:center;gap:7px;padding:10px 3px 3px;color:var(--vscode-descriptionForeground);font-size:12px}.model-dot{width:7px;height:7px;border-radius:50%;background:var(--vscode-testing-iconPassed)}.welcome-actions{display:flex;width:100%;flex-direction:column;gap:8px;margin-top:18px}.welcome-actions button{width:100%;min-height:34px;padding:7px 12px;border-radius:6px;cursor:pointer}.welcome-actions button:disabled{cursor:not-allowed;opacity:.55}.welcome-primary{border:1px solid var(--vscode-button-border,transparent);background:var(--vscode-button-background);color:var(--vscode-button-foreground)}.welcome-primary:hover:not(:disabled){background:var(--vscode-button-hoverBackground)}.welcome-secondary{border:1px solid var(--vscode-button-secondaryBackground);background:transparent;color:var(--vscode-foreground)}.welcome-secondary:hover{background:var(--vscode-button-secondaryHoverBackground)}.welcome-trust{margin:18px 0 0;color:var(--vscode-descriptionForeground);font-size:11px;line-height:1.45}.welcome-error{max-width:100%;margin:16px 0 0;color:var(--vscode-editorError-foreground);font-family:var(--vscode-editor-font-family);font-size:11px;overflow-wrap:anywhere}main{padding:8px}.turn{padding:10px 0 18px;border-bottom:1px solid var(--vscode-sideBarSectionHeader-border)}.user-row{display:flex;justify-content:flex-end;margin:4px 0 14px}.user-bubble{max-width:82%;padding:8px 11px;border-radius:12px 12px 3px 12px;background:var(--vscode-inputOption-activeBackground);white-space:pre-wrap;overflow-wrap:anywhere}.agent-markdown{line-height:1.5;overflow-wrap:anywhere}.agent-markdown p{margin:8px 0}.agent-markdown h2,.agent-markdown h3,.agent-markdown h4{font-size:1em;margin:12px 0 6px}.agent-markdown pre{padding:8px;overflow:auto;background:var(--vscode-textCodeBlock-background);border-radius:5px}.agent-markdown code{font-family:var(--vscode-editor-font-family)}details{margin:7px 0;color:var(--vscode-descriptionForeground)}summary{cursor:pointer;user-select:none;padding:5px 0}.status{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:7px;background:var(--vscode-testing-iconPassed)}.status.failed{background:var(--vscode-testing-iconFailed)}.status.running{background:var(--vscode-progressBar-background)}.status.denied{background:var(--vscode-editorWarning-foreground)}.tools{margin-left:5px;padding-left:10px;border-left:1px solid var(--vscode-tree-indentGuidesStroke)}.tool{display:flex;gap:8px;padding:6px 0;color:var(--vscode-foreground)}.tool-icon{width:12px}.tool-body{min-width:0;flex:1}.tool-name{font-family:var(--vscode-editor-font-family);font-weight:600;color:var(--vscode-symbolIcon-functionForeground)}.tool-args{margin-top:2px;color:var(--vscode-descriptionForeground);font-family:var(--vscode-editor-font-family);font-size:.9em;white-space:pre-wrap;overflow-wrap:anywhere}.small{font-size:.9em}.muted{color:var(--vscode-descriptionForeground)}.notice{margin:7px 0;padding:7px;border-left:2px solid var(--vscode-editorWarning-foreground);background:var(--vscode-textBlockQuote-background)}.notice.error{border-color:var(--vscode-editorError-foreground)}.result-card{margin-top:10px;border:1px solid var(--vscode-sideBar-border);border-radius:6px;overflow:hidden}.result-title{font-weight:600;padding:7px 9px;background:var(--vscode-sideBarSectionHeader-background)}.file{padding:5px 9px;border-top:1px solid var(--vscode-sideBar-border);font-family:var(--vscode-editor-font-family);font-size:.9em}.validation{margin:8px;padding:0 8px;color:var(--vscode-descriptionForeground)}.validation.passed{color:var(--vscode-testing-iconPassed)}.validation.failed{color:var(--vscode-testing-iconFailed)}.running-indicator{padding:10px;color:var(--vscode-progressBar-background)}.composer{position:fixed;bottom:0;left:0;right:0;z-index:4;display:flex;gap:6px;padding:8px;background:var(--vscode-sideBar-background);border-top:1px solid var(--vscode-sideBar-border)}.composer textarea{flex:1;resize:none;padding:7px;background:var(--vscode-input-background);border:1px solid var(--vscode-input-border);border-radius:6px}.composer button{border:0;border-radius:6px;padding:7px 11px;background:var(--vscode-button-background);color:var(--vscode-button-foreground)}.composer button:disabled,.composer textarea:disabled{opacity:.6}.continue{width:100%}.history{padding:10px}.history-search{display:flex;align-items:center;gap:7px;padding:0 9px;border:1px solid var(--vscode-input-border,transparent);border-radius:6px;background:var(--vscode-input-background)}.history-search:focus-within{border-color:var(--vscode-focusBorder)}.history-search span{color:var(--vscode-descriptionForeground);font-size:16px}.history-search input{width:100%;min-width:0;margin:0;padding:8px 0;border:0;outline:0;background:transparent}.scope-tabs{display:grid;grid-template-columns:1fr 1.35fr;gap:3px;margin:10px 0;padding:3px;border-radius:7px;background:var(--vscode-editorWidget-background,var(--vscode-editor-background))}.scope-tabs button{padding:6px;border:0;border-radius:5px;background:transparent;color:var(--vscode-descriptionForeground);cursor:pointer;font-size:12px}.scope-tabs button.selected{background:var(--vscode-list-activeSelectionBackground);color:var(--vscode-list-activeSelectionForeground)}.scope-tabs span{margin-left:3px;opacity:.75}.history-filter{display:flex;align-items:center;justify-content:space-between;gap:8px;margin:10px 2px 14px;color:var(--vscode-descriptionForeground);font-size:12px}.history-filter select{max-width:170px;padding:4px 24px 4px 7px;border:1px solid var(--vscode-dropdown-border,transparent);border-radius:4px;background:var(--vscode-dropdown-background);color:var(--vscode-dropdown-foreground)}.session-group{margin:8px 0 16px}.group-title{display:flex;justify-content:space-between;padding:4px 8px;color:var(--vscode-descriptionForeground);font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.035em}.group-title span{font-weight:400}.session{display:flex;width:100%;gap:8px;align-items:flex-start;text-align:left;border:0;background:transparent;padding:9px 8px;border-radius:6px;cursor:pointer}.session>span{flex:1;min-width:0}.session>small{display:flex;flex-direction:column;align-items:flex-end;gap:5px;color:var(--vscode-descriptionForeground)}.session-title,.session-meta{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.session-meta{margin-top:3px;color:var(--vscode-descriptionForeground);font-size:11px}.session:hover{background:var(--vscode-list-hoverBackground)}.session.active{background:var(--vscode-list-activeSelectionBackground);color:var(--vscode-list-activeSelectionForeground)}.session.remote-session{cursor:not-allowed;opacity:.62}.workspace-lock{font-size:11px}.history-empty{display:flex;padding:42px 12px;flex-direction:column;gap:5px;align-items:center;color:var(--vscode-descriptionForeground);text-align:center}.history-empty strong{color:var(--vscode-foreground)}.history-hint{margin:18px 8px;color:var(--vscode-descriptionForeground);font-size:11px;line-height:1.45}`;
}

function deactivate() {}

module.exports = {
  activate, deactivate, renderChat, safeJson, styles, ChatViewProvider, RuntimeConnection, sameWorkspace,
  renderToolFailureHtml, renderChangesHtml, validationLabel, validateProfile,
};
