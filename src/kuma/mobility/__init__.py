"""mobility：司机入驻、车队合同、计价、派单、打车与拼车（M10）。

表已在库中（`007_mobility.sql`），`FleetContract` 已被 scope 过滤器使用。
落地时遵守 §10-18/19/20：拼车按乘客各自区间计价；派单必须落 `dispatch_decision`
（候选快照 + 打分 + 结果）；司机同一时刻只归属一个运营商。
"""
