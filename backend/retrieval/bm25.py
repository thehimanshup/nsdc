"""BM25 retrieval over structured Chunks.

Pure-Python, zero external dependencies. Good enough for ≤ 10,000 chunks
per agent which is comfortably above what a single state's department
corpus realistically needs.

Improvements over Phase 2's `rag.py` BM25:
  - Operates on Chunk dataclass (structured metadata, not raw text)
  - Includes title + body + tag terms in the indexed text
  - Per-term IDF cached; per-query scoring streams
  - Metadata-aware boost — exact scheme-id / scheme-name hit raises
    the score so a literal "PM-KISAN" query lands the PM-KISAN chunk
"""
from __future__ import annotations

import math
import re
from collections import Counter

from .chunk_store import Chunk


# Devanagari + Tamil + Telugu + Kannada + Malayalam + Gujarati + Punjabi + Odia + Bengali
_WORD_RE = re.compile(
    r"[\wऀ-ॿঀ-৿਀-੿઀-૿"
    r"଀-୿஀-௿ఀ-౿ಀ-೿"
    r"ഀ-ൿ]+",
    re.UNICODE,
)


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _WORD_RE.findall(text or "") if len(t) > 1]


class BM25Index:
    """Tiny pure-Python BM25 — fine for tens of thousands of chunks."""

    K1 = 1.5
    B = 0.75

    # Scoring multipliers
    EXACT_TITLE_BOOST = 2.0      # query token is in title verbatim
    SCHEME_ID_BOOST = 3.0        # query has the exact scheme_id from metadata
    TAG_MATCH_BOOST = 1.5

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        self.tokens = [self._chunk_tokens(c) for c in chunks]
        self.doc_len = [len(t) for t in self.tokens]
        self.avgdl = (sum(self.doc_len) / len(self.doc_len)) if self.doc_len else 0.0
        self.N = len(self.tokens)
        # Inverted index: term -> [(chunk_idx, tf), ...]
        self.inv: dict[str, list[tuple[int, int]]] = {}
        for i, toks in enumerate(self.tokens):
            for term, tf in Counter(toks).items():
                self.inv.setdefault(term, []).append((i, tf))
        # IDF
        self.idf: dict[str, float] = {}
        for term, postings in self.inv.items():
            df = len(postings)
            self.idf[term] = math.log((self.N - df + 0.5) / (df + 0.5) + 1.0)

    @staticmethod
    def _chunk_tokens(c: Chunk) -> list[str]:
        """Build the per-chunk token bag — title and tags get extra weight."""
        # Repeat title + scheme_id + tags so they index with higher tf.
        scheme = (c.metadata or {}).get("scheme_id", "")
        extras = " ".join([c.title, c.title,
                            scheme, scheme,
                            " ".join(c.tags or [])])
        return tokenize(c.body + " " + extras)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def search(self, query: str, k: int = 3,
                min_score: float = 0.0) -> list[tuple[Chunk, float]]:
        q_tokens = tokenize(query)
        if not q_tokens or self.N == 0:
            return []
        scores: dict[int, float] = {}
        for q in q_tokens:
            if q not in self.inv:
                continue
            idf = self.idf.get(q, 0.0)
            for doc_idx, tf in self.inv[q]:
                dl = self.doc_len[doc_idx]
                denom = tf + self.K1 * (1 - self.B + self.B * dl / (self.avgdl or 1))
                score = idf * tf * (self.K1 + 1) / denom
                scores[doc_idx] = scores.get(doc_idx, 0.0) + score

        # Metadata boosts
        ql = (query or "").lower()
        for i, c in enumerate(self.chunks):
            if i not in scores:
                continue
            # Title verbatim → boost
            title_terms = set(tokenize(c.title))
            if title_terms.intersection(q_tokens):
                scores[i] *= self.EXACT_TITLE_BOOST
            # Scheme id match (e.g. "pm-kisan" or "pmfby" in query)
            sid = (c.metadata or {}).get("scheme_id", "")
            if sid and sid.lower() in ql:
                scores[i] *= self.SCHEME_ID_BOOST
            # Tag match
            for tag in (c.tags or []):
                if tag.lower() in ql:
                    scores[i] *= self.TAG_MATCH_BOOST
                    break

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        out: list[tuple[Chunk, float]] = []
        for i, s in ranked[:k]:
            if s < min_score:
                continue
            out.append((self.chunks[i], s))
        return out
