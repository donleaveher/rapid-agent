"""History memory: remember solved questions, reuse them on similar ones.

Payoffs:
  1. Reuse  -- a near-identical past question (same db) returns its known-good SQL
     directly, skipping generation.
  2. Retrieval few-shots -- the most similar past solutions become per-question
     few-shots for the generator (better than static mined ones).

Retrieval is ADAPTIVE by memory size (configurable):
  - small store  -> plain lexical (token Jaccard), no rerank  [SQLOOP_MEMORY_RERANK=off / auto+below threshold]
  - large store  -> HYBRID recall (BM25 sparse + fuzzy char, plus an optional dense
    embedding ranker) fused with Reciprocal Rank Fusion, then an optional
    cross-encoder rerank.

The dense embedding ranker and the cross-encoder reranker are PLUGGABLE hooks
(see set_dense_ranker / set_reranker): off by default (zero deps, fully offline),
plugged in later when the store is big enough to warrant the cost. Everything
degrades gracefully to lexical if a backend is absent.

Config:
  SQLOOP_MEMORY_RERANK = auto | on | off   (default auto)
  SQLOOP_MEMORY_SMALL_N = 30                (auto switches to hybrid at/above this)
"""

from __future__ import annotations

import json
import math
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Optional

MEMORY_PATH = Path(__file__).resolve().parent.parent / "data" / "memory.json"
REUSE_THRESHOLD = 0.9     # >= this similarity (same db) -> reuse the stored SQL
RETRIEVE_MIN_SIM = 0.15   # lexical floor for relevance
RRF_K = 60                # Reciprocal Rank Fusion constant

# Pluggable backends (off by default). Plug real ones in when the store grows.
#   dense ranker:  (query: str, candidates: list[str]) -> list[float] scores
#   reranker:      (query: str, candidates: list[str]) -> list[float] scores
_dense_ranker: Optional[Callable[[str, list[str]], list[float]]] = None
_reranker: Optional[Callable[[str, list[str]], list[float]]] = None


def set_dense_ranker(fn) -> None:
    global _dense_ranker
    _dense_ranker = fn


def set_reranker(fn) -> None:
    global _reranker
    _reranker = fn


# ---- similarity primitives -------------------------------------------------

def _tokens(q: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", (q or "").lower())


def similarity(a: str, b: str) -> float:
    """Token Jaccard in [0,1] (order-insensitive, offline)."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _bm25_scores(query: str, docs: list[str], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Classic BM25 over a small in-memory corpus."""
    doc_toks = [_tokens(d) for d in docs]
    n = len(doc_toks)
    avgdl = sum(len(t) for t in doc_toks) / n if n else 0.0
    df: dict[str, int] = {}
    for toks in doc_toks:
        for term in set(toks):
            df[term] = df.get(term, 0) + 1
    scores = []
    for toks in doc_toks:
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        s = 0.0
        for q in set(_tokens(query)):
            if q not in tf:
                continue
            idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
            f = tf[q]
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * dl / (avgdl or 1)))
        scores.append(s)
    return scores


def _fuzzy_scores(query: str, docs: list[str]) -> list[float]:
    return [SequenceMatcher(None, query.lower(), d.lower()).ratio() for d in docs]


def _rrf(rankings: list[list[int]], n: int) -> list[float]:
    """Reciprocal Rank Fusion of several rankings (lists of doc indices, best-first)."""
    fused = [0.0] * n
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            fused[idx] += 1.0 / (RRF_K + rank)
    return fused


def _order(scores: list[float]) -> list[int]:
    return sorted(range(len(scores)), key=lambda i: -scores[i])


# ---- store -----------------------------------------------------------------

class Memory:
    def __init__(self, path: str | Path = MEMORY_PATH):
        self.path = Path(path)
        self.entries: list[dict] = (
            json.loads(self.path.read_text()) if self.path.exists() else []
        )

    def __len__(self) -> int:
        return len(self.entries)

    def add(self, question: str, sql: str, db_id: str) -> None:
        if not sql.strip():
            return
        for e in self.entries:
            if e["db_id"] == db_id and e["question"] == question:
                e["sql"] = sql
                return
        self.entries.append({"question": question, "sql": sql, "db_id": db_id})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2))

    def _same_db(self, db_id: str) -> list[dict]:
        return [e for e in self.entries if e["db_id"] == db_id]

    def overlap_with(self, examples: list[dict]) -> list[dict]:
        """Memory entries that collide with eval examples (same db_id + question).

        reuse() returns stored SQL without re-executing it, trusting that memory was
        built only from training data. If held-out examples ever leak into memory,
        reuse() would silently return the gold-derived answer -> inflated accuracy.
        This makes that detectable.
        """
        keys = {(e["db_id"], e["question"]) for e in examples}
        return [e for e in self.entries if (e["db_id"], e["question"]) in keys]

    def assert_disjoint_from(self, examples: list[dict], where: str = "eval set") -> None:
        """Raise if memory overlaps `examples` (call before evaluating WITH memory)."""
        clash = self.overlap_with(examples)
        if clash:
            raise ValueError(
                f"memory leakage: {len(clash)} entries overlap the {where} "
                f"(e.g. {clash[0]['db_id']}: {clash[0]['question'][:60]!r}). "
                "Build memory only from data disjoint from the held-out slices.")

    def reuse(self, question: str, db_id: str) -> str | None:
        """Stored SQL for a near-identical past question (same db), else None."""
        best, best_sim = None, 0.0
        for e in self._same_db(db_id):
            s = similarity(question, e["question"])
            if s > best_sim:
                best, best_sim = e, s
        return best["sql"] if best and best_sim >= REUSE_THRESHOLD else None

    def _use_hybrid(self, corpus_size: int) -> bool:
        mode = os.environ.get("SQLOOP_MEMORY_RERANK", "auto").lower()
        if mode == "on":
            return True
        if mode == "off":
            return False
        return corpus_size >= int(os.environ.get("SQLOOP_MEMORY_SMALL_N", "30"))

    def retrieve(self, question: str, db_id: str, k: int = 3) -> list[dict]:
        """Top-k similar past solutions (same db) as few-shot dicts {question, sql}."""
        corpus = self._same_db(db_id)
        if not corpus:
            return []

        if not self._use_hybrid(len(corpus)):  # small store -> plain lexical
            scored = sorted(corpus, key=lambda e: -similarity(question, e["question"]))
            return [{"question": e["question"], "sql": e["sql"]}
                    for e in scored if similarity(question, e["question"]) >= RETRIEVE_MIN_SIM][:k]

        # large store -> hybrid recall (sparse + fuzzy + optional dense) via RRF
        qs = [e["question"] for e in corpus]
        rankings = [_order(_bm25_scores(question, qs)), _order(_fuzzy_scores(question, qs))]
        if _dense_ranker is not None:
            rankings.append(_order(_dense_ranker(question, qs)))
        fused = _rrf(rankings, len(corpus))
        cand_m = min(len(corpus), max(k * 4, 20))  # widen candidate pool as store grows
        cand = _order(fused)[:cand_m]

        if _reranker is not None:  # optional cross-encoder rerank
            rr = _reranker(question, [qs[i] for i in cand])
            cand = [cand[i] for i in _order(rr)]

        return [{"question": corpus[i]["question"], "sql": corpus[i]["sql"]} for i in cand[:k]]
