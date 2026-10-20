#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
import vm from "node:vm";
import assert from "node:assert";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const rootDir = path.resolve(__dirname, "..");

const stockPath = process.argv[2];
const out7zPath = process.argv[3];

if (!stockPath || !out7zPath) {
  console.error("Usage: node tests/test_patcher.mjs <stock BOOST.GCT> <out .7z>");
  process.exit(1);
}

// 1. Load vendored LZMA and patcher in a fresh VM context
const lzmaCode = fs.readFileSync(path.join(rootDir, "patcher/vendor/lzma-c-min.js"), "utf8");
const patcherCode = fs.readFileSync(path.join(rootDir, "patcher/patcher.js"), "utf8");

const ctx = {
  setTimeout: globalThis.setTimeout,
  setImmediate: globalThis.setImmediate,
  crypto: globalThis.crypto,
  atob: globalThis.atob,
  btoa: globalThis.btoa,
  TextEncoder: globalThis.TextEncoder,
  TextDecoder: globalThis.TextDecoder,
  Uint8Array: globalThis.Uint8Array,
  Uint32Array: globalThis.Uint32Array,
  ArrayBuffer: globalThis.ArrayBuffer,
  DataView: globalThis.DataView,
  BigInt: globalThis.BigInt,
  console: console,
};
vm.createContext(ctx);
vm.runInContext(lzmaCode, ctx);
vm.runInContext(patcherCode, ctx);

const { buildAddon } = ctx;
assert.strictEqual(typeof buildAddon, "function", "buildAddon must be exported");

// 2. Load CFG from dist/patcher-config.json and verify gca-patcher.html exists
const cfgPath = path.join(rootDir, "dist/patcher-config.json");
assert(fs.existsSync(cfgPath), "dist/patcher-config.json must exist");
const cfg = JSON.parse(fs.readFileSync(cfgPath, "utf8"));

const htmlPath = path.join(rootDir, "dist/gca-patcher.html");
assert(fs.existsSync(htmlPath), "dist/gca-patcher.html must exist");
const html = fs.readFileSync(htmlPath, "utf8");
assert(cfg.versions.length > 0, "at least one version");
for (const v of cfg.versions) assert(html.includes(v.tailB64), `gca-patcher.html must inline v${v.version}`);
assert(!html.includes("/*@@LZMA@@*/"), "LZMA placeholder must be replaced");
assert(!html.includes("/*@@PATCHER@@*/"), "PATCHER placeholder must be replaced");
assert(!html.includes("/*@@CONFIG@@*/"), "CONFIG placeholder must be replaced");

// 3. Test invalid inputs
const stockBytes = fs.readFileSync(stockPath);

// Wrong size check
const wrongSize = stockBytes.subarray(0, stockBytes.length - 16);
const resWrongSize = await buildAddon(wrongSize, cfg);
assert.strictEqual(resWrongSize.ok, false, "Wrong size input must be rejected");
assert(resWrongSize.error && resWrongSize.error.includes("size"), "Error should explain wrong size");

// Modified file (wrong hash) check
const modifiedBytes = new Uint8Array(stockBytes);
modifiedBytes[16] ^= 0x55;
const resModified = await buildAddon(modifiedBytes, cfg);
assert.strictEqual(resModified.ok, false, "Modified file must be rejected");
assert(resModified.error && resModified.error.includes("Checksum"), "Error should explain checksum mismatch");

// Already patched file check (contains "GCA1" marker)
const distGctPath = path.join(rootDir, "dist/BOOST.GCT");
if (fs.existsSync(distGctPath)) {
  const distGctBytes = fs.readFileSync(distGctPath);
  const resAlreadyPatched = await buildAddon(distGctBytes, cfg);
  assert.strictEqual(resAlreadyPatched.ok, false, "Already patched file must be rejected");
  assert(resAlreadyPatched.error && resAlreadyPatched.error.includes("already contains"),
    "Error should tell user to restore stock file first");
}

// 4. Test valid stock file, every offered version (newest is written out)
let resValid;
for (const v of [...cfg.versions].reverse()) {
  resValid = await buildAddon(stockBytes, { ...cfg, ...v });
  assert.strictEqual(resValid.ok, true, `v${v.version}: valid stock file must succeed: ` + resValid.error);
  assert(resValid.archive instanceof Uint8Array, "Result must contain 7z archive");
  const gctSha256 = Buffer.from(await crypto.subtle.digest("SHA-256", resValid.gct)).toString("hex");
  assert.strictEqual(gctSha256, v.outSha256, `v${v.version}: produced GCT SHA-256 must match`);
  console.log(`v${v.version}: ok (${resValid.archive.length} bytes)`);
}

// 5. Write produced archive to out path
fs.mkdirSync(path.dirname(path.resolve(out7zPath)), { recursive: true });
fs.writeFileSync(out7zPath, resValid.archive);
console.log(`Test passed. Archive written to ${out7zPath} (${resValid.archive.length} bytes).`);
