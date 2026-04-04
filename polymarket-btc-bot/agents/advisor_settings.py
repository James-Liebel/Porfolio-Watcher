from __future__ import annotations

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AdvisorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    llm_provider: Literal["ollama", "openai_compatible"] = Field(
        default="ollama",
        alias="LLM_PROVIDER",
        description="ollama = local Ollama; openai_compatible = Groq, OpenRouter, OpenAI, etc.",
    )

    ollama_base_url: str = Field(default="http://127.0.0.1:11434", alias="OLLAMA_BASE_URL")
    ollama_model: str = Field(default="llama3.2", alias="OLLAMA_MODEL")

    openai_api_base: str = Field(
        default="https://api.openai.com/v1",
        alias="OPENAI_API_BASE",
    )
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")

    advisor_host: str = Field(default="127.0.0.1", alias="ADVISOR_HOST")
    advisor_port: int = Field(default=8780, alias="ADVISOR_PORT")
    advice_cache_seconds: int = Field(default=90, alias="ADVICE_CACHE_SECONDS")
    advisor_http_timeout: float = Field(default=120.0, alias="ADVISOR_HTTP_TIMEOUT")

    agent_a_port: int = Field(default=8765, alias="AGENT_A_PORT")
    agent_b_port: int = Field(default=8767, alias="AGENT_B_PORT")
