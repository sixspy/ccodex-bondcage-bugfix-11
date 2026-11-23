"""空桩禁额（甲路径）与笼锁争用的三条验收红测。

红测口径（先红后改，不得口头代替）：
- 甲：点增补空桩不得改笼总额 / 可用额，也不得改账户冻结 / 余额。
- 乙：空桩若把冻结同步抬高，不得凭虚高可用额继续走解笼拨付。
- 丙：同笼两名柜员同时点空桩完成，须抢笼锁；后到只记争用痕，不落地改额。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app import app
from db import init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _auth(c: TestClient, user: str = "admin") -> dict:
    tok = c.post(
        "/api/login", json={"username": user, "password": "123456"}
    ).json()["token"]
    return {"Authorization": "Bearer " + tok}


def _cage(c: TestClient, h: dict, code: str = "CG-P-SOUTH-001") -> dict:
    rows = c.get("/api/cages", headers=h).json()["items"]
    return [x for x in rows if x["cage_code"] == code][0]


def _account(c: TestClient, h: dict, party_key: str) -> dict:
    return c.get(f"/api/bonds/{party_key}", headers=h).json()["account"]


def test_jia_stub_must_not_change_cage_total():
    """甲：业务失败测盯空桩改笼总额。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _cage(c, h)
    before = dict(cage)
    before_acct = _account(c, h, cage["party_key"])

    r = c.post(
        "/api/topup-stub",
        headers=h,
        json={"cage_id": cage["id"], "amount": 5000, "mode": "同笼扩代"},
    )
    assert r.status_code == 200

    after = c.get(f"/api/cages/{cage['id']}", headers=h).json()["cage"]
    after_acct = _account(c, h, cage["party_key"])

    # 笼面正式零改
    assert after["amount"] == before["amount"], "空桩不得改笼总额"
    assert after["available"] == before["available"], "空桩不得改笼可用额"
    # 账户正式零改
    assert after_acct["frozen"] == before_acct["frozen"], "空桩不得抬高冻结"
    assert after_acct["balance"] == before_acct["balance"], "空桩不得改账户余额"

    jobs = c.get("/api/topup/jobs", headers=h).json()["items"]
    mine = [j for j in jobs if j["cage_id"] == cage["id"]]
    assert len(mine) == 1
    assert mine[0]["status"] == "空桩登记"


def test_yi_inflated_frozen_must_not_enable_release():
    """乙：业务失败测盯抬冻后仍能走解笼。"""
    c = TestClient(app)
    h = _auth(c)
    cage = _cage(c, h)
    original_amount = float(cage["amount"])
    before_acct = _account(c, h, cage["party_key"])

    assert (
        c.post(
            "/api/topup-stub",
            headers=h,
            json={"cage_id": cage["id"], "amount": 5000, "mode": "同笼扩代"},
        ).status_code
        == 200
    )

    assert (
        c.post(
            "/api/attestations",
            headers=h,
            json={"cage_id": cage["id"], "outcome": "通过", "evidence": "验收合格"},
        ).status_code
        == 200
    )
    rel = c.post("/api/releases", headers=h, json={"cage_id": cage["id"]})
    assert rel.status_code == 200
    # 业务失败点：只能按原始真实入笼额出金，抬冻/抬额留下的虚高部分不得解出
    assert rel.json()["amount"] == original_amount, "抬冻后仍按虚高额解笼"

    # 出金后互证：不得出现额/冻漂移，也不得假绿
    proof = c.get(f"/api/proof/{cage['party_key']}", headers=h).json()
    assert proof["frozen_drift"] == 0, "空桩后冻结基线漂移"
    assert proof["integrity_ok"] is True, "空桩后出金互证假绿"

    after_acct = _account(c, h, cage["party_key"])
    assert after_acct["balance"] == before_acct["balance"] - original_amount
    assert after_acct["frozen"] == 0

    recon = c.get(f"/api/reconcile/{cage['cage_code']}", headers=h).json()
    assert recon["balanced"] is True
    assert recon["amount_drift"] == 0
    assert recon["integrity_ok"] is True


def test_bing_two_clerks_same_cage_contention():
    """丙：争用失败测盯双人同点空桩争用。

    先到柜员持有该笼锁且业务尚未收尾时，后到柜员同点空桩完成须抢锁失败：
    只记争用痕，不落地作业、不改额、不改冻。
    """
    c_admin = TestClient(app)
    c_clerk = TestClient(app)
    h_admin = _auth(c_admin, "admin")
    h_clerk = _auth(c_clerk, "clerk")

    cage = _cage(c_clerk, h_clerk)
    party = cage["party_key"]
    lock_key = f"cage:{cage['id']}"
    before_amount = float(cage["amount"])
    before_avail = float(cage["available"])
    before_acct = _account(c_clerk, h_clerk, party)

    # 先到柜员已抢到该笼锁，业务仍在进行（锁未释放）
    assert (
        c_clerk.post(
            "/api/locks/acquire", headers=h_clerk, json={"lock_key": lock_key}
        ).status_code
        == 200
    )

    # 后到柜员同点空桩完成
    late = c_admin.post(
        "/api/topup-stub",
        headers=h_admin,
        json={"cage_id": cage["id"], "amount": 5000, "mode": "同笼扩代"},
    )
    assert late.status_code == 409, "后到争用未拒绝"

    # 后到只记痕：争用痕落在后到柜员名下，且未落地作业
    cont = c_clerk.get("/api/contention", headers=h_clerk).json()["items"]
    assert any(
        x["lock_key"] == lock_key and x["actor"] == "admin" for x in cont
    ), "后到争用未留痕"
    jobs = [
        j
        for j in c_clerk.get("/api/topup/jobs", headers=h_clerk).json()["items"]
        if j["cage_id"] == cage["id"]
    ]
    assert jobs == [], "争用后仍落地了作业"

    after = c_clerk.get(f"/api/cages/{cage['id']}", headers=h_clerk).json()["cage"]
    assert float(after["amount"]) == before_amount
    assert float(after["available"]) == before_avail
    after_acct = _account(c_clerk, h_clerk, party)
    assert after_acct["frozen"] == before_acct["frozen"]
    assert after_acct["balance"] == before_acct["balance"]

    # 先到柜员收尾释放锁后，空桩只插作业，笼面与账户仍零改
    c_clerk.post("/api/locks/release", headers=h_clerk, json={"lock_key": lock_key})
    ok = c_clerk.post(
        "/api/topup-stub",
        headers=h_clerk,
        json={"cage_id": cage["id"], "amount": 5000, "mode": "同笼扩代"},
    )
    assert ok.status_code == 200
    jobs = [
        j
        for j in c_clerk.get("/api/topup/jobs", headers=h_clerk).json()["items"]
        if j["cage_id"] == cage["id"]
    ]
    assert len(jobs) == 1
    final = c_clerk.get(f"/api/cages/{cage['id']}", headers=h_clerk).json()["cage"]
    assert float(final["amount"]) == before_amount
    final_acct = _account(c_clerk, h_clerk, party)
    assert final_acct["frozen"] == before_acct["frozen"]


def test_bing_simultaneous_double_submit_no_corruption():
    """丙补充：双会话双请求同时提交空桩完成，不得双落地、不得改额改冻。"""
    c_admin = TestClient(app)
    c_clerk = TestClient(app)
    h_admin = _auth(c_admin, "admin")
    h_clerk = _auth(c_clerk, "clerk")

    cage = _cage(c_clerk, h_clerk)
    party = cage["party_key"]
    before_amount = float(cage["amount"])
    before_avail = float(cage["available"])
    before_acct = _account(c_clerk, h_clerk, party)

    lock_key = f"cage:{cage['id']}"
    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def submit(name: str, client: TestClient, headers: dict) -> None:
        barrier.wait()
        results[name] = client.post(
            "/api/topup-stub",
            headers=headers,
            json={"cage_id": cage["id"], "amount": 5000, "mode": "同笼扩代"},
        )
        # 若抢到锁，立即收尾释放，模拟柜员点完成后放锁
        client.post("/api/locks/release", headers=headers, json={"lock_key": lock_key})

    t1 = threading.Thread(target=submit, args=("admin", c_admin, h_admin))
    t2 = threading.Thread(target=submit, args=("clerk", c_clerk, h_clerk))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive()

    codes = sorted(r.status_code for r in results.values())
    assert codes[0] in (200, 409)
    assert codes[1] in (200, 409)

    # 不变量：落地作业数 == 成功次数；笼额与冻额零漂移
    jobs = [
        j
        for j in c_clerk.get("/api/topup/jobs", headers=h_clerk).json()["items"]
        if j["cage_id"] == cage["id"]
    ]
    assert len(jobs) == codes.count(200), "作业落地数与成功次数不符"
    after = c_clerk.get(f"/api/cages/{cage['id']}", headers=h_clerk).json()["cage"]
    assert float(after["amount"]) == before_amount
    assert float(after["available"]) == before_avail
    after_acct = _account(c_clerk, h_clerk, party)
    assert after_acct["frozen"] == before_acct["frozen"]
    assert after_acct["balance"] == before_acct["balance"]
