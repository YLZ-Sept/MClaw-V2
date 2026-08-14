"""蝉镜 AI 开放平台服务端接入。

凭证只从服务端环境变量 ``CHANJING_APP_ID`` / ``CHANJING_SECRET_KEY`` 读取，
绝不出现在日志、注释或任何前端代码中。
"""

from .client import BASE_URL, ChanjingClient, ChanjingError, ChanjingConfigError

__all__ = ["BASE_URL", "ChanjingClient", "ChanjingError", "ChanjingConfigError"]
