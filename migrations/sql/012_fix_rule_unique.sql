-- 012 修正规则表唯一约束：region_id / type_code 可为 NULL（兜底规则），
--     而 PG 默认 UNIQUE 视 NULL 互不相等 -> 兜底规则可被重复插入（实测 seed 跑两遍就重复）。
--     改用 PG15+ 的 UNIQUE NULLS NOT DISTINCT，并清掉已产生的重复行。
BEGIN;

-- 1) 去重：同 (规则键) 只留 id 最小的一条
DELETE FROM fin.settlement_rule a
 USING fin.settlement_rule b
 WHERE a.id > b.id
   AND a.rule_type = b.rule_type AND a.subject = b.subject
   AND a.region_id IS NOT DISTINCT FROM b.region_id
   AND a.type_code IS NOT DISTINCT FROM b.type_code
   AND a.version = b.version;

DELETE FROM mkt.points_rule a
 USING mkt.points_rule b
 WHERE a.id > b.id
   AND a.rule_code = b.rule_code
   AND a.region_id IS NOT DISTINCT FROM b.region_id
   AND a.type_code IS NOT DISTINCT FROM b.type_code
   AND a.version = b.version;

-- 2) 丢掉自动命名的旧唯一约束（名字由 PG 生成且会被截断，故动态查出来再删）
DO $do$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT conrelid::regclass AS tbl, conname
    FROM pg_constraint
    WHERE contype = 'u'
      AND conrelid IN ('fin.settlement_rule'::regclass, 'mkt.points_rule'::regclass)
      AND conname NOT IN ('uq_settlement_rule','uq_points_rule')
  LOOP
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
  END LOOP;
END
$do$;

-- 3) 新约束：NULL 也参与唯一性（兜底规则只能有一条 active 版本）
ALTER TABLE fin.settlement_rule
  ADD CONSTRAINT uq_settlement_rule UNIQUE NULLS NOT DISTINCT
  (rule_type, subject, region_id, type_code, version);

ALTER TABLE mkt.points_rule
  ADD CONSTRAINT uq_points_rule UNIQUE NULLS NOT DISTINCT (rule_code, region_id, version);

COMMIT;
