"""持久化模型：方案、计算记录、采用快照与不可变采用历史。"""

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)

from .db import Base


class Plan(Base):
    __tablename__ = "plans"

    plan_id = Column(String(64), primary_key=True)
    revision = Column(Integer, nullable=False)
    payload = Column(JSON, nullable=False)  # 校验后的规范化方案
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Computation(Base):
    __tablename__ = "computations"

    computation_id = Column(String(32), primary_key=True)
    plan_id = Column(
        String(64), ForeignKey("plans.plan_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # 计算时刻的方案修订号与方案负载，与 result 共同构成不可混合的快照
    plan_revision = Column(Integer, nullable=False)
    plan_payload = Column(JSON, nullable=False)
    status = Column(String(16), nullable=False)  # SUCCESS / FAILED
    result = Column(JSON, nullable=True)
    error = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Adoption(Base):
    """每个方案当前生效的采用（指向 adoption_events 中最新一条）。"""

    __tablename__ = "adoptions"

    plan_id = Column(
        String(64), ForeignKey("plans.plan_id", ondelete="CASCADE"), primary_key=True
    )
    # 注意：这里 intentionally 不加 unique —— 同一计算"至多采用一次"
    # 由不可变的 adoption_events.computation_id 唯一约束永久保证；
    # 新采用替换当前采用后，旧计算仍不允许再次被采用。
    computation_id = Column(String(32), nullable=False, index=True)
    snapshot = Column(JSON, nullable=False)  # 方案 + 结果的完整快照
    adopted_at = Column(DateTime(timezone=True), nullable=False)


class AdoptionEvent(Base):
    """不可变采用历史：每个成功计算至多产生一条事件（永久唯一）。"""

    __tablename__ = "adoption_events"

    # SQLite 仅对 INTEGER PRIMARY KEY 自动递增，BigInteger 需在 SQLite
    # 退化为 Integer；PostgreSQL 使用 BIGINT 标识列。
    id = Column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    plan_id = Column(
        String(64),
        ForeignKey("plans.plan_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 一次成功计算最多被采用一次，即使其采用结果后来被其他计算替换
    computation_id = Column(
        String(32),
        ForeignKey("computations.computation_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    plan_revision = Column(Integer, nullable=False)
    snapshot = Column(JSON, nullable=False)  # 采用时刻写入的完整快照
    adopted_at = Column(DateTime(timezone=True), nullable=False)
