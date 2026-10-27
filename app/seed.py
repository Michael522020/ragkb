from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import FAQ, Department, KnowledgeBase, User
from app.providers import read_example
from app.security import hash_password
from app.services import ingest
from app.textutil import normalize_question


async def seed(db: AsyncSession) -> None:
    admin = (
        await db.execute(select(User).where(User.username == settings.admin_username))
    ).scalar_one_or_none()
    if admin is None:
        admin = User(
            username=settings.admin_username,
            hashed_password=hash_password(settings.admin_password),
            role="admin",
        )
        db.add(admin)
        await db.flush()
    if not settings.seed_demo:
        return
    existing = (
        await db.execute(select(KnowledgeBase).where(KnowledgeBase.name == "员工手册（示例）"))
    ).scalar_one_or_none()
    if existing is not None:
        return
    department = (
        await db.execute(select(Department).where(Department.code == "GENERAL"))
    ).scalar_one_or_none()
    if department is None:
        department = Department(code="GENERAL", name="综合部")
        db.add(department)
        await db.flush()
    kb = KnowledgeBase(
        name="员工手册（示例）",
        description="本地演示用的公开知识库，可以在管理端删除。",
        visibility="public",
        owner_id=admin.id,
    )
    db.add(kb)
    await db.flush()
    text = read_example("employee-handbook.md")
    await ingest(db, kb, "employee-handbook.md", text.encode("utf-8"))
    question = "餐补每天多少钱"
    db.add(
        FAQ(
            kb_id=kb.id,
            question=question,
            question_key=normalize_question(question),
            answer="餐补标准为每天 80 元，适用于外出办公且不能返回食堂的工作日。",
        )
    )
