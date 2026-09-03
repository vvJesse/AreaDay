"""Offline sentence embeddings backed by the pinned ONNX model."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np


SKILL_DIR = Path(__file__).resolve().parents[1]
MODEL_MANIFEST = SKILL_DIR / "references" / "embedding-model-manifest.json"
DEFAULT_MODEL_ROOT = Path.home() / ".researchramp" / "models" / "sentence-transformers"
MODEL_DIRECTORY_GLOB = "sentence-transformers--all-MiniLM-L6-v2--*"


def resolve_model_path(model_root: Path | None = None) -> Path:
    """Return the newest installed snapshot of the pinned embedding model."""

    configured = os.environ.get("RESEARCHRAMP_MODEL_DIR")
    root = model_root or (
        Path(configured).expanduser() if configured else DEFAULT_MODEL_ROOT
    )
    matches = sorted(root.glob(MODEL_DIRECTORY_GLOB))
    if not matches:
        raise FileNotFoundError(
            f"The verified all-MiniLM-L6-v2 ONNX model was not found under {root}. "
            "Run the Skill installer first."
        )
    return matches[-1]


def mean_pool_and_normalize(
    token_embeddings: np.ndarray,
    attention_mask: np.ndarray,
) -> np.ndarray:
    """Match Sentence Transformers' mean pooling and L2 normalization."""

    embeddings = np.asarray(token_embeddings, dtype=np.float32)
    mask = np.asarray(attention_mask, dtype=np.float32)
    if embeddings.ndim != 3 or mask.ndim != 2:
        raise ValueError(
            "Expected token embeddings [batch, sequence, dimension] and "
            "attention mask [batch, sequence]"
        )
    if embeddings.shape[:2] != mask.shape:
        raise ValueError("Token embeddings and attention mask shapes do not match")

    expanded_mask = mask[..., np.newaxis]
    token_counts = np.clip(expanded_mask.sum(axis=1), 1e-9, None)
    pooled = (embeddings * expanded_mask).sum(axis=1) / token_counts
    norms = np.linalg.norm(pooled, axis=1, keepdims=True)
    return np.asarray(pooled / np.clip(norms, 1e-12, None), dtype=np.float32)


class OnnxSentenceEncoder:
    """Load and run one local, pinned sentence-transformer ONNX snapshot."""

    def __init__(
        self,
        model_path: Path,
        *,
        manifest_path: Path = MODEL_MANIFEST,
    ) -> None:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("backend") != "onnxruntime":
            raise RuntimeError("Embedding manifest does not select the ONNX Runtime backend")

        self.dimension = int(manifest["embedding_dimension"])
        self.max_sequence_length = int(manifest["max_sequence_length"])
        model_file = model_path / str(manifest["model_file"])
        tokenizer_file = model_path / str(manifest["tokenizer_file"])
        if not model_file.is_file() or not tokenizer_file.is_file():
            raise FileNotFoundError(
                f"The verified ONNX model snapshot is incomplete: {model_path}"
            )

        # Official ONNX Runtime builds include optional telemetry. AreaDay's local
        # inference path disables it before importing the runtime and again through
        # the public API after import.
        os.environ["ORT_DISABLE_TELEMETRY"] = "1"
        import onnxruntime as ort
        from tokenizers import Tokenizer

        ort.disable_telemetry_events()
        self._tokenizer = Tokenizer.from_file(str(tokenizer_file))
        self._tokenizer.enable_truncation(max_length=self.max_sequence_length)
        pad_id = self._tokenizer.token_to_id("[PAD]")
        if pad_id is None:
            raise RuntimeError("The pinned tokenizer does not define a [PAD] token")
        self._tokenizer.enable_padding(
            direction="right",
            pad_id=pad_id,
            pad_type_id=0,
            pad_token="[PAD]",
        )
        self._session = ort.InferenceSession(
            str(model_file),
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {
            model_input.name for model_input in self._session.get_inputs()
        }
        expected_inputs = {"input_ids", "attention_mask", "token_type_ids"}
        if not expected_inputs.issubset(self._input_names):
            raise RuntimeError(
                "Unexpected ONNX model inputs: " + ", ".join(sorted(self._input_names))
            )

    def encode(self, texts: list[str], *, batch_size: int = 32) -> np.ndarray:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)

        batches: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            encodings = self._tokenizer.encode_batch(texts[start : start + batch_size])
            input_ids = np.asarray([item.ids for item in encodings], dtype=np.int64)
            attention_mask = np.asarray(
                [item.attention_mask for item in encodings], dtype=np.int64
            )
            token_type_ids = np.asarray(
                [item.type_ids for item in encodings], dtype=np.int64
            )
            token_embeddings = self._session.run(
                None,
                {
                    "input_ids": input_ids,
                    "attention_mask": attention_mask,
                    "token_type_ids": token_type_ids,
                },
            )[0]
            batches.append(mean_pool_and_normalize(token_embeddings, attention_mask))

        vectors = np.ascontiguousarray(np.concatenate(batches, axis=0), dtype=np.float32)
        if vectors.shape != (len(texts), self.dimension):
            raise RuntimeError(f"Unexpected embedding shape: {vectors.shape}")
        if not np.isfinite(vectors).all():
            raise RuntimeError("Embedding inference returned a non-finite value")
        norms = np.linalg.norm(vectors, axis=1)
        if not np.allclose(norms, 1.0, rtol=1e-4, atol=1e-5):
            raise RuntimeError("Embedding inference returned a non-normalized vector")
        return vectors


@lru_cache(maxsize=4)
def _cached_encoder(model_path: str) -> OnnxSentenceEncoder:
    return OnnxSentenceEncoder(Path(model_path))


def embed_texts(texts: list[str]) -> np.ndarray:
    model_path = resolve_model_path().resolve()
    return _cached_encoder(str(model_path)).encode(texts)
