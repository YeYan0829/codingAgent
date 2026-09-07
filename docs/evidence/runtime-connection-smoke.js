'use strict';

const assert = require('node:assert/strict');
const Module = require('node:module');
const path = require('node:path');

const [validationRoot, extensionRoot, runtimeCommand, workspaceRoot] = process.argv.slice(2);
if (!validationRoot || !extensionRoot || !runtimeCommand || !workspaceRoot) {
  console.error(
    'usage: node runtime-connection-smoke.js <validation-root> <extension-root> <runtime-command> <workspace-root>',
  );
  process.exit(2);
}

let configuredCommand = runtimeCommand;
const vscode = {
  workspace: {
    getConfiguration: () => ({
      get: (name, fallback) => {
        if (name === 'rpcCommand') return configuredCommand;
        if (name === 'sessionRoot') return path.join(validationRoot, 'rpc-sessions');
        return fallback;
      },
    }),
  },
};

const originalLoad = Module._load;
Module._load = function loadWithVscodeMock(name, ...args) {
  if (name === 'vscode') return vscode;
  return originalLoad.call(this, name, ...args);
};

const { RuntimeConnection } = require(path.join(extensionRoot, 'extension.js'));
const context = { extensionPath: extensionRoot, secrets: { get: async () => undefined } };
const timeout = setTimeout(() => {
  console.error('runtime connection smoke timed out');
  process.exit(1);
}, 20_000);

async function runMode(mode) {
  configuredCommand = mode === 'explicit' ? runtimeCommand : 'auto';
  if (mode === 'PATH') {
    process.env.PATH = `${path.dirname(runtimeCommand)}:${process.env.PATH}`;
  }

  const runtime = new RuntimeConnection(context, workspaceRoot, { append: () => {} });
  const config = await runtime._configuration();
  assert.equal(config.command, mode === 'explicit' ? runtimeCommand : 'codeagent-rpc');
  await runtime.ensure();

  const handshake = await runtime.client.request('initialize', { protocolVersion: '1.0' });
  assert.equal(handshake.runtimeVersion, '0.5.0');
  assert.equal(handshake.protocolVersion, '1.0');
  assert.equal(handshake.capabilities.execution, true);

  const created = await runtime.client.request('session/create', {
    workspace: workspaceRoot,
    provider: 'fake',
  });
  const started = await runtime.client.request('session/run', {
    sessionId: created.sessionId,
    message: 'Read README',
  });
  assert.equal(started.executionState, 'running');

  let detail;
  for (let attempt = 0; attempt < 80; attempt += 1) {
    detail = await runtime.client.request('session/get', { sessionId: created.sessionId });
    if (detail.executionState === 'idle') break;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  assert.equal(detail.executionState, 'idle');
  assert.ok((await runtime.client.request('session/list', {})).sessions.length > 0);
  runtime.dispose();
  console.log(`${mode}: VSIX RuntimeConnection -> wheel RPC PASS`);
}

(async () => {
  await runMode('explicit');
  await runMode('PATH');

  configuredCommand = path.join(validationRoot, 'missing-codeagent-rpc');
  const missing = new RuntimeConnection(context, workspaceRoot, { append: () => {} });
  await assert.rejects(missing.ensure(), /ENOENT/);
  missing.dispose();
  console.log('missing executable: rejected PASS');
  clearTimeout(timeout);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
