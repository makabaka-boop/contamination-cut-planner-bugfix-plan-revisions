"""洁净厂房通风管段最小费用隔断服务 —— HTTP 接口层。"""

from contextlib import asynccontextmanager

from anyio import to_thread
from fastapi import Depends, FastAPI, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import services
from .db import get_db, init_db
from .errors import ApiError, error_body
from .validation import validate_plan_payload

PLAN_ID_REGEX = r"^[A-Za-z0-9_-]{1,64}$"


@asynccontextmanager
async def lifespan(_app):
    init_db()
    yield


app = FastAPI(
    title="Cleanroom Ventilation Cut Service",
    version="1.0.0",
    lifespan=lifespan,
)


# ---------- 异常处理：统一错误响应体，错误码稳定 ----------

@app.exception_handler(ApiError)
async def api_error_handler(_request, exc: ApiError):
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(exc.code, exc.message, exc.details),
    )


@app.exception_handler(RequestValidationError)
async def request_validation_handler(_request, exc: RequestValidationError):
    details = [
        {
            "code": "INVALID_PARAMETER",
            "field": ".".join(str(part) for part in err.get("loc", [])),
            "message": err.get("msg", ""),
        }
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=error_body("VALIDATION_ERROR", "request validation failed", details),
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(_request, exc: StarletteHTTPException):
    code = {
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
    }.get(exc.status_code, "HTTP_ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content=error_body(code, str(exc.detail)),
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(_request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content=error_body("INTERNAL_ERROR", "unexpected server error"),
    )


# ---------- 工具 ----------

async def _json_body(request: Request):
    try:
        return await request.json()
    except Exception:
        raise ApiError(400, "INVALID_JSON", "request body is not valid JSON")


def _plan_view(plan):
    return {"plan_id": plan.plan_id, "revision": plan.revision, "plan": plan.payload}


def _parse_expected_revision(body):
    """从请求体提取可选的 ``expected_revision``（乐观并发令牌）。

    - 缺省或显式为 null：不声明修订号，沿用"存在则整版替换、否则
      新建"的旧语义；
    - 正整数：仅当当前修订号与之相等时才接受整版替换，冲突由持久
      层原子判定并以 409 REVISION_CONFLICT 返回；
    - 其他类型/非正数：422 VALIDATION_ERROR（INVALID_EXPECTED_REVISION）。
    """
    if not isinstance(body, dict):
        return None  # 非对象负载由 validate_plan_payload 统一以 422 拒绝
    if "expected_revision" not in body or body["expected_revision"] is None:
        return None
    value = body["expected_revision"]
    # bool 是 int 的子类，必须显式排除
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "expected_revision must be a positive integer",
            [
                {
                    "code": "INVALID_EXPECTED_REVISION",
                    "field": "expected_revision",
                    "message": "expected_revision must be an integer >= 1",
                }
            ],
        )
    return value


def _computation_view(computation):
    return {
        "computation_id": computation.computation_id,
        "plan_id": computation.plan_id,
        "plan_revision": computation.plan_revision,
        "status": computation.status,
        "result": computation.result,
        "error": computation.error,
    }


# ---------- 接口 ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.put("/plans/{plan_id}")
async def save_plan(
    request: Request,
    plan_id: str = Path(pattern=PLAN_ID_REGEX),
    db: Session = Depends(get_db),
):
    """保存（新建或整版替换）方案；非法负载不改写当前方案。

    携带 ``expected_revision`` 时仅当当前修订号一致才接受
    （乐观并发控制），否则返回 409 REVISION_CONFLICT 供调用方
    重新读取后重试。
    """
    body = await _json_body(request)
    expected_revision = _parse_expected_revision(body)
    canonical = validate_plan_payload(body)
    # 阻塞式 psycopg2 写事务（可能等待行锁）放到工作线程执行，
    # 使并发保存请求能在数据库层真正并行地争锁，由条件 UPDATE /
    # 唯一约束给出确定结果。
    plan = await to_thread.run_sync(
        services.save_plan, db, plan_id, canonical, expected_revision
    )
    return _plan_view(plan)


@app.get("/plans/{plan_id}")
def get_plan(plan_id: str = Path(pattern=PLAN_ID_REGEX), db: Session = Depends(get_db)):
    return _plan_view(services.get_plan_or_404(db, plan_id))


@app.post("/plans/{plan_id}/computations")
def compute(plan_id: str = Path(pattern=PLAN_ID_REGEX), db: Session = Depends(get_db)):
    """对当前方案计算最小费用隔断（源侧最小的唯一最低费用割）。"""
    return _computation_view(services.compute(db, plan_id))


@app.get("/plans/{plan_id}/computations/{computation_id}")
def get_computation(
    plan_id: str = Path(pattern=PLAN_ID_REGEX),
    computation_id: str = "",
    db: Session = Depends(get_db),
):
    services.get_plan_or_404(db, plan_id)
    return _computation_view(
        services.get_computation_or_404(db, plan_id, computation_id)
    )


@app.post("/plans/{plan_id}/adopt")
async def adopt(
    request: Request,
    plan_id: str = Path(pattern=PLAN_ID_REGEX),
    db: Session = Depends(get_db),
):
    """采用一次成功计算并保存完整快照；同一计算只能采用一次。"""
    body = await _json_body(request)
    computation_id = body.get("computation_id") if isinstance(body, dict) else None
    if not isinstance(computation_id, str) or not computation_id:
        raise ApiError(
            422,
            "VALIDATION_ERROR",
            "request body must be an object with a computation_id string",
            [
                {
                    "code": "INVALID_COMPUTATION_ID",
                    "field": "computation_id",
                    "message": "non-empty string expected",
                }
            ],
        )
    # services.adopt 是同步的 psycopg2 阻塞事务（内含行锁等待）；
    # 放到工作线程执行，使并发采用请求能在数据库层真正并行地争锁，
    # 由行锁串行化后得到确定结果。
    adoption = await to_thread.run_sync(services.adopt, db, plan_id, computation_id)
    return adoption.snapshot


@app.get("/plans/{plan_id}/adoption")
def get_adoption(
    plan_id: str = Path(pattern=PLAN_ID_REGEX), db: Session = Depends(get_db)
):
    """查询当前已采用结果（完整快照）。"""
    return services.get_adoption(db, plan_id).snapshot
