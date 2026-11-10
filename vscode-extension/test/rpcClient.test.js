"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const {RpcClient} = require("../rpcClient");

test("dispose closes stdin, waits for cleanup and ignores late notifications", async () => {
  const notifications = [];
  const code = `const readline=require('readline');
    const r=readline.createInterface({input:process.stdin});
    r.on('line', line=>{const m=JSON.parse(line);if(m.method==='initialize')process.stdout.write(JSON.stringify({id:m.id,result:{}})+'\\n');});
    r.on('close',()=>{process.stdout.write(JSON.stringify({method:'session/event',params:{late:true}})+'\\n');setTimeout(()=>process.exit(0),50);});`;
  const client = new RpcClient(process.execPath, ["-e", code], {onNotification: (m,p) => notifications.push([m,p])});
  await Promise.all([client.start(), client.start()]);
  const pending = client.request("never").catch(error => error.message);
  await client.dispose();
  assert.match(await pending, /stopped/);
  assert.deepEqual(notifications, []);
  assert.equal(client.process, undefined);
});

test("Runtime exit rejects pending requests and reports disconnection", async () => {
  const states = [];
  const code = `const readline=require('readline');readline.createInterface({input:process.stdin}).on('line',line=>{const m=JSON.parse(line);if(m.method==='initialize')process.stdout.write(JSON.stringify({id:m.id,result:{}})+'\\n');else process.exit(3);});`;
  const client = new RpcClient(process.execPath, ["-e", code], {onConnectionState: state => states.push(state)});
  await client.start();
  await assert.rejects(client.request("exit"), /exited/);
  assert.deepEqual(states, ["connected", "unavailable"]);
  await client.dispose();
});
