"""core：配置、DB/Redis/AMQP 基座、鉴权、权限 scope、审计、中间件。

依赖方向（Task-Server.md §7 铁律）：`api → services → repositories → models`；
`core` 可被所有层 import，但 `core` 不 import `services/api`。
"""
