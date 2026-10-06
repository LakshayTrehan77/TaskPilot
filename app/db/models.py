import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Index, String, Text, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UserRole(enum.StrEnum):
    USER = "USER"
    ADMIN = "ADMIN"


class TaskStatus(enum.StrEnum):
    TODO = "TODO"
    IN_PROGRESS = "IN_PROGRESS"
    BLOCKED = "BLOCKED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class TaskPriority(enum.StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class PlanStatus(enum.StrEnum):
    GENERATING = "GENERATING"
    FAILED = "FAILED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTING = "EXECUTING"
    COMPLETED = "COMPLETED"


class StepStatus(enum.StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"


# A task can only have one plan in these states at a time (see the partial unique index).
ACTIVE_PLAN_STATUSES = (
    PlanStatus.GENERATING,
    PlanStatus.PENDING_APPROVAL,
    PlanStatus.APPROVED,
    PlanStatus.EXECUTING,
)
ACCEPTED_PLAN_STATUSES = (PlanStatus.APPROVED, PlanStatus.EXECUTING, PlanStatus.COMPLETED)

_ACTIVE_SQL = "status IN ('GENERATING', 'PENDING_APPROVAL', 'APPROVED', 'EXECUTING')"


def utcnow() -> datetime:
    return datetime.now(UTC)


def enum_column(enum_cls: type[enum.Enum]) -> Enum:
    # Plain VARCHAR instead of a native PG enum: adding a state later is a code change,
    # not an ALTER TYPE migration.
    return Enum(enum_cls, native_enum=False, length=20)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[UserRole] = mapped_column(enum_column(UserRole), default=UserRole.USER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (Index("ix_tasks_user_id_status", "user_id", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[TaskStatus] = mapped_column(enum_column(TaskStatus), default=TaskStatus.TODO)
    priority: Mapped[TaskPriority] = mapped_column(
        enum_column(TaskPriority), default=TaskPriority.MEDIUM
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    plans: Mapped[list["Plan"]] = relationship(back_populates="task", passive_deletes=True)


class Plan(Base):
    __tablename__ = "plans"
    __table_args__ = (
        UniqueConstraint("task_id", "idempotency_key", name="uq_plans_task_idempotency_key"),
        # Database-level guard against two active plans for one task, even if two
        # requests pass the application check at the same time.
        Index(
            "uq_plans_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=text(_ACTIVE_SQL),
            sqlite_where=text(_ACTIVE_SQL),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[PlanStatus] = mapped_column(
        enum_column(PlanStatus), default=PlanStatus.GENERATING
    )
    summary: Mapped[str] = mapped_column(Text, default="")
    failure_reason: Mapped[str | None] = mapped_column(String(500))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    task: Mapped[Task] = relationship(back_populates="plans")
    steps: Mapped[list["PlanStep"]] = relationship(
        back_populates="plan",
        order_by="PlanStep.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
        lazy="selectin",
    )


class PlanStep(Base):
    __tablename__ = "plan_steps"
    __table_args__ = (UniqueConstraint("plan_id", "position", name="uq_plan_steps_position"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plans.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int]
    status: Mapped[StepStatus] = mapped_column(enum_column(StepStatus), default=StepStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    plan: Mapped[Plan] = relationship(back_populates="steps")
