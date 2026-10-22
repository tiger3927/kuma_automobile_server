# 酷马车联网服务

## 功能

酷马新能源汽车（三轮车）的车联网服务，连接汽车，用户，经销商，制造商，运营商，政府等。

## 技术要点

车联网等功能，采用mqtt（RabbitMQ mqtt docker）

用户，经销商，制造商，运营商，政府等，采用restful api和websocket

后端采用python fastapi，异步方式，前端采用nodejs的vue3工程

工作区根目录  main.py 为入口

## tests 目录

测试代码，临时代码，临时文件，临时数据，均放在tests目录！（该目录**不在 git 里**，见下节）

## 版本与同步纪律（git，2026-10-19 定，硬约束）

- **正式代码只走 git**：本机 `git commit` + `git push` → 服务器 `cd /data/kuma_automobile_server && git pull`（仓库公开，服务器无需凭据）。**禁止**再用 pscp 逐个文件覆盖正式代码（`src/`、`main.py` / `gateway.py` / `worker.py`、`migrations/`、`scripts/`、`deploy/` 配置、`pyproject.toml`、`AGENTS.md`、`README.md`）。
- **把任何新文件加入 git、或从 git 删除文件，必须先经我同意**——包括 `git add` 新路径、`git rm` / `git mv`、以及改 `.gitignore` 让文件进入或脱离跟踪。**一次同意只对当次生效**，不许顺手扩大范围；已跟踪文件的普通内容修改不受此限。
- **不在 git 里的东西**（别试图纳入，也别假设 `git clone` 能拿到）：`tests/`、`mock/`、`.codegraph/`、未跟踪的 md 开发笔记（`Task-Plan.md` / `Task-Server.md` / `Task-DataBase.md` / `Task-Web.md` / `History.md` / `Debug.md` / `CodeGraph使用.md` / `PowerShell_Fix.md`）、`codegraph.json`、凭据文件 `deploy/.env` 与 `deploy/runtime.env`。
  其中 **md 开发笔记不必同步到服务器**——服务器只要代码，笔记在本机维护即可（省掉每次改文档都 pscp 一遍）；
  **`tests/` 要服务器能跑测试**，改动后用 pscp 补到 `/data/kuma_automobile_server/tests/`。
- **凭据防泄露**：`.gitignore` 的 `.env` 只匹配文件名正好叫 `.env`，**`deploy/runtime.env` 不在保护范围**，而这个仓库是公开的。服务器已在 `.git/info/exclude` 加了额外排除（只在本机生效、不会被 push）。**提交前先看 `git status`**，一旦出现 md / `.env` / `runtime.env` / `tests/` 路径，停手问我，不要提交。
- 服务器实例已是 git 仓库（`HEAD` 与本机 `origin/main` 一致）。

## 调试与部署

前端采用  agent-browser ，保存cookies，显式浏览器窗口方式调试，便于人工查看和自动化！

后端在 Debug.md 提及的爱商汇服务器 调试和部署为同一个实例！

服务器上的调试目录   /data/kuma_automobile_server

服务器上采用conda，建立kuma环境的方式作为python环境

所需的PostgreSQL数据库/rabbitmq/redis，在服务器docker中

备注：本机python，采用conda下的base环境，用于临时任务和代码编译检查

## 参考文档

### 规划与实现文档（动手前先查对应的那一份）

| 文档 | 谁说了算（职责边界） |
|---|---|
| [Task-Plan.md](Task-Plan.md) | 业务域划分、身份与权限模型（身份×资源×动作×scope）、各状态机、口径定义、页面与权限矩阵、分期 M8–M14；§0 是全套文档的职责边界与冲突裁决表 |
| [Task-Server.md](Task-Server.md) | 后端进程拓扑（api / gateway / worker 三进程同一份代码）、§7 目录与依赖铁律、MQTT 公网接入与安全、下行控制、里程算法实现、异步规范、容量与内存预算、端口规划、conda `kuma`、里程碑 M0–M7 |
| [Task-DataBase.md](Task-DataBase.md) | PostgreSQL 16 + PostGIS 3.5 镜像与扩展、容器运行、角色与最小权限、schema 与分区、备份恢复、连接池、§11 全量表设计（DDL 级）；`migrations/sql/*.sql` 以它为准 |
| [Task-Web.md](Task-Web.md) | 前端实现计划：`web/` 三工程（`portal` 官网 / `guest` 用户端 / `admin` 管理端含 `/gov`），Vue3 + Vite + TS，**本机构建**（服务器无 node）产物落各 `dist/` 由 FastAPI 作静态资源对外访问；含技术栈定稿、挂载与缓存、前后端契约、分期 W0–W7 与验收 |
| [Debug.md](Debug.md) | 调试与部署服务器（爱商汇）地址、账号与凭据、宝塔面板信息,高德地图 key 及凭据、Google 地图 key（**目前是虚拟占位串，未申请**）；**凭据只落这一份**，其它文档不写密码。涉及地图 key / 服务器 / 数据库口令**先读这一份** |

冲突裁决顺序（引自 `Task-Plan.md` §0）：容量与性能约束（`Task-Server.md` §3）> 安全约束（`Task-Server.md` §8/§9）> 口径定义（`Task-Plan.md` §8.6）> 功能优先级（`Task-Plan.md` §12）。与容量或安全约束冲突的功能让路，不改约束。

### 工具文档

执行本机命令前，先看 PowerShell_Fix.md：智能体在 Windows/PowerShell 下调用命令的转义与编码解决方案（首选 `-EncodedCommand` + UTF-16LE，备选临时 `.ps1` + `-File`；含中文乱码、BOM、`--%` 等踩坑点）。命令出现引号丢失、参数错乱、中文乱码时按该文档排查，不要反复手动加转义。

分析代码前，先看 CodeGraph使用.md：CodeGraph（codegraph v1.6.0）的索引机制与 `codegraph_explore`（MCP）/CLI 用法。理解调用关系、机制、改动前评估影响面优先用 codegraph，精确文本搜索用 Grep，已知文件读段用 Read。本项目索引已建立（2026-10-19）：目前只索引到 `deploy/docker-compose.yml`（1 文件 / 0 符号）——`src/kuma/` 各包目录已按 `Task-Server.md` §7 建好但还没有 `.py`，`migrations/sql/*.sql` 不产符号。代码落地后由 daemon 增量同步，无需手动 sync；核对新鲜度用 `codegraph status --json`（CLI 文案不可信）。重建索引按该文档第 8 节 runbook 执行。



