from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration. Validated at startup (fail fast)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://checkout:checkout_local_pw@localhost:5432/checkout"
    db_pool_size: int = Field(10, ge=1)
    db_max_overflow: int = Field(10, ge=0)
    db_pool_timeout_seconds: float = Field(10, gt=0)
    db_lock_timeout_ms: int = Field(5000, ge=1)
    db_statement_timeout_ms: int = Field(15000, ge=1)

    # Reward rule: every Nth successful order makes one X% coupon available.
    reward_every_n_orders: int = Field(5, ge=1)
    reward_discount_percent: int = Field(10, ge=1, le=100)

    max_line_quantity: int = Field(100, ge=1, le=100)  # DB CHECK caps at 100
    max_cart_items: int = Field(50, ge=1)

    checkout_max_attempts: int = Field(3, ge=1)
    log_level: str = "INFO"


@lru_cache
def get_default_settings() -> Settings:
    return Settings()
