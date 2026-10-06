from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.db.models import (
    ACCEPTED_PLAN_STATUSES,
    ACTIVE_PLAN_STATUSES,
    Plan,
    PlanStatus,
    PlanStep,
    Task,
)


class PlanRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, plan_id: UUID, lock: bool = False) -> Plan | None:
        if not lock:
            return self.db.get(Plan, plan_id)
        # FOR UPDATE serialises approve/reject/execute on the same plan. populate_existing
        # makes sure we see the committed row, not a stale copy in the session.
        query = (
            select(Plan)
            .where(Plan.id == plan_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return self.db.scalar(query)

    def list_for_task(self, task_id: UUID) -> list[Plan]:
        query = select(Plan).where(Plan.task_id == task_id).order_by(Plan.created_at.desc())
        return list(self.db.scalars(query))

    def get_by_idempotency_key(self, task_id: UUID, key: str) -> Plan | None:
        query = select(Plan).where(Plan.task_id == task_id, Plan.idempotency_key == key)
        return self.db.scalar(query)

    def get_active_for_task(self, task_id: UUID) -> Plan | None:
        query = select(Plan).where(Plan.task_id == task_id, Plan.status.in_(ACTIVE_PLAN_STATUSES))
        return self.db.scalar(query)

    def add(self, plan: Plan) -> Plan:
        self.db.add(plan)
        self.db.flush()
        return plan

    def fail_stale_generating(self, task_id: UUID, cutoff: datetime) -> int:
        # A worker that crashed mid-generation would otherwise block the task forever.
        stmt = (
            update(Plan)
            .where(
                Plan.task_id == task_id,
                Plan.status == PlanStatus.GENERATING,
                Plan.created_at < cutoff,
            )
            .values(status=PlanStatus.FAILED, failure_reason="Plan generation timed out")
        )
        return self.db.execute(stmt).rowcount

    def average_step_count_for_user(self, user_id: UUID) -> float | None:
        step_counts = (
            select(func.count(PlanStep.id).label("n"))
            .join(Plan, Plan.id == PlanStep.plan_id)
            .join(Task, Task.id == Plan.task_id)
            .where(Task.user_id == user_id, Plan.status.in_(ACCEPTED_PLAN_STATUSES))
            .group_by(Plan.id)
            .subquery()
        )
        return self.db.scalar(select(func.avg(step_counts.c.n)))
