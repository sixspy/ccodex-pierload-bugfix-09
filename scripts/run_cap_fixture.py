#!/usr/bin/env python3
"""额载错源修复·本机夹具（第24号·额源修壬）。

无三方依赖环境直跑真实后端服务函数：仅 shim fastapi/pydantic 的
Web 框架管道（路由/入参模型），业务逻辑、SQL、锁、校验全部为仓库真码。

口径（超额禁入，两边不得混用）：
  测甲  毛重超地磅额载 → 拒写，正式票零增
  测乙  超额拒写后，锁定对账不得累计
  争用  双会话同磅抢建票须抢地磅锁，败方只留争用痕
收工  第24序逐页点开：地磅→超重→对账→车道→过磅（五页同源）

退出码：全绿 0；见红 1。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# --- 仅 shim Web 框架管道（本机无 fastapi/pydantic，业务真码不动） ---
SHIM_DIR = Path(tempfile.mkdtemp(prefix="pierload_shim_"))
(SHIM_DIR / "fastapi.py").write_text(
    '''"""最小 fastapi 管道替身：仅供本机夹具导入真实服务模块。"""
class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail

class APIRouter:
    def __init__(self, **kw):
        self.routes = []
    def get(self, path, **kw):
        return lambda f: f
    def post(self, path, **kw):
        return lambda f: f

def Header(alias=None, **kw):
    return None
''',
    encoding="utf-8",
)
(SHIM_DIR / "pydantic.py").write_text(
    '''"""最小 pydantic 替身：属性容器 + model_dump。"""
class BaseModel:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
    def model_dump(self):
        return dict(self.__dict__)

def Field(default=None, **kw):
    return default
''',
    encoding="utf-8",
)
sys.path.insert(0, str(SHIM_DIR))
sys.path.insert(0, str(ROOT / "backend"))

import fastapi  # noqa: E402  shim

import db  # noqa: E402  真实库层

db.DB_PATH = Path(tempfile.mkdtemp(prefix="pierload_db_")) / "t.db"
db.init_db()  # 真实 fixtures 落库

import auth  # noqa: E402
import bridges_svc  # noqa: E402
import lanes_svc  # noqa: E402
import locks  # noqa: E402
import overload_svc  # noqa: E402
import reconcile_svc  # noqa: E402
import tickets_svc  # noqa: E402
from models import TicketIn, AxleWeight  # noqa: E402

RED, GREEN = "【红】", "【绿】"
_failures = 0


def check(ok: bool, label: str, detail: str = "") -> bool:
    global _failures
    mark = GREEN if ok else RED
    if not ok:
        _failures += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  {mark} {label}{suffix}")
    return ok


def login(user: str, password: str) -> str:
    tok = auth.login(auth.LoginIn(username=user, password=password))["token"]
    return f"Bearer {tok}"


def ticket(lane_id: int, bl: str, plate: str, axles: list, code: str) -> "TicketIn":
    return TicketIn(
        lane_id=lane_id,
        bl_ref=bl,
        vehicle_plate=plate,
        axle_weights=[AxleWeight(axle=a, w=w) for a, w in axles],
        ticket_code=code,
    )


def lane_id_of(hdr: str, code: str) -> int:
    items = lanes_svc.list_lanes(hdr)["items"]
    return next(x["id"] for x in items if x["lane_code"] == code)


def main() -> int:
    print("第24号·额源修壬 — 超额载仍建票夹具（本机直跑真实服务）")
    admin = login("admin", "admin123")
    writer = login("writer", "writer123")

    print("\n测甲：毛重超地磅额载 → 拒写，正式票零增")
    lane_in = lane_id_of(admin, "LN-N-01-IN")  # 地磅 WB-N-01 额载 80t
    before = len(tickets_svc.list_tickets(admin)["items"])
    try:
        tickets_svc.create_ticket(
            ticket(lane_in, "BL-2026-0103", "测甲·00024", [(1, 45.0), (2, 40.0)], "WT-CAP-024A"),
            authorization=admin,
        )
        check(False, "85t 超 WB-N-01 额载 80t 仍建正式票", "复现：额载校验错源")
    except fastapi.HTTPException as e:
        check(e.status_code == 422, "超地磅额载拒写(422)", f"status={e.status_code} detail={e.detail}")
        check(isinstance(e.detail, str) and "额载" in e.detail, "拒写说明点名额载", str(e.detail))
    after = len(tickets_svc.list_tickets(admin)["items"])
    check(after == before, "正式票零增", f"{before} → {after}")

    print("\n测乙：超额拒写后，锁定对账不得累计")
    recon_before = reconcile_svc.reconcile_one("BL-2026-0101", authorization=admin)
    check(recon_before["balanced"] is True, "对账基线平衡", f"sum={recon_before['ticket_sum_t']}")
    try:
        tickets_svc.create_ticket(
            ticket(lane_in, "BL-2026-0101", "测乙·00024", [(1, 45.0), (2, 40.0)], "WT-CAP-024B"),
            authorization=admin,
        )
        check(False, "85t 超额仍写入 BL-2026-0101", "复现：对账将被累计")
    except fastapi.HTTPException as e:
        check(e.status_code == 422, "超额拒写(422)", f"status={e.status_code}")
    recon_after = reconcile_svc.reconcile_one("BL-2026-0101", authorization=admin)
    check(
        recon_after["ticket_sum_t"] == recon_before["ticket_sum_t"],
        "对账不累计",
        f"{recon_before['ticket_sum_t']} → {recon_after['ticket_sum_t']}",
    )
    check(recon_after["balanced"] is True, "对账仍平衡")

    print("\n争用：双会话同磅抢建票须抢地磅锁，败方只留争用痕")
    bridge = next(
        b for b in bridges_svc.list_bridges(admin)["items"] if b["bridge_code"] == "WB-N-01"
    )
    lock_key = f"bridge:{bridge['id']}"
    locks.acquire_lock(locks.LockIn(lock_key=lock_key), authorization=admin)  # 甲会话持地磅锁
    lane_out = lane_id_of(writer, "LN-N-01-OUT")  # 同磅另一车道
    before = len(tickets_svc.list_tickets(admin)["items"])
    try:
        tickets_svc.create_ticket(
            ticket(lane_out, "BL-2026-0102", "测争·00024", [(1, 30.0), (2, 30.0)], "WT-CAP-024C"),
            authorization=writer,
        )
        check(False, "乙会话同磅抢建仍落票", "复现：未抢地磅锁")
    except fastapi.HTTPException as e:
        check(e.status_code == 409, "败方 409 争用", f"status={e.status_code} detail={e.detail}")
    cont = locks.list_contention(admin)["items"]
    check(
        any(x["lock_key"] == lock_key and x["actor"] == "writer" for x in cont),
        "败方只留争用痕",
        f"contention={[(x['lock_key'], x['actor']) for x in cont]}",
    )
    after = len(tickets_svc.list_tickets(admin)["items"])
    check(after == before, "败方零落票", f"{before} → {after}")

    print("\n收工·第24序逐页点开（五页同源）")
    pg = bridges_svc.list_bridges(admin)["items"]
    check(all("max_gross_t" in b for b in pg), "地磅页：额载在列", f"{len(pg)} 磅")
    pg = overload_svc.list_overloads(admin)["items"]
    check(
        all("max_gross_t" in h for h in pg),
        "超重页：挂起带地磅额载（同源）",
        f"{len(pg)} 挂起",
    )
    pg = reconcile_svc.reconcile_all(admin)["items"]
    check(len(pg) >= 4, "对账页：提单册全量", f"{len(pg)} 册")
    pg = lanes_svc.list_lanes(admin)["items"]
    check(all("max_gross_t" in l for l in pg), "车道页：车道带地磅额载（同源）", f"{len(pg)} 道")
    pg = tickets_svc.list_tickets(admin)["items"]
    over_cap = []
    for t in pg:
        br = next(
            b for b in bridges_svc.list_bridges(admin)["items"]
            if b["bridge_code"] == t["bridge_code"]
        )
        if float(t["gross_t"]) > float(br["max_gross_t"]):
            over_cap.append(t["ticket_code"])
    check(not over_cap, "过磅页：无超额正式票", f"超额票={over_cap or '无'}")

    print(f"\n{'全绿收工' if _failures == 0 else f'见红 {_failures} 处'}（第24号·额源修壬）")
    return 0 if _failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
