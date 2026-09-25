"""真实 PostgreSQL 上的并发保存验收测试。

场景：两名调度员同时编辑同一污染切断方案。期望：
- 并发首次创建：恰有一个请求被接受（200，第 1 版），落败者得到
  409 PLAN_SAVE_CONFLICT 而不是数据库异常（500）；现状不被改写；
- 基于同一修订的并发覆盖：恰有一个请求被接受（修订号 +1），落败者
  得到 409 PLAN_SAVE_CONFLICT；失败写入不改变现有方案，修订号也
  不被落空消耗；
- 每个被接受的内容、修订号与响应一一对应：保存响应绝不回读到他人
  后来写入的内容；
- 随后的计算与采用始终追溯到唯一确定的版本：计算记录冻结的
  (plan_revision, plan_payload) 与采用快照逐项自洽。

并发的真实性由 PostgreSQL 行锁/唯一约束保证：测试让领先事务在提交点
挂起（UPDATE 已执行或 INSERT 已刷新但未提交，行锁/未提交元组持续
持有），确认另一个会话已阻塞在同一把锁上（pg_locks 中出现未授予的
锁等待）后再放行提交，从而两次保存确定地在数据库层相遇，杜绝
"线程时序碰巧串行化"造成的假阳性。

运行：
    TEST_DATABASE_URL=postgresql+psycopg2://cleanroom@/cleanroom?host=/tmp \
        pytest tests/test_plan_concurrency_pg.py
"""

import threading

import pytest
from sqlalchemy import text

from app.config import settings
from app.db import SessionLocal
from app.errors import ApiError
from app import services
from app.validation import validate_plan_payload

pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="并发保存依赖 PostgreSQL 的行锁与唯一约束冲突检测",
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
# 修订后：p4 费用 7 -> 1，最小割从 {p1,p2}=10 变为 {p3,p4}=6
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
# 第三份内容：p3 费用 5 -> 9
THIRD_PLAN = {
    "zones": ["SRC1", "SRC2", "MID", "SAFE1", "SAFE2"],
    "segments": [
        {"id": "p1", "from": "SRC1", "to": "MID", "cost": 4},
        {"id": "p2", "from": "SRC2", "to": "MID", "cost": 6},
        {"id": "p3", "from": "MID", "to": "SAFE1", "cost": 9},
        {"id": "p4", "from": "MID", "to": "SAFE2", "cost": 7},
    ],
    "sources": ["SRC1", "SRC2"],
    "protections": ["SAFE1", "SAFE2"],
}
EXPECTED_V1 = {
    "source_zones": ["SRC1", "SRC2"],
    "cut_segments": ["p1", "p2"],
    "total_cost": 10,
}
EXPECTED_V2 = {
    "source_zones": ["MID", "SRC1", "SRC2"],
    "cut_segments": ["p3", "p4"],
    "total_cost": 6,
}


def put(client, pid, plan):
    return client.put(f"/plans/{pid}", json=plan)


def _wait_for_lock_waiter(pid, timeout=10.0):
    """等待直到另一个会话阻塞在本方案保存的锁竞争上。

    并发保存的锁等待在 PostgreSQL 中有两种观测形态：
    1. 等待者的 UPDATE/INSERT 仍在执行：未授予的 tuple/唯一约束锁；
    2. 持有者已提交更新后，等待者重新评估该行/该元组：阻塞在对持有者
       事务 id 的 ShareLock 上（state 为 idle in transaction）。
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


def _record_save(db, pid, payload, outcome):
    """执行一次保存并把结果（或稳定错误结构）记录到 outcome。"""
    try:
        saved = services.save_plan(db, pid, validate_plan_payload(payload))
        outcome["status"] = 200
        outcome["body"] = {
            "plan_id": saved.plan_id,
            "revision": saved.revision,
            "plan": saved.payload,
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


def _save_with_gate(pid, payload, gate, *, after_commit=False):
    """在独立线程/会话中保存；在提交点挂起，制造确定的锁重叠。

    after_commit=False：先 flush（UPDATE 已执行/INSERT 已落库但未提交，
    行锁或未提交元组持续持有），再通知主线程并等待 gate 放行，最后提交；
    after_commit=True：先真正提交，再在构造响应前挂起——用于验证响应
    不会回读到他人随后写入的内容。
    """
    reached = threading.Event()
    outcome = {}

    def run():
        db = SessionLocal()
        # 仅替换本会话 Session 实例的 commit：第一次提交（保存事务）时
        # 先通知主线程并等待放行，随后恢复真实提交。
        original_commit = db.commit

        def gated_commit(*args, **kwargs):
            if not reached.is_set():
                if after_commit:
                    result = original_commit(*args, **kwargs)
                    reached.set()
                    gate.wait(timeout=15)
                    return result
                db.flush()
                reached.set()
                gate.wait(timeout=15)
            return original_commit(*args, **kwargs)

        db.commit = gated_commit
        try:
            _record_save(db, pid, payload, outcome)
        finally:
            db.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome, reached


def _save_in_thread(pid, payload):
    """在独立线程/会话中执行一次保存，返回 (线程, 结果字典)。"""
    outcome = {}

    def run():
        db = SessionLocal()
        try:
            _record_save(db, pid, payload, outcome)
        finally:
            db.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_concurrent_create_one_wins_one_conflict(client):
    """两人同时首次创建同一方案：一胜一负，落败者是明确冲突而非 500。"""
    pid = "create-race"

    # 领先者：INSERT 已刷新但未提交，持续持有未提交元组
    gate = threading.Event()
    leader, lout, leader_ready = _save_with_gate(pid, VALID_PLAN, gate)
    assert leader_ready.wait(timeout=10), "leader never reached commit point"

    # 落后者此时创建同一方案，必然阻塞在唯一约束冲突检测上
    follower, fout = _save_in_thread(pid, REVISED_PLAN)
    assert _wait_for_lock_waiter(pid), "follower never blocked on the insert"

    # 放行领先者提交；落后者随即得到主键冲突，被转换为明确 409
    gate.set()
    leader.join(timeout=15)
    follower.join(timeout=15)
    assert not leader.is_alive() and not follower.is_alive(), "worker thread hung"

    # ---- 确定结果：恰好一个 200、一个 409 PLAN_SAVE_CONFLICT ----
    assert lout["status"] == 200, lout
    assert lout["body"] == {"plan_id": pid, "revision": 1, "plan": VALID_PLAN}
    assert fout["status"] == 409, fout
    assert fout["code"] == "PLAN_SAVE_CONFLICT", fout
    # 稳定错误结构：非空 message、details 为列表；绝不允许 500
    assert fout["message"]
    assert isinstance(fout["details"], list)

    # ---- 失败写入不改变现状：方案仍是胜者的第 1 版 ----
    got = client.get(f"/plans/{pid}")
    assert got.status_code == 200
    assert got.json() == {"plan_id": pid, "revision": 1, "plan": VALID_PLAN}

    # ---- 随后计算与采用追溯到唯一确定的第 1 版 ----
    comp = client.post(f"/plans/{pid}/computations").json()
    assert comp["plan_revision"] == 1
    assert comp["result"] == EXPECTED_V1
    resp = client.post(
        f"/plans/{pid}/adopt", json={"computation_id": comp["computation_id"]}
    )
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["plan_revision"] == 1
    assert snapshot["plan"] == VALID_PLAN
    assert snapshot["result"] == EXPECTED_V1
    assert client.get(f"/plans/{pid}/adoption").json() == snapshot


def test_concurrent_overwrite_one_wins_one_conflict(client):
    """两人基于同一修订并发覆盖：一胜一负，失败写入不改变现有方案。"""
    pid = "overwrite-race"
    assert put(client, pid, VALID_PLAN).status_code == 200  # 第 1 版

    # 两名调度员都先读到第 1 版，再各自提交不同内容。
    # 领先者：UPDATE 已执行但未提交，持续持有 plans 行锁
    gate = threading.Event()
    leader, lout, leader_ready = _save_with_gate(pid, REVISED_PLAN, gate)
    assert leader_ready.wait(timeout=10), "leader never reached commit point"

    # 落后者此时同样基于第 1 版提交，必然排队在领先者持有的行锁上
    follower, fout = _save_in_thread(pid, THIRD_PLAN)
    assert _wait_for_lock_waiter(pid), "follower never blocked on the plan row lock"

    # 放行领先者提交；落后者被唤醒后发现读到的修订已过期，条件更新
    # 命中 0 行，回滚并以明确 409 失败
    gate.set()
    leader.join(timeout=15)
    follower.join(timeout=15)
    assert not leader.is_alive() and not follower.is_alive(), "worker thread hung"

    # ---- 确定结果：恰好一个 200、一个 409 PLAN_SAVE_CONFLICT ----
    assert lout["status"] == 200, lout
    assert lout["body"] == {"plan_id": pid, "revision": 2, "plan": REVISED_PLAN}
    assert fout["status"] == 409, fout
    assert fout["code"] == "PLAN_SAVE_CONFLICT", fout
    assert fout["message"]
    assert isinstance(fout["details"], list)

    # ---- 失败写入不改变现有方案：内容仍是胜者的，修订号只前进一格 ----
    got = client.get(f"/plans/{pid}")
    assert got.status_code == 200
    assert got.json() == {"plan_id": pid, "revision": 2, "plan": REVISED_PLAN}

    # ---- 随后计算与采用追溯到唯一确定的第 2 版 ----
    comp = client.post(f"/plans/{pid}/computations").json()
    assert comp["plan_revision"] == 2
    assert comp["result"] == EXPECTED_V2
    resp = client.post(
        f"/plans/{pid}/adopt", json={"computation_id": comp["computation_id"]}
    )
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["plan_revision"] == 2
    assert snapshot["plan"] == REVISED_PLAN
    assert snapshot["result"] == EXPECTED_V2
    assert client.get(f"/plans/{pid}/adoption").json() == snapshot

    # ---- 落败方重新读取后可串行保存：第 3 版，与历史版本各自唯一 ----
    resp = put(client, pid, THIRD_PLAN)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 3
    got = client.get(f"/plans/{pid}").json()
    assert got == {"plan_id": pid, "revision": 3, "plan": THIRD_PLAN}


def test_save_response_reflects_own_committed_content(client):
    """提交后、响应构造前他人又保存了一版：响应仍必须是本事务写入的内容。"""
    pid = "response-race"
    put(client, pid, VALID_PLAN)  # 第 1 版

    # 领先者提交第 2 版后、构造响应前被挂起
    gate = threading.Event()
    leader, lout, leader_ready = _save_with_gate(
        pid, REVISED_PLAN, gate, after_commit=True
    )
    assert leader_ready.wait(timeout=10), "leader never passed its commit"

    # 另一请求随后保存第 3 版并提交（领先者已提交，不再持有锁）
    resp = put(client, pid, THIRD_PLAN)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 3

    gate.set()
    leader.join(timeout=15)
    assert not leader.is_alive(), "worker thread hung"

    # 领先请求的响应必须仍是它自己写入的第 2 版内容，
    # 不得回读到他人后来覆盖的第 3 版
    assert lout["status"] == 200, lout
    assert lout["body"] == {"plan_id": pid, "revision": 2, "plan": REVISED_PLAN}

    # 最终状态是第 3 版
    got = client.get(f"/plans/{pid}").json()
    assert got == {"plan_id": pid, "revision": 3, "plan": THIRD_PLAN}


@pytest.mark.parametrize("round_no", range(5))
def test_parallel_overwrite_fuzz(client, round_no):
    """多轮无协调并行覆盖：任何交错下结果自洽——一胜一负或串行两胜。"""
    pid = f"save-fuzz-{round_no}"
    put(client, pid, VALID_PLAN)  # 第 1 版

    barrier = threading.Barrier(2)
    outcomes = {}

    def worker(name, payload):
        barrier.wait()
        db = SessionLocal()
        try:
            saved = services.save_plan(db, pid, validate_plan_payload(payload))
            outcomes[name] = ("ok", saved.revision, saved.payload)
        except ApiError as exc:
            outcomes[name] = (exc.code, None, None)
        except Exception as exc:
            outcomes[name] = ("INTERNAL_ERROR:" + repr(exc), None, None)
        finally:
            db.close()

    t1 = threading.Thread(target=worker, args=("a", REVISED_PLAN))
    t2 = threading.Thread(target=worker, args=("b", THIRD_PLAN))
    t1.start()
    t2.start()
    t1.join(timeout=20)
    t2.join(timeout=20)
    assert set(outcomes) == {"a", "b"}

    codes = [value[0] for value in outcomes.values()]
    # 只可能是成功或明确的并发冲突；绝不能出现 500
    assert all(code in ("ok", "PLAN_SAVE_CONFLICT") for code in codes), codes
    assert "ok" in codes, codes

    oks = {name: value for name, value in outcomes.items() if value[0] == "ok"}
    # 每个被接受的响应，内容必须等于该请求各自提交的内容
    submitted = {"a": REVISED_PLAN, "b": THIRD_PLAN}
    for name, (_tag, _revision, payload) in oks.items():
        assert payload == submitted[name]

    revisions = sorted(value[1] for value in oks.values())
    if len(oks) == 2:
        # 串行落定：两个修订号各自唯一、各自对应一份内容
        assert revisions == [2, 3]
    else:
        # 一胜一负：只产生一个新修订号
        assert revisions == [2]

    # 最终状态必须是某个被接受响应的内容，且与其修订号一一对应
    final = client.get(f"/plans/{pid}").json()
    assert final["revision"] == revisions[-1]
    by_revision = {value[1]: value[2] for value in oks.values()}
    assert final["plan"] == by_revision[revisions[-1]]
