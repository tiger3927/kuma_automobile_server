"""MQTT topic 约定（RabbitMQ MQTT 插件把 topic 映射成 routing key）。

ACL（Task-Server.md §8.5）：车端只能 publish `kuma/{self_vin}/+`、
只能 subscribe `kuma/{self_vin}/cmd`；禁止 `#`。
"""

from __future__ import annotations

UP_TELEMETRY = "kuma/+/telemetry"
UP_ALARM = "kuma/+/alarm"
UP_ACK = "kuma/+/cmd_ack"
UP_HEARTBEAT = "kuma/+/heartbeat"
ALL_UP = "kuma/#"


def topic_telemetry(vin: str) -> str:
    return f"kuma/{vin}/telemetry"


def topic_alarm(vin: str) -> str:
    return f"kuma/{vin}/alarm"


def topic_cmd(vin: str) -> str:
    return f"kuma/{vin}/cmd"


def topic_cmd_ack(vin: str) -> str:
    return f"kuma/{vin}/cmd_ack"


def vin_from_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) >= 2 and parts[0] == "kuma":
        return parts[1]
    return None


def topic_matches_vin(topic: str, username_vin: str) -> bool:
    """VIN 防伪：topic 里的 vin 必须与连接用户名一致（§8.6）。"""
    return vin_from_topic(topic) == username_vin
