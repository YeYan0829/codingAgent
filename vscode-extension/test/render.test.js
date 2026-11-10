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
  renderChat, ChatViewProvider, sameWorkspace, renderToolFailureHtml, renderContextSummaryHtml,
  renderTruncationNoticeHtml, renderChangesHtml, renderTurnHtml, validateProfile,
} = require("../extension.js");

const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[c]));

test('identical product snapshots preserve DOM; changed error and session content still refresh', () => {
  const messages = [];
  const view = new ChatViewProvider({}, '/work/current', {globalState: {get() {}}}, {});
  view.view = {webview: {postMessage: message => messages.push(message)}};
  view.rendered = true;
  view.current = {sessionId: 's', title: 'original', turns: []};
  view._render(); view._render();
  assert.equal(messages.length, 1);
  view.operationError = 'failed'; view._render();
  assert.equal(messages.length, 2);
  view.current.title = 'updated'; view._render();
  assert.equal(messages.length, 3);
  assert.equal(messages.at(-1).state.detail.title, 'updated');
});

test('view resolution delivers CSP HTML before connecting and keeps initialization failure inside the view',async()=>{
  const messages=[],logs=[];
  let html='',initialHtml;
  const runtime={output:{appendLine:text=>logs.push(text)},ensure:async()=>{initialHtml=html;},
    client:{request:async()=>{throw new Error('method not found: workspace/access/get');}}};
  const provider=new ChatViewProvider(runtime,'/demo',{globalState:{get(){}},secrets:{get:async()=>undefined}},{});
  const view={webview:{onDidReceiveMessage(){},postMessage:value=>messages.push(value),set html(value){html=value;}}};
  await assert.doesNotReject(provider.resolveWebviewView(view));
  assert.match(initialHtml,/Content-Security-Policy/);
  assert.match(provider.runtimeError,/workspace\/access\/get/);
  assert.match(messages.at(-1).state.runtimeError,/workspace\/access\/get/);
  assert.match(logs.at(-1),/initialization\/refresh failed/);
});

test('secret storage failure also leaves the view open with a recoverable error',async()=>{
  let html='';
  const provider=new ChatViewProvider({},'/demo',{globalState:{get(){}},secrets:{get:async()=>{throw new Error('SecretStorage unavailable');}}},{});
  await assert.doesNotReject(provider.resolveWebviewView({webview:{onDidReceiveMessage(){},postMessage(){},set html(value){html=value;}}}));
  assert.match(html,/Content-Security-Policy/);assert.equal(provider.runtimeError,'SecretStorage unavailable');
});

test('F5 overrides stale global Runtime settings only in a development extension host',async()=>{
  const {RuntimeConnection}=require('../extension');
  const previousCommand=process.env.CODEAGENT_DEV_RPC_COMMAND,previousRoot=process.env.CODEAGENT_DEV_SESSION_ROOT;
  vscodeMock.ExtensionMode={Development:2,Production:1};
  vscodeMock.workspace={getConfiguration:()=>({get:(key,fallback)=>({rpcCommand:'/old-release/rpc',sessionRoot:'/old-sessions'}[key]??fallback)})};
  const context={extensionPath:require('node:path').resolve(__dirname,'..'),extensionMode:2,secrets:{get:async()=>undefined}};
  try {
    process.env.CODEAGENT_DEV_RPC_COMMAND='auto';process.env.CODEAGENT_DEV_SESSION_ROOT='/demo/sessions';
    const dev=await new RuntimeConnection(context,'/demo',{})._configuration();
    assert.match(dev.command,/\.venv\/bin\/python$/);assert.deepEqual(dev.args,['-m','codeagent.product.rpc','--session-root','/demo/sessions']);
    const production=await new RuntimeConnection({...context,extensionMode:1},'/demo',{})._configuration();
    assert.equal(production.command,'/old-release/rpc');assert.deepEqual(production.args,['--session-root','/old-sessions']);
  } finally {
    if(previousCommand===undefined)delete process.env.CODEAGENT_DEV_RPC_COMMAND;else process.env.CODEAGENT_DEV_RPC_COMMAND=previousCommand;
    if(previousRoot===undefined)delete process.env.CODEAGENT_DEV_SESSION_ROOT;else process.env.CODEAGENT_DEV_SESSION_ROOT=previousRoot;
  }
});

test('Runtime compatibility failure closes the old process before unsupported RPC calls',async()=>{
  const {RuntimeConnection}=require('../extension');
  let disposed=false;
  const runtime=new RuntimeConnection({},'/demo',{});
  runtime._configuration=async()=>({signature:'old',command:'/old-release/rpc'});runtime.signature='old';
  runtime.client={start:async()=>{},initialization:{runtimeVersion:'0.5.0',capabilities:{}},dispose:async()=>{disposed=true;}};
  await assert.rejects(runtime.ensure(),/Runtime 0\.5\.0.*workspace\/access\/get.*\/old-release\/rpc/);
  assert.equal(disposed,true);assert.equal(runtime.client,undefined);
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

  assert.deepEqual(requests, [["workspace/access/get", {workspace: "/work/current"}], ["session/list", {}], ["session/get", {sessionId: "local"}]]);
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


test("context condensation renders as a short product note without diagnostics", () => {
  const html = renderContextSummaryHtml({
    title: "Historical context summarized", message: "40 earlier events · ~512 token summary",
    summary: "USER_REQUIREMENTS: finish fix", reason: "soft_limit", retryCount: 1,
    condenser: {model: "private-condenser"}, eventSeq: 7,
  });

  assert.match(html, /Earlier activity was summarized to keep this task running\./);
  assert.doesNotMatch(html, /Historical context summarized|Context details|USER_REQUIREMENTS|private-condenser/);
});


test("output truncation recovery renders as a short product note without diagnostics", () => {
  const item = {type: "notice", kind: "truncationRecovery", tone: "info",
    message: "Runtime recovered from 2 truncated model outputs.",
    records: [{finishReason: "length", originalReasoningEffort: "high",
      recoveryReasoningEffort: "low"}], eventSeq: 8};
  const html = renderTruncationNoticeHtml(item);
  const warning = renderTruncationNoticeHtml({...item, tone: "warning"});

  assert.match(html, /Continued automatically after a model response was cut short\./);
  assert.match(warning, /Tried to continue automatically after a model response was cut short\./);
  assert.doesNotMatch(html + warning, /Recovery details|finishReason|ReasoningEffort|Runtime recovered/);
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
  assert.match(html, /查看输出/);
  assert.match(html, /traceback/);
  assert.match(html, /过长输出已截断/);
});


test("refresh ignores an older response after a newer session selection", async () => {
  let release;
  const old = new Promise(resolve => { release = resolve; });
  let calls = 0;
  const context = {globalState: {get: () => undefined}, secrets: {get: async () => undefined}};
  const runtime = {ensure: async () => {}, client: {request: async method => {
    if (method === "session/list") return {sessions: [{sessionId: "new", workspace: "/work/current"}]};
    if (++calls === 1) return old;
    return {sessionId: "new", workspace: "/work/current", changesSummary: {available: false}};
  }}};
  const view = new ChatViewProvider(runtime, "/work/current", context, {});
  view._render = () => {};
  const first = view.refresh();
  await new Promise(resolve => setImmediate(resolve));
  await view.refresh();
  release({sessionId: "old", workspace: "/work/current", changesSummary: {available: false}});
  await first;
  assert.equal(view.current.sessionId, "new");
});


test("background notifications coalesce while a refresh is in flight", async () => {
  const context = {globalState: {get: () => undefined}, secrets: {get: async () => undefined}};
  const view = new ChatViewProvider({}, "/work/current", context, {});
  view.current = {sessionId: "s"};
  let release, calls = 0;
  const blocked = new Promise(resolve => { release = resolve; });
  view.refresh = async () => { calls++; if (calls === 1) await blocked; };
  view.handleNotification("session/event", {sessionId: "s"});
  await new Promise(resolve => setTimeout(resolve, 100));
  for (let i = 0; i < 10; i++) view.handleNotification("session/event", {sessionId: "s"});
  assert.equal(calls, 1);
  release();
  await new Promise(resolve => setTimeout(resolve, 110));
  assert.equal(calls, 2);
});


test('operation targets and live states are visible while output stays collapsed', () => {
  const {renderActivityHtml} = require('../extension.js');
  const item = {summary: '读取文件 2 次 · 运行 1 条命令 · 子任务操作 1 次', defaultExpanded: true, tools: [
    {callId: 'read', tool: 'read_file', status: 'succeeded', argumentSummary: 'README.md'},
    {callId: 'spawn', tool: 'spawn_agent', status: 'submitted', argumentSummary: '审查代码'},
    {callId: 'cmd', tool: 'run_command', status: 'failed', argumentSummary: 'python3 verify.py', failure: {
      title: 'Command exited with code 1', summary: 'ZeroDivisionError', output: 'LONG TRACEBACK', kind: 'command_exit_nonzero',
    }},
    {callId: 'current', tool: 'read_file', status: 'running', argumentSummary: 'calculator.py'},
  ]};
  const html = renderActivityHtml(item, 't:1', esc, '', null, 'running');
  const visible = defaultText(html);
  assert.equal((html.match(/class="work-step"/g) || []).length, 4);
  assert.match(visible, /读取 README.md.*完成/);
  assert.match(visible, /读取 calculator.py.*执行中/);
  assert.match(visible, /python3 verify.py/);
  assert.match(visible, /ZeroDivisionError/);
  assert.doesNotMatch(visible, /LONG TRACEBACK|spawn_agent|✓/);
  assert.match(html, /class="work-icon"[^>]*><svg/);
  assert.match(html, /已受理/);
});


test('operation row supplies live progress without a duplicate footer', () => {
  const {renderLiveProgressHtml} = require('../extension.js');
  const turn = {turnId: 'old', userMessage: 'read', items: [{type: 'activityGroup', eventSeq: 2,
    tools: [{tool: 'read_file', callId: 'pending', status: 'running', argumentSummary: 'README.md'}]}]};
  const detail = {turns: [turn], executionState: 'idle'};
  assert.equal(renderLiveProgressHtml(detail, esc), '');
  detail.executionState = 'running';
  assert.equal(renderLiveProgressHtml(detail, esc), '');
  detail.turns.push({turnId: 'new', items: []});
  assert.doesNotMatch(defaultText(renderLiveProgressHtml(detail, esc)), /README.md/);
  detail.executionState = 'awaiting_approval';
  assert.equal(renderLiveProgressHtml(detail, esc), '');
});


test('legacy demo wording has a neutral display while its original response remains inspectable', () => {
  const {renderTurnHtml} = require('../extension.js');
  const turn = {turnId: 't', items: [{type: 'markdown', eventSeq: 2, text: 'Free protocol fixture finished: internal diagnostics', displayText: '本次演练已结束。'}]};
  const html = renderTurnHtml(turn, {turns: [turn]}, esc, esc, () => '');
  assert.match(defaultText(html), /本次演练已结束/);
  assert.doesNotMatch(defaultText(html), /Free protocol fixture/);
  assert.doesNotMatch(html, /原始回复|Free protocol fixture|诊断信息/);
  const diagnostics = require('../extension').renderSessionDetailsHtml({turns: [turn]}, esc);
  assert.match(diagnostics, /原始回复/);
  assert.match(diagnostics, /Free protocol fixture/);
});


test('known reserved and unknown fees stay in session information', () => {
  const {renderSessionDetailsHtml} = require('../extension');
  const html = renderSessionDetailsHtml({orchestration: {totalUsage: {requests: 4, knownTokens: 20,
    unknownUsageRequests: 1, fees: [{currency: 'USD', settledMicrounits: 20, reservedMicrounits: 30, unknownMicrounits: 100, unknownRequests: 1}]}}}, esc);
  assert.match(html, /4 次请求.*20 tokens.*1 次用量未知/);
  assert.match(html, /USD.*已结算 20.*预留 30.*未知 100/);
});


test('repeated waits for the same targets share one stable row and the latest result', () => {
  const {renderActivityHtml} = require('../extension');
  const item = {tools: [{tool: 'wait_agents', callId: 'w1', waitRunIds: ['r'], waitMode: 'all', status: 'submitted', waitSatisfied: false},
    {tool: 'wait_agents', callId: 'w2', waitRunIds: ['r'], waitMode: 'all', status: 'running'}]};
  let html = renderActivityHtml(item, 'turn:1', esc, '', null, 'running');
  assert.equal((html.match(/class="work-step"/g) || []).length, 1);
  assert.match(defaultText(html), /等待子任务的结果.*等待中/);
  assert.match(html, /data-disclosure="turn:1:step:w1"/);
  Object.assign(item.tools[1], {status: 'submitted', waitSatisfied: true});
  html = renderActivityHtml(item, 'turn:1', esc, '', null, 'idle');
  assert.match(defaultText(html), /等待结束/);
  assert.doesNotMatch(defaultText(html), /等待超时/);
  assert.match(html, /data-disclosure="turn:1:step:w1"/);
});


test('background child does not hide parent work and cross-turn history stays in session information', () => {
  const {renderLiveProgressHtml, renderTurnHtml} = require('../extension');
  const task = demoDelegation(); task.runs = [task.runs[0]];
  Object.assign(task.runs[0], {status: 'running', terminalAck: false, execution: 'background'});
  const turn = {turnId: 'turn', delegation: task, items: [{type: 'activityGroup', eventSeq: 1,
    tools: [{tool: 'spawn_agent', callId: 's', runId: 'run-1', status: 'submitted'}]}]};
  const other = {...task.runs[0], rootTurnId: 'next', runId: 'next-run', status: 'completed', terminalAck: true, goal: '另一轮的续问'};
  const detail = {turns: [turn], executionState: 'running', orchestration: {tasks: [task, {runs: [other]}]}};
  assert.match(defaultText(renderLiveProgressHtml(detail, esc)), /主任务处理中/);
  const html = renderTurnHtml(turn, detail, esc, esc, () => '');
  assert.doesNotMatch(html, /next-run|同一调查的其他执行|另一轮的续问/);
  const diagnostics = require('../extension').renderSessionDetailsHtml(detail, esc);
  assert.match(diagnostics, /diagnostic-run-next-run/);
  assert.match(diagnostics, /另一轮的续问/);
  assert.doesNotMatch(defaultText(html), /另一轮的续问/);
});



test('new UI script parses and removes retired budget and delivery controls',()=>{
  const html=renderChat({sessionId:'s',turns:[],executionState:'idle',availableActions:{}},[]);
  assert.doesNotThrow(()=>new Function(html.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1]));
  const {renderSettings,renderNavigation}=require('../ui');
  const nav=renderNavigation('标题',false);
  assert.match(nav,/nav-history/);assert.match(nav,/nav-new/);assert.doesNotMatch(nav,/data-nav/);
  const settings=renderSettings(validateProfile({provider:'fake',model:'c3-fixture'}),{fake:true},esc);
  assert.match(settings,/delegation-enabled/);assert.doesNotMatch(settings,/预算|LangGraph|turn-steps|fee-limit/);
});

test('legacy profile migration keeps delegation preference without sending limits',()=>{
  const provider=new ChatViewProvider({},'/repo',{globalState:{get:()=>({provider:'glm',model:'glm-5.3',reasoningEnabled:true,orchestrationBackend:'langgraph',maxFeeMicrounits:123})}},{});
  assert.equal(provider.profile.delegationEnabled,true);
  assert.equal(provider.profile.maxFeeMicrounits,undefined);
  assert.equal(provider.profile.orchestrationBackend,undefined);
  assert.equal(validateProfile({provider:'glm',model:'glm-5.3',reasoningEnabled:true,reasoningEffort:'low'}).reasoningEffort,'low');
});

test('synchronous task is linked to a child before task receipt',()=>{
  const {activitySteps,renderChildRunHtml}=require('../extension');
  assert.equal(activitySteps([{tool:'task',status:'running'}])[0].kind,'task');
  const html=renderChildRunHtml({runId:'r',agentId:'a',description:'定位入口',role:'code-explorer',status:'running',terminalAck:false,
    currentActivity:{tool:'read_file',argumentSummary:'README.md'},items:[{type:'markdown',text:'公开说明'}]},null,esc,esc,'running');
  assert.match(html,/定位入口/);assert.match(html,/主 Agent 等待此子任务/);assert.match(html,/code-explorer/);assert.match(html,/公开说明/);assert.doesNotMatch(html,/快照|调查 A/);
});

test('current edit review exposes controlled undo and no retired delivery buttons',()=>{
  const html=renderChangesHtml({available:true,state:'current_project',files:['src/a.py'],fileCount:1,undoFiles:['src/a.py']},{availableActions:{canUndo:true}},esc);
  assert.match(html,/已修改 1 个文件/);assert.match(html,/已写入当前项目/);assert.match(html,/aria-label="撤销本轮受控编辑"/);assert.match(html,/data-change-file="src\/a.py"/);assert.doesNotMatch(html,/应用到仓库|丢弃修改/);
});

test('committed undo retains a small status after the last edit card disappears',()=>{
  const html=renderChangesHtml({available:false,state:'current_project',lastUndo:{fileCount:3}},{},esc);
  assert.match(html,/<p class="edit-feedback" role="status"/);
  assert.match(html,/已撤销 3 个文件/);
  assert.doesNotMatch(html,/id="undo-edits"|已修改/);
  assert.equal(renderChangesHtml({available:false,state:'current_project',lastUndo:null},{},esc),'');
  const remaining=renderChangesHtml({available:true,state:'current_project',lastUndo:{fileCount:1},files:['older.py'],fileCount:1},{availableActions:{canUndo:true}},esc);
  assert.match(remaining,/已撤销 1 个文件/);
  assert.match(remaining,/data-change-file="older.py"/);
});

test('current edit review escapes file labels and explains disabled undo',()=>{
  const html=renderChangesHtml({available:true,state:'current_project',files:['src/<img onerror="x">.py'],fileCount:1,undoFiles:[]},{availableActions:{canUndo:false},actionReasons:{canUndo:'任务尚在运行'}},esc);
  assert.doesNotMatch(html,/<img/);assert.match(html,/任务尚在运行/);assert.match(html,/id="undo-edits"[^>]+disabled/);
});

test('latest turn does not duplicate the current edit review as history',()=>{
  const turn={turnId:'t',items:[],changedFiles:['a.py'],userMessage:'修复'};
  const html=renderTurnHtml(turn,{executionState:'idle',turns:[turn],workspaceEdits:{available:true}},esc,x=>x,()=> '');
  assert.doesNotMatch(html,/修改记录/);
  assert.match(renderTurnHtml(turn,{executionState:'idle',turns:[turn]},esc,x=>x,()=> ''),/修改记录/);
});

test('undo captures session and revision before the host confirmation',async()=>{
  const calls=[];let resolve;
  vscodeMock.window={showWarningMessage:()=>new Promise(r=>{resolve=r}),showErrorMessage:()=>{}};
  const provider=new ChatViewProvider({client:{request:async(method,params)=>calls.push({method,params})}},'/repo',{globalState:{get:()=>null}},{});
  provider.current={sessionId:'one',workspaceEdits:{turnId:'t',lastEditSeq:5,undoFiles:['a']},availableActions:{canUndo:true}};
  provider.refresh=async()=>{};
  const action=provider._handleMessage({type:'undoEdits',sessionId:'one',turnId:'t',expectedEditSeq:5});
  provider.current={sessionId:'two'};resolve('撤销受控编辑');await action;
  assert.deepEqual(calls,[{method:'edits/undo',params:{sessionId:'one',turnId:'t',expectedEditSeq:5}}]);
});

function defaultText(html) {
  const details = [];
  let text = '';
  for (const token of html.match(/<[^>]*>|[^<]+/g) || []) {
    if (/^<details\b/.test(token)) details.push({open: /\sopen(?:\s|>)/.test(token), summary: false});
    else if (/^<\/details/.test(token)) details.pop();
    else if (/^<summary\b/.test(token) && details.length) details.at(-1).summary = true;
    else if (/^<\/summary/.test(token) && details.length) details.at(-1).summary = false;
    else if (!token.startsWith('<') && details.every(d => d.open || d.summary)) text += token + ' ';
  }
  return text.replace(/\s+/g, ' ');
}

function demoDelegation() {
  return {rootTurnId: 'turn', status: 'completed', unhandledRuns: [], waits: [], usage: {requests: 18},
    runs: [1, 2, 3].map(n => ({agentId: 'one-agent', runId: 'run-' + n, parentRunId: 'main:turn',
      goal: n === 1 ? '核对项目说明' : '基于历史继续核对', status: 'completed',
      action: n === 1 ? 'spawn' : 'send', execution: n === 1 ? 'background' : 'foreground',
      contextEpoch: n === 3 ? 2 : 1, terminalAck: true, stale: n !== 3, unresolved: [],
      findings: 'README 说明了离线验证方式。', limitations: [], evidenceRefs: [], subjectTree: 'tree'}))};
}

test('stale approval identity is rejected before calling the Runtime',async()=>{
  const calls=[];vscodeMock.window={showErrorMessage:()=>{}};
  const provider=new ChatViewProvider({client:{request:async(...args)=>calls.push(args)}},'/repo',{globalState:{get:()=>null}},{});
  provider.current={sessionId:'s',pendingApproval:{approvalId:'new-approval'}};
  await provider._handleMessage({type:'resolveApproval',sessionId:'s',approvalId:'old-approval',allow:true});
  assert.equal(calls.length,0);assert.match(provider.operationError,/批准请求已变化/);
});

test('invalid saved connection remains editable without silently selecting another model',()=>{
  const provider=new ChatViewProvider({},'/repo',{globalState:{get:()=>({provider:'glm',model:'custom-private-model',reasoningEnabled:true})}},{});
  assert.equal(provider.profile.provider,'glm');assert.equal(provider.profile.model,'custom-private-model');assert.match(provider.operationError,/重新检查/);
  assert.throws(()=>validateProfile(provider.profile),/not supported/);
});

test('history summaries distinguish completed, active and unfinished sessions',()=>{
  const {taskStatus}=require('../ui');
  assert.equal(taskStatus({executionState:'idle',attentionSummary:'Ready'}),'可以继续追问');
  assert.equal(taskStatus({executionState:'idle',attentionSummary:'Continue available'}),'有未完成轮次');
  assert.equal(taskStatus({executionState:'running'}),'正在处理');
});

test('run keeps its explicit session identity while reconnecting',async()=>{
  let release;const calls=[];
  const runtime={ensure:()=>new Promise(resolve=>release=resolve),client:{request:async(method,params)=>calls.push({method,params})}};
  const provider=new ChatViewProvider(runtime,'/repo',{globalState:{get:()=>null}},{});
  provider.current={sessionId:'one'};provider.refresh=async()=>{};
  const action=provider._handleMessage({type:'run',sessionId:'one',message:'inspect'});
  provider.current={sessionId:'two'};release();await action;
  assert.equal(calls[0].params.sessionId,'one');
});
