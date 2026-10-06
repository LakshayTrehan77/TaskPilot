import logging
from uuid import UUID

from sqlalchemy.orm import Session

from app.cache.redis import TaskCache
from app.core.exceptions import ForbiddenError, InvalidTransitionError, NotFoundError
from app.db.models import Task, TaskPriority, TaskStatus, User, UserRole
from app.repositories.task import TaskRepository
from app.schemas.task import TaskCreate, TaskPage, TaskRead, TaskUpdate

logger = logging.getLogger(__name__)

ALLOWED_TASK_TRANSITIONS = {
    TaskStatus.TODO: {TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {
        TaskStatus.TODO,
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.BLOCKED: {TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.COMPLETED: set(),
    TaskStatus.CANCELLED: {TaskStatus.TODO},
}


def can_transition(current: TaskStatus, new: TaskStatus) -> bool:
    return new == current or new in ALLOWED_TASK_TRANSITIONS[current]


def ensure_task_transition(current: TaskStatus, new: TaskStatus) -> None:
    if not can_transition(current, new):
        raise InvalidTransitionError(f"Cannot move task from {current.value} to {new.value}")


def ensure_can_access(user: User, task: Task) -> None:
    if user.role != UserRole.ADMIN and task.user_id != user.id:
        raise ForbiddenError()


class TaskService:
    def __init__(self, db: Session, cache: TaskCache):
        self.db = db
        self.cache = cache
        self.tasks = TaskRepository(db)

    def create(self, user: User, data: TaskCreate) -> Task:
        task = Task(user_id=user.id, **data.model_dump())
        self.tasks.add(task)
        self.db.commit()
        self.cache.invalidate_user(user.id)
        logger.info("task created task_id=%s user_id=%s", task.id, user.id)
        return task

    def get(self, user: User, task_id: UUID) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise NotFoundError("Task not found")
        ensure_can_access(user, task)
        return task

    def list(
        self,
        user: User,
        *,
        page: int,
        page_size: int,
        status: TaskStatus | None,
        priority: TaskPriority | None,
        sort: str,
    ) -> TaskPage:
        # Admin queries span every user, so they bypass the per-user cache.
        use_cache = user.role != UserRole.ADMIN
        params = f"{status}:{priority}:{sort}:{page}:{page_size}"
        if use_cache and (cached := self.cache.get_page(user.id, params)):
            return TaskPage.model_validate(cached)

        items, total = self.tasks.list_page(
            user_id=user.id if use_cache else None,
            status=status,
            priority=priority,
            sort_field=sort.lstrip("-"),
            descending=sort.startswith("-"),
            offset=(page - 1) * page_size,
            limit=page_size,
        )
        result = TaskPage(
            items=[TaskRead.model_validate(t) for t in items],
            total=total,
            page=page,
            page_size=page_size,
        )
        if use_cache:
            self.cache.set_page(user.id, params, result.model_dump(mode="json"))
        return result

    def update(self, user: User, task_id: UUID, data: TaskUpdate) -> Task:
        task = self.get(user, task_id)
        changes = data.model_dump(exclude_unset=True)
        if "status" in changes:
            ensure_task_transition(task.status, changes["status"])
        for field, value in changes.items():
            setattr(task, field, value)
        self.db.commit()
        self.cache.invalidate_user(task.user_id)
        logger.info("task updated task_id=%s user_id=%s status=%s", task.id, user.id, task.status)
        return task

    def delete(self, user: User, task_id: UUID) -> None:
        task = self.get(user, task_id)
        owner_id = task.user_id
        self.tasks.delete(task)
        self.db.commit()
        self.cache.invalidate_user(owner_id)
        logger.info("task deleted task_id=%s user_id=%s", task_id, user.id)
