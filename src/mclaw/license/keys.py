"""内置授权公钥。

公钥随代码分发，公开无妨——它只能验签、无法签发。对应的私钥保存在
仓库之外（签发端），**绝不可进入版本库**。

密钥轮换：新增一条 ``(key_id, pubkey_hex)`` 到 :data:`TRUSTED_PUBLIC_KEYS`
即可，旧授权码继续用旧公钥验签。授权码信封里不携带 key_id，验签时按顺序
逐个尝试——公钥数量极少，开销可忽略。
"""

from __future__ import annotations

# 生产签发密钥（2026-09 生成）。
_KEY_2026_09 = "94fc31b369d35f3f8e58710ea6030be92fe3d06ff2ec6ddfe7c0c3a3bb684a48"

# 验签时按顺序尝试的公钥列表。新密钥追加到列表末尾。
TRUSTED_PUBLIC_KEYS: tuple[tuple[str, str], ...] = (
    ("2026-09", _KEY_2026_09),
)


def public_key_bytes() -> tuple[bytes, ...]:
    """返回全部受信任公钥的原始 32 字节形式。"""
    return tuple(bytes.fromhex(hex_str) for _, hex_str in TRUSTED_PUBLIC_KEYS)
