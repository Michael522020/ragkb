import json
import logging
from pathlib import Path

import httpx
from pypdf import PdfReader

from app.config import ROOT, settings
from app.textutil import local_embed

logger = logging.getLogger(__name__)

_SUFFIXES = {".txt", ".md", ".markdown", ".pdf"}


def parse_document(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix not in _SUFFIXES:
        raise ValueError("只支持 txt、md、pdf")
    if suffix == ".pdf":
        reader = PdfReader(stream=_bytes_stream(data))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n\n".join(pages)
    else:
        text = data.decode("utf-8-sig", errors="replace")
    text = text.replace("\x00", "").strip()
    if not text:
        raise ValueError("没有从文件中解析出文本")
    return text


def _bytes_stream(data: bytes):
    import io

    return io.BytesIO(data)


def read_example(name: str) -> str:
    path = ROOT / "examples" / name
    return path.read_text(encoding="utf-8")


def remote_embedding_enabled() -> bool:
    return bool(settings.embedding_api_key.strip())


def remote_llm_enabled() -> bool:
    return bool(settings.llm_api_key.strip())


async def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    if not remote_embedding_enabled():
        return [local_embed(text) for text in texts]
    vectors: list[list[float]] = []
    for offset in range(0, len(texts), 32):
        vectors.extend(await _remote_embed(texts[offset : offset + 32]))
    return vectors


async def embed_query(text: str) -> list[float]:
    vectors = await embed_texts([text])
    return vectors[0]


async def _remote_embed(texts: list[str]) -> list[list[float]]:
    base = (settings.embedding_base_url or settings.llm_base_url).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{base}/embeddings",
                headers={"Authorization": f"Bearer {settings.embedding_api_key}"},
                json={"model": settings.embedding_model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()["data"]
    except (httpx.HTTPError, KeyError, TypeError) as exc:
        logger.warning("remote embedding failed, using local vectors: %s", type(exc).__name__)
        return [local_embed(text) for text in texts]
    payload.sort(key=lambda item: item.get("index", 0))
    return [item["embedding"] for item in payload]


async def llm_deltas(messages: list[dict]):
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": True,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {settings.llm_api_key}"}
    async with httpx.AsyncClient(timeout=90) as client:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                text = delta.get("content") or ""
                if text:
                    yield text
