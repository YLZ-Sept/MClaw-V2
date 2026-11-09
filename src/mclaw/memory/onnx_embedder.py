"""本地 ONNX embedding 后端（不依赖 torch / sentence-transformers）。

设计目标：
- 运行时只有 onnxruntime + tokenizers + numpy（约 40MB）；
- 模型使用 int8 量化的 bge-small-zh-v1.5（约 24MB），CPU 单查询毫秒级；
- 接口与 sentence_transformers.SentenceTransformer.encode() 兼容，
  返回 numpy 数组（调用方继续用 ``.tolist()``）；
- 任何依赖/模型缺失都抛出带指引的 ImportError/FileNotFoundError，
  由 KnowledgeManager 捕获后优雅降级回纯 FTS5。

模型目录约定（两种都支持）::

    <model_dir>/tokenizer.json
    <model_dir>/onnx/model_quantized.onnx   # 优先
    <model_dir>/onnx/model.onnx

或直接把模型文件放在 <model_dir> 下。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL_DIR_NAME = "bge-small-zh-v1.5-onnx"
MODEL_FILES = ("model_quantized.onnx", "model.onnx")

# 首次运行下载（客户机可联网时）。文件与 sha256 固定，避免半包/被替换。
_MODEL_MIRRORS = (
    "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main",
    "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main",
)
_MODEL_FILES = {
    "tokenizer.json": "48cea5d44424912a6fd1ea647bf4fe50b55ab8b1e5879c3275f80e339e8fae26",
    "config.json": "d4193ead3a810fd694fa8a31d7fc72fbaebc0668b603e398734bf2f6538ff42f",
    "onnx/model_quantized.onnx": (
        "15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc"
    ),
}


def _find_model_file(model_dir: Path) -> Path | None:
    for sub in (model_dir / "onnx", model_dir):
        for name in MODEL_FILES:
            candidate = sub / name
            if candidate.is_file() and candidate.stat().st_size > 0:
                return candidate
    return None


class OnnxEmbedder:
    """bge-small-zh-v1.5 ONNX 推理封装（CLS pooling + L2 normalize）。"""

    def __init__(
        self,
        model_dir: Path | str,
        threads: int = 4,
        max_length: int = 512,
    ):
        try:
            import numpy as np
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:  # pragma: no cover - 取决于安装环境
            raise ImportError(
                "本地 ONNX 语义检索需要安装 vector-lite 模块"
                "（onnxruntime + tokenizers + numpy）"
            ) from e

        self._np = np
        self.model_dir = Path(model_dir)
        tokenizer_path = self.model_dir / "tokenizer.json"
        if not tokenizer_path.is_file():
            raise FileNotFoundError(f"缺少 tokenizer.json: {tokenizer_path}")
        model_file = _find_model_file(self.model_dir)
        if model_file is None:
            raise FileNotFoundError(
                f"缺少 ONNX 模型文件（model_quantized.onnx / model.onnx）: {self.model_dir}"
            )

        self.model_file = model_file.name
        self.tok = Tokenizer.from_file(str(tokenizer_path))
        self.tok.enable_truncation(max_length=max_length)
        self.tok.enable_padding(pad_id=0, pad_token="[PAD]")
        self.max_length = max_length

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        self.session = ort.InferenceSession(
            str(model_file),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.input_names = {i.name for i in self.session.get_inputs()}
        shape = self.session.get_outputs()[0].shape
        self.dimension = int(shape[-1]) if shape and isinstance(shape[-1], int) else 0
        self._lock = threading.Lock()
        logger.info(
            f"[OnnxEmbedder] 就绪: {self.model_dir} ({self.model_file}, "
            f"{self.dimension} 维)"
        )

    @classmethod
    def is_available(cls, model_dir: Path | str) -> tuple[bool, str]:
        """检查依赖与模型文件是否齐全（不加载模型）"""
        try:
            import onnxruntime  # noqa: F401
            import tokenizers  # noqa: F401
            import numpy  # noqa: F401
        except ImportError as e:
            return False, f"缺少依赖: {e}"
        path = Path(model_dir)
        if not (path / "tokenizer.json").is_file():
            return False, f"缺少 tokenizer.json: {path}"
        if _find_model_file(path) is None:
            return False, f"缺少 ONNX 模型文件: {path}"
        return True, ""

    @classmethod
    def ensure_model(cls, model_dir: Path | str) -> tuple[bool, str]:
        """检查模型；缺失时从镜像下载并校验 sha256（需要联网）"""
        ok, reason = cls.is_available(model_dir)
        if ok:
            return True, ""

        import hashlib
        import os
        import urllib.request

        base = Path(model_dir)
        base.mkdir(parents=True, exist_ok=True)
        for rel, expected in _MODEL_FILES.items():
            dest = base / rel
            if dest.is_file() and hashlib.sha256(dest.read_bytes()).hexdigest() == expected:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            last_error: Exception | None = None
            for mirror in _MODEL_MIRRORS:
                url = f"{mirror}/{rel}"
                tmp = dest.with_suffix(dest.suffix + ".part")
                try:
                    logger.info(f"[OnnxEmbedder] 下载模型文件: {url}")
                    with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as fh:
                        while True:
                            block = resp.read(1024 * 256)
                            if not block:
                                break
                            fh.write(block)
                    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
                    if digest != expected:
                        raise ValueError(f"sha256 不匹配: {digest}")
                    os.replace(tmp, dest)
                    last_error = None
                    break
                except Exception as e:  # 换下一个镜像
                    last_error = e
                    try:
                        tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
            if last_error is not None:
                return False, f"下载 {rel} 失败: {last_error}"
        return cls.is_available(model_dir)

    def encode(
        self,
        texts,
        batch_size: int = 16,
        show_progress_bar: bool = False,
        **_kwargs,
    ):
        """与 SentenceTransformer.encode() 对齐的最小接口"""
        np = self._np
        if isinstance(texts, str):
            texts = [texts]
        texts = [str(t) for t in texts]
        if not texts:
            return np.zeros((0, self.dimension), dtype=np.float32)

        out = []
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            encs = self.tok.encode_batch(batch)
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feeds = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.input_names:
                feeds["token_type_ids"] = np.zeros_like(ids)
            with self._lock:
                hidden = self.session.run(None, feeds)[0]
            vec = hidden[:, 0, :].astype(np.float32)
            norm = np.clip(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12, None)
            out.append(vec / norm)
        return np.vstack(out)
