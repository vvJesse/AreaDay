#!/usr/bin/env python3
"""Download if requested, then exercise ResearchRamp's local NLP runtime."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import urllib.parse
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = Path.home() / ".researchramp" / "models" / "sentence-transformers"
MODEL_MANIFEST = SKILL_DIR / "references" / "embedding-model-manifest.json"
SPACY_MODEL = "en_core_web_sm"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def local_model_path(model_dir: Path, repository: str, revision: str) -> Path:
    safe_repository = repository.replace("/", "--")
    return model_dir / f"{safe_repository}--{revision[:12]}"


def ensure_model_files(
    target: Path,
    *,
    endpoint: str,
    repository: str,
    revision: str,
    files: dict[str, str],
    offline: bool,
) -> None:
    if offline:
        missing = [
            relative_path
            for relative_path, expected_hash in files.items()
            if not (target / relative_path).is_file()
            or sha256(target / relative_path) != expected_hash
        ]
        if missing:
            raise RuntimeError(
                "Pinned embedding model is incomplete or corrupted: " + ", ".join(missing)
            )
        return

    import httpx

    target.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": "ResearchRamp/0.1 (public model installer)"}
    timeout = httpx.Timeout(connect=30, read=120, write=30, pool=30)
    with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers) as client:
        for relative_path, expected_hash in files.items():
            destination = target / relative_path
            if destination.is_file() and sha256(destination) == expected_hash:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            staging = destination.with_suffix(destination.suffix + ".part")
            encoded_path = urllib.parse.quote(relative_path, safe="/")
            url = (
                f"{endpoint.rstrip('/')}/{repository}/resolve/{revision}/{encoded_path}"
            )
            try:
                with client.stream("GET", url) as response:
                    response.raise_for_status()
                    digest = hashlib.sha256()
                    with staging.open("wb") as output:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            digest.update(chunk)
                            output.write(chunk)
                actual_hash = digest.hexdigest()
                if actual_hash != expected_hash:
                    raise RuntimeError(
                        f"Integrity check failed for {relative_path} from {endpoint}: "
                        f"expected {expected_hash}, got {actual_hash}"
                    )
                staging.replace(destination)
            finally:
                staging.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify spaCy and Sentence Transformers with real inference."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--download-models",
        action="store_true",
        help="Allow downloading the embedding model before verification.",
    )
    mode.add_argument(
        "--offline",
        action="store_true",
        help="Require all model files to exist locally; make no model download.",
    )
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    model_dir = args.model_dir.resolve()
    if args.download_models:
        model_dir.mkdir(parents=True, exist_ok=True)
    elif not model_dir.exists():
        raise SystemExit(f"Embedding model directory does not exist: {model_dir}")

    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    import spacy
    from sentence_transformers import SentenceTransformer
    from symspellpy import SymSpell, Verbosity
    from fsrs import Card, Rating, Scheduler
    import importlib.resources

    manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
    embedding_model = manifest["repository"]
    revision = manifest["revision"]

    nlp = spacy.load(SPACY_MODEL)
    document = nlp(
        "Truthful statements may still create misleading pragmatic impressions."
    )
    content_tokens = [
        token.lemma_.lower()
        for token in document
        if token.is_alpha and not token.is_stop
    ]
    if not content_tokens or not all(token.pos_ for token in document if token.is_alpha):
        raise RuntimeError("spaCy loaded, but its English tagging/lemmatization did not run")

    spell = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
    dictionary = importlib.resources.files("symspellpy") / "frequency_dictionary_en_82_765.txt"
    if not spell.load_dictionary(str(dictionary), 0, 1):
        raise RuntimeError("SymSpell dictionary did not load")
    segmentation = spell.word_segmentation("largescale", max_edit_distance=0)
    if segmentation.corrected_string != "large scale":
        raise RuntimeError(f"Unexpected SymSpell segmentation: {segmentation.corrected_string}")

    review_card, review_log = Scheduler(desired_retention=0.9).review_card(
        Card(), Rating.Good
    )
    if review_card.due <= review_log.review_datetime:
        raise RuntimeError("FSRS loaded, but did not schedule the reviewed card")

    endpoint = os.environ.get("RESEARCHRAMP_MODEL_ENDPOINT") or os.environ.get(
        "HF_ENDPOINT", "https://huggingface.co"
    )
    snapshot_path = local_model_path(model_dir, embedding_model, revision)
    ensure_model_files(
        snapshot_path,
        endpoint=endpoint,
        repository=embedding_model,
        revision=revision,
        files=manifest["files"],
        offline=args.offline,
    )
    for relative_path, expected_hash in manifest["files"].items():
        path = snapshot_path / relative_path
        if not path.is_file():
            raise RuntimeError(f"Pinned embedding model file is missing: {relative_path}")
        actual_hash = sha256(path)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Embedding model integrity check failed for {relative_path}: "
                f"expected {expected_hash}, got {actual_hash}"
            )

    model = SentenceTransformer(
        str(snapshot_path),
        device="cpu",
        local_files_only=True,
        trust_remote_code=False,
    )
    vectors = model.encode(
        [
            "The literal statement is true.",
            "The statement creates a misleading impression.",
        ],
        normalize_embeddings=True,
        show_progress_bar=False,
    )
    if len(vectors) != 2 or len(vectors[0]) < 100:
        raise RuntimeError(f"Unexpected embedding shape: {getattr(vectors, 'shape', None)}")
    if not all(math.isfinite(float(value)) for row in vectors for value in row):
        raise RuntimeError("Embedding inference returned a non-finite value")

    result = {
        "status": "ok",
        "python_packages": {
            "sentence-transformers": importlib.metadata.version("sentence-transformers"),
            "spacy": importlib.metadata.version("spacy"),
            "spacy-model": importlib.metadata.version(SPACY_MODEL),
            "symspellpy": importlib.metadata.version("symspellpy"),
            "fsrs": importlib.metadata.version("fsrs"),
        },
        "embedding_model": embedding_model,
        "embedding_revision": revision,
        "embedding_dimension": len(vectors[0]),
        "spacy_model": SPACY_MODEL,
        "spacy_content_tokens": content_tokens,
        "symspell_segmentation": segmentation.corrected_string,
        "fsrs_due": review_card.due.isoformat(),
        "model_directory": str(model_dir),
        "download_endpoint": endpoint,
        "integrity_files_verified": len(manifest["files"]),
        "offline_verification": args.offline,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
