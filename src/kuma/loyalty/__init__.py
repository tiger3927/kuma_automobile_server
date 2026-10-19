"""loyalty：积分账户/批次/流水、优惠券、活动（M10）。

表已在库中（`006_mkt.sql`）。落地时遵守 §7.1 铁律 2：
`purchased` 与 `earned` 两类余额分开计量、分开过期与抵扣顺序；
`mkt.points_ledger` 是 append-only，任何调整都走冲正（§10-17）。
"""
