"""额载错源修复（第24号·额源修壬）：超额禁入三红。

口径：毛重超地磅额载(max_gross_t) → 拒写，正式票零增；对账不得累计；
双会话同磅抢建票须抢地磅锁，败方只留争用痕。两边不得混用车道阈值。
"""
import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user="admin", password="admin123") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": password}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _lane(c: TestClient, h: dict, code: str) -> dict:
    lanes = c.get("/api/lanes", headers=h).json()["items"]
    return next(x for x in lanes if x["lane_code"] == code)


def test_over_cap_gross_rejected_formal_zero_increment():
    """测甲：毛重超地磅额载 → 422 拒写，正式票零增。"""
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")  # 所属地磅 WB-N-01 额载 80t
    before = c.get("/api/tickets", headers=h).json()["items"]
    resp = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0103",
            "vehicle_plate": "测甲·00024",
            "axle_weights": [{"axle": 1, "w": 45.0}, {"axle": 2, "w": 40.0}],  # 85t > 额载80t
            "ticket_code": "WT-CAP-024A",
        },
    )
    assert resp.status_code == 422
    assert "额载" in resp.json()["detail"]
    after = c.get("/api/tickets", headers=h).json()["items"]
    assert len(after) == len(before)  # 正式零增
    assert all(t["ticket_code"] != "WT-CAP-024A" for t in after)


def test_over_cap_attempt_reconcile_not_accumulated():
    """测乙：超额拒写后，锁定对账不得累计。"""
    c = TestClient(app)
    h = _auth(c)
    lane = _lane(c, h, "LN-N-01-IN")
    before = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    assert before["balanced"] is True
    resp = c.post(
        "/api/tickets",
        headers=h,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0101",
            "vehicle_plate": "测乙·00024",
            "axle_weights": [{"axle": 1, "w": 45.0}, {"axle": 2, "w": 40.0}],  # 85t > 额载80t
            "ticket_code": "WT-CAP-024B",
        },
    )
    assert resp.status_code == 422
    after = c.get("/api/reconcile/BL-2026-0101", headers=h).json()
    assert after["ticket_sum_t"] == before["ticket_sum_t"]  # 对账不累计
    assert after["balanced"] is True


def test_same_bridge_race_loser_only_contention_trace():
    """争用：双会话同磅抢建票须抢地磅锁，败方只留争用痕。"""
    c = TestClient(app)
    h1 = _auth(c, "admin", "admin123")
    h2 = _auth(c, "writer", "writer123")
    bridges = c.get("/api/bridges", headers=h1).json()["items"]
    bridge = next(b for b in bridges if b["bridge_code"] == "WB-N-01")
    lock_key = f"bridge:{bridge['id']}"
    assert (
        c.post("/api/locks/acquire", headers=h1, json={"lock_key": lock_key}).status_code
        == 200
    )
    lane = _lane(c, h2, "LN-N-01-OUT")  # 同磅 WB-N-01 另一车道
    before = c.get("/api/tickets", headers=h1).json()["items"]
    resp = c.post(
        "/api/tickets",
        headers=h2,
        json={
            "lane_id": lane["id"],
            "bl_ref": "BL-2026-0102",
            "vehicle_plate": "测争·00024",
            "axle_weights": [{"axle": 1, "w": 30.0}, {"axle": 2, "w": 30.0}],  # 60t 超载未超额
            "ticket_code": "WT-CAP-024C",
        },
    )
    assert resp.status_code == 409  # 败方
    cont = c.get("/api/contention", headers=h1).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "writer" for x in cont
    ), "败方须留争用痕"
    after = c.get("/api/tickets", headers=h1).json()["items"]
    assert len(after) == len(before)  # 只留争用痕，不落票
    assert all(t["ticket_code"] != "WT-CAP-024C" for t in after)
