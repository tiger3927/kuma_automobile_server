"""kuma 车联网服务后端。

三进程同一份代码（Task-Server.md §2）：
- `main.py`    api（HTTP + WS）
- `gateway.py` MQTT 网关（单实例）
- `worker.py`  定时任务 / outbox / 分区维护
"""

__version__ = "0.1.0"
