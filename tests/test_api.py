"""HTTP 接口集成测试：保存、计算、采用、已采用结果查询与错误码。"""

import pytest

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
# {SRC1,SRC2} 切 p1+p2=10 优于 {SRC1,SRC2,MID} 切 p3+p4=12
EXPECTED_RESULT = {
    "source_zones": ["SRC1", "SRC2"],
    "cut_segments": ["p1", "p2"],
    "total_cost": 10,
}


def put_plan(client, plan_id, plan):
    return client.put(f"/plans/{plan_id}", json=plan)


def compute(client, plan_id):
    return client.post(f"/plans/{plan_id}/computations")


def adopt(client, plan_id, computation_id):
    return client.post(f"/plans/{plan_id}/adopt", json={"computation_id": computation_id})


# ---------- 方案保存 ----------

def test_save_and_get_plan(client):
    resp = put_plan(client, "plan-a", VALID_PLAN)
    assert resp.status_code == 200
    body = resp.json()
    assert body["plan_id"] == "plan-a"
    assert body["revision"] == 1
    assert body["plan"] == VALID_PLAN

    resp = client.get("/plans/plan-a")
    assert resp.status_code == 200
    assert resp.json()["plan"] == VALID_PLAN


def test_resave_bumps_revision(client):
    put_plan(client, "plan-a", VALID_PLAN)
    resp = put_plan(client, "plan-a", VALID_PLAN)
    assert resp.json()["revision"] == 2


def test_plan_without_zones_derives_them(client):
    plan = {k: v for k, v in VALID_PLAN.items() if k != "zones"}
    resp = put_plan(client, "plan-derived", plan)
    assert resp.status_code == 200
    assert sorted(resp.json()["plan"]["zones"]) == sorted(VALID_PLAN["zones"])


def test_get_unknown_plan(client):
    resp = client.get("/plans/ghost")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PLAN_NOT_FOUND"


def test_invalid_plan_id(client):
    resp = put_plan(client, "bad.id", VALID_PLAN)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


# ---------- 非法方案：稳定错误码且不得写库 ----------

def _bad_plan(**overrides):
    plan = dict(VALID_PLAN)
    plan.update(overrides)
    return plan


INVALID_CASES = [
    ("bad zone id", _bad_plan(zones=["ok", "bad id"]), "INVALID_ZONE_ID"),
    ("zone id too long", _bad_plan(zones=["x" * 33]), "INVALID_ZONE_ID"),
    ("duplicate zone", _bad_plan(zones=["SRC1", "SRC1"]), "DUPLICATE_ZONE_ID"),
    (
        "too many zones",
        _bad_plan(zones=[f"Z{i}" for i in range(301)]),
        "TOO_MANY_ZONES",
    ),
    (
        "bad segment id",
        _bad_plan(segments=[{"id": "bad id", "from": "SRC1", "to": "MID", "cost": 1}]),
        "INVALID_SEGMENT_ID",
    ),
    (
        "duplicate segment id",
        _bad_plan(
            segments=[
                {"id": "dup", "from": "SRC1", "to": "MID", "cost": 1},
                {"id": "dup", "from": "SRC2", "to": "MID", "cost": 2},
            ]
        ),
        "DUPLICATE_SEGMENT_ID",
    ),
    (
        "too many segments",
        _bad_plan(
            segments=[
                {"id": f"s{i}", "from": "SRC1", "to": "SAFE1", "cost": 1}
                for i in range(2001)
            ]
        ),
        "TOO_MANY_SEGMENTS",
    ),
    (
        "segment references unknown zone",
        _bad_plan(
            segments=[{"id": "s1", "from": "SRC1", "to": "NOWHERE", "cost": 1}]
        ),
        "UNKNOWN_ZONE",
    ),
    (
        "negative cost",
        _bad_plan(
            segments=[{"id": "s1", "from": "SRC1", "to": "SAFE1", "cost": -1}]
        ),
        "INVALID_COST",
    ),
    (
        "cost above limit",
        _bad_plan(
            segments=[{"id": "s1", "from": "SRC1", "to": "SAFE1", "cost": 10**9 + 1}]
        ),
        "INVALID_COST",
    ),
    (
        "float cost",
        _bad_plan(
            segments=[{"id": "s1", "from": "SRC1", "to": "SAFE1", "cost": 1.5}]
        ),
        "INVALID_COST",
    ),
    (
        "string cost",
        _bad_plan(
            segments=[{"id": "s1", "from": "SRC1", "to": "SAFE1", "cost": "5"}]
        ),
        "INVALID_COST",
    ),
    (
        "bool cost",
        _bad_plan(
            segments=[{"id": "s1", "from": "SRC1", "to": "SAFE1", "cost": True}]
        ),
        "INVALID_COST",
    ),
    ("empty sources", _bad_plan(sources=[]), "EMPTY_SOURCES"),
    ("empty protections", _bad_plan(protections=[]), "EMPTY_PROTECTIONS"),
    ("missing sources", _bad_plan(sources=None), "INVALID_SOURCES_FIELD"),
    (
        "source/protection overlap",
        _bad_plan(sources=["SRC1", "SAFE1"]),
        "SOURCE_PROTECTION_OVERLAP",
    ),
    ("unknown source zone", _bad_plan(sources=["GHOST"]), "UNKNOWN_ZONE"),
]


@pytest.mark.parametrize("label, plan, detail_code", INVALID_CASES,
                         ids=[c[0] for c in INVALID_CASES])
def test_invalid_plan_rejected_and_not_stored(client, label, plan, detail_code):
    resp = put_plan(client, "bad-plan", plan)
    assert resp.status_code == 422, label
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert detail_code in {d["code"] for d in error["details"]}, label
    # 非法方案不得写库
    assert client.get("/plans/bad-plan").status_code == 404


def test_invalid_resave_keeps_previous_plan(client):
    put_plan(client, "plan-c", VALID_PLAN)
    resp = put_plan(client, "plan-c", _bad_plan(sources=[]))
    assert resp.status_code == 422
    got = client.get("/plans/plan-c").json()
    assert got["revision"] == 1
    assert got["plan"] == VALID_PLAN


def test_non_object_body_rejected(client):
    resp = client.put("/plans/plan-d", json=[1, 2, 3])
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_malformed_json_rejected(client):
    resp = client.put(
        "/plans/plan-e", content=b"{broken", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "INVALID_JSON"


# ---------- 计算 ----------

def test_compute_success(client):
    put_plan(client, "p1", VALID_PLAN)
    resp = compute(client, "p1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "SUCCESS"
    assert body["result"] == EXPECTED_RESULT
    assert body["computation_id"]

    # 计算记录可查询
    resp = client.get(f"/plans/p1/computations/{body['computation_id']}")
    assert resp.status_code == 200
    assert resp.json()["result"] == EXPECTED_RESULT


def test_compute_is_deterministic(client):
    put_plan(client, "p1", VALID_PLAN)
    first = compute(client, "p1").json()["result"]
    second = compute(client, "p1").json()["result"]
    assert first == second == EXPECTED_RESULT


def test_compute_unknown_plan(client):
    resp = compute(client, "ghost")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "PLAN_NOT_FOUND"


def test_segment_and_zone_order_do_not_change_result(client):
    shuffled = dict(
        VALID_PLAN,
        zones=list(reversed(VALID_PLAN["zones"])),
        segments=list(reversed(VALID_PLAN["segments"])),
    )
    put_plan(client, "order-a", VALID_PLAN)
    put_plan(client, "order-b", shuffled)
    result_a = compute(client, "order-a").json()["result"]
    result_b = compute(client, "order-b").json()["result"]
    assert result_a == result_b == EXPECTED_RESULT


def test_total_cost_exceeds_32bit_via_api(client):
    plan = {
        "segments": [
            {"id": f"s{i}", "from": "A", "to": "B", "cost": 10**9} for i in range(4)
        ],
        "sources": ["A"],
        "protections": ["B"],
    }
    put_plan(client, "big", plan)
    result = compute(client, "big").json()["result"]
    assert result["total_cost"] == 4 * 10**9  # > 2**32
    assert result["cut_segments"] == ["s0", "s1", "s2", "s3"]


# ---------- 采用与已采用结果查询 ----------

def test_adopt_and_get_adoption(client):
    put_plan(client, "p2", VALID_PLAN)
    computation_id = compute(client, "p2").json()["computation_id"]

    resp = adopt(client, "p2", computation_id)
    assert resp.status_code == 200
    snapshot = resp.json()
    assert snapshot["computation_id"] == computation_id
    assert snapshot["result"] == EXPECTED_RESULT
    assert snapshot["plan"] == VALID_PLAN
    assert snapshot["plan_id"] == "p2"
    assert snapshot["adopted_at"]

    resp = client.get("/plans/p2/adoption")
    assert resp.status_code == 200
    assert resp.json() == snapshot


def test_adopt_same_computation_twice_rejected(client):
    put_plan(client, "p3", VALID_PLAN)
    computation_id = compute(client, "p3").json()["computation_id"]
    assert adopt(client, "p3", computation_id).status_code == 200
    first = client.get("/plans/p3/adoption").json()

    resp = adopt(client, "p3", computation_id)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "COMPUTATION_ALREADY_ADOPTED"
    # 已采用结果保持不变
    assert client.get("/plans/p3/adoption").json() == first


def test_adopt_unknown_computation(client):
    put_plan(client, "p4", VALID_PLAN)
    resp = adopt(client, "p4", "does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "COMPUTATION_NOT_FOUND"
    # 不产生已采用结果
    assert client.get("/plans/p4/adoption").status_code == 404


def test_adopt_computation_of_other_plan(client):
    put_plan(client, "pa", VALID_PLAN)
    put_plan(client, "pb", VALID_PLAN)
    computation_id = compute(client, "pa").json()["computation_id"]
    resp = adopt(client, "pb", computation_id)
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "COMPUTATION_NOT_FOUND"


def test_new_adoption_replaces_previous(client):
    put_plan(client, "p5", VALID_PLAN)
    first_id = compute(client, "p5").json()["computation_id"]
    assert adopt(client, "p5", first_id).status_code == 200

    second_id = compute(client, "p5").json()["computation_id"]
    resp = adopt(client, "p5", second_id)
    assert resp.status_code == 200
    current = client.get("/plans/p5/adoption").json()
    assert current["computation_id"] == second_id


def test_failed_operations_do_not_clobber_adoption(client):
    put_plan(client, "p6", VALID_PLAN)
    computation_id = compute(client, "p6").json()["computation_id"]
    assert adopt(client, "p6", computation_id).status_code == 200
    before = client.get("/plans/p6/adoption").json()

    # 非法整版不改写方案，也不影响已采用结果
    assert put_plan(client, "p6", _bad_plan(sources=[])).status_code == 422
    # 采用不存在的结果失败
    assert adopt(client, "p6", "nope").status_code == 404
    # 重复采用失败
    assert adopt(client, "p6", computation_id).status_code == 409

    assert client.get("/plans/p6/adoption").json() == before
    assert client.get("/plans/p6").json()["plan"] == VALID_PLAN


def test_adoption_not_found(client):
    resp = client.get("/plans/ghost/adoption")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "ADOPTION_NOT_FOUND"


def test_adopt_body_validation(client):
    put_plan(client, "p7", VALID_PLAN)
    resp = client.post("/plans/p7/adopt", json={})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


def test_unknown_route(client):
    resp = client.get("/no-such-route")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"
