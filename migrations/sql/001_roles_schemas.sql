-- 001 角色 / 扩展 / schema（依 Task-DataBase.md §3 §4 §5.1）
-- 幂等：角色存在则只改密码；schema/扩展用 IF NOT EXISTS
-- 执行：bash apply.sh（密码从 deploy/.env 读，用 psql 变量传入，不落本文件）
\set ON_ERROR_STOP on

SELECT count(*) > 0 AS app_exists FROM pg_roles WHERE rolname = 'kuma_app' \gset
\if :app_exists
\echo 'kuma_app 已存在 -> 仅重置密码'
ALTER ROLE kuma_app WITH PASSWORD :'apppw';
\else
-- 注：Task-DataBase.md §3 规划稿里的 VALID CONNECTION 在 PG16 不存在（PG17 才有 VALID FOR ...），
--     口令轮换靠运维改 .env + bash deploy/fix_creds.sh，不靠 role 有效期子句。
CREATE ROLE kuma_app WITH
  LOGIN PASSWORD :'apppw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  CONNECTION LIMIT 20;
\endif

SELECT count(*) > 0 AS ro_exists FROM pg_roles WHERE rolname = 'kuma_ro' \gset
\if :ro_exists
\echo 'kuma_ro 已存在 -> 仅重置密码'
ALTER ROLE kuma_ro WITH PASSWORD :'ropw';
\else
CREATE ROLE kuma_ro WITH
  LOGIN PASSWORD :'ropw'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
  CONNECTION LIMIT 10;
\endif

GRANT CONNECT ON DATABASE kuma TO kuma_app, kuma_ro;

-- 扩展：§4 三个 + citext（§11.1 的 account.username / email 用 citext 做大小写不敏感唯一）
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS citext;

-- schema：车联网 5 + 商业 5（本次一次性建全量，M1 起由 Alembic 接管增量）
CREATE SCHEMA IF NOT EXISTS core;
CREATE SCHEMA IF NOT EXISTS biz;
CREATE SCHEMA IF NOT EXISTS tele;
CREATE SCHEMA IF NOT EXISTS geo;
CREATE SCHEMA IF NOT EXISTS rep;
CREATE SCHEMA IF NOT EXISTS commerce;
CREATE SCHEMA IF NOT EXISTS fin;
CREATE SCHEMA IF NOT EXISTS mkt;
CREATE SCHEMA IF NOT EXISTS mobility;
CREATE SCHEMA IF NOT EXISTS audit;

-- 角色级会话参数（§5.1 search_path、§8 超时）
-- 注意：search_path 是**不加引号**的逗号列表；写成 'a, b' 会被当成一个标识符，
-- 结果 public 不在路径里，PostGIS 函数全部解析不到（迁移 0004 修的就是这个）。
ALTER ROLE kuma_app IN DATABASE kuma SET search_path = core, biz, tele, geo, rep, commerce, fin, mkt, mobility, audit, public;
ALTER ROLE kuma_app IN DATABASE kuma SET statement_timeout = '30s';
ALTER ROLE kuma_ro  IN DATABASE kuma SET search_path = rep, public;
ALTER ROLE kuma_ro  IN DATABASE kuma SET statement_timeout = '300s';
ALTER ROLE kuma_ro  IN DATABASE kuma SET default_transaction_read_only = on;
