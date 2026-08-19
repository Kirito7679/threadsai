from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Threads app
    threads_app_id: str = ""
    threads_app_secret: str = ""
    threads_redirect_uri: str = "http://localhost:8000/auth/callback"

    app_base_url: str = "http://localhost:8000"
    dashboard_password: str = "admin"
    secret_key: str = "dev-secret-change-me"

    # Запасной вход по паролю даёт доступ к первому аккаунту в базе. Для
    # собственного развёртывания это удобно, для публичного сервиса — чёрный
    # ход в чужой кабинет. Перед выходом в общий доступ ставится false.
    enable_password_login: bool = True

    database_url: str = "sqlite:///./data/app.db"

    # DeepSeek
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_reasoner_model: str = "deepseek-reasoner"

    # Цены DeepSeek в долларах за миллион токенов — нужны, чтобы считать
    # себестоимость аккаунта. Тариф меняется, поэтому вынесен в переменные.
    deepseek_price_input: float = 0.27  # промпт мимо кеша
    deepseek_price_cached: float = 0.07  # промпт из кеша
    deepseek_price_output: float = 1.10  # ответ

    timezone: str = "Europe/Moscow"
    drafts_per_run: int = 5
    research_interval_hours: int = 6
    metrics_interval_minutes: int = 60
    generation_hour: int = 8
    enable_scheduler: bool = True

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @property
    def scopes(self) -> str:
        return ",".join(
            [
                "threads_basic",
                "threads_content_publish",
                "threads_manage_insights",
                "threads_read_replies",
                "threads_manage_replies",
                "threads_keyword_search",
            ]
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
