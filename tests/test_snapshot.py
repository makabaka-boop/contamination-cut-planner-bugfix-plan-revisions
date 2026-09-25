"""快照一致性与"每个成功计算至多采用一次"的验收测试（两个后端均运行）。

覆盖：
1. 计算后修订方案再采用旧计算：快照中的方案/修订号/切断清单/费用必须
   全部冻结在计算时刻，与当前方案无关，且清单确实能隔断快照中的污染路径；
2. 一次成功计算先被采用、再被另一次计算替换后，原计算仍不可再次采用；
3. 历史采用次数随合法替换增长，任何失败/冲突都不改变当前方案与当前快照。
"""

from collections import deque

from app.db import SessionLocal
from app.models import AdoptionEvent

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


def compute(client, pid):
    return client.post(f"/plans/{pid}/computations").json()


def adopt(client, pid, cid):
    return client.post(f"/plans/{pid}/adopt", json={"computation_id": cid})


def count_events(plan_id):
    db = SessionLocal()
    try:
        return (
            db.query(AdoptionEvent)
            .filter(AdoptionEvent.plan_id == plan_id)
            .count()
        )
    finally:
        db.close()


def cut_isolates_sources(plan, cut_ids):
    """删除 cut_ids 管段后，任何污染源都不应能到达任何保护区。"""
    cut = set(cut_ids)
    adjacency = {}
    for seg in plan["segments"]:
        if seg["id"] in cut:
            continue
        adjacency.setdefault(seg["from"], []).append(seg["to"])
    seen = set(plan["sources"])
    queue = deque(plan["sources"])
    while queue:
        node = queue.popleft()
        for nxt in adjacency.get(node, []):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen.isdisjoint(plan["protections"])


def test_compute_after_revision_then_adopt_freezes_coherent_snapshot(client):
    """修订前计算、修订后采用：快照必须完整冻结计算时刻的那一版。"""
    pid = "freeze"
    assert put(client, pid, VALID_PLAN).status_code == 200
    c1 = compute(client, pid)
    assert c1["plan_revision"] == 1
    assert c1["result"] == EXPECTED_V1

    # 修订方案：当前方案变为第 2 版，最小割也变了
    resp = put(client, pid, REVISED_PLAN)
    assert resp.status_code == 200
    assert resp.json()["revision"] == 2
    c2 = compute(client, pid)
    assert c2["plan_revision"] == 2
    assert c2["result"] == EXPECTED_V2

    # 采用旧计算：快照必须仍是第 1 版，而不是当前第 2 版
    resp = adopt(client, pid, c1["computation_id"])
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["computation_id"] == c1["computation_id"]
    assert snapshot["plan_revision"] == 1
    assert snapshot["plan"] == VALID_PLAN
    assert snapshot["result"] == EXPECTED_V1
    # 清单确实能隔断快照方案中的全部污染路径
    assert cut_isolates_sources(snapshot["plan"], snapshot["result"]["cut_segments"])

    # 持久化后的查询结果必须与成功响应完全一致
    got = client.get(f"/plans/{pid}/adoption")
    assert got.status_code == 200
    assert got.json() == snapshot

    # 当前方案仍是第 2 版，没有被采用动作改写
    current = client.get(f"/plans/{pid}").json()
    assert current["revision"] == 2
    assert current["plan"] == REVISED_PLAN

    # 计算记录本身也不可变：旧计算仍记录第 1 版的结果
    got_c1 = client.get(f"/plans/{pid}/computations/{c1['computation_id']}").json()
    assert got_c1["plan_revision"] == 1
    assert got_c1["result"] == EXPECTED_V1


def test_adopt_revised_computation_replaces_with_coherent_v2_snapshot(client):
    """采用新计算后，快照整体切换到新版本，且同样自洽。"""
    pid = "replace-coherent"
    put(client, pid, VALID_PLAN)
    c1 = compute(client, pid)
    put(client, pid, REVISED_PLAN)
    c2 = compute(client, pid)

    assert adopt(client, pid, c1["computation_id"]).status_code == 200
    resp = adopt(client, pid, c2["computation_id"])
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["computation_id"] == c2["computation_id"]
    assert snapshot["plan_revision"] == 2
    assert snapshot["plan"] == REVISED_PLAN
    assert snapshot["result"] == EXPECTED_V2
    assert cut_isolates_sources(REVISED_PLAN, ["p3", "p4"])

    assert client.get(f"/plans/{pid}/adoption").json() == snapshot


def test_replaced_computation_cannot_be_adopted_again(client):
    """先采用 c1、再用 c2 替换后，c1 不允许被再次采用。"""
    pid = "reuse"
    put(client, pid, VALID_PLAN)
    c1 = compute(client, pid)
    assert adopt(client, pid, c1["computation_id"]).status_code == 200

    put(client, pid, REVISED_PLAN)
    c2 = compute(client, pid)
    assert adopt(client, pid, c2["computation_id"]).status_code == 200

    # 当前生效快照属于 c2（第 2 版）
    current = client.get(f"/plans/{pid}/adoption").json()
    assert current["computation_id"] == c2["computation_id"]
    assert current["plan_revision"] == 2

    # 重用旧计算被拒：错误码稳定，且当前快照不被改动
    resp = adopt(client, pid, c1["computation_id"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "COMPUTATION_ALREADY_ADOPTED"
    assert client.get(f"/plans/{pid}/adoption").json() == current

    # 历史采用次数：只有 c1、c2 各一次，失败的重用不计入
    assert count_events(pid) == 2
    resp = adopt(client, pid, c1["computation_id"])
    assert resp.status_code == 409
    assert count_events(pid) == 2

    # c2 本身也不能再次采用
    resp = adopt(client, pid, c2["computation_id"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "COMPUTATION_ALREADY_ADOPTED"
    assert client.get(f"/plans/{pid}/adoption").json() == current
    assert count_events(pid) == 2


def test_failed_readopt_does_not_touch_plan_or_revision(client):
    """冲突/重复采用不得改变当前方案修订号与生效快照。"""
    pid = "no-clobber"
    put(client, pid, VALID_PLAN)
    c1 = compute(client, pid)
    put(client, pid, REVISED_PLAN)
    c2 = compute(client, pid)
    # c1 先采用、再被 c2 替换
    assert adopt(client, pid, c1["computation_id"]).status_code == 200
    assert adopt(client, pid, c2["computation_id"]).status_code == 200
    before = client.get(f"/plans/{pid}/adoption").json()

    # 已被替换下来的 c1 不可重用
    assert adopt(client, pid, c1["computation_id"]).status_code == 409
    # 已生效的 c2 重复采用同样失败
    assert adopt(client, pid, c2["computation_id"]).status_code == 409
    # 不存在的计算 404
    assert adopt(client, pid, "missing").status_code == 404

    assert client.get(f"/plans/{pid}/adoption").json() == before
    assert client.get(f"/plans/{pid}").json()["revision"] == 2
