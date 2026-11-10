"use strict";

const { spawn } = require("child_process");
const readline = require("readline");

class RpcClient {
  constructor(command, args, options = {}) {
    this.command = command;
    this.args = args;
    this.options = options;
    this.nextId = 1;
    this.pending = new Map();
    this.process = undefined;
    this.disposed = false;
  }

  async start() {
    if (this.starting) return this.starting;
    if (this.process) return;
    if (this.disposed) throw new Error("CodeAgent Runtime client was disposed");
    this.starting = this._start();
    try { await this.starting; } finally { this.starting = undefined; }
  }

  async _start() {
    const child = spawn(this.command, this.args, {
      cwd: this.options.cwd,
      env: { ...process.env, ...(this.options.env || {}) },
      stdio: ["pipe", "pipe", "pipe"],
      windowsHide: true
    });
    this.process = child;
    readline.createInterface({ input: child.stdout }).on("line", line => { if (!this.disposed && this.process === child) this._handleLine(line); });
    child.stderr.on("data", data => this.options.onLog?.(data.toString()));
    child.stdin.on("error", error => { if (this.process === child) this._close(error); });
    child.on("error", error => { if (this.process === child) this._close(error); });
    child.on("exit", (code, signal) => { if (this.process === child) this._close(new Error(`CodeAgent Runtime exited (${code ?? signal})`)); });
    this.initialization = await this.request("initialize", { protocolVersion: "1.0" });
    if (!this.disposed && this.process === child) this.options.onConnectionState?.("connected");
  }

  request(method, params = {}) {
    if (!this.process?.stdin.writable) return Promise.reject(new Error("CodeAgent Runtime is not running"));
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.process.stdin.write(`${JSON.stringify({ jsonrpc: "2.0", id, method, params })}\n`);
    });
  }

  dispose() {
    if (this.stopping) return this.stopping;
    this.disposed = true;
    const child = this.process;
    this._rejectPending(new Error("CodeAgent Runtime stopped"));
    if (!child) return Promise.resolve();
    // EOF 由 Runtime 转为 Stop + join；不直接杀掉拥有子 transport 的进程。
    this.stopping = new Promise(resolve => {
      const timer = setTimeout(() => child.kill("SIGTERM"), 5000);
      timer.unref?.();
      child.once("exit", () => { clearTimeout(timer); resolve(); });
      child.once("error", () => { clearTimeout(timer); resolve(); });
      child.stdin.end();
    });
    return this.stopping;
  }

  _handleLine(line) {
    if (this.disposed) return;
    let message;
    try {
      message = JSON.parse(line);
    } catch {
      this.options.onLog?.(`Invalid Runtime protocol line: ${line}`);
      return;
    }
    if (message.id === undefined) {
      if (typeof message.method === "string") this.options.onNotification?.(message.method, message.params ?? {});
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    if (message.error) pending.reject(new Error(message.error.message));
    else pending.resolve(message.result);
  }

  _close(error) {
    this.process = undefined;
    this._rejectPending(error);
    if (!this.disposed) this.options.onConnectionState?.("unavailable", error);
  }

  _rejectPending(error) {
    for (const pending of this.pending.values()) pending.reject(error);
    this.pending.clear();
  }
}

module.exports = { RpcClient };
