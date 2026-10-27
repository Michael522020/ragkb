import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "data" / "pytest.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    path = Path(str(DB_PATH) + suffix) if suffix else DB_PATH
    if path.exists():
        path.unlink()

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + DB_PATH.resolve().as_posix()
os.environ["JWT_SECRET"] = "pytest-secret"
os.environ["ADMIN_USERNAME"] = "admin"
os.environ["ADMIN_PASSWORD"] = "Admin123!"
os.environ["SEED_DEMO"] = "false"
os.environ["LLM_API_KEY"] = ""
os.environ["EMBEDDING_API_KEY"] = ""
