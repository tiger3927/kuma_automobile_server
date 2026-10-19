"""commerce：商城、买车链、二手车、售后（M8/M11/M14）。

表已在库中（`004_commerce.sql`）。本包在 M8 落地，届时遵守 §10 一致性规则：
- 下单只锁 `vehicle_listing` 行（不锁 `core.vehicle`）；
- 写订单 + `audit.outbox` 同事务；
- 收款走 `fin`，佣金走 `fin.settlement_item`（抵扣前口径）。
"""
