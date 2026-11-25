"""Qwen3 embeddings from the llama.cpp runtime shipped inside Seekino."""
from __future__ import annotations

import ctypes as ct
import math
from pathlib import Path
import sys
from threading import Lock

from gateway.embedding import EmbeddingResult
from gateway.local.qwen_assets import DIMENSIONS, MODEL_ID, QUERY_INSTRUCTION
from gateway.usage import track_model_call


def _library_path() -> Path:
    bundle = Path(__file__).resolve().parents[2]
    if sys.platform == "darwin":
        return bundle.parent / "Frameworks" / "libseekino-qwen.dylib"
    if sys.platform == "win32":
        return bundle / "qwen-runtime" / "seekino-qwen.dll"
    raise RuntimeError("Local Qwen3 is supported on macOS and Windows desktop")


def _load_bridge():
    path = _library_path()
    # ggml opens its CPU plugin using LoadLibraryW, whose dependency lookup
    # is independent of the search flags ctypes uses for the bridge itself.
    openmp = ct.CDLL(str(path.with_name("libomp.dll"))) if sys.platform == "win32" else None
    lib = ct.CDLL(str(path))
    lib._openmp = openmp
    lib.qwen_init.argtypes = [ct.c_char_p]
    lib.qwen_init.restype = ct.c_int
    lib.qwen_open.argtypes = [ct.c_char_p]
    lib.qwen_open.restype = ct.c_void_p
    lib.qwen_encode.argtypes = [ct.c_void_p, ct.c_char_p, ct.c_int, ct.POINTER(ct.c_float), ct.POINTER(ct.c_int)]
    lib.qwen_encode.restype = ct.c_int
    lib.qwen_cancel.argtypes = [ct.POINTER(ct.c_int)]
    lib.qwen_cancel.restype = None
    lib.qwen_close.argtypes = [ct.c_void_p]
    lib.qwen_close.restype = None
    if lib.qwen_init(str(path.parent).encode()) != 0:
        raise RuntimeError("无法加载随应用提供的 Qwen3 CPU 运行库")
    return lib


# Concurrent indexing/search clients share one model mapping and serialize use of its context.
_pool_lock = Lock()
_sessions = {}
_bridge = None


class _Session:
    def __init__(self, path):
        global _bridge
        if _bridge is None:
            _bridge = _load_bridge()
        self.lib = _bridge
        self.context = self.lib.qwen_open(str(path).encode())
        if not self.context:
            raise RuntimeError("无法载入本机 Qwen3 模型")
        self.lock = Lock()
        self.clients = 0


class LocalQwenEmbedder:
    provider_name = "local_qwen"
    model_name = MODEL_ID

    def __init__(self, model_path: Path):
        self.path = model_path.resolve()
        self._lock = Lock()
        self._session = None
        self._closed = False

    def embed_query(self, query: str) -> EmbeddingResult:
        if not query.strip():
            raise ValueError("query must not be empty")
        return self._embed(f"Instruct: {QUERY_INSTRUCTION}\nQuery: {query.strip()}")

    def embed_text(self, text: str) -> EmbeddingResult:
        if not text.strip():
            raise ValueError("text must not be empty")
        return self._embed(text.strip())

    def _embed(self, text: str) -> EmbeddingResult:
        raw = text.encode("utf-8")
        if len(raw) > 2**31 - 1:
            raise ValueError("Qwen3 input is too large")
        with track_model_call(provider=self.provider_name, model=MODEL_ID, operation="text_embedding") as call, self._lock:
            call.raise_if_cancelled()
            if self._closed:
                raise RuntimeError("Embedding model is closed")
            if self._session is None:
                with _pool_lock:
                    if self.path not in _sessions:
                        _sessions[self.path] = _Session(self.path)
                    self._session = _sessions[self.path]
                    self._session.clients += 1
            s = self._session
            result = (ct.c_float * DIMENSIONS)()
            flag = ct.c_int(0)
            unregister = call.add_cancel_callback(lambda: s.lib.qwen_cancel(ct.byref(flag)))
            try:
                with s.lock:
                    call.raise_if_cancelled()
                    tokens = s.lib.qwen_encode(s.context, raw, len(raw), result, ct.byref(flag))
                call.raise_if_cancelled()
            finally:
                unregister()
            if tokens == -2:
                raise ValueError("文本超过本机 Qwen3 的 8192 token 上限")
            if tokens <= 0:
                raise RuntimeError("本机 Qwen3 编码失败")
            norm = math.hypot(*result)
            if not math.isfinite(norm) or not norm:
                raise ValueError("Qwen3 returned an invalid vector")
            call.capture_response({"model": MODEL_ID, "usage": {"input_tokens": tokens, "total_tokens": tokens}})
            return EmbeddingResult(model=MODEL_ID, modality="text", vector=[v / norm for v in result])

    def embed_image(self, image_path):
        raise ValueError("Local Qwen3 supports text only")

    def embed_video(self, video_path):
        raise ValueError("Local Qwen3 supports text only")

    def embed_sparse(self, text):
        raise ValueError("Local Qwen3 provides dense vectors only")

    def close(self):
        with self._lock, _pool_lock:
            self._closed = True
            if self._session is not None:
                s = self._session
                self._session = None
                s.clients -= 1
                if not s.clients:
                    s.lib.qwen_close(s.context)
                    del _sessions[self.path]
