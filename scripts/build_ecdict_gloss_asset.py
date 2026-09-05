#!/usr/bin/env python3
"""Normalize ECDICT bilingual fields into AreaDay's compact runtime asset."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import re
from pathlib import Path


WORD_PATTERN = re.compile(r"^[a-z][a-z'-]*$")


def compact(value: str, maximum: int = 420) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in value.splitlines()]
    lines = [line for line in lines if line and not line.startswith("[")]
    if not lines:
        return ""
    text = lines[0]
    return text if len(text) <= maximum else text[: maximum - 1].rstrip() + "…"


def build(source: Path, output: Path) -> dict[str, int | str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    count = 0
    with source.open(encoding="utf-8", newline="") as input_handle, gzip.open(
        temporary, "wt", encoding="utf-8", newline=""
    ) as output_handle:
        reader = csv.DictReader(input_handle)
        writer = csv.DictWriter(
            output_handle,
            fieldnames=["lemma", "part_of_speech", "meaning_en", "meaning_zh"],
            delimiter="\t",
        )
        writer.writeheader()
        for row in reader:
            lemma = str(row.get("word") or "").strip().casefold()
            meaning_zh = compact(str(row.get("translation") or ""))
            if not WORD_PATTERN.fullmatch(lemma) or not meaning_zh:
                continue
            writer.writerow(
                {
                    "lemma": lemma,
                    "part_of_speech": str(row.get("pos") or "").strip(),
                    "meaning_en": compact(str(row.get("definition") or "")),
                    "meaning_zh": meaning_zh,
                }
            )
            count += 1
    temporary.replace(output)
    return {
        "entry_count": count,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(args.source.resolve(), args.output.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
