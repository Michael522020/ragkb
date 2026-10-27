import re
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import FAQ, Chunk, Department, Document, KBDepartment, KnowledgeBase, QAMessage, QASession, User
from app.security import create_access_token, decode_access_token, hash_password, verify_password
from app.services import (
    accessible_kbs,
    answer_events,
    can_read,
    can_write,
    delete_document,
    get_kb,
    ingest,
    reprocess_document,
)
from app.textutil import normalize_question

router = APIRouter()
bearer = HTTPBearer(auto_error=False)
_USERNAME = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff]{3,32}$")


class AuthIn(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)


class PasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class DepartmentIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=80)


class UserPatch(BaseModel):
    role: str | None = None
    department_id: str | None = None
    clear_department: bool = False
    is_active: bool | None = None


class KnowledgeBaseIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=2000)
    visibility: str = "private"
    department_ids: list[str] = Field(default_factory=list)
    chunk_size: int = Field(default=480, ge=80, le=4000)
    chunk_overlap: int = Field(default=60, ge=0, le=1000)


class KnowledgeBasePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, max_length=2000)


class FAQIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=8000)


class AskIn(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    knowledge_base_id: str | None = None
    session_id: str | None = None


def _user_out(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "department_id": user.department_id,
        "department_name": user.department.name if user.department else None,
        "is_active": user.is_active,
    }


def _kb_out(kb: KnowledgeBase) -> dict:
    return {
        "id": kb.id,
        "name": kb.name,
        "description": kb.description,
        "visibility": kb.visibility,
        "owner_id": kb.owner_id,
        "chunk_size": kb.chunk_size,
        "chunk_overlap": kb.chunk_overlap,
        "departments": [
            {"id": item.id, "code": item.code, "name": item.name} for item in kb.departments
        ],
        "created_at": kb.created_at.isoformat(),
    }


def _doc_out(document: Document) -> dict:
    return {
        "id": document.id,
        "knowledge_base_id": document.kb_id,
        "filename": document.filename,
        "status": document.status,
        "error": document.error,
        "chunk_count": document.chunk_count,
        "created_at": document.created_at.isoformat(),
    }


async def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="未登录")
    payload = decode_access_token(credentials.credentials)
    user = await db.get(User, payload.get("sub"))
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail="用户不可用")
    return user


async def optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    if credentials is None:
        return None
    return await current_user(credentials, db)


async def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def _check_username(username: str) -> None:
    if _USERNAME.fullmatch(username) is None:
        raise HTTPException(status_code=400, detail="用户名需为 3 到 32 位字母、数字、下划线或中文")


async def _require_kb(db: AsyncSession, kb_id: str, user: User | None, write: bool) -> KnowledgeBase:
    kb = await get_kb(db, kb_id)
    if kb is None or not can_read(user, kb):
        raise HTTPException(status_code=404, detail="知识库不存在")
    if write and not can_write(user, kb):
        raise HTTPException(status_code=403, detail="没有写入权限")
    return kb


@router.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "version": settings.app_version,
        "embedding": "remote" if settings.embedding_api_key.strip() else "local",
        "llm": "remote" if settings.llm_api_key.strip() else "extractive",
    }


@router.post("/auth/register", status_code=201)
async def register(body: AuthIn, db: AsyncSession = Depends(get_db)) -> dict:
    _check_username(body.username)
    exists = (
        await db.execute(select(User).where(User.username == body.username))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    user = User(username=body.username, hashed_password=hash_password(body.password), role="member")
    db.add(user)
    await db.commit()
    user = await _reload_user(db, user.id)
    return {"access_token": create_access_token(user.id, user.username, user.role), "user": _user_out(user)}


@router.post("/auth/login")
async def login(body: AuthIn, db: AsyncSession = Depends(get_db)) -> dict:
    user = (await db.execute(select(User).where(User.username == body.username))).scalar_one_or_none()
    if user is None or not user.is_active or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    return {"access_token": create_access_token(user.id, user.username, user.role), "user": _user_out(user)}


@router.get("/auth/me")
async def me(user: User = Depends(current_user)) -> dict:
    return _user_out(user)


@router.post("/auth/password")
async def change_password(
    body: PasswordIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if not verify_password(body.old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="原密码不正确")
    user.hashed_password = hash_password(body.new_password)
    await db.commit()
    return {"ok": True}


@router.get("/departments")
async def list_departments(
    _user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    rows = (await db.execute(select(Department).order_by(Department.created_at.asc()))).scalars().all()
    return [{"id": item.id, "code": item.code, "name": item.name} for item in rows]


@router.post("/departments", status_code=201)
async def create_department(
    body: DepartmentIn,
    _admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    code = body.code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9_\-]{1,32}", code):
        raise HTTPException(status_code=400, detail="部门编码只能包含大写字母、数字、下划线或横线")
    exists = (await db.execute(select(Department).where(Department.code == code))).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="部门编码已存在")
    department = Department(code=code, name=body.name.strip())
    db.add(department)
    await db.commit()
    await db.refresh(department)
    return {"id": department.id, "code": department.code, "name": department.name}


@router.delete("/departments/{department_id}", status_code=204)
async def delete_department(
    department_id: str,
    _admin: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    department = await db.get(Department, department_id)
    if department is None:
        raise HTTPException(status_code=404, detail="部门不存在")
    in_use = (
        await db.execute(select(func.count()).select_from(User).where(User.department_id == department_id))
    ).scalar_one()
    linked = (
        await db.execute(
            select(func.count()).select_from(KBDepartment).where(KBDepartment.department_id == department_id)
        )
    ).scalar_one()
    if in_use or linked:
        raise HTTPException(status_code=409, detail="部门仍被用户或知识库使用")
    await db.delete(department)
    await db.commit()


@router.get("/users")
async def list_users(_admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (await db.execute(select(User).order_by(User.created_at.asc()))).scalars().all()
    return [_user_out(user) for user in rows]


@router.patch("/users/{user_id}")
async def patch_user(
    user_id: str,
    body: UserPatch,
    actor: User = Depends(admin_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    if body.role is not None:
        if body.role not in {"admin", "member"}:
            raise HTTPException(status_code=400, detail="角色只能是 admin 或 member")
        if user.role == "admin" and body.role != "admin":
            await _keep_one_admin(db, user.id)
        user.role = body.role
    if body.is_active is not None:
        if user.id == actor.id and not body.is_active:
            raise HTTPException(status_code=400, detail="不能停用自己")
        if user.role == "admin" and not body.is_active:
            await _keep_one_admin(db, user.id)
        user.is_active = body.is_active
    if body.clear_department:
        user.department_id = None
    elif body.department_id is not None:
        department = await db.get(Department, body.department_id)
        if department is None:
            raise HTTPException(status_code=400, detail="部门不存在")
        user.department_id = department.id
    await db.commit()
    user = await _reload_user(db, user.id)
    return _user_out(user)


async def _keep_one_admin(db: AsyncSession, user_id: str) -> None:
    count = (
        await db.execute(
            select(func.count()).select_from(User).where(User.role == "admin", User.is_active.is_(True), User.id != user_id)
        )
    ).scalar_one()
    if count < 1:
        raise HTTPException(status_code=400, detail="至少保留一名启用的管理员")


@router.get("/knowledge-bases")
async def list_knowledge_bases(
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    return [_kb_out(kb) for kb in await accessible_kbs(db, user)]


@router.post("/knowledge-bases", status_code=201)
async def create_knowledge_base(
    body: KnowledgeBaseIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    if body.visibility not in {"public", "department", "private"}:
        raise HTTPException(status_code=400, detail="可见范围无效")
    if body.chunk_overlap >= body.chunk_size:
        raise HTTPException(status_code=400, detail="重叠长度必须小于分段长度")
    departments = await _load_departments(db, body.department_ids)
    if body.visibility == "public":
        if user.role != "admin":
            raise HTTPException(status_code=403, detail="只有管理员可以创建公开知识库")
        departments = []
    elif body.visibility == "private":
        departments = []
    else:
        if user.role != "admin":
            if user.department_id is None or [item.id for item in departments] != [user.department_id]:
                raise HTTPException(status_code=403, detail="部门知识库只能关联你所在的部门")
        if not departments:
            raise HTTPException(status_code=400, detail="部门知识库至少关联一个部门")
    kb = KnowledgeBase(
        name=body.name.strip(),
        description=body.description.strip(),
        visibility=body.visibility,
        owner_id=user.id,
        chunk_size=body.chunk_size,
        chunk_overlap=body.chunk_overlap,
    )
    kb.departments = departments
    db.add(kb)
    await db.commit()
    stored = await get_kb(db, kb.id)
    return _kb_out(stored)


@router.patch("/knowledge-bases/{kb_id}")
async def patch_knowledge_base(
    kb_id: str,
    body: KnowledgeBasePatch,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    kb = await _require_kb(db, kb_id, user, write=True)
    if body.name is not None:
        kb.name = body.name.strip()
    if body.description is not None:
        kb.description = body.description.strip()
    await db.commit()
    stored = await get_kb(db, kb.id)
    return _kb_out(stored)


@router.delete("/knowledge-bases/{kb_id}", status_code=204)
async def delete_knowledge_base(
    kb_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    kb = await get_kb(db, kb_id)
    if kb is None or not can_read(user, kb):
        raise HTTPException(status_code=404, detail="知识库不存在")
    if user.role != "admin" and kb.owner_id != user.id:
        raise HTTPException(status_code=403, detail="只有管理员或创建人可以删除知识库")
    await db.execute(delete(Chunk).where(Chunk.kb_id == kb.id))
    await db.execute(delete(FAQ).where(FAQ.kb_id == kb.id))
    await db.execute(delete(Document).where(Document.kb_id == kb.id))
    await db.execute(delete(KBDepartment).where(KBDepartment.kb_id == kb.id))
    await db.delete(kb)
    await db.commit()


@router.get("/knowledge-bases/{kb_id}/documents")
async def list_documents(
    kb_id: str,
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _require_kb(db, kb_id, user, write=False)
    rows = (
        await db.execute(select(Document).where(Document.kb_id == kb_id).order_by(Document.created_at.desc()))
    ).scalars().all()
    return [_doc_out(item) for item in rows]


@router.post("/knowledge-bases/{kb_id}/documents", status_code=201)
async def upload_document(
    kb_id: str,
    file: UploadFile = File(...),
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    kb = await _require_kb(db, kb_id, user, write=True)
    data = await file.read()
    filename = Path(file.filename or "untitled.txt").name or "untitled.txt"
    document = await ingest(db, kb, filename, data)
    await db.commit()
    await db.refresh(document)
    return _doc_out(document)


@router.post("/documents/{document_id}/reprocess")
async def reprocess(
    document_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    kb = await _require_kb(db, document.kb_id, user, write=True)
    if not document.raw_text.strip():
        raise HTTPException(status_code=400, detail="没有可重新处理的文本")
    await reprocess_document(db, kb, document, document.raw_text)
    await db.commit()
    await db.refresh(document)
    return _doc_out(document)


@router.delete("/documents/{document_id}", status_code=204)
async def remove_document(
    document_id: str,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    document = await db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="文档不存在")
    await _require_kb(db, document.kb_id, user, write=True)
    await delete_document(db, document)
    await db.commit()


@router.get("/knowledge-bases/{kb_id}/faqs")
async def list_faqs(
    kb_id: str,
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _require_kb(db, kb_id, user, write=False)
    rows = (await db.execute(select(FAQ).where(FAQ.kb_id == kb_id).order_by(FAQ.created_at.desc()))).scalars().all()
    return [{"id": item.id, "question": item.question, "answer": item.answer} for item in rows]


@router.post("/knowledge-bases/{kb_id}/faqs", status_code=201)
async def create_faq(
    kb_id: str,
    body: FAQIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _require_kb(db, kb_id, user, write=True)
    key = normalize_question(body.question)
    exists = (
        await db.execute(select(FAQ).where(FAQ.kb_id == kb_id, FAQ.question_key == key))
    ).scalar_one_or_none()
    if exists is not None:
        raise HTTPException(status_code=409, detail="相同问题已存在")
    faq = FAQ(kb_id=kb_id, question=body.question.strip(), question_key=key, answer=body.answer.strip())
    db.add(faq)
    await db.commit()
    await db.refresh(faq)
    return {"id": faq.id, "question": faq.question, "answer": faq.answer}


@router.post("/qa/ask")
async def ask(
    body: AskIn,
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    kbs = await accessible_kbs(db, user)
    if body.knowledge_base_id:
        kbs = [kb for kb in kbs if kb.id == body.knowledge_base_id]
        if not kbs:
            raise HTTPException(status_code=404, detail="知识库不存在或无权访问")
    if not kbs:
        raise HTTPException(status_code=400, detail="没有可检索的知识库")
    user_id = user.id if user else None
    if body.session_id:
        session = await db.get(QASession, body.session_id)
        if session is None or session.user_id != user_id:
            raise HTTPException(status_code=404, detail="会话不存在")
    stream = answer_events(body.question.strip(), [kb.id for kb in kbs], user_id, body.session_id)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/qa/sessions")
async def list_sessions(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)) -> list[dict]:
    rows = (
        await db.execute(
            select(QASession).where(QASession.user_id == user.id).order_by(QASession.updated_at.desc()).limit(50)
        )
    ).scalars().all()
    return [
        {
            "id": item.id,
            "title": item.title,
            "updated_at": item.updated_at.isoformat(),
        }
        for item in rows
    ]


@router.get("/qa/sessions/{session_id}")
async def get_session(
    session_id: str,
    user: User | None = Depends(optional_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    session = await db.get(QASession, session_id)
    owner = user.id if user else None
    if session is None or session.user_id != owner:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = (
        await db.execute(
            select(QAMessage).where(QAMessage.session_id == session.id).order_by(QAMessage.created_at.asc())
        )
    ).scalars().all()
    return {
        "id": session.id,
        "title": session.title,
        "messages": [
            {
                "role": message.role,
                "content": message.content,
                "citations": message.citations or [],
            }
            for message in messages
        ],
    }


async def _reload_user(db: AsyncSession, user_id: str) -> User:
    stmt = select(User).where(User.id == user_id)
    return (await db.execute(stmt)).scalars().unique().one()


async def _load_departments(db: AsyncSession, ids: list[str]) -> list[Department]:
    unique_ids = list(dict.fromkeys(ids))
    if not unique_ids:
        return []
    rows = (await db.execute(select(Department).where(Department.id.in_(unique_ids)))).scalars().all()
    if len(rows) != len(unique_ids):
        raise HTTPException(status_code=400, detail="部门不存在")
    order = {department_id: index for index, department_id in enumerate(unique_ids)}
    return sorted(rows, key=lambda item: order[item.id])
