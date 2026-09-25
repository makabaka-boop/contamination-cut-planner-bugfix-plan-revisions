"""真实 PostgreSQL 上的并发首次采用验收测试。

场景：同一方案的两个不同成功计算被**并发首次采用**。期望：
- 结果确定：恰有一次采用成功（200），另一次得到 409 ADOPTION_CONFLICT；
- 不再出现 500，也不会把方案行竞争误报为 COMPUTATION_ALREADY_ADOPTED；
- 成功响应与最终 GET /adoption 查询到的记录完全一致；
- adoption_events 中恰有一条采用（历史采用次数为 1），当前方案修订号
  与原（无采用）状态之外的内容不被失败者改写。

并发的真实性由 PostgreSQL 行锁保证：测试主动持有该方案 plans 行的
FOR UPDATE 锁，确认另一个会话已经阻塞在同一把锁上（pg_locks 中出现
未授予的锁等待）后再在同一事务中执行采用并提交，从而两次采用确定地
在锁上相遇，杜绝"线程时序碰巧串行化"造成的假阳性。

运行：
    TEST_DATABASE_URL=postgresql+psycopg2://cleanroom@/cleanroom?host=/tmp \
        pytest tests/test_concurrency_pg.py
"""

import threading

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal
from app.errors import ApiError
from app.models import AdoptionEvent
from app import services

pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="并发采用依赖 PostgreSQL 的 SELECT ... FOR UPDATE 行锁",
)

VALID_PLAN = {
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": [
        {"id": "p1", "from": "SRC1", "to": "MID", "cost": 4},
        {"id": "p2", "from": "SRC2", "to": "MID", "cost": 6},
        {"id": "p3", "from": "MID", "to": "SAFE1", "cost": 5},
        {"id": "p4", "from": "MID", "to": "SAFE2", "cost": 7},
    ],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"],
}
REVISED_PLAN = {
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": [
        {"id": "p1", "from": "SRC1", "to": "MID", "cost": 4},
        {"id": "p2", "from": "SRC2", "to": "MID", "cost": 6},
        {"id": "p3", "from": "MID", "to": "SAFE1", "cost": 5},
        {"id": "p4", "from": "MID", "to": "SAFE2", "cost": 1},
    ],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"],
}


def put(client, pid, plan):
    return client.put(f"/plans/{pid}", json=plan)


def compute(client, pid):
    return client.post(f"/plans/{pid}/computations").json()


def _wait_for_lock_waiter(pid, timeout=10.0):
    """等待直到另一个会话因采用而阻塞在本方案的采用锁竞争上。

    FOR UPDATE 行锁等待在 PostgreSQL 中有两种观测形态：
    1. 等待者的 SELECT ... FOR UPDATE 仍在执行：未授予的 tuple 锁；
    2. 持有者已提交更新后，等待者重新评估该行：阻塞在对持有者事务 id
       的 ShareLock 上（state 为 idle in transaction，query 字段已不是
       FOR UPDATE）。
    因此统一检测"活动会话正在等待一个属于他人事务 id 的锁"。
    """
    import time

    deadline = time.monotonic() + timeout
    watcher = SessionLocal()
    try:
        sql = text(
            """
            SELECT 1
            FROM pg_stat_activity a
            WHERE a.datname = current_database()
              AND a.pid <> pg_backend_pid()
              AND a.wait_event_type = 'Lock'
              AND EXISTS (
                SELECT 1
                FROM pg_locks w
                JOIN pg_locks h
                  ON h.locktype = 'transactionid'
                 AND h.transactionid = w.transactionid
                 AND h.granted AND h.pid <> w.pid
                WHERE w.pid = a.pid AND NOT w.granted
                  AND w.locktype = 'transactionid'
              )
            LIMIT 1
            """
        )
        while time.monotonic() < deadline:
            if watcher.execute(sql).first() is not None:
                return True
            time.sleep(0.02)
        return False
    finally:
        watcher.close()


def _adopt_with_gate(pid, cid, gate):
    """在独立线程/会话中采用；提交前在 gate 上阻塞，制造确定的锁重叠。

    gate 是一个 threading.Event：采用事务已完成加锁与插入（行锁持续
    持有）但尚未提交时 set 自己的 reached 事件并等待 gate 放行，
    主线程借此确认另一个采用已经排队在同一把行锁上后再放行提交。
    """
    import app.services as svc

    reached = threading.Event()
    outcome = {}

    real_commit = None

    def run():
        db = SessionLocal()
        # 仅替换本会话 Session 实例的 commit：第一次提交（采用事务）时
        # 先通知主线程并等待放行，随后恢复真实提交。
        original_commit = db.commit

        def gated_commit(*args, **kwargs):
            if not reached.is_set():
                reached.set()
                gate.wait(timeout=15)
            return original_commit(*args, **kwargs)

        db.commit = gated_commit
        try:
            adoption = svc.adopt(db, pid, cid)
            outcome["status"] = 200
            outcome["snapshot"] = adoption.snapshot
        except ApiError as exc:
            outcome["status"] = exc.status_code
            outcome["code"] = exc.code
            outcome["message"] = exc.message
        except Exception as exc:  # 任何非预期错误都让测试显式失败
            outcome["status"] = 500
            outcome["code"] = "INTERNAL_ERROR"
            outcome["message"] = repr(exc)
        finally:
            db.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome, reached


def _adopt_in_thread(pid, cid):
    """在独立线程/会话中执行一次采用，返回 (线程, 结果字典)。"""
    outcome = {}

    def run():
        db = SessionLocal()
        try:
            adoption = services.adopt(db, pid, cid)
            outcome["status"] = 200
            outcome["snapshot"] = adoption.snapshot
        except ApiError as exc:
            outcome["status"] = exc.status_code
            outcome["code"] = exc.code
            outcome["message"] = exc.message
        except Exception as exc:  # 任何非预期错误都让测试显式失败
            outcome["status"] = 500
            outcome["code"] = "INTERNAL_ERROR"
            outcome["message"] = repr(exc)
        finally:
            db.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_concurrent_first_adoption_is_deterministic(client):
    """两个不同计算并发首次采用：一胜一负，结果与最终记录一致。"""
    pid = "concurrent-first"
    put(client, pid, VALID_PLAN)
    c1 = compute(client, pid)
    put(client, pid, REVISED_PLAN)
    c2 = compute(client, pid)
    assert c1["plan_revision"] == 1 and c2["plan_revision"] == 2

    # 领先者：采用事务在提交点被 gate 挂住，从而持续持有 plans 行锁
    gate = threading.Event()
    leader, lout, leader_ready = _adopt_with_gate(pid, c1["computation_id"], gate)
    assert leader_ready.wait(timeout=10), "leader never reached commit point"

    # 落后者此时开始采用，必然排队在领先者持有的同一把行锁上
    follower, fout = _adopt_in_thread(pid, c2["computation_id"])
    assert _wait_for_lock_waiter(pid), "follower never blocked on the plan row lock"

    # 放行领先者提交；落后者随后被唤醒，在 READ COMMITTED 下读到
    # 领先者的提交并判定为并发冲突
    gate.set()
    leader.join(timeout=15)
    follower.join(timeout=15)
    assert not leader.is_alive() and not follower.is_alive(), "worker thread hung"

    # ---- 确定结果：恰好一个 200、一个 409 ADOPTION_CONFLICT ----
    assert lout["status"] == 200, lout
    assert fout["status"] == 409, fout
    assert fout["code"] == "ADOPTION_CONFLICT", fout
    # 绝不允许 500，也不允许把行竞争误报为"计算已采用"
    assert fout["code"] != "COMPUTATION_ALREADY_ADOPTED"

    # ---- 成功响应与最终查询到的记录逐项一致 ----
    final = client.get(f"/plans/{pid}/adoption")
    assert final.status_code == 200
    assert final.json() == lout["snapshot"]
    winner = lout["snapshot"]
    assert winner["computation_id"] == c1["computation_id"]
    assert winner["plan_revision"] == 1
    assert winner["plan"] == VALID_PLAN
    assert winner["result"] == c1["result"]
    assert winner["result"]["cut_segments"] == ["p1", "p2"]
    assert winner["result"]["total_cost"] == 10

    # ---- 历史采用次数恰为 1，且只属于成功的那次计算 ----
    db = SessionLocal()
    try:
        events = db.query(AdoptionEvent).filter(AdoptionEvent.plan_id == pid).all()
        assert len(events) == 1
        assert events[0].computation_id == c1["computation_id"]
        assert events[0].plan_revision == 1
    finally:
        db.close()

    # 当前方案修订号仍是 2，失败者未改写任何数据
    assert client.get(f"/plans/{pid}").json()["revision"] == 2

    # 落后者随后重试自己的计算：此时属于"顺序替换"，应成功并生成
    # 第二条不可变采用历史；而领先者的 c1 已不可再用
    resp = client.post(
        f"/plans/{pid}/adopt", json={"computation_id": c2["computation_id"]}
    )
    assert resp.status_code == 200
    assert resp.json()["plan_revision"] == 2
    db = SessionLocal()
    try:
        assert db.query(AdoptionEvent).filter(
            AdoptionEvent.plan_id == pid
        ).count() == 2
    finally:
        db.close()
    resp = client.post(
        f"/plans/{pid}/adopt", json={"computation_id": c1["computation_id"]}
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "COMPUTATION_ALREADY_ADOPTED"


def test_concurrent_first_adoption_same_computation(client):
    """两个请求并发采用**同一个**成功计算：一胜，另一者必须得到
    COMPUTATION_ALREADY_ADOPTED（而不是 ADOPTION_CONFLICT 或 500）。"""
    pid = "concurrent-same"
    put(client, pid, VALID_PLAN)
    c1 = compute(client, pid)

    gate = threading.Event()
    leader, lout, leader_ready = _adopt_with_gate(pid, c1["computation_id"], gate)
    assert leader_ready.wait(timeout=10)
    follower, fout = _adopt_in_thread(pid, c1["computation_id"])
    assert _wait_for_lock_waiter(pid)
    gate.set()
    leader.join(timeout=15)
    follower.join(timeout=15)

    assert lout["status"] == 200, lout
    assert fout["status"] == 409, fout
    assert fout["code"] == "COMPUTATION_ALREADY_ADOPTED", fout

    final = client.get(f"/plans/{pid}/adoption")
    assert final.status_code == 200
    assert final.json() == lout["snapshot"]
    db = SessionLocal()
    try:
        assert (
            db.query(AdoptionEvent).filter(AdoptionEvent.plan_id == pid).count() == 1
        )
    finally:
        db.close()


@pytest.mark.parametrize("round_no", range(5))
def test_parallel_first_adoption_fuzz(client, round_no):
    """多轮无协调并行首次采用：任何交错下都不得出现 500/误报，最终状态自洽。"""
    pid = f"fuzz-{round_no}"
    put(client, pid, VALID_PLAN)
    c1 = compute(client, pid)
    put(client, pid, REVISED_PLAN)
    c2 = compute(client, pid)

    barrier = threading.Barrier(2)
    outcomes = {}

    def worker(name, cid):
        barrier.wait()
        db = SessionLocal()
        try:
            adoption = services.adopt(db, pid, cid)
            outcomes[name] = ("ok", adoption.snapshot)
        except ApiError as exc:
            outcomes[name] = (exc.code, None)
        except Exception as exc:
            outcomes[name] = ("INTERNAL_ERROR:" + repr(exc), None)
        finally:
            db.close()

    t1 = threading.Thread(target=worker, args=("a", c1["computation_id"]))
    t2 = threading.Thread(target=worker, args=("b", c2["computation_id"]))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert set(outcomes) == {"a", "b"}

    codes = {name: value[0] for name, value in outcomes.items()}
    # 两个不同计算并发首次采用：只可能是成功或并发冲突；
    # 不可能是 COMPUTATION_ALREADY_ADOPTED，更不能出现 500。
    assert set(codes.values()) <= {"ok", "ADOPTION_CONFLICT"}, codes
    assert "ok" in codes.values(), codes

    # 至少一次成功时，成功响应必须等于最终记录；历史次数等于成功次数
    db = SessionLocal()
    try:
        event_count = (
            db.query(AdoptionEvent).filter(AdoptionEvent.plan_id == pid).count()
        )
    finally:
        db.close()
    ok_snapshots = [v[1] for v in outcomes.values() if v[0] == "ok"]
    assert len(ok_snapshots) == event_count
    final = client.get(f"/plans/{pid}/adoption")
    assert final.status_code == 200
    final_snapshot = final.json()
    if ok_snapshots:
        # 最终记录必须属于某个真正返回 200 的采用
        assert final_snapshot in ok_snapshots
        assert final_snapshot["computation_id"] in {
            c1["computation_id"], c2["computation_id"]
        }
        # 快照整体自洽：修订号、方案、结果属于同一计算的冻结版本
        source = c1 if final_snapshot["computation_id"] == c1["computation_id"] else c2
        expected_plan = VALID_PLAN if source["plan_revision"] == 1 else REVISED_PLAN
        assert final_snapshot["plan_revision"] == source["plan_revision"]
        assert final_snapshot["plan"] == expected_plan
        assert final_snapshot["result"] == source["result"]
