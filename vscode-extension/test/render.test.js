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

const { renderChat, ChatViewProvider, sameWorkspace } = require("../extension.js");

test("generated webview script is valid JavaScript", () => {
  const html = renderChat({sessionId: "s1", title: "Demo", turns: []}, []);
  const match = html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/);

  assert.ok(match);
  assert.doesNotThrow(() => new Function(match[1]));
  assert.match(html, /Loading CodeAgent/);
  assert.match(html, /"temperature":0\.2/);
  assert.match(html, /class="save"/);
  assert.match(html, /productState/);
  assert.match(html, /captureUiState/);
  assert.match(html, /Continue current task/);
  assert.match(html, /Current Delivery/);
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
