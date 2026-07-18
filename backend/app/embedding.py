"""Local sentence embeddings with a dependency-free lexical fallback.

Portable builds may bundle an ONNX backend and install it with
``set_backend``.  The built-in hashing backend deliberately performs no
network access and keeps search available when the optional runtime/model is
missing.
"""

from __future__ import annotations

import hashlib
import math
import re
from array import array
from pathlib import Path
from typing import Protocol, Sequence

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", re.I)


class EmbeddingBackend(Protocol):
    model_id: str
    model_sha256: str
    dimension: int
    tokenizer_version: str

    def encode(self, texts: Sequence[str]) -> list[list[float]]: ...


def _tokens(text: str) -> list[str]:
    return TOKEN_RE.findall(str(text or "").casefold())


_CONCEPTS = {
    "approve": "authorization", "approved": "authorization", "approval": "authorization",
    "authorize": "authorization", "authorized": "authorization",
    "buy": "procurement", "purchase": "procurement", "purchases": "procurement",
    "supplier": "vendor", "suppliers": "vendor", "employee": "staff", "employees": "staff",
    "inspect": "review", "inspected": "review", "checking": "review", "checked": "review",
    "quarterly": "three-month", "annually": "yearly", "annual": "yearly",
}


def _features(text: str) -> list[str]:
    tokens = _tokens(text)
    concepts = [_CONCEPTS.get(token, token) for token in tokens]
    return tokens + concepts + [f"{a}_{b}" for a, b in zip(concepts, concepts[1:])]


class HashingEmbeddingBackend:
    """Small, deterministic CPU backend used as the always-available runtime.

    It is not presented as a neural model.  It supplies stable normalized
    vectors for exact cosine search and makes the lexical-only degradation
    path useful in locked-down installs where the bundled ONNX asset cannot be
    loaded.
    """

    model_id = "local-hashing-embeddings-v1"
    model_sha256 = hashlib.sha256(model_id.encode()).hexdigest()
    dimension = 384
    tokenizer_version = "audit-tokenizer-v1"

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        output: list[list[float]] = []
        for text in texts:
            vector = array("f", [0.0]) * self.dimension
            features = _features(text)
            for feature in features:
                digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
                value = int.from_bytes(digest, "little")
                vector[value % self.dimension] += -1.0 if value & 1 else 1.0
            norm = math.sqrt(sum(value * value for value in vector))
            output.append([value / norm for value in vector] if norm else list(vector))
        return output


class OnnxSentenceEmbeddingBackend:
    """CPU-only bundled ONNX sentence encoder; never downloads model files.

    Portable packaging can place ``model.onnx`` and ``tokenizer.json`` in the
    configured directory. Optional runtime imports stay isolated here so a
    missing/corrupt bundle degrades cleanly.
    """

    tokenizer_version = "tokenizers-json-v1"

    def __init__(self, model_dir: Path, *, model_id: str = "bundled-sentence-encoder"):
        import numpy as np  # type: ignore
        import onnxruntime as ort  # type: ignore
        from tokenizers import Tokenizer  # type: ignore

        self._np = np
        self.model_path = Path(model_dir) / "model.onnx"
        tokenizer_path = Path(model_dir) / "tokenizer.json"
        if not self.model_path.is_file() or not tokenizer_path.is_file():
            raise FileNotFoundError("Bundled embedding model or tokenizer is missing.")
        self.model_id = model_id
        self.model_sha256 = hashlib.sha256(self.model_path.read_bytes()).hexdigest()
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._session = ort.InferenceSession(str(self.model_path), providers=["CPUExecutionProvider"])
        shape = self._session.get_outputs()[0].shape
        self.dimension = int(shape[-1]) if isinstance(shape[-1], int) else 384

    def encode(self, texts: Sequence[str]) -> list[list[float]]:
        encodings = self._tokenizer.encode_batch([str(text or "") for text in texts])
        width = max((len(item.ids) for item in encodings), default=1)
        input_ids, masks = [], []
        for item in encodings:
            length = len(item.ids); padding = width - length
            input_ids.append(item.ids + [0] * padding)
            masks.append([1] * length + [0] * padding)
        ids = self._np.asarray(input_ids, dtype=self._np.int64)
        mask = self._np.asarray(masks, dtype=self._np.int64)
        expected = {value.name for value in self._session.get_inputs()}
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in expected:
            feed["token_type_ids"] = self._np.zeros_like(ids)
        hidden = self._session.run(None, {key: value for key, value in feed.items() if key in expected})[0]
        weights = mask[..., None]
        pooled = (hidden * weights).sum(axis=1) / self._np.maximum(weights.sum(axis=1), 1)
        norms = self._np.linalg.norm(pooled, axis=1, keepdims=True)
        return (pooled / self._np.maximum(norms, 1e-12)).astype(self._np.float32).tolist()


_backend: EmbeddingBackend | None = HashingEmbeddingBackend()
_backend_error: str | None = None


def _load_bundled() -> None:
    global _backend, _backend_error
    model_dir = Path(__file__).resolve().parent / "assets" / "embedding"
    if not model_dir.exists():
        return
    try:
        _backend = OnnxSentenceEmbeddingBackend(model_dir)
        _backend_error = None
    except Exception as error:
        _backend = HashingEmbeddingBackend()
        _backend_error = f"Bundled ONNX encoder unavailable; using local fallback: {error}"


_load_bundled()


def set_backend(backend: EmbeddingBackend | None, error: str | None = None) -> None:
    """Install a bundled/test backend. ``None`` explicitly enables lexical-only search."""
    global _backend, _backend_error
    _backend, _backend_error = backend, error


def get_backend() -> EmbeddingBackend | None:
    return _backend


def runtime_identity() -> str:
    backend = get_backend()
    if backend is None:
        return "lexical-only:audit-tokenizer-v1"
    return ":".join((backend.model_sha256, str(backend.dimension), backend.tokenizer_version))


def status() -> dict:
    backend = get_backend()
    return {
        "available": backend is not None,
        "lexical_fallback": True,
        "backend": getattr(backend, "model_id", None),
        "model_sha256": getattr(backend, "model_sha256", None),
        "dimension": getattr(backend, "dimension", None),
        "tokenizer_version": getattr(backend, "tokenizer_version", "audit-tokenizer-v1"),
        "runtime_identity": runtime_identity(),
        "error": _backend_error,
    }
