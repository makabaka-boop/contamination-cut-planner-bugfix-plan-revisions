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


# ===========================================================================
# 并发保存验收：并发首次创建、并发整版覆盖，以及随后的计算与采用。
#
# 并发的真实性由 PostgreSQL 行锁/唯一约束保证（与并发采用同样的做法）：
# - 覆盖：领先者的条件 UPDATE 已执行（持有 plans 行锁）但在提交点被
#   gate 挂住，确认落后者已阻塞在同一行的锁竞争上后再放行提交，两次
#   保存因此确定地在锁上相遇；
# - 创建：两个会话的提交由 Barrier 同步放行，两条 INSERT 确定地在
#   主键唯一约束上相遇，一条成功、另一条收到唯一冲突。
# ===========================================================================

# 第三种方案：p1 费用 4 -> 1，最小割变为切 p1+p2=7，与前两版均不同
FOLLOWER_PLAN = {
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": [
        {"id": "p1", "from": "SRC1", "to": "MID", "cost": 1},
        {"id": "p2", "from": "SRC2", "to": "MID", "cost": 6},
        {"id": "p3", "from": "MID", "to": "SAFE1", "cost": 5},
        {"id": "p4", "from": "MID", "to": "SAFE2", "cost": 7},
    ],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"],
}


def _save_in_thread(pid, payload, expected_revision, pre_commit=None):
    """在独立线程/会话中执行一次保存，返回 (线程, 结果字典)。

    pre_commit 在该会话真正提交前调用一次：可用于在持锁未提交状态
    下挂起（gate），或让多个会话在提交点同步（Barrier）。
    """
    outcome = {"payload": payload, "expected_revision": expected_revision}

    def run():
        db = SessionLocal()
        if pre_commit is not None:
            original_commit = db.commit

            def hooked_commit(*args, **kwargs):
                pre_commit()
                return original_commit(*args, **kwargs)

            db.commit = hooked_commit
        try:
            plan = services.save_plan(db, pid, payload, expected_revision)
            outcome["status"] = 200
            outcome["view"] = {
                "plan_id": plan.plan_id,
                "revision": plan.revision,
                "plan": plan.payload,
            }
        except ApiError as exc:
            outcome["status"] = exc.status_code
            outcome["code"] = exc.code
            outcome["message"] = exc.message
            outcome["details"] = exc.details
        except Exception as exc:  # 任何非预期错误都让测试显式失败
            outcome["status"] = 500
            outcome["code"] = "INTERNAL_ERROR"
            outcome["message"] = repr(exc)
        finally:
            db.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_concurrent_create_same_plan_is_clear_conflict(client):
    """两人同时首次创建同一方案：一胜（200 rev 1），落败者得到明确的
    409 REVISION_CONFLICT（PLAN_ALREADY_EXISTS），而不是数据库异常；
    落败写入不得改变胜者方案，随后计算与采用追溯确定版本。"""
    pid = "concurrent-create"

    # 两个会话各自的提交先在 Barrier 处会合，再同时放行，确保两条
    # INSERT 在主键唯一约束上真正相撞
    barrier = threading.Barrier(2)
    t1, o1 = _save_in_thread(pid, VALID_PLAN, None, pre_commit=lambda: barrier.wait(15))
    t2, o2 = _save_in_thread(
        pid, REVISED_PLAN, None, pre_commit=lambda: barrier.wait(15)
    )
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert not t1.is_alive() and not t2.is_alive(), "worker thread hung"

    winners = [o for o in (o1, o2) if o["status"] == 200]
    losers = [o for o in (o1, o2) if o["status"] != 200]
    assert len(winners) == 1 and len(losers) == 1, (o1, o2)
    winner, loser = winners[0], losers[0]

    # ---- 胜者：200、修订号 1、响应即本次提交的内容 ----
    assert winner["view"]["revision"] == 1
    assert winner["view"]["plan"] == winner["payload"]

    # ---- 落败者：明确冲突，绝不暴露 500/数据库异常 ----
    assert loser["status"] == 409, loser
    assert loser["code"] == "REVISION_CONFLICT", loser
    assert [d["code"] for d in loser["details"]] == ["PLAN_ALREADY_EXISTS"], loser

    # ---- 落败写入未改变任何数据：最终方案即胜者内容，修订号仍为 1 ----
    final = client.get(f"/plans/{pid}")
    assert final.status_code == 200
    assert final.json() == winner["view"]

    # ---- 落败者重新读取后按新修订号提交：冲突可恢复，修订号唯一递增 ----
    retry = client.put(
        f"/plans/{pid}",
        json={**loser["payload"], "expected_revision": 1},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["revision"] == 2
    assert retry.json()["plan"] == loser["payload"]

    # ---- 随后的计算与采用追溯到确定版本（第 2 版，落败者内容）----
    c = compute(client, pid)
    assert c["status"] == "SUCCESS"
    assert c["plan_revision"] == 2
    resp = client.post(
        f"/plans/{pid}/adopt", json={"computation_id": c["computation_id"]}
    )
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["plan_revision"] == 2
    assert snapshot["plan"] == loser["payload"]
    assert snapshot["result"] == c["result"]
    assert client.get(f"/plans/{pid}/adoption").json() == snapshot


def test_concurrent_overwrite_same_revision_one_wins(client):
    """两次编辑都先读到修订 1，再各自提交不同内容：恰有一次被接受为
    修订 2 且响应等于其提交；另一次得到 409 REVISION_CONFLICT
    （REVISION_MISMATCH），落败写入不得改变方案。随后计算与采用
    冻结的全部是胜者版本。"""
    pid = "concurrent-overwrite"
    put(client, pid, VALID_PLAN)  # 当前修订 1

    # 领先者：条件 UPDATE 已执行并持有行锁，在提交点挂起
    gate = threading.Event()
    reached = threading.Event()
    leader, lout = _save_in_thread(
        pid,
        REVISED_PLAN,
        1,
        pre_commit=lambda: (reached.set(), gate.wait(timeout=15)),
    )
    assert reached.wait(timeout=10), "leader never reached commit point"

    # 落后者此时提交同样基于修订 1 的覆盖：必然阻塞在同一行锁上
    follower, fout = _save_in_thread(pid, FOLLOWER_PLAN, 1)
    assert _wait_for_lock_waiter(pid), "follower never blocked on the plan row lock"

    gate.set()
    leader.join(timeout=20)
    follower.join(timeout=20)
    assert not leader.is_alive() and not follower.is_alive(), "worker thread hung"

    # ---- 恰一胜一负：不允许两个 200，也不允许 500 ----
    assert lout["status"] == 200, lout
    assert fout["status"] == 409, fout
    assert fout["code"] == "REVISION_CONFLICT", fout
    assert [d["code"] for d in fout["details"]] == ["REVISION_MISMATCH"], fout

    # ---- 胜者响应：修订号恰好推进一次（2），内容属于本次提交 ----
    assert lout["view"]["revision"] == 2
    assert lout["view"]["plan"] == REVISED_PLAN

    # ---- 落败写入未改变现有方案：最终记录与胜者响应逐项一致，
    #      落败者的方案内容在任何地方都不可见 ----
    final = client.get(f"/plans/{pid}")
    assert final.status_code == 200
    assert final.json() == lout["view"]
    assert final.json()["plan"] != FOLLOWER_PLAN

    # ---- 随后计算：冻结修订号 2 与胜者负载 ----
    c = compute(client, pid)
    assert c["plan_revision"] == 2
    # REVISED_PLAN 的最小割：切 p3+p4=6
    assert c["result"] == {
        "source_zones": ["MID", "SRC1", "SRC2"],
        "cut_segments": ["p3", "p4"],
        "total_cost": 6,
    }
    got_c = client.get(f"/plans/{pid}/computations/{c['computation_id']}")
    assert got_c.status_code == 200
    assert got_c.json()["plan_revision"] == 2
    assert got_c.json()["result"] == c["result"]

    # ---- 随后采用：快照与计算冻结版本一致，且与最终采用记录一致 ----
    resp = client.post(
        f"/plans/{pid}/adopt", json={"computation_id": c["computation_id"]}
    )
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["plan_revision"] == 2
    assert snapshot["plan"] == REVISED_PLAN
    assert snapshot["result"] == c["result"]
    assert client.get(f"/plans/{pid}/adoption").json() == snapshot

    # ---- 落败者重新读取后基于修订 2 重试：属于正常串行编辑，应成功 ----
    retry = client.put(
        f"/plans/{pid}",
        json={**FOLLOWER_PLAN, "expected_revision": 2},
    )
    assert retry.status_code == 200
    assert retry.json()["revision"] == 3
    assert client.get(f"/plans/{pid}").json() == retry.json()


@pytest.mark.parametrize("round_no", range(5))
def test_parallel_tokenless_overwrite_fuzz(client, round_no):
    """未携带修订号的并发整版替换（旧语义保持兼容）：两次写入都被接受
    时修订号必须互不相同，且每个响应的修订号与负载都属于自己的提交；
    最终记录是修订号较大的那份，任何交错下都不得出现 500。"""
    pid = f"save-fuzz-{round_no}"
    put(client, pid, VALID_PLAN)  # 当前修订 1

    # 不做额外协调：任何交错下结果都应成立（行锁把两次原子 +1
    # 串行化为修订 2 与修订 3）
    t1, o1 = _save_in_thread(pid, REVISED_PLAN, None)
    t2, o2 = _save_in_thread(pid, FOLLOWER_PLAN, None)
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert o1["status"] == 200 and o2["status"] == 200, (o1, o2)

    # 两次接受的修订号互不相同，恰好是 {2, 3}（同一行锁串行化两次 +1）
    assert {o1["view"]["revision"], o2["view"]["revision"]} == {2, 3}, (o1, o2)
    # 每个响应的负载都是自己提交的内容（内容、修订号、响应一一对应）
    for outcome in (o1, o2):
        assert outcome["view"]["plan"] == outcome["payload"]
    # 最终记录属于修订号较大的那次提交，与该次响应逐项一致
    later = max(o1, o2, key=lambda o: o["view"]["revision"])
    assert client.get(f"/plans/{pid}").json() == later["view"]


def test_concurrent_overwrite_through_http(client):
    """端到端：两个并发 HTTP PUT 携带相同 expected_revision，
    经 FastAPI/线程池/PostgreSQL 后恰一胜一负，错误信封稳定。"""
    pid = "http-overwrite"
    put(client, pid, VALID_PLAN)

    results = {}
    barrier = threading.Barrier(2)

    def http_worker(name, plan):
        barrier.wait()
        results[name] = client.put(
            f"/plans/{pid}",
            json={**plan, "expected_revision": 1},
        )

    t1 = threading.Thread(target=http_worker, args=("a", REVISED_PLAN))
    t2 = threading.Thread(target=http_worker, args=("b", FOLLOWER_PLAN))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert set(results) == {"a", "b"}

    statuses = {name: resp.status_code for name, resp in results.items()}
    assert sorted(statuses.values()) == [200, 409], statuses
    ok = next(resp for resp in results.values() if resp.status_code == 200)
    bad = next(resp for resp in results.values() if resp.status_code == 409)
    assert ok.json()["revision"] == 2
    error = bad.json()["error"]
    assert error["code"] == "REVISION_CONFLICT"
    assert {d["code"] for d in error["details"]} == {"REVISION_MISMATCH"}

    # 最终记录即 200 响应；随后计算与采用都指向该版本
    assert client.get(f"/plans/{pid}").json() == ok.json()
    c = compute(client, pid)
    assert c["plan_revision"] == 2
    resp = client.post(
        f"/plans/{pid}/adopt", json={"computation_id": c["computation_id"]}
    )
    assert resp.status_code == 200
    assert resp.json()["plan_revision"] == 2
    assert resp.json()["result"] == c["result"]

