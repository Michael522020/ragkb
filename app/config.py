from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "ragkb.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    app_name: str = "RAGKB"
    app_version: str = "1.0.0"
    database_url: str = f"sqlite+aiosqlite:///{DEFAULT_DB.as_posix()}"
    jwt_secret: str = "please-change-this-secret"
    jwt_expire_minutes: int = 60 * 12
    admin_username: str = "admin"
    admin_password: str = "Admin123!"
    seed_demo: bool = True
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "text-embedding-3-small"
    top_k: int = 4
    local_embedding_dim: int = 256


settings = Settings()
