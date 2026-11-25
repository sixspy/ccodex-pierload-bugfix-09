"""过磅票：轴重采集与裁定入闸。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user, require_writer
from db import connect, now_iso, write_audit
from locks import release, try_acquire
from models import TicketIn
from validators import (
    adjudicate_overload,
    validate_axle_count,
    validate_axle_weights,
    validate_gross_cap,
    validate_lane_open,
)

router = APIRouter(tags=["tickets"])


def _next_ticket_code(conn, lane_id: int) -> str:
    lane = conn.execute(
        "SELECT lane_code FROM weigh_lanes WHERE id=?", (lane_id,)
    ).fetchone()
    prefix = lane["lane_code"] if lane else f"LN-{lane_id}"
    n = conn.execute(
        "SELECT COUNT(*) c FROM weigh_tickets WHERE lane_id=?", (lane_id,)
    ).fetchone()["c"]
    return f"WT-{prefix}-{n + 1:03d}"


@router.get("/api/tickets")
def list_tickets(authorization: AuthHeader = None, status: str | None = None):
    require_user(authorization)
    with connect() as conn:
        q = """SELECT t.*, l.lane_code, l.overload_threshold_t,
                      b.bridge_code, b.max_gross_t
               FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id"""
        if status:
            rows = conn.execute(
                q + " WHERE t.status=? ORDER BY t.id DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(q + " ORDER BY t.id DESC").fetchall()
        items = []
        for r in rows:
            d = dict(r)
            d["axle_weights"] = json.loads(d["axle_weights"] or "[]")
            items.append(d)
        return {"items": items}


@router.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, authorization: AuthHeader = None):
    require_user(authorization)
    with connect() as conn:
        row = conn.execute(
            """SELECT t.*, l.lane_code, l.overload_threshold_t, b.name bridge_name,
                      b.bridge_code, b.max_gross_t
               FROM weigh_tickets t
               JOIN weigh_lanes l ON l.id=t.lane_id
               JOIN weigh_bridges b ON b.id=l.bridge_id
               WHERE t.id=?""",
            (ticket_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "过磅票不存在")
        data = dict(row)
        data["axle_weights"] = json.loads(data["axle_weights"] or "[]")
        holds = conn.execute(
            "SELECT * FROM overload_holds WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        seals = conn.execute(
            "SELECT * FROM gate_seals WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        releases = conn.execute(
            "SELECT * FROM gate_releases WHERE ticket_id=? ORDER BY id DESC",
            (ticket_id,),
        ).fetchall()
        return {
            "ticket": data,
            "overload_holds": [dict(h) for h in holds],
            "gate_seals": [dict(s) for s in seals],
            "gate_releases": [dict(r) for r in releases],
        }


@router.post("/api/tickets")
def create_ticket(body: TicketIn, authorization: AuthHeader = None):
    user = require_writer(authorization)
    with connect() as conn:
        lane = conn.execute(
            """SELECT l.*, b.bridge_code, b.max_gross_t, b.axle_limit
               FROM weigh_lanes l JOIN weigh_bridges b ON b.id=l.bridge_id
               WHERE l.id=?""",
            (body.lane_id,),
        ).fetchone()
        if not lane:
            raise HTTPException(404, "车道不存在")
        # 同磅抢建票抢地磅锁（非同车道锁）：败方只留争用痕，不落票
        lock_key = f"bridge:{lane['bridge_id']}"
        if not try_acquire(conn, lock_key, user):
            # 争用痕须先落库，异常回滚不得吞掉
            conn.commit()
            raise HTTPException(409, "争用：地磅已被占用，只记争用，不写业务")
        try:
            validate_lane_open(dict(lane))
            axles = [a.model_dump() for a in body.axle_weights]
            validate_axle_count(axles, int(lane["axle_limit"]))
            gross, _peak, _spread = validate_axle_weights(axles)
            # 额载取自地磅额载字段；车道阈值只用于超限旁路，两边不得混用
            try:
                validate_gross_cap(gross, float(lane["max_gross_t"]))
            except HTTPException as exc:
                # 超额拒写须留痕：偏离说明点名「额载修复」，当班封死
                write_audit(
                    conn,
                    user,
                    "ticket_cap_reject",
                    body.ticket_code or f"lane:{body.lane_id}",
                    {
                        "gross_t": gross,
                        "max_gross_t": float(lane["max_gross_t"]),
                        "bridge_code": lane["bridge_code"],
                        "reason": exc.detail,
                        "rule": "额载修复·超额禁入",
                    },
                )
                conn.commit()
                raise
            bl = conn.execute(
                "SELECT * FROM bl_manifests WHERE bl_code=?", (body.bl_ref,)
            ).fetchone()
            if not bl:
                raise HTTPException(422, "提单册未登记")
            is_over, overweight = adjudicate_overload(
                gross, float(lane["overload_threshold_t"])
            )
            code = body.ticket_code or _next_ticket_code(conn, body.lane_id)
            if conn.execute(
                "SELECT id FROM weigh_tickets WHERE ticket_code=?", (code,)
            ).fetchone():
                raise HTTPException(409, "票号已存在")
            now = now_iso()
            # 超额禁入：超地磅额载的读数不得晋升正式票（已由 validate_gross_cap 拦截）；
            # 超车道阈值但未超额者只走影子超载票旁路，此处落票即为正式零改的唯一写点。
            if is_over:
                status = "旁路挂起"
            else:
                status = "已裁定"
            cur = conn.execute(
                """INSERT INTO weigh_tickets(ticket_code,lane_id,bl_ref,vehicle_plate,
                   axle_weights,gross_t,generation,status,adjudicated_at,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    code,
                    body.lane_id,
                    body.bl_ref,
                    body.vehicle_plate,
                    json.dumps(axles, ensure_ascii=False),
                    gross,
                    1,
                    status,
                    now if status == "已裁定" else None,
                    now,
                ),
            )
            ticket_id = cur.lastrowid
            if is_over:
                hold_key = f"HOLD-{code}"
                conn.execute(
                    """INSERT INTO overload_holds(ticket_id,hold_key,overweight_t,threshold_t,
                       status,opened_at,side_lane_note) VALUES (?,?,?,?,?,?,?)""",
                    (
                        ticket_id,
                        hold_key,
                        overweight,
                        float(lane["overload_threshold_t"]),
                        "开放",
                        now,
                        lane["side_lane_ref"] or "旁路待驳",
                    ),
                )
            write_audit(
                conn,
                user,
                "ticket_weigh",
                code,
                {
                    "ticket_id": ticket_id,
                    "gross_t": gross,
                    "status": status,
                    "overload": is_over,
                },
            )
            conn.commit()
            return {
                "id": ticket_id,
                "ticket_code": code,
                "gross_t": gross,
                "status": status,
                "overload": is_over,
            }
        finally:
            # 释放须落库：拒写路径上已提交的业务痕不得被回滚连带吞掉锁
            release(conn, lock_key, user)
            conn.commit()