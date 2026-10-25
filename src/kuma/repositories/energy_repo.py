"""换电域读写（M15）。

**为什么这个 repo 大量用裸 SQL**（照 `fence_repo.py` 的做法）：
1. 就近查站要 PostGIS（`ST_DWithin` / `ST_Distance`），ORM 里写不出来且不引入 geoalchemy2；
2. 换电必须**锁住两块电池**再改状态——`SELECT ... FOR UPDATE SKIP LOCKED` 是并发下
   不出现"一电池两车"的第一道防线（第二道是 `uq_binding_battery_active`）；
3. `station_stock` 用 `INSERT ... ON CONFLICT DO UPDATE` 原子增减，避免"读-改-写"竞态。

**这里不做业务判断**：型号白名单、协议是否 active、押金是否到位、拒单原因码——
全部在 `services/swap_service.py`（`Task-Server.md` §7 依赖铁律）。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MODEL_COLS = """
    model_code, name, vendor, spec_doc_media_id,
    kwh_nominal, kwh_accept_min, voltage_nominal_v, voltage_max_v, voltage_cutoff_v,
    capacity_ah, chemistry, cell_format, cycle_life_rated, charge_max_c, discharge_max_c,
    temp_charge_min_c, temp_charge_max_c,
    dims_l_mm, dims_w_mm, dims_h_mm, weight_kg, needs_handling_aid,
    mounting, mounting_drawing_media_id, ip_rating,
    connector_type, connector_keying, bms_proto, bms_sn_field, telemetry_readable,
    compatible_types, cert_marks, hazard_class, recall_batch_support,
    status, version, approved_by, approved_at, created_at, updated_at
"""

BATTERY_COLS = """
    id, battery_sn, model_code, owner_org_id, batch_no, kwh_actual, accepted_at,
    accept_media_id, cycles, soc, station_id, status, quality_complaints,
    created_at, updated_at
"""

STATION_COLS = """
    id, code, org_id, name, address, region_id, business_hours, allowed_models,
    bay_count, needs_handling_aid, status, created_at, updated_at,
    ST_AsText(geom) AS geom_wkt
"""

SWAP_COLS = """
    id, swap_no, station_id, operator_org_id, lease_id, account_id, vin, model_code,
    out_battery_sn, in_battery_sn, price_rule_id, pricing, amount, cash_amount,
    points_offset, currency, collect_mode, pay_order_id, idempotency_key,
    status, reject_reason, re_swap_of, created_at, completed_at
"""

LEASE_COLS = """
    id, lease_no, holder_type, holder_account_id, holder_org_id, operator_org_id,
    model_code, deposit_amount_snapshot, currency, deposit_waived, terms_version,
    terms_accepted_at, status, joined_at, ended_at, created_at, updated_at
"""

DEPOSIT_COLS = """
    id, deposit_no, lease_id, payer_type, payer_id, operator_org_id, amount, currency,
    status, collected_at, collected_voucher_no, proof_media_id, refunded_at,
    refund_voucher_no, forfeit_reason, forfeit_swap_order_id, created_at, updated_at
"""


# "参数没传"与"传了 None"必须能区分：station_id=None 是"电池离站"（置 NULL），
# 不传才是"位置不动"。用 None 当默认值会把两者混成一个，导致换电后电池位置丢失。
_UNSET = object()


#: 本 repo 里出现过的 jsonb 列。**裸 `text()` 绑定不带列类型信息**，asyncpg 收到
#: list/dict 会直接 `DataError: 'list' object has no attribute 'encode'`（实测），
#: 写 `CAST(:x AS jsonb)` 也救不了——CAST 在服务端，编码在客户端就炸。
#: 读侧不用管：SQLAlchemy 的 asyncpg 方言已把 jsonb 结果解码成 list/dict。
JSONB_COLS = frozenset({"compatible_types", "cert_marks", "business_hours", "allowed_models",
                        "pricing", "payload"})


def _j(value: Any) -> Any:
    """jsonb 入参序列化（None 原样传，交给列默认值）。"""
    return None if value is None else json.dumps(value, ensure_ascii=False)


def _jsonb(params: dict[str, Any]) -> dict[str, Any]:
    return {k: (_j(v) if k in JSONB_COLS else v) for k, v in params.items()}


def _params(**kw: Any) -> dict[str, Any]:
    return _jsonb(kw)


class EnergyRepo:
    # ══ 型号字典 ═══════════════════════════════════════════════════════════
    async def get_model(self, session: AsyncSession, model_code: str) -> dict[str, Any] | None:
        row = (await session.execute(text(
            f"SELECT {MODEL_COLS} FROM energy.battery_model WHERE model_code = :c"
        ), {"c": model_code})).mappings().one_or_none()
        return dict(row) if row else None

    async def list_models(self, session: AsyncSession, *, status: str | None = None,
                          limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        sql = f"SELECT {MODEL_COLS} FROM energy.battery_model WHERE true"
        p: dict[str, Any] = {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        if status:
            sql += " AND status = :status"
            p["status"] = status
        sql += " ORDER BY model_code LIMIT :limit OFFSET :offset"
        return [dict(r) for r in (await session.execute(text(sql), p)).mappings().all()]

    async def insert_model(self, session: AsyncSession, *, m: dict[str, Any]) -> dict[str, Any]:
        m = _jsonb(m)                       # compatible_types / cert_marks 是 jsonb
        cols = ", ".join(m.keys())
        binds = ", ".join(f":{k}" for k in m)
        await session.execute(text(
            f"INSERT INTO energy.battery_model ({cols}) VALUES ({binds})"  # 列名来自服务层白名单
        ), m)
        await session.flush()
        row = await self.get_model(session, m["model_code"])
        assert row is not None
        return row

    async def patch_model(self, session: AsyncSession, model_code: str,
                          **fields: Any) -> dict[str, Any] | None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)      # 列名来自服务层白名单，非用户输入
        if sets:
            await session.execute(text(
                f"UPDATE energy.battery_model SET {sets}, updated_at = now() "
                "WHERE model_code = :c"), _params(c=model_code, **fields))
            await session.flush()
        return await self.get_model(session, model_code)

    async def set_model_status(self, session: AsyncSession, model_code: str, status: str,
                               approved_by: int | None) -> dict[str, Any] | None:
        await session.execute(text("""
            UPDATE energy.battery_model
               SET status = :s, approved_by = :by, approved_at = now(), updated_at = now()
             WHERE model_code = :c
        """), {"s": status, "by": approved_by, "c": model_code})
        await session.flush()
        return await self.get_model(session, model_code)

    # ══ 换电站 ═════════════════════════════════════════════════════════════
    async def get_station(self, session: AsyncSession, *, station_id: int | None = None,
                          code: str | None = None) -> dict[str, Any] | None:
        if station_id is None and code is None:
            raise ValueError("get_station 需要 station_id 或 code")
        sql = f"SELECT {STATION_COLS} FROM energy.station WHERE "
        p: dict[str, Any] = {}
        if station_id is not None:
            sql += "id = :id"
            p["id"] = station_id
        else:
            sql += "code = :code"
            p["code"] = code
        row = (await session.execute(text(sql), p)).mappings().one_or_none()
        return dict(row) if row else None

    async def list_stations(self, session: AsyncSession, *, org_ids: list[int] | None,
                            status: str | None = None,
                            limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        sql = f"SELECT {STATION_COLS} FROM energy.station WHERE true"
        p: dict[str, Any] = {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        if org_ids is not None:
            sql += " AND org_id = ANY(:orgs)"
            p["orgs"] = list(org_ids)
        if status:
            sql += " AND status = :status"
            p["status"] = status
        sql += " ORDER BY id DESC LIMIT :limit OFFSET :offset"
        return [dict(r) for r in (await session.execute(text(sql), p)).mappings().all()]

    async def insert_station(self, session: AsyncSession, *, code: str, name: str, org_id: int,
                             lng: float, lat: float, address: str | None, region_id: str | None,
                             business_hours: dict, allowed_models: list[str],
                             bay_count: int | None, needs_handling_aid: bool) -> dict[str, Any]:
        await session.execute(text("""
            INSERT INTO energy.station (code, name, org_id, geom, address, region_id,
                                        business_hours, allowed_models, bay_count,
                                        needs_handling_aid)
            VALUES (:code, :name, :org_id,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326),
                    :address, :region_id,
                    CAST(:business_hours AS jsonb), CAST(:allowed_models AS jsonb),
                    :bay_count, :needs_handling_aid)
        """), {"code": code, "name": name, "org_id": org_id, "lng": lng, "lat": lat,
               "address": address, "region_id": region_id,
               "business_hours": _j(business_hours), "allowed_models": _j(allowed_models),
               "bay_count": bay_count, "needs_handling_aid": needs_handling_aid})
        await session.flush()
        row = await self.get_station(session, code=code)
        assert row is not None
        return row

    async def patch_station(self, session: AsyncSession, station_id: int,
                            **fields: Any) -> dict[str, Any] | None:
        sets = ", ".join(f"{k} = :{k}" for k in fields)      # 列名来自服务层白名单
        if sets:
            await session.execute(text(
                f"UPDATE energy.station SET {sets}, updated_at = now() WHERE id = :id"),
                _params(id=station_id, **fields))
            await session.flush()
        return await self.get_station(session, station_id=station_id)

    async def set_allowed_models(self, session: AsyncSession, station_id: int,
                                 models: list[str]) -> dict[str, Any] | None:
        await session.execute(text("""
            UPDATE energy.station
               SET allowed_models = CAST(:m AS jsonb), updated_at = now()
             WHERE id = :id
        """), {"m": _j(models), "id": station_id})
        await session.flush()
        return await self.get_station(session, station_id=station_id)

    async def nearby(self, session: AsyncSession, *, lng: float, lat: float, radius_km: float,
                     model_code: str | None, limit: int) -> list[dict[str, Any]]:
        """就近换电站 + 该型号可用电池数（只读 `station_stock`，不猜数，§10-24）。"""
        p = {"lng": lng, "lat": lat, "r": radius_km * 1000.0,
             "limit": max(1, min(limit, 100)), "mc": model_code}
        sql = """
            SELECT s.id AS station_id, s.code, s.name, s.org_id, s.address,
                   s.business_hours, s.status,
                   ST_AsText(s.geom) AS geom_wkt,
                   -- 必须 ::numeric：ST_Distance(geography) 返回 double precision，
                   -- PG 没有 round(double precision, int)（实测报 function round(...) does not exist）
                   round((ST_Distance(s.geom::geography,
                         ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography) / 1000.0)::numeric, 2
                   ) AS distance_km,
                   s.allowed_models,
                   COALESCE(jsonb_agg(jsonb_build_object(
                       'model_code', ss.model_code, 'ready', ss.ready_count,
                       'charging', ss.charging_count, 'faulty', ss.faulty_count,
                       'updated_at', ss.updated_at) ORDER BY ss.model_code)
                       FILTER (WHERE ss.id IS NOT NULL), '[]'::jsonb) AS stock
            FROM energy.station s
            LEFT JOIN energy.station_stock ss ON ss.station_id = s.id
            WHERE s.status = 'active'
              AND ST_DWithin(s.geom::geography,
                    ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography, :r)
        """
        if model_code:
            sql += " AND s.allowed_models @> CAST(:mc_arr AS jsonb)"
            p["mc_arr"] = f'["{model_code}"]'
        sql += """
            GROUP BY s.id
            ORDER BY ST_Distance(s.geom::geography,
                       ST_SetSRID(ST_MakePoint(:lng, :lat), 4326)::geography)
            LIMIT :limit
        """
        return [dict(r) for r in (await session.execute(text(sql), p)).mappings().all()]

    # ══ 电池台账与库存 ═════════════════════════════════════════════════════
    async def get_battery(self, session: AsyncSession, battery_sn: str) -> dict[str, Any] | None:
        row = (await session.execute(text(
            f"SELECT {BATTERY_COLS} FROM energy.battery WHERE battery_sn = :sn"
        ), {"sn": battery_sn})).mappings().one_or_none()
        return dict(row) if row else None

    async def list_batteries(self, session: AsyncSession, *, owner_org_ids: list[int] | None,
                             station_id: int | None = None, status: str | None = None,
                             model_code: str | None = None, q: str | None = None,
                             limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
        """电池台账翻页 + 总数（W6 admin 台账页与 SOC 巡检清单要按状态/站点/型号过滤）。

        `owner_org_ids=None` 表示不收敛（平台 / 监管），否则按 **`owner_org_id`** 过滤——
        与 `station_service._assert_battery_visible` 同一口径：电池按**归属运营商**可见，
        不是按所在站点。别家运营商把电池寄放在我的站，那块电池不是我台账里的资产
        （押金赔偿、库存计数都按归属算）。三个过滤列各有索引（`idx_battery_owner` /
        `idx_battery_station` / `idx_battery_model`）。

        `count(*) OVER()` 而不是第二条 count：台账正被巡检与换电并发改状态，
        分开两条 SQL 会数出"页内容里没有的那一块"（§11-5 要的就是"共 N 块"能对上）。
        """
        sql = f"SELECT {BATTERY_COLS}, count(*) OVER() AS total FROM energy.battery WHERE true"
        p: dict[str, Any] = {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        if owner_org_ids is not None:
            sql += " AND owner_org_id = ANY(:orgs)"
            p["orgs"] = list(owner_org_ids)
        if station_id is not None:
            sql += " AND station_id = :sid"
            p["sid"] = station_id
        if status:
            sql += " AND status = :status"
            p["status"] = status
        if model_code:
            sql += " AND model_code = :mc"
            p["mc"] = model_code
        if q:
            sql += " AND battery_sn ILIKE :q"
            p["q"] = f"%{q}%"
        # 按 SN 升序：台账的自然键，翻页稳定（`created_at` 会因批量入池并列而漏行/重行）
        sql += " ORDER BY battery_sn LIMIT :limit OFFSET :offset"
        items: list[dict[str, Any]] = []
        total = 0
        for r in (await session.execute(text(sql), p)).mappings().all():
            d = dict(r)
            total = int(d.pop("total"))
            items.append(d)
        return items, total

    async def lock_batteries(self, session: AsyncSession, sns: list[str]) -> list[dict[str, Any]]:
        """**换电并发防线**：按 SN 升序锁行（固定顺序避免死锁），已被别人锁住 → 返回空。

        `FOR UPDATE OF b SKIP LOCKED`：并发的第二笔换电不会排队等待（那会让站点作业台
        卡住），而是拿不到锁 → 服务层按"电池正被另一笔换电占用"拒单。
        """
        rows = (await session.execute(text("""
            SELECT id, battery_sn, model_code, owner_org_id, kwh_actual, soc, station_id,
                   status, cycles, quality_complaints
              FROM energy.battery
             WHERE battery_sn = ANY(:sns)
             ORDER BY battery_sn
               FOR UPDATE OF battery SKIP LOCKED
        """), {"sns": sorted(sns)})).mappings().all()
        return [dict(r) for r in rows]

    async def insert_battery(self, session: AsyncSession, *, battery_sn: str, model_code: str,
                             owner_org_id: int, batch_no: str | None, kwh_actual: float | None,
                             accept_media_id: int | None, station_id: int | None,
                             status: str) -> dict[str, Any]:
        await session.execute(text("""
            INSERT INTO energy.battery (battery_sn, model_code, owner_org_id, batch_no,
                                        kwh_actual, accepted_at, accept_media_id,
                                        station_id, status)
            VALUES (:battery_sn, :model_code, :owner_org_id, :batch_no, :kwh_actual,
                    now(), :accept_media_id, :station_id, :status)
        """), {"battery_sn": battery_sn, "model_code": model_code,
               "owner_org_id": owner_org_id, "batch_no": batch_no, "kwh_actual": kwh_actual,
               "accept_media_id": accept_media_id, "station_id": station_id, "status": status})
        await session.flush()
        row = await self.get_battery(session, battery_sn)
        assert row is not None
        return row

    async def set_battery_state(self, session: AsyncSession, battery_sn: str, *, status: str,
                                station_id: Any = _UNSET, soc: Any = None,
                                bump_cycles: bool = False,
                                bump_complaints: bool = False) -> dict[str, Any] | None:
        """改状态与位置。`station_id` 未传则不动；传 None 表示"离站"（置 NULL）。"""
        sets = ["status = :status", "updated_at = now()"]
        p: dict[str, Any] = {"sn": battery_sn, "status": status}
        if station_id is not _UNSET:
            sets.append("station_id = :station_id")
            p["station_id"] = station_id
        if soc is not None:
            sets.append("soc = :soc")
            p["soc"] = soc
        if bump_cycles:
            sets.append("cycles = cycles + 1")
        if bump_complaints:
            sets.append("quality_complaints = quality_complaints + 1")
        await session.execute(text(
            f"UPDATE energy.battery SET {', '.join(sets)} WHERE battery_sn = :sn"), p)
        await session.flush()
        return await self.get_battery(session, battery_sn)

    async def append_flow(self, session: AsyncSession, *, battery_sn: str, from_status: str | None,
                          to_status: str, station_id: int | None, vin: str | None,
                          swap_order_id: int | None, actor_account_id: int, actor_role: str | None,
                          note: str | None, media_id: int | None) -> None:
        await session.execute(text("""
            INSERT INTO energy.battery_flow (battery_sn, from_status, to_status, station_id,
                                             vin, swap_order_id, actor_account_id, actor_role,
                                             note, media_id)
            VALUES (:battery_sn, :from_status, :to_status, :station_id, :vin,
                    :swap_order_id, :actor_account_id, :actor_role, :note, :media_id)
        """), {"battery_sn": battery_sn, "from_status": from_status, "to_status": to_status,
               "station_id": station_id, "vin": vin, "swap_order_id": swap_order_id,
               "actor_account_id": actor_account_id, "actor_role": actor_role,
               "note": note, "media_id": media_id})
        await session.flush()

    async def flows(self, session: AsyncSession, battery_sn: str, *, limit: int = 100
                    ) -> list[dict[str, Any]]:
        rows = (await session.execute(text("""
            SELECT id, battery_sn, from_status, to_status, station_id, vin, swap_order_id,
                   actor_account_id, actor_role, note, media_id, created_at
              FROM energy.battery_flow
             WHERE battery_sn = :sn
             ORDER BY created_at DESC, id DESC
             LIMIT :limit
        """), {"sn": battery_sn, "limit": max(1, min(limit, 500))})).mappings().all()
        return [dict(r) for r in rows]

    async def stock(self, session: AsyncSession, station_id: int) -> list[dict[str, Any]]:
        rows = (await session.execute(text("""
            SELECT station_id, model_code, ready_count, charging_count, faulty_count, updated_at
              FROM energy.station_stock WHERE station_id = :id ORDER BY model_code
        """), {"id": station_id})).mappings().all()
        return [dict(r) for r in rows]

    async def stock_adjust(self, session: AsyncSession, *, station_id: int, model_code: str,
                           ready_delta: int, charging_delta: int, faulty_delta: int) -> None:
        """原子增减库存（与电池行同事务）。

        **不能写成 `INSERT … VALUES(:rd,:cd,:fd) ON CONFLICT DO UPDATE`**（实测踩坑）：
        PG 先按 INSERT 分支做 CHECK 再检测冲突，所以减库存（rd=-1）在行已存在时也会
        先拿 -1 去过 `ready_count >= 0` → 直接 check_violation，ON CONFLICT 根本没机会生效。
        正确写法：先 upsert 一行全 0（永不违反 CHECK），再 UPDATE 叠加增量。

        `CHECK (ready_count >= 0)` 仍是最后一道防线：某条路径少写一次增减时，
        UPDATE 会抛 check_violation，而不是把库存悄悄改成负数。
        """
        p = {"station_id": station_id, "model_code": model_code,
             "rd": ready_delta, "cd": charging_delta, "fd": faulty_delta}
        await session.execute(text("""
            INSERT INTO energy.station_stock
                   (station_id, model_code, ready_count, charging_count, faulty_count, updated_at)
            VALUES (:station_id, :model_code, 0, 0, 0, now())
            ON CONFLICT (station_id, model_code) DO NOTHING
        """), p)
        await session.execute(text("""
            UPDATE energy.station_stock
               SET ready_count    = ready_count    + :rd,
                   charging_count = charging_count + :cd,
                   faulty_count   = faulty_count   + :fd,
                   updated_at     = now()
             WHERE station_id = :station_id AND model_code = :model_code
        """), p)
        await session.flush()

    async def stock_recompute(self, session: AsyncSession, station_id: int) -> list[dict[str, Any]]:
        """从 `battery` 重算库存（对账 job 用；不一致只告警，不自动改，§11.11）。"""
        rows = (await session.execute(text("""
            SELECT model_code,
                   COUNT(*) FILTER (WHERE status = 'ready')    AS ready,
                   COUNT(*) FILTER (WHERE status = 'charging') AS charging,
                   COUNT(*) FILTER (WHERE status = 'faulty')   AS faulty
              FROM energy.battery
             WHERE station_id = :id
             GROUP BY model_code ORDER BY model_code
        """), {"id": station_id})).mappings().all()
        return [dict(r) for r in rows]

    # ══ 车上电池绑定 ═══════════════════════════════════════════════════════
    async def active_binding(self, session: AsyncSession, *, vin: str | None = None,
                             battery_sn: str | None = None) -> dict[str, Any] | None:
        if not vin and not battery_sn:
            raise ValueError("active_binding 需要 vin 或 battery_sn")
        sql = ("SELECT id, vin, battery_sn, station_id, swap_order_id, reason, "
               "effective_from, ended_at FROM core.vehicle_battery_binding "
               "WHERE ended_at IS NULL AND ")
        p: dict[str, Any] = {}
        if vin:
            sql += "vin = :vin"
            p["vin"] = vin
        else:
            sql += "battery_sn = :sn"
            p["sn"] = battery_sn
        row = (await session.execute(text(sql), p)).mappings().one_or_none()
        return dict(row) if row else None

    async def end_binding(self, session: AsyncSession, binding_id: int) -> None:
        await session.execute(text(
            "UPDATE core.vehicle_battery_binding SET ended_at = now() WHERE id = :id "
            "AND ended_at IS NULL"), {"id": binding_id})
        await session.flush()

    async def insert_binding(self, session: AsyncSession, *, vin: str, battery_sn: str,
                             station_id: int | None, swap_order_id: int | None,
                             reason: str) -> int:
        return (await session.execute(text("""
            INSERT INTO core.vehicle_battery_binding
                   (vin, battery_sn, station_id, swap_order_id, reason)
            VALUES (:vin, :battery_sn, :station_id, :swap_order_id, :reason)
            RETURNING id
        """), {"vin": vin, "battery_sn": battery_sn, "station_id": station_id,
               "swap_order_id": swap_order_id, "reason": reason})).scalar_one()

    async def leased_batteries_of_holder(self, session: AsyncSession, *, holder_type: str,
                                         holder_id: int, operator_org_id: int,
                                         ) -> list[dict[str, Any]]:
        """持有者名下车辆当前装着的、属于该运营商池的电池（退协议前必须先处理）。"""
        owner_col = "v.owner_account_id" if holder_type == "account" else "v.owner_org_id"
        rows = (await session.execute(text(f"""
            SELECT vb.vin, vb.battery_sn, b.model_code, b.status, b.station_id
              FROM core.vehicle_battery_binding vb
              JOIN energy.battery b ON b.battery_sn = vb.battery_sn
              JOIN core.vehicle v     ON v.vin = vb.vin
             WHERE vb.ended_at IS NULL
               AND b.owner_org_id = :op
               AND {owner_col} = :id          -- 列名来自 holder_type 白名单，不是用户输入
        """), {"op": operator_org_id, "id": holder_id})).mappings().all()
        return [dict(r) for r in rows]

    # ══ 协议、押金、规则 ═══════════════════════════════════════════════════
    async def get_lease(self, session: AsyncSession, lease_id: int) -> dict[str, Any] | None:
        row = (await session.execute(text(
            f"SELECT {LEASE_COLS} FROM energy.battery_lease WHERE id = :id"), {"id": lease_id}
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def find_lease(self, session: AsyncSession, *, account_id: int | None = None,
                         org_id: int | None = None, model_code: str | None = None,
                         operator_org_id: int | None = None,
                         statuses: tuple[str, ...] = ("active", "overdue"),
                         ) -> dict[str, Any] | None:
        sql = f"SELECT {LEASE_COLS} FROM energy.battery_lease WHERE status = ANY(:st)"
        p: dict[str, Any] = {"st": list(statuses)}
        if account_id is not None:
            sql += " AND holder_account_id = :acc"
            p["acc"] = account_id
        if org_id is not None:
            sql += " AND holder_org_id = :org"
            p["org"] = org_id
        if model_code:
            sql += " AND model_code = :mc"
            p["mc"] = model_code
        if operator_org_id is not None:
            sql += " AND operator_org_id = :op"
            p["op"] = operator_org_id
        row = (await session.execute(text(sql + " ORDER BY id DESC LIMIT 1"), p)
               ).mappings().one_or_none()
        return dict(row) if row else None

    async def insert_lease(self, session: AsyncSession, *, lease_no: str, holder_type: str,
                           holder_account_id: int | None, holder_org_id: int | None,
                           operator_org_id: int, model_code: str, deposit_amount: int,
                           currency: str, deposit_waived: bool, terms_version: str) -> dict[str, Any]:
        await session.execute(text("""
            INSERT INTO energy.battery_lease (lease_no, holder_type, holder_account_id,
                holder_org_id, operator_org_id, model_code, deposit_amount_snapshot, currency,
                deposit_waived, terms_version, terms_accepted_at)
            VALUES (:lease_no, :holder_type, :holder_account_id, :holder_org_id,
                    :operator_org_id, :model_code, :deposit_amount, :currency,
                    :deposit_waived, :terms_version, now())
        """), {"lease_no": lease_no, "holder_type": holder_type,
               "holder_account_id": holder_account_id, "holder_org_id": holder_org_id,
               "operator_org_id": operator_org_id, "model_code": model_code,
               "deposit_amount": deposit_amount, "currency": currency,
               "deposit_waived": deposit_waived, "terms_version": terms_version})
        await session.flush()
        row = (await session.execute(text(
            "SELECT id FROM energy.battery_lease WHERE lease_no = :n"), {"n": lease_no}
        )).scalar_one()
        got = await self.get_lease(session, row)
        assert got is not None
        return got

    async def set_lease_status(self, session: AsyncSession, lease_id: int, status: str,
                               *, joined: bool = False, ended: bool = False) -> dict[str, Any] | None:
        sets = ["status = :s", "updated_at = now()"]
        p: dict[str, Any] = {"id": lease_id, "s": status}
        if joined:
            sets.append("joined_at = now()")
        if ended:
            sets.append("ended_at = now()")
        await session.execute(text(
            f"UPDATE energy.battery_lease SET {', '.join(sets)} WHERE id = :id"), p)
        await session.flush()
        return await self.get_lease(session, lease_id)

    async def deposit_of_lease(self, session: AsyncSession, lease_id: int, *,
                               open_only: bool = True) -> dict[str, Any] | None:
        sql = f"SELECT {DEPOSIT_COLS} FROM energy.deposit_ledger WHERE lease_id = :l"
        if open_only:
            sql += " AND status IN ('open','held')"
        row = (await session.execute(text(sql + " ORDER BY id DESC LIMIT 1"), {"l": lease_id})
               ).mappings().one_or_none()
        return dict(row) if row else None

    async def insert_deposit(self, session: AsyncSession, *, deposit_no: str, lease_id: int,
                             payer_type: str, payer_id: int, operator_org_id: int, amount: int,
                             currency: str, status: str, collected_voucher_no: str | None,
                             proof_media_id: int | None) -> dict[str, Any]:
        await session.execute(text("""
            INSERT INTO energy.deposit_ledger (deposit_no, lease_id, payer_type, payer_id,
                operator_org_id, amount, currency, status, collected_at,
                collected_voucher_no, proof_media_id)
            VALUES (:deposit_no, :lease_id, :payer_type, :payer_id, :operator_org_id,
                    :amount, :currency, :status,
                    CASE WHEN :status = 'held' THEN now() ELSE NULL END,
                    :collected_voucher_no, :proof_media_id)
        """), {"deposit_no": deposit_no, "lease_id": lease_id, "payer_type": payer_type,
               "payer_id": payer_id, "operator_org_id": operator_org_id, "amount": amount,
               "currency": currency, "status": status,
               "collected_voucher_no": collected_voucher_no, "proof_media_id": proof_media_id})
        await session.flush()
        row = (await session.execute(text(
            "SELECT id FROM energy.deposit_ledger WHERE deposit_no = :n"), {"n": deposit_no}
        )).scalar_one()
        got = (await session.execute(text(
            f"SELECT {DEPOSIT_COLS} FROM energy.deposit_ledger WHERE id = :id"), {"id": row}
        )).mappings().one()
        return dict(got)

    async def set_deposit_status(self, session: AsyncSession, deposit_id: int, status: str,
                                 *, voucher_no: str | None = None,
                                 forfeit_reason: str | None = None,
                                 forfeit_swap_order_id: int | None = None) -> dict[str, Any] | None:
        sets = ["status = :s", "updated_at = now()"]
        p: dict[str, Any] = {"id": deposit_id, "s": status}
        if status == "held":
            sets.append("collected_at = now()")
        if status == "refunded":
            sets.append("refunded_at = now()")
            if voucher_no:
                sets.append("refund_voucher_no = :rv")
                p["rv"] = voucher_no
        if forfeit_reason:
            sets.append("forfeit_reason = :fr")
            p["fr"] = forfeit_reason
        if forfeit_swap_order_id is not None:
            sets.append("forfeit_swap_order_id = :fs")
            p["fs"] = forfeit_swap_order_id
        await session.execute(text(
            f"UPDATE energy.deposit_ledger SET {', '.join(sets)} WHERE id = :id"), p)
        await session.flush()
        row = (await session.execute(text(
            f"SELECT {DEPOSIT_COLS} FROM energy.deposit_ledger WHERE id = :id"), {"id": deposit_id}
        )).mappings().one_or_none()
        return dict(row) if row else None

    async def org_region(self, session: AsyncSession, org_id: int) -> str | None:
        """org 所在国家（押金与价目按地区分档的默认口径）。"""
        return (await session.execute(text(
            "SELECT region_id FROM core.org WHERE id = :id"), {"id": org_id})).scalar()

    async def effective_price(self, session: AsyncSession, *, model_code: str,
                              region_id: str | None, station_org_id: int,
                              ) -> dict[str, Any] | None:
        """取当前生效单价：站级优先 → 区域兜底 → 全区兜底（NULL 列即兜底行）。"""
        rows = (await session.execute(text("""
            SELECT id, region_id, model_code, station_org_id, price, currency, min_soc_pct,
                   version, effective_from
              FROM energy.swap_price
             WHERE model_code = :mc AND status = 'active'
               AND effective_from <= current_date
               AND (effective_to IS NULL OR effective_to >= current_date)
               AND (region_id IS NULL OR region_id = :region)
               AND (station_org_id IS NULL OR station_org_id = :org)
             ORDER BY (station_org_id IS NULL), (region_id IS NULL), version DESC
             LIMIT 1
        """), {"mc": model_code, "region": region_id, "org": station_org_id})
               ).mappings().all()
        return dict(rows[0]) if rows else None

    async def effective_deposit_rule(self, session: AsyncSession, *, model_code: str,
                                     region_id: str | None) -> dict[str, Any] | None:
        rows = (await session.execute(text("""
            SELECT id, region_id, model_code, amount, currency, waiver_min_kyc, version
              FROM energy.deposit_rule
             WHERE model_code = :mc AND status = 'active'
               AND effective_from <= current_date
               AND (effective_to IS NULL OR effective_to >= current_date)
               AND (region_id IS NULL OR region_id = :region)
             ORDER BY (region_id IS NULL), version DESC
             LIMIT 1
        """), {"mc": model_code, "region": region_id})).mappings().all()
        return dict(rows[0]) if rows else None

    async def insert_price(self, session: AsyncSession, *, model_code: str, region_id: str | None,
                           station_org_id: int | None, price: int, currency: str,
                           min_soc_pct: int, version: int, effective_from, effective_to,
                           proposed_by: int) -> dict[str, Any]:
        row = (await session.execute(text("""
            INSERT INTO energy.swap_price (model_code, region_id, station_org_id, price, currency,
                min_soc_pct, version, effective_from, effective_to, status, proposed_by)
            VALUES (:model_code, :region_id, :station_org_id, :price, :currency,
                    :min_soc_pct, :version, :effective_from, :effective_to,
                    'pending_review', :proposed_by)
            RETURNING id, model_code, region_id, station_org_id, price, currency,
                      min_soc_pct, version, effective_from, effective_to, status
        """), {"model_code": model_code, "region_id": region_id,
               "station_org_id": station_org_id, "price": price, "currency": currency,
               "min_soc_pct": min_soc_pct, "version": version,
               "effective_from": effective_from, "effective_to": effective_to,
               "proposed_by": proposed_by})).mappings().one()
        await session.flush()
        return dict(row)

    async def insert_rule(self, session: AsyncSession, *, model_code: str, region_id: str | None,
                          amount: int, currency: str, waiver_min_kyc: int, version: int,
                          effective_from, effective_to, proposed_by: int) -> dict[str, Any]:
        row = (await session.execute(text("""
            INSERT INTO energy.deposit_rule (model_code, region_id, amount, currency,
                waiver_min_kyc, version, effective_from, effective_to, status, proposed_by)
            VALUES (:model_code, :region_id, :amount, :currency, :waiver_min_kyc,
                    :version, :effective_from, :effective_to, 'pending_review', :proposed_by)
            RETURNING id, model_code, region_id, amount, currency, waiver_min_kyc, version,
                      effective_from, effective_to, status
        """), {"model_code": model_code, "region_id": region_id, "amount": amount,
               "currency": currency, "waiver_min_kyc": waiver_min_kyc, "version": version,
               "effective_from": effective_from, "effective_to": effective_to,
               "proposed_by": proposed_by})).mappings().one()
        await session.flush()
        return dict(row)

    async def approve_rule(self, session: AsyncSession, *, table: str, rule_id: int,
                           approve: bool, approver: int) -> dict[str, Any] | None:
        """价目与押金规则共用：申报 → 平台审核 → active（改规则不追溯历史单，§11.0-6）。"""
        if table not in ("swap_price", "deposit_rule"):
            raise ValueError(f"未知规则表 {table}")
        row = (await session.execute(text(
            f"UPDATE energy.{table} SET status = :s, approved_by = :by "
            "WHERE id = :id RETURNING id, status"
        ), {"s": "active" if approve else "draft", "by": approver, "id": rule_id})
        ).mappings().one_or_none()
        await session.flush()
        return dict(row) if row else None

    # ══ 换电单 ═════════════════════════════════════════════════════════════
    async def find_swap(self, session: AsyncSession, *, swap_id: int | None = None,
                        swap_no: str | None = None,
                        idempotency_key: str | None = None) -> dict[str, Any] | None:
        p: dict[str, Any]
        if swap_id is not None:
            sql, p = f"SELECT {SWAP_COLS} FROM energy.swap_order WHERE id = :id", {"id": swap_id}
        elif swap_no:
            sql, p = f"SELECT {SWAP_COLS} FROM energy.swap_order WHERE swap_no = :n", {"n": swap_no}
        elif idempotency_key:
            sql, p = (f"SELECT {SWAP_COLS} FROM energy.swap_order WHERE idempotency_key = :k",
                      {"k": idempotency_key})
        else:
            raise ValueError("find_swap 需要 swap_id / swap_no / idempotency_key")
        row = (await session.execute(text(sql), p)).mappings().one_or_none()
        return dict(row) if row else None

    async def insert_swap(self, session: AsyncSession, *, swap: dict[str, Any]) -> dict[str, Any]:
        swap = _jsonb(swap)                 # pricing 是 jsonb 计价快照
        cols = ", ".join(swap.keys())
        binds = ", ".join(f":{k}" for k in swap)
        row = (await session.execute(text(
            f"INSERT INTO energy.swap_order ({cols}) VALUES ({binds}) RETURNING id"
        ), swap)).scalar_one()
        await session.flush()
        got = await self.find_swap(session, swap_id=row)
        assert got is not None
        return got

    async def set_swap_status(self, session: AsyncSession, swap_id: int, status: str,
                              *, reject_reason: str | None = None,
                              completed: bool = False) -> dict[str, Any] | None:
        sets = ["status = :s"]
        p: dict[str, Any] = {"id": swap_id, "s": status}
        if reject_reason:
            sets.append("reject_reason = :r")
            p["r"] = reject_reason
        if completed:
            sets.append("completed_at = now()")
        await session.execute(text(
            f"UPDATE energy.swap_order SET {', '.join(sets)} WHERE id = :id"), p)
        await session.flush()
        return await self.find_swap(session, swap_id=swap_id)

    async def apply_offset(self, session: AsyncSession, swap_id: int, *, points_offset: int,
                           cash_amount: int, pricing: dict[str, Any]) -> dict[str, Any] | None:
        """把积分抵扣写回换电单（`amount` 不变，只改 `cash_amount` 与 `points_offset`）。

        **`amount` 必须保持抵扣前金额**——运营商按 `amount` 收款（§10-14），
        `cash_amount` 是用户实际要付的差额，两者之差已由 `fin.subsidy_ledger` 记账。
        `pricing` 里存规则快照（id/version/汇率/拆分明细），改规则不追溯历史单（§11.0-6）。
        """
        await session.execute(text(
            "UPDATE energy.swap_order SET points_offset = :po, cash_amount = :ca, "
            "pricing = :pricing WHERE id = :id"
        ), {"po": points_offset, "ca": cash_amount, "pricing": _j(pricing), "id": swap_id})
        await session.flush()
        return await self.find_swap(session, swap_id=swap_id)

    async def list_swaps(self, session: AsyncSession, *, scope_subquery: Any,
                         status: str | None, station_id: int | None,
                         limit: int, offset: int) -> tuple[list[dict[str, Any]], int]:
        """换电流水 + 总数（`Task-Web.md` §11-5：换电流水页要显示"共 N 条"）。

        `count(*) OVER()` 与行同一快照：换电单在被并发改状态（operator_done →
        account_done），第二条 count 查询会数到"页内容里没有的那一单"。
        """
        sql = (f"SELECT {SWAP_COLS}, count(*) OVER() AS total FROM energy.swap_order "
               f"WHERE id IN ({scope_subquery})")
        p: dict[str, Any] = {"limit": max(1, min(limit, 200)), "offset": max(0, offset)}
        if status:
            sql += " AND status = :status"
            p["status"] = status
        if station_id is not None:
            sql += " AND station_id = :sid"
            p["sid"] = station_id
        sql += " ORDER BY created_at DESC, id DESC LIMIT :limit OFFSET :offset"
        items: list[dict[str, Any]] = []
        total = 0
        for r in (await session.execute(text(sql), p)).mappings().all():
            d = dict(r)
            total = int(d.pop("total"))
            items.append(d)
        return items, total

    async def append_swap_event(self, session: AsyncSession, *, swap_order_id: int, event: str,
                                payload: dict[str, Any], actor_account_id: int | None) -> None:
        await session.execute(text("""
            INSERT INTO energy.swap_event (swap_order_id, event, payload, actor_account_id)
            VALUES (:swap_order_id, :event, CAST(:payload AS jsonb), :actor_account_id)
        """), {"swap_order_id": swap_order_id, "event": event, "payload": _j(payload),
               "actor_account_id": actor_account_id})
        await session.flush()

    async def swap_events(self, session: AsyncSession, swap_order_id: int
                          ) -> list[dict[str, Any]]:
        rows = (await session.execute(text("""
            SELECT id, swap_order_id, event, payload, actor_account_id, created_at
              FROM energy.swap_event WHERE swap_order_id = :id ORDER BY id
        """), {"id": swap_order_id})).mappings().all()
        return [dict(r) for r in rows]

    async def rejection_stats(self, session: AsyncSession, *, station_id: int | None,
                              since: datetime) -> list[dict[str, Any]]:
        """拒单原因码统计（P29 的可观测性：白名单配错要看得见）。"""
        sql = ("SELECT reject_reason, COUNT(*) AS n FROM energy.swap_order "
               "WHERE status = 'rejected' AND reject_reason IS NOT NULL AND created_at >= :since")
        p: dict[str, Any] = {"since": since}
        if station_id is not None:
            sql += " AND station_id = :sid"
            p["sid"] = station_id
        sql += " GROUP BY reject_reason ORDER BY n DESC"
        return [dict(r) for r in (await session.execute(text(sql), p)).mappings().all()]

    # ══ 押金三问（负债报表，不进收入）════════════════════════════════════
    async def deposit_position(self, session: AsyncSession,
                               operator_org_ids: list[int] | None) -> list[dict[str, Any]]:
        sql = ("SELECT operator_org_id, currency, COUNT(*) AS deposit_rows, "
               "COALESCE(SUM(amount) FILTER (WHERE collected_at IS NOT NULL), 0) AS collected, "
               "COALESCE(SUM(amount) FILTER (WHERE status = 'refunded'), 0)   AS refunded, "
               "COALESCE(SUM(amount) FILTER (WHERE status IN ('open','held')), 0) AS held_now, "
               "COALESCE(SUM(amount) FILTER (WHERE status = 'forfeited'), 0)  AS forfeited "
               "FROM energy.deposit_ledger WHERE true")
        p: dict[str, Any] = {}
        if operator_org_ids is not None:
            sql += " AND operator_org_id = ANY(:orgs)"
            p["orgs"] = list(operator_org_ids)
        sql += " GROUP BY operator_org_id, currency ORDER BY operator_org_id"
        return [dict(r) for r in (await session.execute(text(sql), p)).mappings().all()]
