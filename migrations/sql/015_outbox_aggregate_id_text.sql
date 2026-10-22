-- 015 outbox.aggregate_id: bigint -> text
-- 原因（2026-10-22，C1 补事件生产方时撞到）：不是所有聚合根都有整数主键——
-- 下行指令的主键是 `biz.command.cmd_id`（text）。用假整数或 0 占位会让
-- `idx_outbox_agg` 与"按聚合根回放事件"的用法失去意义，所以把列放宽成 text
-- （现有数值行自动转字符串，索引随 ALTER 重建，语义不变）。
BEGIN;

ALTER TABLE audit.outbox ALTER COLUMN aggregate_id TYPE text;

COMMENT ON COLUMN audit.outbox.aggregate_id IS
  '聚合根标识：整数主键存十进制字符串；有自然键的（command=cmd_id）存自然键';

COMMIT;
