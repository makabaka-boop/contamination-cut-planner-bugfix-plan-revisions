"""业务逻辑：方案保存、最小割计算、采用与已采用结果查询。

事务约定：
- 方案校验通过后才写库，非法整版不会改写当前方案；
- 计算失败仅落一条 FAILED 记录，不触碰方案与已采用结果；
- 成功计算在创建时冻结计算时刻的方案修订号、方案负载与最小割结果，
  三者共同组成不可混合的快照；采用时只使用该冻结快照，绝不读取
  "当前方案"，因此计算后方案被修订也不会污染快照；
- 采用历史 adoption_events 对 computation_id 永久唯一，保证
  "每个成功计算至多采用一次"——即使该采用后来被其他计算替换，
  原计算仍不可再次采用；
- 采用事务先对 plans 行加行锁串行化并发采用：仅当当前生效采用在
  持锁前后一致时才允许写入，否则以 409 ADOPTION_CONFLICT 拒绝，
  保证并发采用的返回结果确定且与最终记录一致。任何失败/冲突都
  回滚，当前方案与原采用快照不变。
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from . import models
from .errors import ApiError
from .flow import solve_min_cut


def get_plan_or_404(db, plan_id):
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        raise ApiError(404, "PLAN_NOT_FOUND", f"plan {plan_id!r} does not exist")
    return plan


def save_plan(db, plan_id, canonical_payload):
    """保存（新建或整版替换）方案；payload 必须先通过校验。"""
    plan = db.get(models.Plan, plan_id)
    if plan is None:
        plan = models.Plan(plan_id=plan_id, revision=1, payload=canonical_payload)
        db.add(plan)
    else:
        plan.revision += 1
        plan.payload = canonical_payload
    db.commit()
    db.refresh(plan)
    return plan


def compute(db, plan_id):
    """对当前方案执行最小割计算并持久化计算记录。

    计算记录冻结计算时刻的方案修订号 (plan_revision)、方案负载
    (plan_payload) 与最小割结果 (result)；之后方案再被修订也不影响
    该记录，采用时三者始终属于同一版本。
    """
    plan = get_plan_or_404(db, plan_id)
    computation_id = uuid.uuid4().hex
    # 冻结快照输入，避免与随后的写操作共享可变引用
    plan_payload = plan.payload
    plan_revision = plan.revision
    try:
        result = solve_min_cut(plan_payload)
    except Exception as exc:  # 已校验输入不应失败；兜底记录失败
        computation = models.Computation(
            computation_id=computation_id,
            plan_id=plan.plan_id,
            plan_revision=plan_revision,
            plan_payload=plan_payload,
            status="FAILED",
            result=None,
            error={"code": "INTERNAL_ERROR", "message": str(exc)},
        )
        db.add(computation)
        db.commit()
        raise ApiError(500, "COMPUTATION_FAILED", "min-cut computation failed")
    computation = models.Computation(
        computation_id=computation_id,
        plan_id=plan.plan_id,
        plan_revision=plan_revision,
        plan_payload=plan_payload,
        status="SUCCESS",
        result=result,
        error=None,
    )
    db.add(computation)
    db.commit()
    db.refresh(computation)
    return computation


def get_computation_or_404(db, plan_id, computation_id):
    computation = db.get(models.Computation, computation_id)
    if computation is None or computation.plan_id != plan_id:
        raise ApiError(
            404,
            "COMPUTATION_NOT_FOUND",
            f"computation {computation_id!r} does not exist for plan {plan_id!r}",
        )
    return computation


def _get_adoption_row(db, plan_id):
    return db.get(models.Adoption, plan_id)


def _get_adoption_event(db, computation_id):
    return (
        db.query(models.AdoptionEvent)
        .filter(models.AdoptionEvent.computation_id == computation_id)
        .first()
    )


def adopt(db, plan_id, computation_id):
    """采用一次成功计算，保存计算时刻冻结的完整快照。

    规则：
    - 只能采用本方案状态为 SUCCESS 的计算；
    - 每个成功计算至多采用一次（adoption_events 永久唯一），即使其
      先前采用已被替换，再次采用仍返回 409 COMPUTATION_ALREADY_ADOPTED；
    - 新采用替换该方案当前生效采用；
    - 并发采用由 plans 行锁串行化；若当前生效采用在等待锁期间被他人
      改变，则本请求以 409 ADOPTION_CONFLICT 失败，绝不出现"响应成功
      但最终记录属于另一次采用"或 500。
    """
    # ---- 加锁前校验（不与当前方案/修订发生任何关联，只读计算快照）----
    get_plan_or_404(db, plan_id)
    computation = get_computation_or_404(db, plan_id, computation_id)
    if computation.status != "SUCCESS":
        raise ApiError(
            409,
            "COMPUTATION_NOT_ADOPTABLE",
            f"computation {computation_id!r} did not succeed",
        )
    if _get_adoption_event(db, computation_id) is not None:
        raise ApiError(
            409,
            "COMPUTATION_ALREADY_ADOPTED",
            f"computation {computation_id!r} has already been adopted",
        )

    # 并发采用同一方案时，以当前生效采用的 computation_id 作为一致性令牌。
    # 首次采用时令牌为 None（此时尚无生效采用）。
    before_row = _get_adoption_row(db, plan_id)
    before_token = before_row.computation_id if before_row is not None else None

    # ---- 对方案行加锁，串行化同一方案上的所有采用 ----
    # SQLite 不支持 SELECT ... FOR UPDATE，SQLAlchemy 对其退化为普通查询；
    # 并发验收在真实 PostgreSQL 上进行。
    plan = (
        db.query(models.Plan)
        .filter(models.Plan.plan_id == plan_id)
        .with_for_update()
        .populate_existing()
        .one()
    )

    # ---- 持锁后复查（顺序有意义）----
    # 1) 同一计算并发重复采用：精确报 COMPUTATION_ALREADY_ADOPTED；
    if _get_adoption_event(db, computation_id) is not None:
        db.rollback()
        raise ApiError(
            409,
            "COMPUTATION_ALREADY_ADOPTED",
            f"computation {computation_id!r} has already been adopted",
        )
    # 2) 不同计算并发首次采用：等待期间生效采用若被改变，属并发冲突。
    after_row = _get_adoption_row(db, plan_id)
    after_token = after_row.computation_id if after_row is not None else None
    if after_token != before_token:
        db.rollback()
        raise ApiError(
            409,
            "ADOPTION_CONFLICT",
            "a concurrent adoption changed the current adopted result; "
            "re-query and retry with the intended computation",
        )

    adopted_at = datetime.now(timezone.utc)
    # 快照完全来自计算记录冻结的内容：计算时刻的修订号、方案负载与
    # 最小割结果同属一个版本，绝不使用当前 plan 的修订号或负载。
    snapshot = {
        "plan_id": plan.plan_id,
        "plan_revision": computation.plan_revision,
        "computation_id": computation_id,
        "adopted_at": adopted_at.isoformat(),
        "plan": computation.plan_payload,
        "result": computation.result,
    }

    event = models.AdoptionEvent(
        plan_id=plan.plan_id,
        computation_id=computation_id,
        plan_revision=computation.plan_revision,
        snapshot=snapshot,
        adopted_at=adopted_at,
    )
    db.add(event)
    if after_row is None:
        adoption = models.Adoption(
            plan_id=plan_id,
            computation_id=computation_id,
            snapshot=snapshot,
            adopted_at=adopted_at,
        )
        db.add(adoption)
    else:
        adoption = after_row
        adoption.computation_id = computation_id
        adoption.snapshot = snapshot
        adoption.adopted_at = adopted_at

    try:
        db.commit()
    except IntegrityError:
        # 极端竞态（历史唯一约束）下的最终防线：回滚，现状不变
        db.rollback()
        raise ApiError(
            409,
            "COMPUTATION_ALREADY_ADOPTED",
            f"computation {computation_id!r} has already been adopted",
        )
    db.refresh(adoption)
    return adoption


def get_adoption(db, plan_id):
    adoption = db.get(models.Adoption, plan_id)
    if adoption is None:
        raise ApiError(
            404,
            "ADOPTION_NOT_FOUND",
            f"plan {plan_id!r} has no adopted result",
        )
    return adoption
