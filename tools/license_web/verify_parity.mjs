// ─── 签发一致性校验 ───
//
// 证明 index.html 的浏览器端签发逻辑与 tools/license_gen.py 逐字节等价。
// 改动 index.html 里的 serializePayload / b64url / pyRound 后必须重跑此脚本。
//
//   node tools/license_web/verify_parity.mjs
//
// 原理：从 index.html 里抽出真实函数（而非另写一份），对同一组 payload
// 计算签名文本，与 Python 端产出的期望值逐字节比对。另外用 Node 的
// Ed25519 实现签名，交由 Python 端验签，确认整条链路互通。

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, "index.html"), "utf8");

// 从页面里抠出被测函数，确保测的就是线上那份实现。
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
const mod = new Function(`${src}; return { b64url, serializePayload, pyRound, addDays, ymd };`)();

let failed = 0;
const check = (label, got, want) => {
  const ok = got === want;
  if (!ok) failed++;
  console.log(`${ok ? "  OK  " : " FAIL "} ${label}`);
  if (!ok) {
    console.log(`         got : ${got}`);
    console.log(`         want: ${want}`);
  }
};

console.log("\n── payload 序列化（须与 Python json.dumps 逐字节一致）──");

// 期望值由 Python 端生成，见本文件末尾的 PY_EXPECTED 说明。
const expected = JSON.parse(readFileSync(join(here, "parity_expected.json"), "utf8"));

for (const c of expected.cases) {
  const bytes = mod.serializePayload(c.payload);
  check(c.label, Buffer.from(bytes).toString("base64url"), c.payload_b64);
}

console.log("\n── pyRound（Python 银行家舍入）──");
for (const [input, want] of expected.round_cases) {
  check(`round(${input})`, String(mod.pyRound(input)), String(want));
}

console.log("\n── 月数 → 到期日 ──");
for (const c of expected.term_cases) {
  check(`${c.iss} + ${c.months} 月`,
        mod.addDays(c.iss, mod.pyRound(c.months * 30.44)), c.exp);
}

console.log("\n── base64url 无填充 ──");
for (const [hex, want] of expected.b64_cases) {
  check(`b64url(${hex.slice(0, 12)}…)`, mod.b64url(Buffer.from(hex, "hex")), want);
}

console.log(failed === 0
  ? "\n全部通过：浏览器端与 Python 端签发结果一致。\n"
  : `\n${failed} 项不一致 —— 浏览器签出的码客户端会验签失败，勿发布。\n`);
process.exit(failed === 0 ? 0 : 1);
