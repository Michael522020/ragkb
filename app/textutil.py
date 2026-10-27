import hashlib
import math
import re
from dataclasses import dataclass

from app.config import settings

_WORD = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+")
_SENTENCE = re.compile(r"(?<=[。！？\n])")


def tokenize(text: str) -> list[str]:
    lowered = text.lower()
    tokens = _WORD.findall(lowered)
    for run in _CJK_RUN.findall(lowered):
        tokens.extend(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def normalize_question(text: str) -> str:
    compact = re.sub(r"\s+", "", text.strip().lower())
    return compact[:500]


def chunk_text(text: str, size: int, overlap: int) -> list[str]:
    cleaned = text.replace("\x00", "").strip()
    if not cleaned:
        return []
    size = max(80, size)
    overlap = min(max(0, overlap), size // 2)
    chunks: list[str] = []
    start = 0
    while start < len(cleaned):
        end = min(len(cleaned), start + size)
        if end < len(cleaned):
            newline = cleaned.rfind("\n", start + size // 2, end)
            if newline > start:
                end = newline
        piece = cleaned[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


def local_embed(text: str, dim: int | None = None) -> list[float]:
    width = dim or settings.local_embedding_dim
    vector = [0.0] * width
    for token in tokenize(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % width
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    return _normalize(vector)


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


@dataclass
class Hit:
    chunk_id: str
    document_id: str
    filename: str
    chunk_index: int
    content: str
    score: float
    vector_score: float
    lexical_score: float


def rank_chunks(
    query: str,
    query_vector: list[float],
    rows: list[dict],
    top_k: int,
) -> list[Hit]:
    if not rows or top_k <= 0:
        return []
    query_tokens = tokenize(query)
    lexical_scores: list[tuple[float, int]] = []
    vector_scores: list[tuple[float, int]] = []
    for index, row in enumerate(rows):
        lexical_scores.append((_lexical(query_tokens, row["content"]), index))
        embedding = row.get("embedding") or []
        vector_scores.append((cosine(query_vector, embedding), index))
    lexical_order = [index for _score, index in sorted(lexical_scores, reverse=True)]
    vector_order = [index for _score, index in sorted(vector_scores, reverse=True)]
    fused = _rrf([vector_order, lexical_order])
    ordered = sorted(fused, key=fused.get, reverse=True)
    hits: list[Hit] = []
    lexical_by_index = dict((index, score) for score, index in lexical_scores)
    vector_by_index = dict((index, score) for score, index in vector_scores)
    for index in ordered[:top_k]:
        row = rows[index]
        lexical = lexical_by_index[index]
        vector = vector_by_index[index]
        if lexical <= 0 and vector < 0.45:
            continue
        hits.append(
            Hit(
                chunk_id=row["id"],
                document_id=row["document_id"],
                filename=row["filename"],
                chunk_index=row["chunk_index"],
                content=row["content"],
                score=fused[index],
                vector_score=vector,
                lexical_score=lexical,
            )
        )
    return hits


def _lexical(query_tokens: list[str], content: str) -> float:
    if not query_tokens:
        return 0.0
    content_tokens = tokenize(content)
    if not content_tokens:
        return 0.0
    counts: dict[str, int] = {}
    for token in content_tokens:
        counts[token] = counts.get(token, 0) + 1
    overlap = sum(counts.get(token, 0) for token in set(query_tokens))
    if overlap == 0:
        return 0.0
    return overlap / math.sqrt(len(content_tokens))


def _rrf(rank_lists: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rank_lists:
        for rank, index in enumerate(ranking, start=1):
            scores[index] = scores.get(index, 0.0) + 1.0 / (k + rank)
    return scores


def extractive_answer(question: str, hits: list[Hit]) -> str:
    query_tokens = set(tokenize(question))
    lines = ["根据已检索到的资料：", ""]
    for number, hit in enumerate(hits, start=1):
        sentences = [part.strip() for part in _SENTENCE.split(hit.content) if part.strip()]
        picked = [sentence for sentence in sentences if query_tokens & set(tokenize(sentence))]
        body = "".join(picked[:3]) if picked else hit.content.strip()[:300]
        lines.append(f"[{number}] {body}")
        lines.append("")
    return "\n".join(lines).strip()
