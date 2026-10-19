"""集中配置（pydantic-settings）。

约定：
- 所有外部地址/配额从环境读，代码里禁止写死国家或主机（Task-Plan.md §12.1、Task-Server.md §10）。
- api / gateway / worker 共用同一份配置，只有 `role` 不同。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Role = Literal["api", "gateway", "worker"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # 进程
    kuma_env: Literal["dev", "prod"] = "dev"
    role: Role = "api"
    api_port: int = Field(8100, alias="KUMA_API_PORT")
    gateway_health_port: int = Field(8101, alias="KUMA_GATEWAY_HEALTH_PORT")
    worker_health_port: int = Field(8102, alias="KUMA_WORKER_HEALTH_PORT")

    # PostgreSQL
    dsn: str = Field("postgresql+asyncpg://kuma_app@127.0.0.1:5432/kuma", alias="KUMA_DSN")
    # 维护通道（分区 DDL、rep 物化视图刷新）：kuma_app 只有 DML 权限，
    # 这类动作必须走 kuma_owner（Task-DataBase.md §5.1/§11.0）
    maintenance_dsn: str = Field("", alias="KUMA_MAINTENANCE_DSN")
    pool_size: int = Field(5, alias="KUMA_POOL_SIZE")
    max_overflow: int = Field(5, alias="KUMA_MAX_OVERFLOW")
    pool_timeout: int = Field(5, alias="KUMA_POOL_TIMEOUT")

    # Redis（复用 orio-search-redis：DB5 + 前缀 + 强制 TTL）
    redis_url: str = Field("redis://127.0.0.1:6379/5", alias="KUMA_REDIS_URL")
    redis_prefix: str = Field("kuma:", alias="KUMA_REDIS_PREFIX")
    state_ttl: int = Field(90, alias="KUMA_STATE_TTL")

    # RabbitMQ
    amqp_url: str = Field("amqp://guest@127.0.0.1:5672/%2Fkuma", alias="KUMA_AMQP_URL")
    mqtt_url: str = Field("mqtt://127.0.0.1:1883", alias="KUMA_MQTT_URL")
    mqtt_user: str = Field("kuma_mq", alias="KUMA_MQTT_USER")
    mqtt_password: str = Field("", alias="KUMA_MQTT_PASSWORD")

    # 鉴权
    jwt_secret: str = Field("dev-only-secret-change-me", alias="KUMA_JWT_SECRET")
    jwt_alg: str = Field("HS256", alias="KUMA_JWT_ALG")
    access_ttl: int = Field(900, alias="KUMA_ACCESS_TTL")
    refresh_ttl: int = Field(2_592_000, alias="KUMA_REFRESH_TTL")

    # 遥测管道
    tele_batch_size: int = Field(500, alias="KUMA_TELE_BATCH_SIZE")
    tele_flush_interval: float = Field(1.0, alias="KUMA_TELE_FLUSH_INTERVAL")
    tele_queue_max: int = Field(20_000, alias="KUMA_TELE_QUEUE_MAX")
    # 原始遥测点保留天数：调试机 20G 盘，1000 台 @5s ≈ 17.3M 行/天 ≈ 5GB/天 → 默认 3 天（生产正式机设 7）
    tele_keep_days: int = Field(3, alias="KUMA_TELE_KEEP_DAYS")
    tele_precreate_days: int = Field(2, alias="KUMA_TELE_PRECREATE_DAYS")
    deadband_min_dist_m: float = Field(15.0, alias="KUMA_DEADBAND_MIN_DIST_M")
    deadband_max_speed: float = Field(3.0, alias="KUMA_DEADBAND_MAX_SPEED")
    backfill_max_hours: int = Field(72, alias="KUMA_BACKFILL_MAX_HOURS")

    # 下行控制
    cmd_max_retry: int = Field(3, alias="KUMA_CMD_MAX_RETRY")
    cmd_timeout: int = Field(20, alias="KUMA_CMD_TIMEOUT")
    cmd_speed_limit_kmh: float = Field(5.0, alias="KUMA_CMD_SPEED_LIMIT_KMH")

    # 地图
    google_key: str = Field("", alias="KUMA_GOOGLE_KEY")
    amap_key: str = Field("", alias="KUMA_AMAP_KEY")
    maps_cache_ttl: int = Field(3600, alias="KUMA_MAPS_CACHE_TTL")
    maps_daily_quota: int = Field(20_000, alias="KUMA_MAPS_DAILY_QUOTA")

    # 轨迹查询硬约束（512MB PG 下防 OOM，Task-DataBase.md §8）
    trace_max_days: int = Field(7, alias="KUMA_TRACE_MAX_DAYS")

    @property
    def is_prod(self) -> bool:
        return self.kuma_env == "prod"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_prod and (
        s.jwt_secret.startswith("dev-only") or "CHANGE_ME" in s.dsn or "CHANGE_ME" in s.amqp_url
    ):
        raise RuntimeError("prod 环境仍在使用占位凭据，拒绝启动")
    return s
