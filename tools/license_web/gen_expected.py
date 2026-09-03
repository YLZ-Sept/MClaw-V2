#!/usr/bin/env python3
"""生成 parity_expected.json —— 浏览器端签发一致性的期望值。

由 Python 端（tools/license_gen.py 的同一套序列化逻辑）产出基准数据，
供 verify_parity.mjs 比对。改动签发逻辑后重跑：

    python tools/license_web/gen_expected.py
    node   tools/license_web/verify_parity.mjs
"""

from __future__ import annotations

import base64
import json
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def serialize(payload: dict) -> bytes:
    """与 license_gen.py 完全相同的序列化。"""
    return json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")


# 覆盖易错点：中文、空 tier、users=0、单功能、长客户名、特殊字符
CASES = [
    (
        "常规 5 功能全开",
        {
            "v": 1, "sn": "MC-2026-0001", "cust": "某某科技有限公司",
            "fp": "2F72-4463-9B50-7666-617F", "iss": "2026-09-03",
            "exp": "2027-03-03", "tier": "标准版", "users": 20,
            "feat": ["im_channels", "knowledge_base", "mcp", "plugins", "skills"],
        },
    ),
    (
        "空 tier + 不限用户",
        {
            "v": 1, "sn": "MC-2026-0002", "cust": "Acme Inc.",
            "fp": "AAAA-BBBB-CCCC-DDDD-EEEE", "iss": "2026-01-01",
            "exp": "2026-12-31", "tier": "", "users": 0,
            "feat": ["skills"],
        },
    ),
    (
        "含缺失段的指纹",
        {
            "v": 1, "sn": "MC-2026-0003", "cust": "测试客户（含括号）& 符号",
            "fp": "1234-XXXX-5678-XXXX-9ABC", "iss": "2026-06-15",
            "exp": "2026-07-15", "tier": "试用", "users": 5,
            "feat": ["mcp", "skills"],
        },
    ),
    (
        "引号与反斜杠",
        {
            "v": 1, "sn": "MC-2026-0004", "cust": 'A"B\\C 公司',
            "fp": "FFFF-0000-1111-2222-3333", "iss": "2026-02-29"
            if False else "2026-02-28",
            "exp": "2026-08-28", "tier": "", "users": 100,
            "feat": ["im_channels", "plugins"],
        },
    ),
]

ROUND_CASES = [0.5, 1.5, 2.5, 3.5, -0.5, 15.22, 15.5, 91.32, 182.64, 365.28]

TERM_CASES = [
    ("2026-09-03", 1), ("2026-09-03", 3), ("2026-09-03", 6),
    ("2026-09-03", 12), ("2026-09-03", 24), ("2026-01-31", 1),
    ("2026-02-28", 12), ("2026-12-15", 6), ("2026-09-03", 0.5),
]

B64_CASES = [
    "00", "0000", "000000", "ff", "ffff", "ffffff",
    "deadbeef", "0102030405060708090a0b0c0d0e0f10",
    "fb" * 64,  # Ed25519 签名长度
]


def main() -> int:
    out = {
        "_note": "由 gen_expected.py 生成，勿手改。verify_parity.mjs 用它比对浏览器端实现。",
        "cases": [
            {"label": label, "payload": p, "payload_b64": b64url(serialize(p))}
            for label, p in CASES
        ],
        "round_cases": [[x, round(x)] for x in ROUND_CASES],
        "term_cases": [
            {
                "iss": iss,
                "months": months,
                "exp": (
                    date.fromisoformat(iss) + timedelta(days=round(months * 30.44))
                ).isoformat(),
            }
            for iss, months in TERM_CASES
        ],
        "b64_cases": [[h, b64url(bytes.fromhex(h))] for h in B64_CASES],
    }
    path = HERE / "parity_expected.json"
    path.write_text(
        json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"已生成 {path}")
    print(f"  payload 用例 {len(out['cases'])} 组")
    print(f"  舍入用例 {len(out['round_cases'])} 组")
    print(f"  期限用例 {len(out['term_cases'])} 组")
    print(f"  base64 用例 {len(out['b64_cases'])} 组")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
