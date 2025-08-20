from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, AliasChoices


class Settings(BaseSettings):
    # Pydantic v2 config
    model_config = SettingsConfigDict(extra="ignore", env_file=".env", env_file_encoding="utf-8")
    telegram_bot_token: str = "CHANGE_ME"
    admin_user_id: int | None = None

    # Payment provider switch
    payment_provider: str = "yookassa"  # yookassa

    # Robokassa (disabled)

    # YooKassa
    yk_shop_id: str | None = None
    yk_api_key: str | None = None

    # 3x-ui panel
    x3ui_base_url: str = "http://127.0.0.1:2053"  # without trailing slash
    x3ui_username: str | None = None
    x3ui_password: str | None = None
    x3ui_api_key: str | None = None  # if you use a fork with API keys
    x3ui_inbound_id: int = 1
    x3ui_client_days: int = 30
    x3ui_client_traffic_gb: int | None = None  # None = unlimited

    # Public base URL (accepts env PUBLIC_BASE_URL or BASE_URL)
    public_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("PUBLIC_BASE_URL", "BASE_URL"),
    )

    # Pricing/tariffs
    plan_name: str = "Monthly"
    plan_days: int = 30
    plan_price_rub: int = 300

    sqlite_url: str = "sqlite+aiosqlite:///./bot.db"

    # No pydantic v1 Config


settings = Settings()
