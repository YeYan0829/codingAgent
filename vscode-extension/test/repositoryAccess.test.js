"use strict";
const test = require('node:test');
const assert = require('node:assert/strict');
const Module = require('node:module');
const load = Module._load;
Module._load = function(name, ...rest) {
  if (name === 'vscode') return {window:{setStatusBarMessage(){}}};
  return load.call(this,name,...rest);
};
const {ChatViewProvider,renderChat} = require('../extension');
const {renderRepositoryAccess} = require('../repositoryAccess');
Module._load = load;
const esc = s => String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');

test('repository form allows empty setup and escapes paths and retained drafts',()=>{
  const html=renderRepositoryAccess({configured:false,deniedPaths:[]},null,esc);
  assert.match(html,/可以留空/);assert.match(html,/保存访问设置并继续/);
  assert.match(html,/每行一项/);assert.match(html,/无法撤回此前已发送/);
  assert.doesNotMatch(renderRepositoryAccess({configured:true},'</textarea><script>bad</script>',esc),/<script>/);
  const webview=renderChat(undefined,[],undefined,{},undefined,undefined,'/repo',{configured:false,revision:'0',deniedPaths:[]});
  assert.doesNotThrow(()=>new Function(webview.match(/<script nonce="[^"]+">([\s\S]*?)<\/script>/)[1]));
  assert.match(webview,/workspace|repositoryAccess/);
});

test('save uses repository RPC, waits for success, and does not modify model preferences', async()=>{
  const calls=[];
  const view={workspacePath:'/repo',runtime:{ensure:async()=>{},client:{request:async(method,params)=>{calls.push([method,params]);return {configured:true,revision:'2',deniedPaths:params.deniedPaths};}}},refresh:async()=>calls.push(['refresh'])};
  await ChatViewProvider.prototype.saveRepositoryAccess.call(view,{paths:'private/\n.env\n',expectedRevision:'1'});
  assert.deepEqual(calls[0],['workspace/access/set',{workspace:'/repo',deniedPaths:['private/','.env'],expectedRevision:'1'}]);
  assert.deepEqual(calls[1],['refresh']);
  assert.equal(view.repositoryAccess.revision,'2');
});

test('failed settings save keeps previous backend-confirmed settings',async()=>{
  const previous={configured:true,revision:'1',deniedPaths:[]};
  const view={workspacePath:'/repo',repositoryAccess:previous,runtime:{ensure:async()=>{},client:{request:async()=>{throw Error('busy');}}},refresh:async()=>assert.fail('not saved')};
  await assert.rejects(ChatViewProvider.prototype.saveRepositoryAccess.call(view,{paths:'private',expectedRevision:'1'}),/busy/);
  assert.equal(view.repositoryAccess,previous);
});

test('new task cannot skip first repository choice even with a configured model',async()=>{
  const calls=[];
  const view={workspacePath:'/repo',profile:{provider:'fake',model:'fake'},secretState:{fake:true},_render(){},runtime:{ensure:async()=>{},client:{request:async(method)=>{calls.push(method);return {configured:false,revision:'0',deniedPaths:[]};}}}};
  await assert.rejects(ChatViewProvider.prototype.newSession.call(view),/可以留空/);
  assert.deepEqual(calls,['workspace/access/get']);
});
