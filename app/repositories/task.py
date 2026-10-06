from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Task, TaskPriority, TaskStatus

SORT_COLUMNS = {
    "created_at": Task.created_at,
    "updated_at": Task.updated_at,
    "title": Task.title,
}
OPEN_STATUSES = (TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED)


class TaskRepository:
    def __init__(self, db: Session):
        self.db = db

    def get(self, task_id: UUID) -> Task | None:
        return self.db.get(Task, task_id)

    def add(self, task: Task) -> Task:
        self.db.add(task)
        self.db.flush()
        return task

    def delete(self, task: Task) -> None:
        self.db.delete(task)

    def list_page(
        self,
        *,
        user_id: UUID | None,
        status: TaskStatus | None,
        priority: TaskPriority | None,
        sort_field: str,
        descending: bool,
        offset: int,
        limit: int,
    ) -> tuple[list[Task], int]:
        query = select(Task)
        if user_id is not None:
            query = query.where(Task.user_id == user_id)
        if status is not None:
            query = query.where(Task.status == status)
        if priority is not None:
            query = query.where(Task.priority == priority)

        total = self.db.scalar(select(func.count()).select_from(query.subquery())) or 0

        column = SORT_COLUMNS[sort_field]
        order = column.desc() if descending else column.asc()
        # id as tie-breaker keeps pages stable when many rows share a sort value.
        rows = self.db.scalars(query.order_by(order, Task.id).offset(offset).limit(limit))
        return list(rows), total

    def list_open_for_user(self, user_id: UUID, exclude_id: UUID, limit: int) -> list[Task]:
        query = (
            select(Task)
            .where(Task.user_id == user_id, Task.id != exclude_id, Task.status.in_(OPEN_STATUSES))
            .order_by(Task.created_at.desc())
            .limit(limit)
        )
        return list(self.db.scalars(query))
