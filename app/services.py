import logging

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import FAQ, Chunk, Document, KnowledgeBase, QAMessage, QASession, User, utcnow
from app.providers import embed_query, embed_texts, llm_deltas, parse_document, remote_llm_enabled
from app.textutil import Hit, extractive_answer, normalize_question, rank_chunks, chunk_text

logger = logging.getLogger(__name__)

ABSTAIN = "知识库里没有足够相关的内容，无法回答这个问题。"
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def can_read(user: User | None, kb: KnowledgeBase) -> bool:
    if user and user.role == "admin":
        return True
    if kb.visibility == "public":
        return True
    if user is None:
        return False
    if kb.owner_id == user.id:
        return True
    if kb.visibility == "department" and user.department_id:
        return any(department.id == user.department_id for department in kb.departments)
    return False


def can_write(user: User | None, kb: KnowledgeBase) -> bool:
    if user is None:
        return False
    if user.role == "admin" or kb.owner_id == user.id:
        return True
    if kb.visibility == "department" and user.department_id:
        return any(department.id == user.department_id for department in kb.departments)
    return False


async def accessible_kbs(db: AsyncSession, user: User | None) -> list[KnowledgeBase]:
    rows = (await db.execute(select(KnowledgeBase).order_by(KnowledgeBase.created_at.desc()))).scalars().unique().all()
    return [kb for kb in rows if can_read(user, kb)]


async def get_kb(db: AsyncSession, kb_id: str) -> KnowledgeBase | None:
    stmt = select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
    return (await db.execute(stmt)).scalars().unique().one_or_none()


async def ingest(db: AsyncSession, kb: KnowledgeBase, filename: str, data: bytes) -> Document:
    document = Document(kb_id=kb.id, filename=filename, status="pending")
    db.add(document)
    await db.flush()
    try:
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("文件超过 15MB")
        text = parse_document(filename, data)
        document.raw_text = text
        parts = chunk_text(text, kb.chunk_size, kb.chunk_overlap)
        if not parts:
            raise ValueError("切分后没有可用文本")
        vectors = await embed_texts(parts)
        for index, (part, vector) in enumerate(zip(parts, vectors)):
            db.add(
                Chunk(
                    document_id=document.id,
                    kb_id=kb.id,
                    chunk_index=index,
                    content=part,
                    embedding=vector,
                )
            )
        document.status = "ready"
        document.chunk_count = len(parts)
        document.error = ""
    except ValueError as exc:
        document.status = "failed"
        document.error = str(exc)
    except Exception:
        logger.exception("ingest failed")
        document.status = "failed"
        document.error = "文档处理失败"
    return document


async def reprocess_document(db: AsyncSession, kb: KnowledgeBase, document: Document, text: str) -> Document:
    await db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    parts = chunk_text(text, kb.chunk_size, kb.chunk_overlap)
    vectors = await embed_texts(parts)
    for index, (part, vector) in enumerate(zip(parts, vectors)):
        db.add(
            Chunk(
                document_id=document.id,
                kb_id=kb.id,
                chunk_index=index,
                content=part,
                embedding=vector,
            )
        )
    document.status = "ready" if parts else "failed"
    document.chunk_count = len(parts)
    document.error = "" if parts else "切分后没有可用文本"
    return document


async def delete_document(db: AsyncSession, document: Document) -> None:
    await db.execute(delete(Chunk).where(Chunk.document_id == document.id))
    await db.delete(document)


async def match_faq(db: AsyncSession, kb_ids: list[str], question: str) -> FAQ | None:
    key = normalize_question(question)
    if not key or not kb_ids:
        return None
    stmt = select(FAQ).where(FAQ.kb_id.in_(kb_ids), FAQ.question_key == key)
    return (await db.execute(stmt)).scalars().first()


async def retrieve(db: AsyncSession, kb_ids: list[str], question: str) -> list[Hit]:
    if not kb_ids:
        return []
    stmt = (
        select(Chunk, Document.filename)
        .join(Document, Document.id == Chunk.document_id)
        .where(Chunk.kb_id.in_(kb_ids), Document.status == "ready")
    )
    pairs = (await db.execute(stmt)).all()
    rows = [
        {
            "id": chunk.id,
            "document_id": chunk.document_id,
            "filename": filename,
            "chunk_index": chunk.chunk_index,
            "content": chunk.content,
            "embedding": chunk.embedding or [],
        }
        for chunk, filename in pairs
    ]
    query_vector = await embed_query(question)
    return rank_chunks(question, query_vector, rows, settings.top_k)


def citations_from_hits(hits: list[Hit]) -> list[dict]:
    return [
        {
            "kind": "chunk",
            "document_id": hit.document_id,
            "filename": hit.filename,
            "chunk_index": hit.chunk_index,
            "score": round(hit.score, 6),
            "excerpt": hit.content[:280],
        }
        for hit in hits
    ]


def sse(event: str, data: dict) -> str:
    import json

    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def answer_events(
    question: str,
    kb_ids: list[str],
    user_id: str | None,
    session_id: str | None,
):
    from app.db import SessionLocal

    async with SessionLocal() as db:
        session = await _load_session(db, session_id, user_id)
        history = await _history(db, session.id)
        db.add(QAMessage(session_id=session.id, role="user", content=question))
        if not session.title:
            session.title = question[:40]
        session.updated_at = utcnow()
        await db.commit()

        yield sse("session", {"session_id": session.id})
        faq = await match_faq(db, kb_ids, question)
        citations: list[dict]
        if faq:
            citations = [{"kind": "faq", "question": faq.question, "knowledge_base_id": faq.kb_id}]
            answer = faq.answer
            yield sse("citations", {"citations": citations})
            for piece in _pieces(answer):
                yield sse("chunk", {"text": piece})
        else:
            hits = await retrieve(db, kb_ids, question)
            if not hits:
                citations = []
                answer = ABSTAIN
                yield sse("citations", {"citations": citations})
                yield sse("chunk", {"text": answer})
            else:
                citations = citations_from_hits(hits)
                yield sse("citations", {"citations": citations})
                if remote_llm_enabled():
                    answer = ""
                    try:
                        async for delta in llm_deltas(_llm_messages(question, hits, history)):
                            answer += delta
                            yield sse("chunk", {"text": delta})
                    except Exception:
                        logger.exception("llm stream failed")
                        if not answer:
                            answer = extractive_answer(question, hits)
                            yield sse("chunk", {"text": answer})
                            yield sse("error", {"message": "模型调用失败，已改用资料摘录"})
                        else:
                            yield sse("error", {"message": "模型输出中断"})
                else:
                    answer = extractive_answer(question, hits)
                    for piece in _pieces(answer):
                        yield sse("chunk", {"text": piece})

        db.add(QAMessage(session_id=session.id, role="assistant", content=answer, citations=citations))
        session.updated_at = utcnow()
        await db.commit()
        yield sse("done", {"session_id": session.id})


async def _load_session(db: AsyncSession, session_id: str | None, user_id: str | None) -> QASession:
    if session_id:
        session = await db.get(QASession, session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="会话不存在")
        return session
    session = QASession(user_id=user_id, title="")
    db.add(session)
    await db.flush()
    return session


async def _history(db: AsyncSession, session_id: str) -> list[dict]:
    stmt = (
        select(QAMessage)
        .where(QAMessage.session_id == session_id)
        .order_by(QAMessage.created_at.asc())
    )
    messages = (await db.execute(stmt)).scalars().all()
    return [{"role": message.role, "content": message.content} for message in messages[-8:]]


def _llm_messages(question: str, hits: list[Hit], history: list[dict]) -> list[dict]:
    context = []
    for number, hit in enumerate(hits, start=1):
        context.append(f"[{number}] 文件：{hit.filename}\n{hit.content}")
    system = (
        "你是企业知识库助手。只能依据给定资料回答。"
        "如果资料不足以回答，就明确说知识库中没有相关内容，不要编造来源。"
        "使用中文，简洁，并在对应句子末尾用 [编号] 标注依据。\n\n"
        + "\n\n".join(context)
    )
    prior = history[-6:]
    return [
        {"role": "system", "content": system},
        *prior,
        {"role": "user", "content": question},
    ]


def _pieces(text: str, width: int = 36):
    for start in range(0, len(text), width):
        yield text[start : start + width]
