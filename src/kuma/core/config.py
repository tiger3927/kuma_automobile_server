"""集中配置（pydantic-settings）。

约定：
- 所有外部地址/配额从环境读，代码里禁止写死国家或主机（Task-Plan.md §12.1、Task-Server.md §10）。
- api / gateway / worker 共用同一份配置，只有 `role` 不同。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlparse

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
    # 前端产物目录（`web/admin` 的 build 结果，`base: "/admin/"`）。空或目录不存在 = 不挂静态，
    # 本机开发走 vite dev（5173 把 /api 代理到线上）。生产指仓库外的目录，
    # 因为 `web/` 不进 git、产物是传上去的，不能让 dist 混进版本库。
    admin_dist: str = Field("", alias="KUMA_ADMIN_DIST")

    # PostgreSQL
    dsn: str = Field("postgresql+asyncpg://kuma_app@127.0.0.1:5432/kuma", alias="KUMA_DSN")
    # 维护通道（分区 DDL、rep 物化视图刷新）：kuma_app 只有 DML 权限，
    # 这类动作必须走 kuma_owner（Task-DataBase.md §5.1/§11.0）
    maintenance_dsn: str = Field("", alias="KUMA_MAINTENANCE_DSN")
    pool_size: int = Field(5, alias="KUMA_POOL_SIZE")
    max_overflow: int = Field(5, alias="KUMA_MAX_OVERFLOW")
    pool_timeout: int = Field(5, alias="KUMA_POOL_TIMEOUT")

    # Redis：kuma 自己的容器（127.0.0.1:6380，2026-10-22 起，§4.1）。
    # 此前复用 orio-search-redis 的 DB5——那个容器 requirepass 为空（本机任意进程可读写全部 db），
    # 且有 256MiB cgroup 硬限却 maxmemory=0 + noeviction（涨满被 docker 杀掉，连坐邻居项目）。
    # 这里的默认值只给本机开发用（无口令）；生产由 deploy/runtime.env 注入带口令 URL，
    # 且 scripts/gen_systemd_env.sh 缺 KUMA_REDIS_PASSWORD 会直接报错退出。
    redis_url: str = Field("redis://127.0.0.1:6380/0", alias="KUMA_REDIS_URL")
    redis_prefix: str = Field("kuma:", alias="KUMA_REDIS_PREFIX")
    state_ttl: int = Field(90, alias="KUMA_STATE_TTL")

    # RabbitMQ
    amqp_url: str = Field("amqp://guest@127.0.0.1:5672/%2Fkuma", alias="KUMA_AMQP_URL")
    # 管理口（探针查队列深度用）。留空则从 amqp_url 的 host 推 `http://<host>:15672`。
    # 为什么需要它：AMQP `queue.declare-ok` 里的 message_count/consumer_count 在 RabbitMQ
    # 已废弃且不保证准确——2026-10-21 实测 management 报 messages=1，passive declare 报 0。
    mq_mgmt_url: str = Field("", alias="KUMA_MQ_MGMT_URL")
    mqtt_url: str = Field("mqtt://127.0.0.1:1883", alias="KUMA_MQTT_URL")
    mqtt_user: str = Field("kuma_mq", alias="KUMA_MQTT_USER")
    mqtt_password: str = Field("", alias="KUMA_MQTT_PASSWORD")
    mqtt_vhost: str = Field("/kuma", alias="KUMA_MQTT_VHOST")
    # RabbitMQ HTTP auth 的对接路径密钥：空 = 不挂载 /internal/mqtt-auth/*。
    # 8100 被 nginx 全量反代到公网，路径里带随机段才不会被人当凭据探针枚举（见 api/app.py）。
    mq_auth_token: str = Field("", alias="KUMA_MQ_AUTH_TOKEN")

    # 鉴权
    jwt_secret: str = Field("dev-only-secret-change-me", alias="KUMA_JWT_SECRET")
    jwt_alg: str = Field("HS256", alias="KUMA_JWT_ALG")
    access_ttl: int = Field(900, alias="KUMA_ACCESS_TTL")
    refresh_ttl: int = Field(2_592_000, alias="KUMA_REFRESH_TTL")
    # 证件号哈希的 pepper（KYC）。哈希必须**确定性**——§2.3-25"同证件多账号 → 关联标记"
    # 靠的就是同一个证件号算出同一个哈希；但裸 `sha256(证件号)` 能被撞库还原
    # （各国证件号格式固定、长度有限），所以必须 HMAC 加 pepper。
    # 留空回落到 `jwt_secret`：能跑，但轮换 jwt_secret 会让历史关联断掉——生产应显式配置。
    id_hash_pepper: str = Field("", alias="KUMA_ID_HASH_PEPPER")

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

    # 连接风暴闸门（C9，§8.7 的连接侧）：MQTT 每次连接都会回调 /internal/mqtt-auth/user，
    # 所以这里是"每分钟连接次数"，不是消息数。
    # 单车 10 次/分钟 = 正常重连（掉线重连、网络抖动）的 5 倍以上余量；
    # 全局 300 次/分钟按 100 台试点算 = 每台每分钟最多 3 次连接也不会触发。
    # 超限直接 `deny`：argon2 校验在闸门之后，风暴不会打满 api CPU。
    mq_connect_per_vin_per_min: int = Field(10, alias="KUMA_MQ_CONNECT_PER_VIN_PER_MIN")
    mq_connect_burst_per_min: int = Field(300, alias="KUMA_MQ_CONNECT_BURST_PER_MIN")

    # 入站闸门（§8.7）：单车每分钟消息上限。
    # 2026-10-22 之前 `rate_check()` 已实现但**没接进 `_route()` 热路径**，等于形同虚设
    # （一台车狂发只受全局 flush 批量与队列背压约束）。车端正常 10s 上报 = 6 条/分钟，
    # 60 是 10 倍余量；突发补报靠 dedup 与 backfill 窗口，不受这个限制影响正常流量。
    rate_limit_per_min: int = Field(60, alias="KUMA_RATE_LIMIT_PER_MIN")

    # 行程切分（M5，口径见 `docs/mileage-spec.md`）：最后一报超过这个间隔就结束行程。
    # 必须**晚于**离线判定（`state_ttl=90s`）——否则离线告警与行程结束会互相抢，
    # 表现为"车刚被判离线，行程又被新到的补报点续上"。
    trip_gap_s: int = Field(180, alias="KUMA_TRIP_GAP_S")

    # 告警（M5）：同类告警抑制窗口 + 默认静默时长。
    # 阈值本身（超速 km/h、急刹降幅）从硬编码常量迁到 `biz.alarm_rule` 表，见迁移 0006。
    alarm_dedupe_window_s: int = Field(600, alias="KUMA_ALARM_DEDUPE_WINDOW_S")
    alarm_silence_default_s: int = Field(3600, alias="KUMA_ALARM_SILENCE_DEFAULT_S")

    # 订阅到期（C6）：到期先进宽限期（只降级：停 WS 推送与下行指令），宽限期过完转 expired。
    # 业务口径由本次决策定：**不删数据、不停遥测入库**——遥测是合规与结算的依据，
    # 订阅买的的是"服务"（实时推送 + 控制），不是"记录权"。
    sub_grace_days: int = Field(7, alias="KUMA_SUB_GRACE_DAYS")

    # 地图
    google_key: str = Field("", alias="KUMA_GOOGLE_KEY")
    amap_key: str = Field("", alias="KUMA_AMAP_KEY")
    maps_cache_ttl: int = Field(3600, alias="KUMA_MAPS_CACHE_TTL")
    maps_daily_quota: int = Field(20_000, alias="KUMA_MAPS_DAILY_QUOTA")

    # 轨迹查询硬约束（512MB PG 下防 OOM，Task-DataBase.md §8）
    trace_max_days: int = Field(7, alias="KUMA_TRACE_MAX_DAYS")

    # 媒体（core.media + 本地磁盘；v1 无对象存储）
    # 默认值是**服务器**路径；本机 dev 必须用 KUMA_MEDIA_ROOT 覆盖到可写目录，
    # 否则上传会因 /data 不存在而失败（fail fast，不静默写到当前目录）。
    media_root: str = Field("/data/kuma_media", alias="KUMA_MEDIA_ROOT")
    media_max_bytes: int = Field(5 * 1024 * 1024, alias="KUMA_MEDIA_MAX_BYTES")

    @property
    def is_prod(self) -> bool:
        return self.kuma_env == "prod"

    @property
    def mqtt_host(self) -> str:
        """aiomqtt 2.x 把第一个参数当**主机名**（不再解析 URL）→ 必须在这里拆好。

        2026-10-20 实测：直接把 `mqtt://127.0.0.1:1883` 传给 `aiomqtt.Client()`
        会被当主机名去做 DNS，报 `[Errno -2] Name or service not known`。
        """
        return urlparse(self.mqtt_url).hostname or "127.0.0.1"

    @property
    def mqtt_port(self) -> int:
        parsed = urlparse(self.mqtt_url)
        return parsed.port or (8883 if parsed.scheme == "mqtts" else 1883)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if s.is_prod and (
        s.jwt_secret.startswith("dev-only") or "CHANGE_ME" in s.dsn or "CHANGE_ME" in s.amqp_url
    ):
        raise RuntimeError("prod 环境仍在使用占位凭据，拒绝启动")
    return s
