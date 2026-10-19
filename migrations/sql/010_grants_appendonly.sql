-- 010 授权与 append-only 保护（§3 + §5.1 同步要求 + §11.0 第 5 条）
BEGIN;

-- 1) kuma_app：业务 schema 全 DML（不含 rep 写、不含 DDL）
GRANT USAGE ON SCHEMA core, biz, tele, geo, commerce, fin, mkt, mobility, audit, rep TO kuma_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA core, biz, tele, geo, commerce, fin, mkt, mobility TO kuma_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA audit TO kuma_app;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA core, biz, tele, geo, commerce, fin, mkt, mobility, audit TO kuma_app;

-- 未来新建表自动授权（§5.1：新增 schema 必须同步，这里已含全部 10 个 schema）
ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA core, biz, tele, geo, commerce, fin, mkt, mobility
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kuma_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA audit
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO kuma_app;
ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA core, biz, tele, geo, commerce, fin, mkt, mobility, audit
  GRANT USAGE, SELECT ON SEQUENCES TO kuma_app;

-- 2) append-only 表：对 kuma_app 只留 SELECT/INSERT（§11.0 第 5 条，迁移内执行不靠手工）
REVOKE UPDATE, DELETE ON mkt.points_ledger        FROM kuma_app;
REVOKE UPDATE, DELETE ON fin.fin_journal          FROM kuma_app;
REVOKE UPDATE, DELETE ON fin.subsidy_ledger       FROM kuma_app;
REVOKE UPDATE, DELETE ON biz.command_audit        FROM kuma_app;
REVOKE UPDATE, DELETE ON commerce.used_car_report FROM kuma_app;
REVOKE UPDATE, DELETE ON mobility.ride_event      FROM kuma_app;
REVOKE UPDATE, DELETE ON audit.audit_log          FROM kuma_app;

-- 3) kuma_ro：只见 rep（报表/政府/看板，与业务写物理隔离）
GRANT USAGE ON SCHEMA rep TO kuma_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA rep TO kuma_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE kuma_owner IN SCHEMA rep GRANT SELECT ON TABLES TO kuma_ro;

COMMIT;
