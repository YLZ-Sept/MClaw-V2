// ─── 纯 JS 签名路径校验 ───
//
// 验证 index.html 内联的 noble-ed25519 回退路径与 Python 端产出相同的码。
// WebCrypto 路径已由 sign_e2e.mjs 覆盖；这里专门测浏览器不支持
// 原生 Ed25519 时走的那条路——商务的机器上实际走的就是它。
//
//   node tools/license_web/verify_noble.mjs [私钥路径]

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createPrivateKey, sign as nodeSign, webcrypto } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "index.html"), "utf8");

// noble 需要 crypto.subtle 做 SHA-512；Node 里挂上即可。
// noble 需要 crypto.subtle 做 SHA-512；Node 24 下 globalThis.crypto 是只读的。
Object.defineProperty(globalThis, "crypto", { value: webcrypto, writable: false, configurable: true });

// 取出页面里内联的那份库（测的就是商务实际运行的代码）。
const vendor = html.match(
  /\/\* ==== VENDOR:noble-ed25519 BEGIN[^\n]*\n([\s\S]*?)\/\* ==== VENDOR:noble-ed25519 END ==== \*\//,
);
if (!vendor) throw new Error("index.html 里没有 vendor 区块，请先跑 build.py");
new Function(vendor[1])();
const noble = globalThis.nobleEd25519;
if (!noble) throw new Error("vendor 区块没有暴露 nobleEd25519");

// 取出页面里的 pkcs8ToSeed / 编码函数。
function extract(name) {
  const re = new RegExp(`function ${name}\\s*\\([^)]*\\)\\s*\\{`, "m");
  const m = html.match(re);
  if (!m) throw new Error(`index.html 里找不到函数 ${name}`);
  let i = m.index + m[0].length, depth = 1;
  while (i < html.length && depth > 0) {
    if (html[i] === "{") depth++;
    else if (html[i] === "}") depth--;
    i++;
  }
  return html.slice(m.index, i);
}
const fns = ["b64url", "serializePayload", "pemToDer", "pkcs8ToSeed", "pyRound", "addDays", "ymd"]
  .map(extract).join("\n");
const m = new Function(
  `${fns}; return { b64url, serializePayload, pemToDer, pkcs8ToSeed, pyRound, addDays, ymd };`,
)();

const keyPath = process.argv[2] || "E:/mclaw-license-keys/private.pem";
const pem = readFileSync(keyPath, "utf8");
const seed = m.pkcs8ToSeed(m.pemToDer(pem));
const nodeKey = createPrivateKey(pem);

let failed = 0;
const check = (label, a, b) => {
  const ok = a === b;
  if (!ok) failed++;
  console.log(`${ok ? "  OK  " : " FAIL "} ${label}`);
  if (!ok) { console.log(`         noble : ${a}`); console.log(`         node  : ${b}`); }
};

console.log("\n── pkcs8ToSeed 解析出的私钥与 Node 一致 ──");
const noblePub = Buffer.from(await noble.getPublicKeyAsync(seed)).toString("hex");
const nodePub = createPrivateKey(pem).export({ format: "jwk" }).x;
check("公钥", noblePub, Buffer.from(nodePub, "base64url").toString("hex"));

console.log("\n── 签名逐字节一致（Ed25519 确定性签名）──");
const payloads = [
  { v: 1, sn: "MC-2026-0001", cust: "某某科技有限公司", fp: "2F72-4463-9B50-7666-617F",
    iss: "2026-09-03", exp: "2027-03-03", tier: "标准版", users: 20,
    feat: ["im_channels", "knowledge_base", "mcp", "plugins", "skills"] },
  { v: 1, sn: "MC-2026-0002", cust: 'A"B\\C 公司', fp: "1234-XXXX-5678-XXXX-9ABC",
    iss: "2026-01-01", exp: "2026-12-31", tier: "", users: 0, feat: ["skills"] },
];
for (const p of payloads) {
  const b64 = m.b64url(m.serializePayload(p));
  const msg = Buffer.from(`MC1.${b64}`, "ascii");
  const a = m.b64url(await noble.signAsync(msg, seed));
  const b = m.b64url(nodeSign(null, msg, nodeKey));
  check(p.cust, a, b);
}

console.log(failed === 0
  ? "\n纯 JS 路径与原生实现产出完全一致。\n"
  : `\n${failed} 项不一致 —— 回退路径会签出废码，勿发布。\n`);
process.exit(failed === 0 ? 0 : 1);
