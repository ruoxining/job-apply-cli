"""Description vectors + lexical ranking, aggregated to whole experiences.

A small exact cosine index is deliberate: no external vector service is necessary
for a personal library. SQLite caches immutable model/content-addressed vectors.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from .library import Experience, fingerprint

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
REVISION = "e8f8c211226b894fcb81acc59f3b34ba3efd5f42"
# These queries require set coverage, not merely the most similar anecdote.
COVERAGE = re.compile(
    r"\b(current|currently|present|now|ever|never|none|all|total|years?|dates?|"
    r"months?|when|since|duration|graduate[ds]?)\b|\b(?:19|20)\d{2}\b|目前|当前|现在|所有|从未|总共|年|月|毕业",
    re.IGNORECASE,
)


class Encoder(Protocol):
    identity: str

    def chunks(self, text: str) -> list[str]: ...
    def encode(self, texts: list[str]) -> list[list[float]]: ...


class SentenceEncoder:
    identity = f"{MODEL}@{REVISION}:mean:l2:window96:overlap16"

    def __init__(self, cache_dir: Path | None = None):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(
            MODEL,
            revision=REVISION,
            device="cpu",
            trust_remote_code=False,
            cache_folder=str(cache_dir) if cache_dir else None,
        )

    def chunks(self, text: str) -> list[str]:
        tokenizer = self.model.tokenizer
        tokens = tokenizer.encode(text, add_special_tokens=False)
        # Model max sequence length is 128; do not silently truncate long descriptions.
        return [
            tokenizer.decode(tokens[i : i + 96], skip_special_tokens=True)
            for i in range(0, len(tokens), 80)
        ] or [text]

    def encode(self, texts: list[str]) -> list[list[float]]:
        return self.model.encode(
            texts,
            normalize_embeddings=True,
            show_progress_bar=False,
            batch_size=32,
        ).tolist()


@lru_cache(maxsize=1)
def get_sentence_encoder() -> SentenceEncoder:
    """Keep the pinned CPU model warm across sequential jobs in a worker process."""
    return SentenceEncoder()


def lexical_tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:[+#.-][a-z0-9]+)*", text.casefold())
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(run)
        words.extend(run[i : i + 2] for i in range(len(run) - 1))
    return words


def cosine(left, right) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("embedding dimension mismatch")
    denominator = math.sqrt(sum(v * v for v in left) * sum(v * v for v in right))
    if not denominator or not all(math.isfinite(v) for v in [*left, *right]):
        raise ValueError("invalid embedding vector")
    return sum(a * b for a, b in zip(left, right, strict=True)) / denominator


class VectorCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT NOT NULL)"
            )
        path.chmod(0o600)

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def get_many(self, encoder: Encoder, texts: list[str]) -> tuple[list[list[float]], int]:
        keys = [fingerprint([encoder.identity, text]) for text in texts]
        values = {}
        with self.connect() as db:
            for key in set(keys):
                row = db.execute("SELECT vector FROM vectors WHERE key=?", (key,)).fetchone()
                if row:
                    values[key] = json.loads(row[0])
        missing = {key: text for key, text in zip(keys, texts, strict=True) if key not in values}
        if missing:
            vectors = encoder.encode(list(missing.values()))
            if len(vectors) != len(missing):
                raise ValueError("encoder omitted vectors")
            for key, vector in zip(missing, vectors, strict=True):
                cosine(vector, vector)  # reject nonfinite/empty/zero vectors before caching
                values[key] = vector
            with self.connect() as db:
                db.executemany(
                    "INSERT OR REPLACE INTO vectors VALUES (?, ?)",
                    [(key, json.dumps(values[key])) for key in missing],
                )
        return [values[key] for key in keys], len(missing)


@dataclass
class Retrieval:
    experiences: list[Experience]
    matches: list[dict]
    route: str
    elapsed_seconds: float


class HybridRetriever:
    def __init__(self, experiences: list[Experience], encoder: Encoder, cache: VectorCache):
        started = time.perf_counter()
        self.experiences, self.encoder, self.cache = experiences, encoder, cache
        self.chunks = []
        for item in experiences:
            for description_index, description in enumerate(item.description):
                for chunk in encoder.chunks(description):
                    self.chunks.append((item.id, description_index, chunk))
        self.vectors, self.new_vectors = cache.get_many(encoder, [c[2] for c in self.chunks])
        self.index_seconds = time.perf_counter() - started

    def retrieve(
        self, query: str, *, top_k: int = 5, force_ids: set[str] | None = None
    ) -> Retrieval:
        started = time.perf_counter()
        if top_k < 1:
            raise ValueError("top_k must be positive")
        if COVERAGE.search(query):
            # Small personal corpus: full coverage is cheaper and safer than a brittle
            # classifier that drops an education or employment interval. Still require
            # explicit evidence of completeness before deriving a negative answer.
            return Retrieval(self.experiences, [], "coverage_full", time.perf_counter() - started)
        if not self.chunks:
            return Retrieval([], [], "hybrid", time.perf_counter() - started)
        query_chunks = self.encoder.chunks(query)
        query_vectors, _ = self.cache.get_many(self.encoder, query_chunks)
        semantic = [max(cosine(q, v) for q in query_vectors) for v in self.vectors]
        documents = []
        by_id = {item.id: item for item in self.experiences}
        for identifier, _, chunk in self.chunks:
            documents.append(
                Counter(
                    lexical_tokens(
                        chunk + " " + json.dumps(by_id[identifier].metadata, ensure_ascii=False)
                    )
                )
            )
        query_terms = set(lexical_tokens(query))
        df = Counter(term for doc in documents for term in doc)
        avg_length = sum(sum(doc.values()) for doc in documents) / len(documents)
        lexical = []
        for doc in documents:
            score = 0.0
            for term in query_terms:
                tf = doc[term]
                if tf:
                    idf = math.log(1 + (len(documents) - df[term] + 0.5) / (df[term] + 0.5))
                    score += (
                        idf
                        * tf
                        * 2.2
                        / (tf + 1.2 * (0.25 + 0.75 * sum(doc.values()) / max(avg_length, 1)))
                    )
            lexical.append(score)
        combined = [0.0] * len(documents)
        for scores in (semantic, lexical):
            for rank, index in enumerate(
                sorted(range(len(scores)), key=lambda i: (-scores[i], i)), 1
            ):
                if scores[index] > 0:
                    combined[index] += 1 / (60 + rank)
        best = {}
        for i in sorted(range(len(combined)), key=lambda i: (-combined[i], i)):
            identifier, description_index, _ = self.chunks[i]
            if identifier not in best:
                best[identifier] = {
                    "experience_id": identifier,
                    "description_index": description_index,
                    "rrf": combined[i],
                    "cosine": semantic[i],
                    "bm25": lexical[i],
                }
        selected = list(best)[:top_k]
        for identifier in sorted(force_ids or set()):
            if identifier in by_id and identifier not in selected:
                selected.append(identifier)
        return Retrieval(
            [by_id[i] for i in selected],
            [best[i] for i in selected],
            "hybrid",
            time.perf_counter() - started,
        )
