#!/usr/bin/env node

"use strict";

const { spawn } = require("child_process");
const path = require("path");

const isWindows = process.platform === "win32";
const binName = isWindows ? "tilth.exe" : "tilth";
const bin = path.join(__dirname, "bin", binName);

const child = spawn(bin, process.argv.slice(2), { stdio: "inherit" });

const SIGNALS = ["SIGTERM", "SIGINT", "SIGHUP"];
const forwarders = {};
for (const sig of SIGNALS) {
  forwarders[sig] = () => {
    if (!child.killed) child.kill(sig);
  };
  process.on(sig, forwarders[sig]);
}

child.on("error", (err) => {
  console.error(`tilth: failed to run binary at ${bin}`);
  console.error(err.message);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    // Remove only our own forwarders before re-raising, so the signal takes the
    // default terminate action instead of being swallowed by a live handler.
    for (const sig of SIGNALS) process.removeListener(sig, forwarders[sig]);
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 0);
  }
});