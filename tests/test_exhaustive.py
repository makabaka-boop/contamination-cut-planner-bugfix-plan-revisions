"""穷举对拍：不超过 8 个区域的全部合法（污染源, 保护区）划分。

对每个随机生成的图，枚举所有"污染源非空、保护区非空、互不相交"的
划分，将服务算法的结果与暴力枚举所有源侧集合的结果对拍，并验证
删除切断管段后确实不存在任何污染源到保护区的路径。
"""

import random
from collections import deque

import pytest

from app.flow import solve_min_cut


def precompute_cut_costs(zones, segments):
    """cut_cost[mask] = 从 mask 内指向 mask 外的管段费用之和。"""
    n = len(zones)
    index = {zone: i for i, zone in enumerate(zones)}
    cut_cost = [0] * (1 << n)
    for seg in segments:
        from_bit = 1 << index[seg["from"]]
        to_bit = 1 << index[seg["to"]]
        if from_bit == to_bit:  # 自环永不跨越割
            continue
        cost = seg["cost"]
        for mask in range(1 << n):
            if mask & from_bit and not mask & to_bit:
                cut_cost[mask] += cost
    return cut_cost, index


def brute_force_min_cut(zones, segments, sources, protections, cut_cost, index):
    """枚举全部 2^n 个源侧集合，返回 (升序源侧, 升序切断管段, 总费用)。

    在所有最低费用割中，源侧取所有最优掩码的交集（最小割源侧关于
    交运算封闭，交集本身仍是最优割）。
    """
    n = len(zones)
    src_mask = 0
    for zone in sources:
        src_mask |= 1 << index[zone]
    prot_mask = 0
    for zone in protections:
        prot_mask |= 1 << index[zone]

    best_cost = None
    intersection = 0
    for mask in range(1 << n):
        if mask & src_mask != src_mask or mask & prot_mask:
            continue
        cost = cut_cost[mask]
        if best_cost is None or cost < best_cost:
            best_cost, intersection = cost, mask
        elif cost == best_cost:
            intersection &= mask
    assert best_cost is not None
    # 交集本身必须仍是最低费用割（格封闭性）
    assert cut_cost[intersection] == best_cost

    side = {zone for zone in zones if intersection & (1 << index[zone])}
    cut = sorted(
        seg["id"] for seg in segments if seg["from"] in side and seg["to"] not in side
    )
    return sorted(side), cut, best_cost


def all_legal_partitions(zones):
    """枚举全部合法划分：污染源、保护区均非空且互不相交。"""
    n = len(zones)
    full = (1 << n) - 1
    for src_mask in range(1, 1 << n):
        complement = full ^ src_mask
        sub = complement
        while sub:
            sources = [zones[i] for i in range(n) if src_mask >> i & 1]
            protections = [zones[i] for i in range(n) if sub >> i & 1]
            yield sources, protections
            sub = (sub - 1) & complement


def assert_cut_disconnects(zones, segments, sources, protections, result):
    """删除被切断管段后，任何污染源不得到达任何保护区。"""
    cut = set(result["cut_segments"])
    adjacency = {zone: [] for zone in zones}
    for seg in segments:
        if seg["id"] not in cut:
            adjacency[seg["from"]].append(seg["to"])
    reachable = set(sources)
    queue = deque(sources)
    while queue:
        node = queue.popleft()
        for nxt in adjacency[node]:
            if nxt not in reachable:
                reachable.add(nxt)
                queue.append(nxt)
    assert not (reachable & set(protections)), "仍存在污染源到保护区的路径"


def random_segments(rng, n):
    count = 2 * n + rng.randint(0, n)
    segments = []
    for i in range(count):
        frm = rng.choice(range(n))
        to = rng.choice(range(n))  # 允许自环与平行管段
        roll = rng.random()
        if roll < 0.15:
            cost = 0  # 零费用管段
        elif roll < 0.9:
            cost = rng.randint(1, 8)
        else:
            cost = 10**9  # 偶发大费用
        segments.append({"id": f"e{i}", "from": f"Z{frm}", "to": f"Z{to}", "cost": cost})
    return segments


@pytest.mark.parametrize("n", [2, 3, 4, 5, 6, 7, 8])
def test_exhaustive_legal_partitions(n):
    rng = random.Random(0xC1EA0000 + n)
    zones = [f"Z{i}" for i in range(n)]
    segments = random_segments(rng, n)
    cut_cost, index = precompute_cut_costs(zones, segments)

    checked = 0
    for sources, protections in all_legal_partitions(zones):
        plan = {
            "zones": zones,
            "segments": segments,
            "sources": sources,
            "protections": protections,
        }
        result = solve_min_cut(plan)
        exp_side, exp_cut, exp_total = brute_force_min_cut(
            zones, segments, sources, protections, cut_cost, index
        )
        assert result["total_cost"] == exp_total, (sources, protections)
        assert result["source_zones"] == exp_side, (sources, protections)
        assert result["cut_segments"] == exp_cut, (sources, protections)
        assert_cut_disconnects(zones, segments, sources, protections, result)
        checked += 1
    # 合法划分总数 = 3^n - 2^(n+1) + 1
    assert checked == 3**n - 2 ** (n + 1) + 1
