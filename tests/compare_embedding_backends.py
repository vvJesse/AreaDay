#!/usr/bin/env python3
"""Compare the retired PyTorch backend with the replacement ONNX backend."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from onnx_embeddings import OnnxSentenceEncoder  # noqa: E402


TEXTS = [
    "Pragmatic inference can make a literally true statement misleading.",
    "A truthful sentence may nevertheless create a false conversational implicature.",
    "Transformer encoders map text into a semantic vector space.",
    "We evaluate calibration under distribution shift.",
    "The telescope measured atmospheric carbon dioxide concentrations.",
    "Large language models can express uncertainty in natural language.",
    "Reinforcement learning optimizes a policy from delayed rewards.",
    "A randomized clinical trial compares two treatments.",
    "",
    " ".join(["semantic"] * 400),
]

PYTORCH_PROBE = r"""
import json
import sys
from sentence_transformers import SentenceTransformer

model = SentenceTransformer(sys.argv[1], device="cpu", local_files_only=True)
vectors = model.encode(
    json.loads(sys.argv[2]),
    batch_size=32,
    show_progress_bar=False,
    normalize_embeddings=True,
)
print(json.dumps(vectors.tolist()))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pytorch-python", type=Path, required=True)
    parser.add_argument("--pytorch-model", type=Path, required=True)
    parser.add_argument("--onnx-model", type=Path, required=True)
    args = parser.parse_args()

    baseline_payload = subprocess.check_output(
        [
            str(args.pytorch_python),
            "-c",
            PYTORCH_PROBE,
            str(args.pytorch_model),
            json.dumps(TEXTS),
        ],
        text=True,
    )
    baseline = np.asarray(json.loads(baseline_payload), dtype=np.float32)
    candidate = OnnxSentenceEncoder(args.onnx_model).encode(TEXTS)

    vector_cosines = np.sum(baseline * candidate, axis=1)
    baseline_similarities = baseline @ baseline.T
    candidate_similarities = candidate @ candidate.T
    maximum_similarity_delta = float(
        np.max(np.abs(baseline_similarities - candidate_similarities))
    )
    baseline_rankings = np.argsort(-baseline_similarities, axis=1)
    candidate_rankings = np.argsort(-candidate_similarities, axis=1)
    rankings_equal = bool(np.array_equal(baseline_rankings, candidate_rankings))

    result = {
        "status": "ok",
        "text_count": len(TEXTS),
        "embedding_shape": list(candidate.shape),
        "minimum_same_text_cosine": float(np.min(vector_cosines)),
        "maximum_pairwise_similarity_delta": maximum_similarity_delta,
        "rankings_equal": rankings_equal,
    }
    if float(np.min(vector_cosines)) < 0.9999:
        raise RuntimeError(f"ONNX vector drift is too large: {result}")
    if maximum_similarity_delta > 1e-4:
        raise RuntimeError(f"ONNX similarity drift is too large: {result}")
    if not rankings_equal:
        raise RuntimeError(f"ONNX similarity rankings changed: {result}")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
