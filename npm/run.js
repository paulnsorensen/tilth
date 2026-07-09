#!/usr/bin/env node

"use strict";

const { spawn } = require("child_process");
const path = require("path");

const isWindows = process.platform === "win32";
const binName = isWindows ? "tilth.exe" : "tilth";
const bin = path.join(__dirname, "bin", binName);

const child = spawn(bin, process.argv.slice(2), { stdio: "inherit" });

for (const sig of ["SIGTERM", "SIGINT", "SIGHUP"]) {
  process.on(sig, () => {
    if (!child.killed) child.kill(sig);
  });
}

child.on("error", (err) => {
  console.error(`tilth: failed to run binary at ${bin}`);
  console.error(err.message);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    // Restore default disposition before re-raising, else the still-registered
    // handler swallows the signal and Node would exit 0 instead of dying with it.
    for (const s of ["SIGTERM", "SIGINT", "SIGHUP"]) process.removeAllListeners(s);
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 0);
  }
});