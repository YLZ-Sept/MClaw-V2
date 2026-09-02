"""授权码解析与 Ed25519 验签。

授权码信封::

    MC1.<base64url(payload_json)>.<base64url(signature)>

签名覆盖 ``"MC1." + base64url(payload)`` 的 ASCII 字节——即前两段的完整
文本。改动 payload 的任何一个字符都会使签名失配。

payload 为明文（base64url 非加密），客户可自行解码查看授权内容。这是刻意
设计：透明度换信任，且授权内容本就不是秘密。
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from dataclasses import dataclass, field
from datetime import date

from .keys import public_key_bytes

logger = logging.getLogger(__name__)

PREFIX = "MC1"

# payload 允许的最大字节数，防止构造超大 JSON 触发解析开销。
_MAX_PAYLOAD_BYTES = 8192

# 已知功能开关标识。授权码中出现的未知标识会被忽略（向前兼容：老版本
# 客户端遇到新功能名不应崩溃）。
KNOWN_FEATURES: frozenset[str] = frozenset(
    {"plugins", "skills", "mcp", "knowledge_base", "im_channels"}
)


class LicenseVerifyError(Exception):
    """授权码格式错误或签名校验失败。"""


@dataclass(frozen=True)
class LicensePayload:
    """已验签的授权内容。"""

    version: int
    serial: str
    customer: str
    fingerprint: str
    issued: date
    expires: date
    tier: str
    max_users: int
    features: frozenset[str]
    raw: dict = field(repr=False, default_factory=dict)

    def has_feature(self, name: str) -> bool:
        return name in self.features

    @property
    def unlimited_users(self) -> bool:
        return self.max_users <= 0


def _b64url_decode(segment: str) -> bytes:
    """无填充 base64url 解码。"""
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (binascii.Error, ValueError) as exc:
        raise LicenseVerifyError(f"授权码编码无效: {exc}") from exc


def b64url_encode(data: bytes) -> str:
    """无填充 base64url 编码（签发端共用）。"""
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _parse_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise LicenseVerifyError(f"授权码字段 {field_name} 不是日期字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise LicenseVerifyError(f"授权码字段 {field_name} 日期格式无效: {value}") from exc


def _verify_signature(signed_text: bytes, signature: bytes) -> bool:
    """逐个尝试受信任公钥。任一通过即可。"""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    for raw_key in public_key_bytes():
        try:
            Ed25519PublicKey.from_public_bytes(raw_key).verify(signature, signed_text)
            return True
        except InvalidSignature:
            continue
        except Exception as exc:  # 公钥常量损坏等
            logger.error("授权公钥加载失败: %s", exc)
            continue
    return False


def verify_license_code(code: str) -> LicensePayload:
    """校验授权码并返回其内容。

    仅做**格式与签名**校验，不检查指纹与有效期——那属于
    :class:`~mclaw.license.manager.LicenseManager` 的策略层。

    Raises:
        LicenseVerifyError: 格式错误、签名不匹配或字段缺失。
    """
    if not code or not isinstance(code, str):
        raise LicenseVerifyError("授权码为空")

    # 客户从邮件/聊天窗口复制常带换行与空格。
    normalized = "".join(code.split())

    parts = normalized.split(".")
    if len(parts) != 3:
        raise LicenseVerifyError("授权码格式无效（应为三段，以 . 分隔）")

    prefix, payload_b64, sig_b64 = parts
    if prefix != PREFIX:
        raise LicenseVerifyError(f"授权码版本不支持: {prefix}")

    if len(payload_b64) > _MAX_PAYLOAD_BYTES:
        raise LicenseVerifyError("授权码内容过长")

    signature = _b64url_decode(sig_b64)
    if len(signature) != 64:
        raise LicenseVerifyError("授权码签名长度无效")

    signed_text = f"{PREFIX}.{payload_b64}".encode("ascii")
    if not _verify_signature(signed_text, signature):
        raise LicenseVerifyError("授权码签名校验失败——内容被篡改或非本方签发")

    payload_bytes = _b64url_decode(payload_b64)
    try:
        data = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LicenseVerifyError(f"授权码内容解析失败: {exc}") from exc

    if not isinstance(data, dict):
        raise LicenseVerifyError("授权码内容不是对象")

    version = data.get("v")
    if version != 1:
        raise LicenseVerifyError(f"授权码协议版本不支持: {version}")

    fingerprint = data.get("fp")
    if not isinstance(fingerprint, str) or not fingerprint:
        raise LicenseVerifyError("授权码缺少机器指纹")
    issued = _parse_date(data.get("iss"), "iss")
    expires = _parse_date(data.get("exp"), "exp")
    if expires < issued:
        raise LicenseVerifyError("授权码到期日早于签发日")

    raw_users = data.get("users", 0)
    if not isinstance(raw_users, int) or isinstance(raw_users, bool) or raw_users < 0:
        raise LicenseVerifyError("授权码用户数字段无效")

    raw_features = data.get("feat", [])
    if not isinstance(raw_features, list):
        raise LicenseVerifyError("授权码功能列表字段无效")
    features = frozenset(f for f in raw_features if isinstance(f, str))

    unknown = features - KNOWN_FEATURES
    if unknown:
        # 向前兼容：新版签发的码在老客户端上仍可用，未知功能忽略即可。
        logger.info("授权码含本版本未知的功能标识，已忽略: %s", sorted(unknown))

    return LicensePayload(
        version=version,
        serial=str(data.get("sn", "")),
        customer=str(data.get("cust", "")),
        fingerprint=fingerprint.strip().upper(),
        issued=issued,
        expires=expires,
        tier=str(data.get("tier", "")),
        max_users=raw_users,
        features=features,
        raw=data,
    )
