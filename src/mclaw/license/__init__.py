"""离线授权（License）子系统。

私钥签发、公钥验签的非对称授权体系。客户持有全部源码与公钥也无法伪造
授权码——安全性建立在 Ed25519 的数学性质上，而非代码保密性。

模块划分：

- :mod:`keys`        — 内置公钥常量
- :mod:`fingerprint` — 硬件指纹采集与容错匹配
- :mod:`verifier`    — 授权码解析与签名校验
- :mod:`manager`     — 授权状态机、``data/license.json`` 读写、时钟回拨防护
"""

from __future__ import annotations

from .fingerprint import (
    FINGERPRINT_SEGMENTS,
    MISSING_SEGMENT,
    collect_fingerprint,
    fingerprint_matches,
)
from .manager import (
    GRACE_PERIOD_DAYS,
    WARN_BEFORE_DAYS,
    LicenseManager,
    LicenseState,
    LicenseStatus,
)
from .verifier import LicensePayload, LicenseVerifyError, verify_license_code

__all__ = [
    "FINGERPRINT_SEGMENTS",
    "GRACE_PERIOD_DAYS",
    "MISSING_SEGMENT",
    "WARN_BEFORE_DAYS",
    "LicenseManager",
    "LicensePayload",
    "LicenseState",
    "LicenseStatus",
    "LicenseVerifyError",
    "collect_fingerprint",
    "fingerprint_matches",
    "verify_license_code",
]
