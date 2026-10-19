import json
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    telegram_bot_token: str = ""
    allowed_user_ids: str = ""

    owner_name: str = "Owner"
    owner_email: str = ""
    default_timezone: str = "Asia/Tehran"

    resend_api_key: str = ""
    email_from: str = ""
    email_font_url: str = ""  # optional publicly hosted Vazir font; no local files

    @field_validator("email_font_url")
    @classmethod
    def _public_email_font(cls, value: str) -> str:
        from app.tools.email_renderer import public_font_url

        return public_font_url(value) if value else ""

    postgres_user: str = "assistant"
    postgres_password: str = ""
    postgres_db: str = "assistant"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    audio_dir: str = "/data/audio"

    stt_model_path: str = "/opt/vosk/model"
    stt_min_confidence: float = 0.5

    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_s: float = 30.0
    deepseek_max_retries: int = 3

    # USD per 1M tokens, per model; "*" is the fallback. Peak-hour list prices (an upper bound); edit
    # to match your plan. Cached input tokens use input_hit.
    llm_prices_json: str = (
        '{"deepseek-chat": {"input_miss": 0.30, "input_hit": 0.006, "output": 1.20},'
        ' "deepseek-flash": {"input_miss": 0.30, "input_hit": 0.006, "output": 1.20},'
        ' "*": {"input_miss": 0.30, "input_hit": 0.006, "output": 1.20}}'
    )

    # Budgets count prompt+completion tokens. 0 = no limit. "Today" is the Asia/Tehran calendar day,
    # "month" the current Jalali month.
    daily_token_budget: int = 0
    monthly_token_budget: int = 0
    budget_hard_stop: bool = False  # refuse new requests once a budget reaches 100%

    # Dashboard (separate read-only database role, password login, signed session cookie)
    dashboard_db_user: str = "dashboard_ro"
    dashboard_db_password: str = ""
    dashboard_password_hash: str = ""  # python -m app.dashboard.hashpw
    dashboard_secret_key: str = ""
    dashboard_cookie_secure: bool = False  # true behind HTTPS
    dashboard_host: str = "localhost"
    dashboard_port: int = 8080
    # Serve under a sub-path, e.g. "/dashboard" for https://example.com/dashboard (the reverse proxy
    # must strip the prefix). Empty = served at the root of its own host name.
    dashboard_base_path: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_calendar_id: str = "primary"

    log_level: str = "INFO"
    debug: bool = False  # exposes the OpenAPI docs; keep off in production

    admin_api_key: str = ""  # header X-Admin-Key; admin endpoints are disabled while empty
    max_voice_seconds: int = 60
    rate_limit_per_minute: int = 10  # messages per Telegram user
    audio_max_age_s: int = 3600  # leftover audio older than this is deleted
    telegram_proxy_url: str = ""  # optional HTTPS/SOCKS proxy for api.telegram.org
    # Optional reverse-proxy "gateway" for the Telegram Bot API, e.g. https://relay.example.com
    # (it must forward /bot<TOKEN>/... and /file/bot<TOKEN>/... to api.telegram.org)
    telegram_api_base_url: str = ""
    bot_heartbeat_file: str = "/tmp/bot-heartbeat"
    mcp_port: int = 8765
    mcp_owner_telegram_id: int = 0  # 0 = first ALLOWED_USER_IDS entry

    # Google "Desktop app" client file (downloaded from Cloud Console); used when the
    # GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET variables are empty.
    google_credentials_file: str = "secrets/credentials.json"

    @model_validator(mode="after")
    def _google_client_from_file(self) -> "Settings":
        path = Path(self.google_credentials_file)
        if (not self.google_client_id or not self.google_client_secret) and path.is_file():
            try:
                client = json.loads(path.read_text(encoding="utf-8"))["installed"]
                self.google_client_id = self.google_client_id or client["client_id"]
                self.google_client_secret = self.google_client_secret or client["client_secret"]
            except (ValueError, KeyError):
                pass  # malformed file: Google calls will report missing credentials
        return self

    @property
    def allowed_ids(self) -> frozenset[int]:
        return frozenset(
            int(part) for part in self.allowed_user_ids.replace(" ", "").split(",") if part
        )

    @property
    def dashboard_database_url(self) -> URL:
        return self.database_url.set(
            username=self.dashboard_db_user, password=self.dashboard_db_password
        )

    @property
    def database_url(self) -> URL:
        # Built from parts so passwords with special characters need no escaping.
        return URL.create(
            "postgresql+psycopg",
            username=self.postgres_user,
            password=self.postgres_password,
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
