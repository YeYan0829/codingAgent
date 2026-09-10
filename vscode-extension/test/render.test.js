"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const Module = require("module");
const vscodeMock = {};

const originalLoad = Module._load;
Module._load = function(request, parent, isMain) {
  if (request === "vscode") return vscodeMock;
  return originalLoad(request, parent, isMain);
};

const {
  renderChat, ChatViewProvider, sameWorkspace, renderToolFailureHtml, renderChangesHtml, validateProfile,
} = require("../extension.js");

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

test("generated webview script is valid JavaScript", () => {
  const html = renderChat({sessionId: "s1", title: "Demo", turns: []}, []);
  const match = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/);

  assert.ok(match);
  assert.doesNotThrow(() => new Function(match[1]));
  assert.match(html, /Loading CodeAgent/);
  assert.match(html, /"temperature":0\.2/);
  assert.match(html, /class="save"/);
  assert.match(html, /Enable reasoning/);
  assert.match(html, /Reasoning effort/);
  assert.match(html, /reasoningEnabled/);
  assert.match(html, /productState/);
  assert.match(html, /captureUiState/);
  assert.match(html, /Continue current task/);
  assert.match(html, /Changes ready for review/);
  assert.match(html, /failure\.title/);
  assert.match(html, /Show output/);
  assert.match(html, /Long output was truncated/);
  assert.match(html, /Validation command succeeded at that time/);
  assert.match(html, /Review Diff/);
  assert.match(html, /renderChangesHtml/);
  assert.match(html, /Waiting for your approval/);
  assert.match(html, /Copy user message/);
  assert.match(html, /Copy agent response/);
  assert.match(html, /submissionRejected/);
  assert.match(html, /e\.isComposing/);
  assert.match(html, /Allow command permissions\?/);
  assert.match(html, /Create isolated workspace\?/);
  assert.match(html, /Start coding with CodeAgent/);
  assert.match(html, /Changes stay in an isolated worktree until you accept them/);
  assert.match(html, /class=\"welcome-primary\"/);
  assert.doesNotMatch(html, /No CodeAgent sessions found\.<br>/);
  assert.match(html, /Search tasks and repositories/);
  assert.match(html, /All workspaces/);
  assert.match(html, /Needs attention/);
  assert.match(html, /Open that workspace to continue them/);
  assert.match(html, /No sessions in this Session Root/);
});

test("model profile validates provider-specific reasoning settings", () => {
  const glm = validateProfile({
    provider: "glm", model: "glm-5.2", reasoningEnabled: true, reasoningEffort: "max",
  });
  assert.equal(glm.reasoningEnabled, true);
  assert.equal(glm.reasoningEffort, "max");
  assert.throws(() => validateProfile({
    provider: "glm", model: "glm-5.2", reasoningEnabled: true, reasoningEffort: "low",
  }), /high, max/);
  assert.throws(() => validateProfile({
    provider: "deepseek", model: "custom", reasoningEnabled: true, reasoningEffort: "high",
  }), /not supported/);
});

test("reasoning profile is saved and restored from global state", async () => {
  const updates = [];
  let stored = {
    provider: "glm", model: "glm-5.2", reasoningEnabled: true, reasoningEffort: "max",
    temperature: 0.2, maxTokens: 4000, maxStepsPerTurn: 12, maxModelStepsPerUserTurn: 48,
  };
  const context = {
    globalState: {
      get: key => key === "productProfile" ? stored : undefined,
      update: async (key, value) => { updates.push([key, value]); stored = value; },
    },
    secrets: {get: async () => undefined, store: async () => {}},
  };
  const runtime = {restart: () => { throw new Error("unexpected restart"); }};
  vscodeMock.window = {setStatusBarMessage: () => {}};
  const view = new ChatViewProvider(runtime, "/work/current", context, {});

  assert.equal(view.profile.reasoningEnabled, true);
  assert.equal(view.profile.reasoningEffort, "max");
  await view.saveSettings({
    provider: "deepseek", model: "deepseek-v4-pro", reasoningEnabled: true, reasoningEffort: "low",
    temperature: 0.2, maxTokens: 4000, maxStepsPerTurn: 12, maxModelStepsPerUserTurn: 48,
  }, "");

  assert.equal(updates[0][0], "productProfile");
  assert.equal(updates[0][1].reasoningEffort, "low");
  const restored = new ChatViewProvider(runtime, "/work/current", context, {});
  assert.equal(restored.profile.reasoningEnabled, true);
  assert.equal(restored.profile.reasoningEffort, "low");
});

test("legacy GLM profile without an effort migrates to high", () => {
  const context = {
    globalState: {get: () => ({provider: "glm", model: "glm-5.3", reasoningEnabled: true})},
    secrets: {get: async () => undefined, store: async () => {}},
  };
  const view = new ChatViewProvider({}, "/work/current", context, {});

  assert.equal(view.profile.reasoningEffort, "high");
});

test("workspace identity is normalized before session access", () => {
  assert.equal(sameWorkspace("/tmp/project/../project", "/tmp/project"), true);
  assert.equal(sameWorkspace("/tmp/project", "/tmp/other"), false);
  assert.equal(sameWorkspace("", "/tmp/project"), false);
});

test("refresh discovers global history but opens only the newest current-workspace session", async () => {
  const requests = [];
  const summaries = [
    {sessionId: "foreign", workspace: "/work/other", title: "Other task"},
    {sessionId: "local", workspace: "/work/current", title: "Current task"},
  ];
  const view = {
    workspacePath: "/work/current", sessions: [], current: undefined, runtimeError: "old", operationError: "old",
    runtime: {ensure: async () => {}, client: {request: async (method, params) => {
      requests.push([method, params]);
      if (method === "session/list") return {sessions: summaries};
      return {...summaries[1], changesSummary: {available: false}};
    }}},
    _hydrateChanges: async () => {}, _render: () => {},
  };

  await ChatViewProvider.prototype.refresh.call(view);

  assert.deepEqual(requests, [["session/list", {}], ["session/get", {sessionId: "local"}]]);
  assert.equal(view.current.sessionId, "local");
  assert.equal(view.sessions.length, 2);
});

test("cross-workspace session cannot be opened from the current window", async () => {
  let requested = false;
  const view = {
    workspacePath: "/work/current",
    sessions: [{sessionId: "foreign", workspace: "/work/other"}],
    runtime: {ensure: async () => {}, client: {request: async () => { requested = true; }}},
  };

  await assert.rejects(ChatViewProvider.prototype.openSession.call(view, "foreign"), /Open this session's workspace/);
  assert.equal(requested, false);
});

test("embedded state escapes script-breaking markup", () => {
  const html = renderChat({sessionId: "s1", title: "</script><script>bad()</script>", turns: []}, []);

  assert.doesNotMatch(html, /<script>bad\(\)<\/script>/);
  assert.match(html, /\\u003c\/script\\u003e/);
});

test("context condensation renders as a compact system item with expandable details", () => {
  const html = renderChat({sessionId: "s1", title: "Context", turns: [{
    turnId: "t1", userMessage: "Continue", changedFiles: [], validation: null, attention: [],
    items: [{type: "contextSummary", title: "Historical context summarized",
      message: "40 earlier events · ~512 token summary", summary: "GOAL: finish fix",
      reason: "soft_limit", retryCount: 1, condenser: {model: "glm-5.3"}, eventSeq: 7}],
  }]}, []);

  assert.match(html, /Historical context summarized/);
  assert.match(html, /40 earlier events/);
  assert.match(html, /Context details/);
  assert.match(html, /GOAL: finish fix/);
  assert.doesNotMatch(html, /agent-markdown">GOAL: finish fix/);
});

test("composer clears submitted text and restores it only after rejection", () => {
  const html = renderChat({sessionId: "s1", title: "Demo", turns: []}, []);
  const script = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1];

  assert.match(script, /submissionPending=true;draft='';prompt\.value='';prompt\.disabled=true/);
  assert.match(script, /submissionRejected/);
  assert.match(script, /if\(!draft\)draft=String\(data\.message\|\|''\)/);
  assert.match(script, /sendButton\.disabled=!prompt\.value\.trim\(\)\|\|submissionPending/);
});

test("message copy uses the extension-host clipboard bridge", () => {
  const html = renderChat({sessionId: "s1", title: "Demo", turns: []}, []);

  assert.match(html, /type:'copyText'/);
  assert.match(html, /copyPayloads\[copyId\]/);
  assert.match(html, /role="status" aria-live="polite"/);
  assert.doesNotMatch(html, /data-copy-text/);
});

test("selecting a history item closes history before opening the session", () => {
  const html = renderChat({sessionId: "s1", title: "Current", turns: []}, [
    {sessionId: "s1", workspace: "/work/current", title: "Current", isCurrentWorkspace: true},
    {sessionId: "s2", workspace: "/work/current", title: "Previous", isCurrentWorkspace: true},
  ]);
  const script = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1];

  assert.match(script, /el\.onclick=\(\)=>\{historyOpen=false;vscode\.postMessage\(\{type:'selectSession',sessionId:el\.dataset\.session\}\);\}/);
});

test("failed tool rendering shows the reason and expandable bounded output", () => {
  const html = renderToolFailureHtml({failure: {
    kind: "command_exit_nonzero",
    title: "Command exited with code 1",
    summary: "ZeroDivisionError: division by zero",
    output: "traceback",
    outputTruncated: true,
  }}, "turn:tool", esc);

  assert.match(html, /Command exited with code 1/);
  assert.match(html, /ZeroDivisionError: division by zero/);
  assert.match(html, /Show output/);
  assert.match(html, /traceback/);
  assert.match(html, /Long output was truncated/);
});

test("applied changes keep Review Diff and remove delivery actions", () => {
  const html = renderChangesHtml({
    state: "applied", available: true, fileCount: 1, additions: 2, deletions: 0,
    fileStats: [{path: "calculator.py", additions: 2, deletions: 0, previewAvailable: true}],
    validation: {status: "passed", command: "python3 verify.py", historical: true},
  }, {availableActions: {}, actionReasons: {}}, esc);

  assert.match(html, /Applied · 1 file/);
  assert.match(html, /calculator.py/);
  assert.match(html, /Review Diff/);
  assert.match(html, /Validation command succeeded at that time/);
  assert.doesNotMatch(html, /id="accept-changes"/);
  assert.doesNotMatch(html, /id="discard-changes"/);
});

test("pending changes remain a first-class card with delivery actions", () => {
  const html = renderChangesHtml({
    state: "pending", available: true, fileCount: 1, files: ["calculator.py"],
    validation: {status: "passed", appliesToCurrentChanges: true},
  }, {availableActions: {canAcceptChanges: true, canDiscardChanges: true}, actionReasons: {}}, esc);

  assert.match(html, /Changes ready for review · 1 file/);
  assert.match(html, /Review Diff/);
  assert.match(html, /Current revision has validation evidence/);
  assert.match(html, /id="accept-changes"/);
  assert.match(html, /id="discard-changes"/);
});

test("discarded changes show a receipt without current review actions", () => {
  const html = renderChangesHtml(
    {state: "discarded", available: false},
    {deliveryReceipt: {kind: "discarded", message: "Agent changes discarded; source unchanged"}},
    esc,
  );

  assert.match(html, /Agent changes discarded; source unchanged/);
  assert.doesNotMatch(html, /Review Diff/);
  assert.doesNotMatch(html, /id="accept-changes"/);
  assert.doesNotMatch(html, /id="discard-changes"/);
});

test("budget increase is an explicit control request and cancellation does not run", async () => {
  const calls = [];
  let options;
  let answer;
  vscodeMock.window = {showInputBox: async value => {options = value; return answer;}};
  const view = {
    current: {sessionId: "s1", availableActions: {canIncreaseBudget: true},
      turnBudget: {turnId: "t1", used: 48, limit: 48, maxLimit: 1000}},
    runtime: {ensure: async () => {}, client: {request: async (...args) => calls.push(args)}},
    refresh: async () => {},
  };
  const message = {sessionId: "s1", turnId: "t1", expectedLimit: 48};
  await ChatViewProvider.prototype.increaseBudget.call(view, message);
  assert.deepEqual(calls, []);
  assert.equal(options.value, "");
  assert.equal(options.validateInput("96"), undefined);
  assert.equal(options.validateInput("+24"), undefined);
  for (const invalid of ["+0", "+953", "48", "0", "1001", "3.5", "NaN", ""]) assert.ok(options.validateInput(invalid));
  answer = "+24";
  await ChatViewProvider.prototype.increaseBudget.call(view, message);
  assert.deepEqual(calls, [["session/increaseBudget", {sessionId: "s1", turnId: "t1", expectedLimit: 48, additionalSteps: 24}]]);
  answer = "96";
  await ChatViewProvider.prototype.increaseBudget.call(view, message);
  assert.deepEqual(calls[1], ["session/increaseBudget", {sessionId: "s1", turnId: "t1", expectedLimit: 48, newLimit: 96}]);
  await assert.rejects(ChatViewProvider.prototype.increaseBudget.call(view, {...message, turnId: "old"}), /changed/);
  assert.equal(calls.length, 2);
});
