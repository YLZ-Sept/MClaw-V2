#!/usr/bin/env python3
"""MClaw 授权码签发工具（签发端专用）。

**此文件不随产品分发**——它只在你的签发机器上运行，且需要私钥。

用法::

    python tools/license_gen.py \\
        --key    E:/mclaw-license-keys/private.pem \\
        --cust   "某某科技有限公司" \\
        --fp     2F72-4463-9B50-7666-617F \\
        --months 6 \\
        --users  20 \\
        --feat   plugins,skills,mcp,knowledge_base,im_channels

流水号（``--sn``）不指定时按台账自动递增。每次签发都会追加一行到
``license_ledger.csv``（与私钥同目录），便于对账与续费提醒。

生成新密钥对::

    python tools/license_gen.py genkey --out E:/mclaw-license-keys/private.pem
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import sys
from datetime import date, timedelta
from pathlib import Path

# 复用产品侧的常量与编码，避免签发端与验证端各写一份而漂移。
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "src"))

from mclaw.license.fingerprint import (  # noqa: E402
    FINGERPRINT_SEGMENTS,
    MISSING_SEGMENT,
    MIN_USABLE_SEGMENTS,
)
from mclaw.license.verifier import KNOWN_FEATURES, PREFIX  # noqa: E402

LEDGER_NAME = "license_ledger.csv"
LEDGER_HEADER = ["sn", "cust", "fp", "iss", "exp", "users", "tier", "feat", "code"]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _load_private_key(path: Path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if not path.exists():
        raise SystemExit(f"私钥不存在: {path}")
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit(f"不是 Ed25519 私钥: {path}")
    return key


def _validate_fingerprint(fp: str) -> str:
    normalized = fp.strip().upper()
    parts = normalized.split("-")
    expected = len(FINGERPRINT_SEGMENTS)
    if len(parts) != expected:
        raise SystemExit(f"指纹应为 {expected} 段，收到 {len(parts)} 段: {fp}")
    for part in parts:
        if part == MISSING_SEGMENT:
            continue
        if len(part) != 4 or not all(c in "0123456789ABCDEF" for c in part):
            raise SystemExit(f"指纹段格式无效: {part}")
    usable = sum(1 for p in parts if p != MISSING_SEGMENT)
    if usable < MIN_USABLE_SEGMENTS:
        raise SystemExit(
            f"该指纹仅 {usable} 段可用（需至少 {MIN_USABLE_SEGMENTS} 段），"
            "签出的授权码在客户机器上必然失配。请先排查客户机器的硬件信息采集。"
        )
    return normalized


def _validate_features(raw: str) -> list[str]:
    features = [f.strip() for f in raw.split(",") if f.strip()]
    if not features:
        raise SystemExit("至少需要指定一项功能（--feat）")
    unknown = set(features) - KNOWN_FEATURES
    if unknown:
        raise SystemExit(
            f"未知功能标识: {sorted(unknown)}\n可用: {sorted(KNOWN_FEATURES)}"
        )
    return sorted(set(features))


def _next_serial(ledger: Path) -> str:
    """按台账生成下一个流水号，形如 ``MC-2026-0007``。"""
    year = date.today().year
    used = 0
    if ledger.exists():
        with ledger.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                sn = (row.get("sn") or "").strip()
                parts = sn.split("-")
                if len(parts) == 3 and parts[0] == "MC" and parts[1] == str(year):
                    try:
                        used = max(used, int(parts[2]))
                    except ValueError:
                        continue
    return f"MC-{year}-{used + 1:04d}"


def _append_ledger(ledger: Path, row: dict) -> None:
    exists = ledger.exists()
    with ledger.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=LEDGER_HEADER)
        if not exists:
            writer.writeheader()
        writer.writerow(row)


def cmd_genkey(args: argparse.Namespace) -> int:
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    out = Path(args.out)
    if out.exists():
        # 覆盖私钥会让所有已签发的授权码永久失效且无法补签。
        raise SystemExit(f"目标已存在，拒绝覆盖: {out}")

    out.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    out.write_bytes(
        key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.PKCS8, ser.NoEncryption())
    )
    pub_hex = key.public_key().public_bytes(ser.Encoding.Raw, ser.PublicFormat.Raw).hex()

    print(f"私钥已写入: {out}")
    print(f"公钥（填入 src/mclaw/license/keys.py）: {pub_hex}")
    print("\n注意：私钥务必离线备份。一旦丢失，所有客户都需要重新签发授权码。")
    return 0


def cmd_sign(args: argparse.Namespace) -> int:
    key_path = Path(args.key)
    key = _load_private_key(key_path)
    ledger = key_path.parent / LEDGER_NAME

    fingerprint = _validate_fingerprint(args.fp)
    features = _validate_features(args.feat)

    issued = date.today()
    if args.expires:
        try:
            expires = date.fromisoformat(args.expires)
        except ValueError:
            raise SystemExit(f"到期日格式无效（应为 YYYY-MM-DD）: {args.expires}")
    else:
        expires = issued + timedelta(days=round(args.months * 30.44))

    if expires <= issued:
        raise SystemExit(f"到期日 {expires} 必须晚于签发日 {issued}")

    serial = args.sn or _next_serial(ledger)

    payload = {
        "v": 1,
        "sn": serial,
        "cust": args.cust,
        "fp": fingerprint,
        "iss": issued.isoformat(),
        "exp": expires.isoformat(),
        "tier": args.tier,
        "users": args.users,
        "feat": features,
    }

    payload_b64 = _b64url(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    )
    signed_text = f"{PREFIX}.{payload_b64}".encode("ascii")
    signature = key.sign(signed_text)
    code = f"{PREFIX}.{payload_b64}.{_b64url(signature)}"

    if not args.dry_run:
        _append_ledger(
            ledger,
            {
                "sn": serial,
                "cust": args.cust,
                "fp": fingerprint,
                "iss": issued.isoformat(),
                "exp": expires.isoformat(),
                "users": args.users,
                "tier": args.tier,
                "feat": ",".join(features),
                "code": code,
            },
        )

    print("─" * 72)
    print(f"流水号  : {serial}")
    print(f"客户    : {args.cust}")
    print(f"指纹    : {fingerprint}")
    print(f"有效期  : {issued.isoformat()} → {expires.isoformat()}  ({(expires - issued).days} 天)")
    print(f"用户数  : {'不限' if args.users <= 0 else args.users}")
    print(f"功能    : {', '.join(features)}")
    print("─" * 72)
    print(code)
    print("─" * 72)
    print(f"长度 {len(code)} 字符")
    if args.dry_run:
        print("（dry-run：未写入台账）")
    else:
        print(f"已记入台账: {ledger}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="MClaw 授权码签发工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")

    gen = sub.add_parser("genkey", help="生成新的 Ed25519 密钥对")
    gen.add_argument("--out", required=True, help="私钥输出路径（.pem）")
    gen.set_defaults(func=cmd_genkey)

    sign = sub.add_parser("sign", help="签发授权码（默认子命令）")
    _add_sign_args(sign)
    sign.set_defaults(func=cmd_sign)

    # 允许省略 sign 子命令直接传参。
    _add_sign_args(parser)
    parser.set_defaults(func=cmd_sign)

    args = parser.parse_args()
    if args.command is None and not args.cust:
        parser.print_help()
        return 1
    return args.func(args)


def _add_sign_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--key", default="E:/mclaw-license-keys/private.pem", help="私钥路径")
    p.add_argument("--cust", help="客户名称")
    p.add_argument("--fp", help="客户机器指纹（激活页可复制）")
    p.add_argument("--sn", default="", help="流水号（默认按台账自增）")
    p.add_argument("--months", type=float, default=6, help="有效期月数（默认 6）")
    p.add_argument("--expires", default="", help="到期日 YYYY-MM-DD（优先于 --months）")
    p.add_argument("--users", type=int, default=0, help="最大用户数，0=不限（默认 0）")
    p.add_argument("--tier", default="", help="档位备注（不参与逻辑判断）")
    p.add_argument(
        "--feat",
        default=",".join(sorted(KNOWN_FEATURES)),
        help="功能列表，逗号分隔（默认全开）",
    )
    p.add_argument("--dry-run", action="store_true", help="只输出不写台账")


if __name__ == "__main__":
    raise SystemExit(main())
