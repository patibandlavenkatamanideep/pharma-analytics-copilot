"""Application settings. Every secret is read from the environment; nothing
sensitive is ever written to a file in this repository."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PAC_", env_file=".env", extra="ignore")

    # --- database -----------------------------------------------------------
    # Three separate login roles. A scoped connection has no membership path to
    # the Exec role, so it cannot SET ROLE its way to pricing data.
    db_host: str = "127.0.0.1"
    db_port: int = 5432
    db_name: str = "pharma_analytics"

    db_owner_user: str = "pac_owner"
    db_owner_password: str = ""

    db_auth_user: str = "pac_auth_login"
    db_auth_password: str = ""

    db_exec_user: str = "pac_exec_login"
    db_exec_password: str = ""

    db_scoped_user: str = "pac_scoped_login"
    db_scoped_password: str = ""

    # --- execution limits ---------------------------------------------------
    statement_timeout_ms: int = 5_000
    max_result_rows: int = 5_000
    max_result_bytes: int = 4_000_000

    # --- LLM ----------------------------------------------------------------
    # "bedrock" uses AWS Bedrock; "offline" uses the deterministic rule-based
    # planner so that every non-LLM layer stays testable without credentials.
    llm_provider: Literal["bedrock", "offline"] = "offline"
    bedrock_region: str = "us-east-1"
    bedrock_model_id: str = "anthropic.claude-opus-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    llm_max_tokens: int = 4_096
    llm_timeout_s: float = 30.0

    # --- sessions -----------------------------------------------------------
    session_ttl_hours: int = 12
    cookie_secure: bool = True
    cookie_name: str = "pac_session"

    # --- versions (stamped onto every audit row) ----------------------------
    schema_version: str = "1.0.0"
    policy_version: str = "1.0.0"

    environment: Literal["local", "cloud"] = "local"

    def dsn(self, role: Literal["owner", "auth", "exec", "scoped"]) -> str:
        user, password = {
            "owner": (self.db_owner_user, self.db_owner_password),
            "auth": (self.db_auth_user, self.db_auth_password),
            "exec": (self.db_exec_user, self.db_exec_password),
            "scoped": (self.db_scoped_user, self.db_scoped_password),
        }[role]
        auth = f"{user}:{password}@" if password else f"{user}@"
        return f"postgresql://{auth}{self.db_host}:{self.db_port}/{self.db_name}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
