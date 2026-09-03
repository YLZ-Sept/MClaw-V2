// ─── 端到端签发验证 ───
//
// 用 index.html 的真实签发逻辑 + 真实私钥签出一个码，交给产品端
// （mclaw.license.verifier）验签。这一步通过，才能确认商务在浏览器里
// 签出的码客户装机后真的能激活。
//
//   node tools/license_web/sign_e2e.mjs <私钥路径> [机器码]
//
// 默认私钥 E:/mclaw-license-keys/private.pem。签出的码只打印，不写台账。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createPrivateKey, sign as nodeSign } from "node:crypto";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "index.html"), "utf8");

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

const src = ["b64url", "serializePayload", "pyRound", "addDays", "ymd"]
  .map(extract).join("\n");
const m = new Function(`${src}; return { b64url, serializePayload, pyRound, addDays, ymd };`)();

const keyPath = process.argv[2] || "E:/mclaw-license-keys/private.pem";
const fp = (process.argv[3] || "EFBD-85BE-231C-0D90-FCDD").toUpperCase();

const iss = m.ymd(new Date());
const exp = m.addDays(iss, m.pyRound(6 * 30.44));
const payload = {
  v: 1,
  sn: "E2E-BROWSER-001",
  cust: "端到端测试（浏览器签发）",
  fp,
  iss,
  exp,
  tier: "e2e",
  users: 7,
  feat: ["im_channels", "knowledge_base", "mcp", "plugins", "skills"],
};

const payloadB64 = m.b64url(m.serializePayload(payload));
const signedText = Buffer.from(`MC1.${payloadB64}`, "ascii");
const key = createPrivateKey(readFileSync(keyPath));
const sig = nodeSign(null, signedText, key);
const code = `MC1.${payloadB64}.${m.b64url(sig)}`;

console.log(JSON.stringify({ payload, code }, null, 2));
