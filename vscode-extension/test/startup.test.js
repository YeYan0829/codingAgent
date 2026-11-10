"use strict";

const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const Module=require('node:module');
const {execFileSync}=require('node:child_process');
const originalLoad=Module._load;
const mock={ExtensionMode:{Development:2},workspace:{getConfiguration:()=>({get:(key,fallback)=>key==='rpcCommand'?'/stale-release/bin/codeagent-rpc':key==='sessionRoot'?'/stale-sessions':fallback})}};
Module._load=function(name,...rest){return name==='vscode'?mock:originalLoad.call(this,name,...rest);};
const {RuntimeConnection,ChatViewProvider}=require('../extension');
Module._load=originalLoad;

test('actual view resolver starts current Python RPC despite stale global settings',async()=>{
  const project=path.resolve(__dirname,'../..'),python=path.join(project,'.venv/bin/python');
  const temp=fs.mkdtempSync(path.join(os.tmpdir(),'codeagent-view-startup-')),workspace=path.join(temp,'workspace');
  const oldCommand=process.env.CODEAGENT_DEV_RPC_COMMAND,oldRoot=process.env.CODEAGENT_DEV_SESSION_ROOT;
  const logs=[],states=[];let html='',runtime;
  try {
    execFileSync(python,['-m','codeagent.product.demo','--root',workspace],{cwd:project});
    process.env.CODEAGENT_DEV_RPC_COMMAND='auto';process.env.CODEAGENT_DEV_SESSION_ROOT=path.join(temp,'sessions');
    const context={extensionPath:path.join(project,'vscode-extension'),extensionMode:2,globalState:{get(){}},secrets:{get:async()=>undefined}};
    runtime=new RuntimeConnection(context,workspace,{appendLine:line=>logs.push(line)});
    const provider=new ChatViewProvider(runtime,workspace,context,{});
    await provider.resolveWebviewView({webview:{onDidReceiveMessage(){},postMessage:message=>states.push(message),set html(value){html=value;}}});
    assert.equal(provider.runtimeError,undefined);assert.match(html,/Content-Security-Policy/);
    assert.equal(provider.repositoryAccess.workspace,workspace);assert.equal(provider.repositoryAccess.configured,false);
    assert.equal(runtime.client.initialization.capabilities.repositoryAccess,true);
    assert.equal(states.at(-1).state.workspacePath,workspace);
    assert.ok(logs.some(line=>line.includes(python)&&!line.includes('/stale-release/')));
  } finally {
    await runtime?.dispose();
    if(oldCommand===undefined)delete process.env.CODEAGENT_DEV_RPC_COMMAND;else process.env.CODEAGENT_DEV_RPC_COMMAND=oldCommand;
    if(oldRoot===undefined)delete process.env.CODEAGENT_DEV_SESSION_ROOT;else process.env.CODEAGENT_DEV_SESSION_ROOT=oldRoot;
    fs.rmSync(temp,{recursive:true,force:true});
  }
});
