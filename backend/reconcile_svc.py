"""提单册与过磅票对账证明。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from auth import AuthHeader, require_user
from validators import validate_bl_match

router = APIRouter(tags=["reconcile"])


@router.get("/api/reconcile")
def reconcile_all(authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        rows = conn.execute(
            """SELECT bl_code, declared_gross_t FROM bl_manifests ORDER BY bl_code"""
        ).fetchall()
        items = []
        for bl in rows:
            items.append(_reconcile_bl(conn, bl["bl_code"]))
        return {"items": items}


@router.get("/api/reconcile/{bl_code}")
def reconcile_one(bl_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        bl = conn.execute(
            "SELECT * FROM bl_manifests WHERE bl_code=?", (bl_code,)
        ).fetchone()
        if not bl:
            raise HTTPException(404, "提单不存在")
        return _reconcile_bl(conn, bl_code)


def _reconcile_bl(conn, bl_code: str) -> dict:
    bl = conn.execute(
        "SELECT * FROM bl_manifests WHERE bl_code=?", (bl_code,)
    ).fetchone()
    tickets = conn.execute(
        """SELECT t.ticket_code, t.gross_t, t.status, t.generation,
                  b.bridge_code, b.max_gross_t
           FROM weigh_tickets t
           JOIN weigh_lanes l ON l.id=t.lane_id
           JOIN weigh_bridges b ON b.id=l.bridge_id
           WHERE t.bl_ref=? AND t.status NOT IN ('作废')""",
        (bl_code,),
    ).fetchall()
    if not tickets:
        return {
            "bl_code": bl_code,
            "declared_gross_t": bl["declared_gross_t"],
            "ticket_sum_t": 0,
            "balanced": False,
            "reason": "无过磅票",
            "tickets": [],
        }
    ticket_sum = round(sum(float(t["gross_t"]) for t in tickets), 3)
    declared = float(bl["declared_gross_t"])
    matched = validate_bl_match(declared, ticket_sum, tolerance=0.1)
    # 额载同源：与过磅/车道/地磅页同取地磅额载字段，超额票不计入正式对账
    over_cap = [
        t["ticket_code"]
        for t in tickets
        if float(t["gross_t"]) > float(t["max_gross_t"])
    ]
    seal_rows = conn.execute(
        """SELECT s.seal_ref, s.ticket_generation, t.ticket_code
           FROM gate_seals s JOIN weigh_tickets t ON t.id=s.ticket_id
           WHERE t.bl_ref=?""",
        (bl_code,),
    ).fetchall()
    proof_ok = matched and len(seal_rows) >= 1 and not over_cap
    return {
        "bl_code": bl_code,
        "declared_gross_t": declared,
        "ticket_sum_t": ticket_sum,
        "balanced": matched,
        "cap_violations": over_cap,
        "cap_ok": not over_cap,
        "proof_ready": proof_ok,
        "seal_count": len(seal_rows),
        "tickets": [dict(t) for t in tickets],
        "seals": [dict(s) for s in seal_rows],
    }


@router.get("/api/proof/ticket/{ticket_code}")
def proof_ticket(ticket_code: str, authorization: AuthHeader = None):
    require_user(authorization)
    from db import connect

    with connect() as conn:
        ticket = conn.execute(
            "SELECT * FROM weigh_tickets WHERE ticket_code=?", (ticket_code,)
        ).fetchone()
        if not ticket:
            raise HTTPException(404, "票不存在")
        bl = conn.execute(
            "SELECT * FROM bl_manifests WHERE bl_code=?", (ticket["bl_ref"],)
        ).fetchone()
        seal = conn.execute(
            """SELECT * FROM gate_seals WHERE ticket_id=? ORDER BY id DESC LIMIT 1""",
            (ticket["id"],),
        ).fetchone()
        release = conn.execute(
            """SELECT * FROM gate_releases WHERE ticket_id=? AND status='已放行'""",
            (ticket["id"],),
        ).fetchone()
        overload_closed = not conn.execute(
            """SELECT id FROM overload_holds WHERE ticket_id=? AND status='开放'""",
            (ticket["id"],),
        ).fetchone()
        manifest_match = False
        if seal and bl:
            manifest_match = validate_bl_match(
                float(bl["declared_gross_t"]), float(ticket["gross_t"])
            )
        return {
            "ticket_code": ticket_code,
            "bl_ref": ticket["bl_ref"],
            "gross_t": ticket["gross_t"],
            "manifest_matches_bl": manifest_match,
            "has_seal": seal is not None,
            "seal_generation": seal["ticket_generation"] if seal else None,
            "released": release is not None,
            "overload_closed": overload_closed,
            "coherent": bool(
                manifest_match and overload_closed and (seal or release)
            ),
        }