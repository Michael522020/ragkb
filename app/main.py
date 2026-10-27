import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router
from app.config import ROOT, settings
from app.db import Base, SessionLocal, engine
from app.seed import seed

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    (ROOT / "data").mkdir(parents=True, exist_ok=True)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        await seed(session)
        await session.commit()
    if settings.jwt_secret == "please-change-this-secret":
        logging.getLogger(__name__).warning("JWT_SECRET is still the default value")
    yield


app = FastAPI(title=settings.app_name, version=settings.app_version, lifespan=lifespan)
app.include_router(router, prefix="/api/v1")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
async def guest_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/admin")
async def admin_page() -> FileResponse:
    return FileResponse(ROOT / "static" / "admin.html")
