"""最小割求解的针对性单元测试：

并列割取源侧最小、多源多汇、零费用割、超过 32 位的总费用。
"""

from app.flow import solve_min_cut


def make_plan(zones, segments, sources, protections):
    return {
        "zones": zones,
        "segments": [
            {"id": sid, "from": frm, "to": to, "cost": cost}
            for sid, frm, to, cost in segments
        ],
        "sources": sources,
        "protections": protections,
    }


def test_tie_cut_returns_minimal_source_side():
    # 两条同价割：{S} 切 e1 或 {S,A} 切 e2，必须返回源侧更小的 {S}
    plan = make_plan(
        ["S", "A", "T"],
        [("e1", "S", "A", 5), ("e2", "A", "T", 5)],
        ["S"],
        ["T"],
    )
    assert solve_min_cut(plan) == {
        "source_zones": ["S"],
        "cut_segments": ["e1"],
        "total_cost": 5,
    }


def test_tie_cut_diamond():
    # 菱形同价割：最小源侧只含源点
    plan = make_plan(
        ["S", "A", "B", "T"],
        [
            ("sa", "S", "A", 3),
            ("sb", "S", "B", 3),
            ("ab", "A", "B", 1),
            ("at", "A", "T", 3),
            ("bt", "B", "T", 3),
        ],
        ["S"],
        ["T"],
    )
    result = solve_min_cut(plan)
    assert result["source_zones"] == ["S"]
    assert result["cut_segments"] == ["sa", "sb"]
    assert result["total_cost"] == 6


def test_multi_source_multi_sink():
    plan = make_plan(
        ["S1", "S2", "M", "T1", "T2"],
        [
            ("a", "S1", "M", 4),
            ("b", "S2", "M", 6),
            ("c", "M", "T1", 5),
            ("d", "M", "T2", 7),
            ("e", "S1", "S2", 1),
        ],
        ["S1", "S2"],
        ["T1", "T2"],
    )
    # 合法割必须包含 S1、S2：{S1,S2} 切 a+b=10，{S1,S2,M} 切 c+d=12
    assert solve_min_cut(plan) == {
        "source_zones": ["S1", "S2"],
        "cut_segments": ["a", "b"],
        "total_cost": 10,
    }


def test_zero_cost_cut():
    # 零费用管段即可隔断
    plan = make_plan(
        ["S", "A", "T"],
        [("z", "S", "A", 0), ("w", "A", "T", 5)],
        ["S"],
        ["T"],
    )
    assert solve_min_cut(plan) == {
        "source_zones": ["S"],
        "cut_segments": ["z"],
        "total_cost": 0,
    }


def test_unreachable_protection_means_empty_zero_cut():
    # 保护区本就不可达：空切断清单、费用 0，源侧为源点可达区域
    plan = make_plan(
        ["S", "T", "X"],
        [("a", "S", "X", 3)],
        ["S"],
        ["T"],
    )
    assert solve_min_cut(plan) == {
        "source_zones": ["S", "X"],
        "cut_segments": [],
        "total_cost": 0,
    }


def test_total_cost_exceeds_32bit():
    # 3 * 10**9 > 2**31
    plan = make_plan(
        ["S", "T"],
        [(f"p{i}", "S", "T", 10**9) for i in range(3)],
        ["S"],
        ["T"],
    )
    result = solve_min_cut(plan)
    assert result["total_cost"] == 3 * 10**9
    assert result["cut_segments"] == ["p0", "p1", "p2"]


def test_total_cost_at_full_scale():
    # 上限规模：2000 条 * 10**9 = 2*10**12，验证 64 位容量
    plan = make_plan(
        ["S", "T"],
        [(f"p{i:04d}", "S", "T", 10**9) for i in range(2000)],
        ["S"],
        ["T"],
    )
    result = solve_min_cut(plan)
    assert result["total_cost"] == 2 * 10**12
    assert len(result["cut_segments"]) == 2000


def test_self_loop_is_never_cut():
    plan = make_plan(
        ["S", "T"],
        [("loop", "S", "S", 9), ("a", "S", "T", 2)],
        ["S"],
        ["T"],
    )
    result = solve_min_cut(plan)
    assert result["cut_segments"] == ["a"]
    assert result["total_cost"] == 2


def test_result_is_order_independent():
    segments = [
        ("a", "S1", "M", 4),
        ("b", "S2", "M", 6),
        ("c", "M", "T1", 5),
        ("d", "M", "T2", 7),
    ]
    plan_a = make_plan(["S1", "S2", "M", "T1", "T2"], segments, ["S1", "S2"], ["T1", "T2"])
    plan_b = make_plan(
        ["T2", "T1", "M", "S2", "S1"], list(reversed(segments)), ["S2", "S1"], ["T2", "T1"]
    )
    assert solve_min_cut(plan_a) == solve_min_cut(plan_b)
